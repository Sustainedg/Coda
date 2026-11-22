"""平台自更新：从 GitHub Release 检测、安装新版本，并原地重启进程。

部署形态只有两种，本模块对两种都成立：

* **托管模式**（Docker / PyPanel / serve.sh 都一样）：应用目录 ``releases/`` 下按版本
  各占一个子目录，``current`` 软链指向正在运行的版本。更新 = 下载 Release 产物包
  解压成新子目录 + 改指针 + ``os.execv`` 原地自重启。``execv`` 保持同一个 PID：
  容器里 uvicorn 是 PID 1，自替换后**容器不退出**（不需要重建、不需要 docker.sock）；
  PyPanel 只托管进程（不会自动重启崩溃的服务），同 PID 意味着面板完全无感知。
* **源码模式**（开发者直接在仓库里跑，没有 ``current`` 指针）：更新只检测不安装
  ——升级要动正在运行的代码本身，开发机上由 git 管，一键更新没有意义。

版本级更新的口径：**只有 GitHub Release（tag 名去掉 ``v`` 前缀后等于
``__version__``）才算一个版本**，main 上的日常 commit 不构成更新。产物包是
``release.sh`` 打的 tar.gz（backend/ 源码 + 预构建 frontend/dist + requirements.txt），
随 Release 附 SHA256 校验值。

发布地址、代理注入与 ``runtime/provisioning.py`` 同一套：GitHub 不是每个环境都直连
得通，代理设置（``platform_settings.proxy_url``）对更新生效。

回滚：``releases/`` 里保留历史版本，回滚 = 指针切回旧目录 + 同样 ``execv`` 重启。
它是叶子模块：只依赖标准库、``httpx`` 与 ``settings``，不认识数据库以外的任何层。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .settings import get_settings

logger = logging.getLogger("coda.selfupdate")

#: 默认仓库。私有部署可用 ``CODA_UPDATE_REPO`` 指到自己的 fork（owner/repo）。
DEFAULT_REPO = "Sustainedg/Coda"

#: GitHub API 基址。自建 GitHub Enterprise 可换 ``CODA_UPDATE_GITHUB_API``。
DEFAULT_API_BASE = "https://api.github.com"

#: 一次检测 / 下载请求的预算。检测是小请求，下载的读超时放宽（大包在慢链路上要跑几分钟）。
CHECK_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(600.0, connect=10.0, read=120.0)

#: 产物包体积上限：backend 源码 + dist 也就几十 MB，给三倍余量防失控下载。
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

#: 解压出的文件数上限（与 skillhub 同一档安全约束）。
MAX_ARCHIVE_FILES = 20_000

#: 产物包里应当出现的标志文件（解压后用于校验“这真的是一个能启动的应用版本”）。
#: main.py 必须在：缺了它新版本起不来（execv 后直接 Error loading ASGI app）。
_PAYLOAD_MARKERS = ("backend/app/__init__.py", "backend/app/main.py", "frontend/dist/index.html")

#: 下载产物包时优先选的文件名（release.sh 打包固定的名字），找不到再按扩展名兜底。
_ASSET_NAME = "coda.tar.gz"

#: 版本号里允许的字符（tag 形如 v0.44 / V0.44，目录名要拿它拼，不能放任任意串）。
_VERSION_RE = re.compile(r"^[vV]\d+(?:\.\d+)*$")

#: 更新锁：install/rollback 是“改磁盘 + 重启进程”的长动作，同一时刻只允许一个。
_lock = asyncio.Lock()
#: 正在进行中的动作描述（界面上展示“正在安装 V0.44…”用）。
_busy: str | None = None


class SelfUpdateError(Exception):
    """自更新失败。message 面向用户，可直接透传到界面。"""


@dataclass(frozen=True)
class Release:
    """一个 GitHub Release 的可安装摘要。"""

    tag: str
    version: str
    title: str
    notes: str
    published_at: str
    asset_url: str
    asset_size: int
    sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "version": self.version,
            "title": self.title,
            "notes": self.notes,
            "published_at": self.published_at,
            "asset_size": self.asset_size,
        }


def repo_slug() -> str:
    """发布仓库的 ``owner/repo``。"""
    return get_settings().update_repo or DEFAULT_REPO


def api_base() -> str:
    """GitHub API 基址（无末尾斜杠）。"""
    return (get_settings().update_github_api or DEFAULT_API_BASE).rstrip("/")


async def _client_kwargs() -> dict[str, Any]:
    """httpx 客户端参数：走设置里的代理（与内核安装/更新同一套出口）。"""
    kwargs: dict[str, Any] = {"follow_redirects": True}
    # 运行在测试里时拿不到数据库里的代理设置，保持直连即可
    from .api.system import get_proxy_url  # 局部导入避免环：api → 本模块 → api

    try:
        proxy = await get_proxy_url()
    except Exception:  # noqa: BLE001 - 测试 / 数据库未就绪时按无代理处理
        proxy = None
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _normalize_version(tag: str) -> str | None:
    """tag → 本项目口径的版本号（``v0.44`` → ``V0.44``）；不是版本 tag 则 None。

    项目的 ``__version__`` 形如 ``V0.43``（大写 V + 数字），发版 tag 用 ``v0.44``；
    两种写法都认，统一成大写 V 前缀再比较，发版时就不必纠结大小写。
    """
    if not _VERSION_RE.match(tag or ""):
        return None
    return f"V{tag[1:]}"


def _fetch_dict(data: dict[str, Any], key: str) -> str:
    """从 Release JSON 里取字符串字段（缺了给空串，调用方自己决定是否可用）。"""
    value = data.get(key)
    return value if isinstance(value, str) else ""


def parse_release(data: dict[str, Any]) -> Release | None:
    """把 GitHub Release JSON 翻成 :class:`Release`；不是版本 tag 或没有产物包则 None。

    SHA256 从两个地方找：资产名 ``coda.tar.gz.sha256`` 的内容，或 Release 正文里
    ``sha256: <hex>`` 行 —— ``release.sh`` 两处都会写，先到先得。
    """
    tag = _fetch_dict(data, "tag_name")
    version = _normalize_version(tag)
    if version is None:
        return None
    assets = data.get("assets")
    if not isinstance(assets, list):
        return None
    target = None
    for item in assets:
        if not isinstance(item, dict):
            continue
        if _fetch_dict(item, "name") == _ASSET_NAME:
            target = item
    if target is None:
        return None
    size = target.get("size")
    return Release(
        tag=tag,
        version=version,
        title=_fetch_dict(data, "name") or tag,
        notes=_fetch_dict(data, "body"),
        published_at=_fetch_dict(data, "published_at"),
        asset_url=_fetch_dict(target, "browser_download_url"),
        asset_size=size if isinstance(size, int) else 0,
        sha256=None,  # 校验值单独取（要下载 .sha256 资产或解析正文，见 _resolve_sha256）
    )


def _extract_sha256_from_notes(notes: str) -> str | None:
    """从 Release 正文里找 ``sha256: <64 位 hex>``。

    ``release.sh`` 把校验值写成正文里的一行（冒号中英文都认、大小写都认）；
    找不到返回 None —— 没有校验值时安装照常进行，只是跳过校验。
    """
    match = re.search(r"sha256\s*[:：]\s*([0-9a-fA-F]{64})", (notes or ""), re.IGNORECASE)
    return match.group(1).lower() if match else None


def _app_root() -> Path:
    """应用根目录：源码模式 = 仓库根，托管模式 = 正在运行的版本目录。

    ``selfupdate.py`` 的三级 parent 在源码模式下是仓库根
    （``backend/app/…`` → 根），在托管模式下是版本目录
    （``releases/<版本>/backend/app/…`` → ``releases/<版本>``）。
    """
    return Path(__file__).resolve().parent.parent.parent


def releases_root() -> Path:
    """托管模式下各版本目录与 ``current`` 指针的存放处。

    两种形态里 ``releases/`` 的相对位置不同，各算各的、谁存在用谁：

    * 托管模式：版本目录（``_app_root()``）的**父**目录就是 ``releases/``；
    * 源码模式：仓库根（``_app_root()``）下的 ``releases/`` 子目录
      （开发者一般没有；若真建了，按托管对待也没错）。

    都不存在时返回源码形态的路径（首次安装的创建目标）。
    """
    app_root = _app_root()
    managed_candidate = app_root.parent
    source_candidate = app_root / "releases"
    # 托管判据：父目录名是 releases 且其中有 current 指针或版本目录
    if managed_candidate.name == "releases" and managed_candidate.is_dir():
        return managed_candidate
    return source_candidate


def current_link() -> Path:
    """``current`` 软链路径。"""
    return releases_root() / "current"


def managed_root() -> Path | None:
    """托管模式下**正在运行的版本目录**；源码模式返回 None。

    判据是 ``releases/current`` 软链存在且指向一个真目录。开发者直接在仓库里跑时
    仓库里没有这个结构，一切安装 / 回滚动作都直接报“源码模式不支持”。
    """
    link = current_link()
    if not link.is_symlink():
        return None
    target = link.resolve()
    return target if target.is_dir() else None


def installed_versions() -> list[str]:
    """releases/ 下已经装好的版本号（降序，最新在前）。"""
    root = releases_root()
    if not root.is_dir():
        return []
    versions = [
        item.name
        for item in root.iterdir()
        if item.is_dir() and not item.is_symlink() and _normalize_version(item.name)
    ]
    return sorted(versions, key=_version_sort_key, reverse=True)


def _version_sort_key(version: str) -> tuple[int, ...]:
    """``V0.43`` → ``(0, 43)``、``V0.1.1`` → ``(0, 1, 1)``，用于版本排序。

    按语义化版本逐段比较，但**缺失的段补 0**：``V0.44`` 与 ``V0.44.0`` 视为同一个版本。
    不补 0 的话 ``(0, 44) < (0, 44, 0)``，同一个版本会被判成「旧版」，更新器会反复
    提示有新版却装不上。

    注意：``V0.1.1`` 的数值小于 ``V0.11``（第二段 1 < 11），这是语义化版本的既定规则。
    """
    parts = [int(part) for part in version.lstrip("vV").split(".")]
    # 去掉末尾的 0 段，让 V0.44 与 V0.44.0 得到同一个键；全是 0 时保留一段。
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def version_gt(a: str, b: str) -> bool:
    return _version_sort_key(a) > _version_sort_key(b)


def version_eq(a: str, b: str) -> bool:
    return _version_sort_key(a) == _version_sort_key(b)


def deployment_mode() -> str:
    """当前部署形态：``managed``（托管，可一键更新）/ ``source``（源码，只检测）。"""
    return "managed" if managed_root() is not None else "source"


def restart_command() -> list[str]:
    """``os.execv`` 要用的完整 argv。

    进程可能是 python 直接起的（serve.sh / 容器 / PyPanel），也可能是
    ``python -m uvicorn`` 起的——后者的 ``sys.argv[0]`` 是 uvicorn 的
    ``__main__.py`` **脚本路径**，拿它当 execv 的目标会 Exec format error。
    统一重建为 ``python -m uvicorn <原参数>``：解释器由 ``sys.executable``
    给出（永远正确），其余参数照抄 ``sys.argv[1:]``（host/port/app 等怎么
    起的就怎么重启）。``--reload`` 剥掉：它是开发模式的进程树，exec 语义
    对它不成立；托管模式下也不该有它。

    重启后必须还是**这同一个进程位**（同 PID、同 stdin/stdout、同环境变量），
    PyPanel / Docker 才能继续托管它。
    """
    args = [part for part in sys.argv[1:] if part != "--reload"]
    return [sys.executable, "-m", "uvicorn", *args]


async def check_latest(*, client: httpx.AsyncClient | None = None) -> Release | None:
    """查询 GitHub 最新 Release；网络失败抛 :class:`SelfUpdateError`。

    返回 None 表示“最新 Release 不是版本发布”（tag 不是版本号）；
    版本 tag 存在但产物缺失时抛错，避免把发布事故伪装成“没有新版”。
    """
    url = f"{api_base()}/repos/{repo_slug()}/releases/latest"
    kwargs = await _client_kwargs() if client is None else {}
    try:
        if client is not None:
            response = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=CHECK_TIMEOUT, **kwargs) as http:
                response = await http.get(url)
        if response.status_code == 404:
            raise SelfUpdateError("发布仓库还没有任何 Release（先去 GitHub 上发一个版本）")
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise SelfUpdateError(f"访问 GitHub 失败：{exc}") from None
    if not isinstance(data, dict):
        raise SelfUpdateError("GitHub 返回了意外的数据结构")
    release = parse_release(data)
    if release is None:
        # 版本 tag 已经发出来、却没有 coda.tar.gz，说明发布只完成了一半。
        # 此时返回 None 会让界面看起来“没有新版”，把发布事故伪装成已是最新；
        # 直接报出缺什么，用户与维护者都能立刻定位。
        tag = _fetch_dict(data, "tag_name")
        if _normalize_version(tag):
            raise SelfUpdateError(
                f"最新 Release {tag} 缺少 {_ASSET_NAME}（或产物名不正确），可能发布尚未完成；"
                "请维护者补传更新包与 .sha256 后再检测"
            )
    return release


async def _resolve_sha256(release: Release, *, client: httpx.AsyncClient | None) -> str | None:
    """取产物包的 SHA256：先下载 ``.sha256`` 资产，没有再解析正文。"""
    url = f"{api_base()}/repos/{repo_slug()}/releases/tags/{release.tag}"
    try:
        if client is not None:
            response = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=CHECK_TIMEOUT, **await _client_kwargs()) as http:
                response = await http.get(url)
        assets = response.json().get("assets", []) if response.status_code == 200 else []
    except httpx.HTTPError:
        assets = []
    for item in assets:
        if isinstance(item, dict) and item.get("name") == f"{_ASSET_NAME}.sha256":
            try:
                download_url = item["browser_download_url"]
                if client is not None:
                    content = (await client.get(download_url)).text
                else:
                    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT, **await _client_kwargs()) as http:
                        content = (await http.get(download_url)).text
                match = re.search(r"[0-9a-fA-F]{64}", content)
                if match:
                    return match.group(0).lower()
            except (httpx.HTTPError, KeyError):
                continue
    return _extract_sha256_from_notes(release.notes)


async def _download_asset(url: str, expected_sha256: str | None) -> bytes:
    """下载产物包并校验 SHA256（有校验值时）。"""
    kwargs = await _client_kwargs()
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, **kwargs) as http:
            response = await http.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SelfUpdateError(f"下载更新包失败：{exc}") from None
    archive = response.content
    if not archive:
        raise SelfUpdateError("下载到的更新包是空的")
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise SelfUpdateError("更新包体积异常，已拒绝安装")
    if expected_sha256:
        digest = hashlib.sha256(archive).hexdigest()
        if digest != expected_sha256:
            raise SelfUpdateError(
                f"更新包校验失败（sha256 不匹配：期望 {expected_sha256[:12]}…，实际 {digest[:12]}…）"
            )
    return archive


def _safe_extract(archive: bytes, dest: Path) -> None:
    """把产物包安全解压到 ``dest``（dest 必须是空目录）。

    安全约束与 ``skillhub.extract_skill_archive`` 同一套：拒绝绝对路径 / 父级穿越、
    跳过符号链接与设备文件、限制文件数与总字节数。产物包是 ``release.sh`` 打的，
    顶层就是 ``backend/`` 与 ``frontend/``，不剥顶层直接落盘。
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except tarfile.TarError:
        raise SelfUpdateError("下载到的不是有效的 tar.gz 更新包") from None
    with tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        if not members:
            raise SelfUpdateError("更新包里没有文件")
        if len(members) > MAX_ARCHIVE_FILES:
            raise SelfUpdateError("更新包内文件过多，已拒绝安装")
        if sum(m.size for m in members) > MAX_ARCHIVE_BYTES:
            raise SelfUpdateError("更新包解压后体积过大，已拒绝安装")
        root = dest.resolve()
        for member in tf.getmembers():
            parts = [p for p in Path(member.name).parts if p not in ("", ".")]
            if not parts or ".." in parts or Path(member.name).is_absolute():
                raise SelfUpdateError(f"更新包内路径不安全：{member.name}")
            target = root.joinpath(*parts)
            # resolve 后必须仍在 dest 之下（双保险，防拼接逃逸）
            if not str(target).startswith(f"{root}{os.sep}") and target != root:
                raise SelfUpdateError(f"更新包内路径不安全：{member.name}")
            if member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
            elif member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            # 符号链接 / 设备文件一律跳过：产物包里不该有它们


def _verify_payload(dest: Path) -> None:
    """确认解压结果真的是一个应用版本（标志文件齐全）。"""
    for marker in _PAYLOAD_MARKERS:
        if not (dest / marker).is_file():
            raise SelfUpdateError(f"更新包内容不完整：缺少 {marker}")


def _busy_error() -> str | None:
    """当前有动作在进行时返回给界面的提示。"""
    return f"已有更新动作在进行（{_busy}），请等它结束" if _busy else None


async def install_release(release: Release, *, keep: int = 3) -> dict[str, Any]:
    """安装一个 Release 到 ``releases/<版本>/``，切指针并重启进程。

    ``keep``： releases/ 里最多保留的版本数（正在运行的除外），旧版本清掉给回滚
    保留 ``keep - 1`` 个即可。返回的 dict 是重启前写下的“重启动作记录”，重启后
    由 ``pending_restart_note()`` 读给界面（用户看到的最后一条信息不会凭空消失）。
    """
    global _busy
    async with _lock:
        if _busy:
            raise SelfUpdateError(_busy_error() or "已有更新动作在进行")
        _busy = f"安装 {release.version}"
        try:
            return await _install_locked(release, keep=keep)
        finally:
            _busy = None


async def _install_locked(release: Release, *, keep: int) -> dict[str, Any]:
    root = releases_root()
    managed_root_path = managed_root()
    if managed_root_path is None:
        raise SelfUpdateError(
            "当前是源码模式（没有 releases/current 结构），请用 git 更新；"
            "托管模式（Docker / 安装包）才支持一键更新"
        )
    target_dir = root / release.version
    if target_dir.exists() and version_eq(release.version, __version__):
        raise SelfUpdateError(f"{release.version} 就是当前正在运行的版本")

    logger.info("自更新：开始安装 %s（%s 字节）", release.version, release.asset_size)
    sha = await _resolve_sha256(release, client=None)
    archive = await _download_asset(release.asset_url, sha)

    # 先落到临时目录、验证通过后原子改名成正式目录：中途失败不留半成品
    tmp_dir = Path(tempfile.mkdtemp(prefix=".update-", dir=root))
    try:
        _safe_extract(archive, tmp_dir)
        _verify_payload(tmp_dir)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        tmp_dir.rename(target_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    _prune_versions(keep=keep, pinned={__version__, release.version})

    _write_restart_marker("update", release.version)
    _switch_current(target_dir)
    return {"action": "update", "version": release.version, "restarting": True}


async def rollback() -> dict[str, Any]:
    """回滚到 releases/ 里最新的非当前版本，切指针并重启进程。"""
    global _busy
    async with _lock:
        if _busy:
            raise SelfUpdateError(_busy_error() or "已有更新动作在进行")
        if managed_root() is None:
            raise SelfUpdateError("当前是源码模式，没有可回滚的版本")
        candidates = [v for v in installed_versions() if not version_eq(v, __version__)]
        if not candidates:
            raise SelfUpdateError("releases/ 里没有其他可回滚的版本")
        target = candidates[0]  # installed_versions 降序，第一个就是最近的旧版本
        _busy = f"回滚 {target}"
        try:
            _write_restart_marker("rollback", target)
            _switch_current(releases_root() / target)
            return {"action": "rollback", "version": target, "restarting": True}
        finally:
            _busy = None


def _prune_versions(*, keep: int, pinned: set[str]) -> None:
    """把 releases/ 收敛到 ``keep`` 个版本以内（当前运行与正在装的永远保留）。"""
    root = releases_root()
    versions = installed_versions()
    excess = [v for v in versions if v not in pinned][max(0, keep - len(pinned)) :]
    for version in excess:
        shutil.rmtree(root / version, ignore_errors=True)
        logger.info("自更新：清理旧版本 %s", version)


def _switch_current(target_dir: Path) -> None:
    """把 ``current`` 指到 ``target_dir``（临时软链 + 原子改名，不存在中间态）。"""
    link = current_link()
    tmp_link = link.with_name(".current.tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(target_dir, target_is_directory=True)
    tmp_link.replace(link)  # rename 覆盖旧链，原子生效


def restart_marker_path() -> Path:
    """重启动作记录文件（放在数据目录，跨重启存活）。"""
    return get_settings().data_dir / "restart-marker.json"


def _write_restart_marker(action: str, version: str) -> None:
    """重启前写一条记录：进程回来后界面能解释“刚才发生了什么”。"""
    payload = {
        "action": action,
        "version": version,
        "from_version": __version__,
        "at": time.time(),
    }
    path = restart_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_restart_marker() -> dict[str, Any] | None:
    """读取并清除重启记录；没有记录返回 None。"""
    path = restart_marker_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        data = None
    path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else None


def schedule_restart(delay_seconds: float = 0.8) -> None:
    """延时后 ``os.execv`` 原地重启进程。

    必须延时：本函数从请求处理里调，立刻 exec 会掐断还没发出去的 HTTP 响应。
    0.8s 足够响应落地（本地 / 局域网），又不会让用户等。

    execv 的语义是把**当前进程映像**替换为新程序：PID、打开的文件描述符、
    环境变量全部保留。两个必须处理的陷阱：

    1. ``python -m uvicorn`` 进程的 ``sys.argv[0]`` 是 uvicorn 脚本路径而非解释器，
       直接 execv 会 Exec format error —— 由 :func:`restart_command` 重建 argv。
    2. **cwd 不随 exec 变化**：进程的工作目录还停在旧版本的 ``backend/``，
       重启后 ``python -m uvicorn`` 会从旧目录导入代码。因此 exec 前先把 cwd
       切到 ``releases/current/backend``（current 已指向新版本）。
    """
    argv = restart_command()

    def _exec() -> None:
        logger.info("自更新：%.1f 秒后原地重启 → %s", delay_seconds, argv[:4])
        time.sleep(delay_seconds)
        try:
            # cwd 切到新版本的 backend：托管模式下载入的是新代码，源码模式无 current
            # 链时保持原 cwd（本来也到不了这里——源码模式装不了）。
            new_backend = current_link().resolve() / "backend"
            if new_backend.is_dir():
                os.chdir(new_backend)
            os.execv(argv[0], argv)
        except OSError:
            # exec 失败不能让进程死掉（服务还活着，只是没换成新版本）
            logger.exception("自更新：原地重启失败（execv 抛错），服务继续运行旧版本")

    threading.Thread(target=_exec, daemon=True).start()


def status(current_release: Release | None = None) -> dict[str, Any]:
    """更新面板的完整状态（检测由调用方先做，结果传进来避免重复请求 GitHub）。"""
    managed = managed_root() is not None
    installed = installed_versions()
    pending = None
    try:
        pending = read_restart_marker()
    except OSError:
        pending = None
    return {
        "mode": "managed" if managed else "source",
        "current_version": __version__,
        "latest": current_release.to_dict() if current_release else None,
        "update_available": bool(
            current_release and (managed or version_gt(current_release.version, __version__))
        ) if current_release else False,
        "can_install": managed,
        "installed_versions": installed,
        "rollback_available": managed and len(installed) > 1,
        "pending_restart": pending,
    }
