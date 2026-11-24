"""CodeBuddy 的多账号槽位（平台侧的「登录快照」）。

内核本身是**单账号**模型：一次只保留一份登录 session（`<共享数据根>/Data/Public/auth/*.info`），
再登录另一个账号会直接覆盖它（同一账号下的个人 / 企业上下文不算多账号）。平台要支持
「登录几个账号、随意切换」，做法很直接：

1. 每次登录成功后，把内核写出的 session 文件**原样复制**到槽位目录；
2. 切换时把选定槽位的字节**原样写回**内核的 session 路径，并清掉登出标记；
3. session 文件是用本机密钥加密的，同一台机器上复制 / 还原即可，平台不需要解析内容。

槽位目录放在真实 `HOME` 下（与 session 文件一样不跟 `CODEBUDDY_CONFIG_DIR` 走）：
``~/.codebuddy/platform-accounts/`` —— ``index.json`` 记账号元数据，``<槽位>.info`` 存字节。

已知边界：

* **正在跑的对话不会中途换号**：一次运行启动时读一次登录态，切换只影响之后启动的运行；
* 账号元数据（昵称 / 企业）来自登录时的状态接口，旧 session 在首次登录新账号时才会归档；
* 槽位文件仍受本机磁盘权限保护（0600），但它是可复制的登录凭据，别外传。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

#: 账号登录态的落盘位置（相对各平台的共享数据根）：``Data/Public/auth/<hash>.info``。
_AUTH_SUBDIR = Path("Data") / "Public" / "auth"

#: 平台自己的槽位目录名（放在真实 ``HOME/.codebuddy`` 下）。
_ACCOUNTS_DIRNAME = "platform-accounts"
_INDEX_FILE = "index.json"
_INDEX_VERSION = 1


# --------------------------------------------------------------------------- 路径


def auth_store_dir(home: str | Path | None = None) -> Path:
    """内核账号 session 的落盘目录（``FileAuthenticationStorage`` 使用）。

    * Linux：``~/.local/share/CodeBuddyExtension/Data/Public/auth``
    * macOS：``~/Library/Application Support/CodeBuddyExtension/Data/Public/auth``
    * Windows：``%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth``

    ``home`` 只用于测试注入；正常运行时跟随真实 ``HOME``，与 ``CODEBUDDY_CONFIG_DIR``
    的隔离无关。
    """
    base = Path(home).expanduser() if home else Path.home()
    if sys.platform == "darwin":
        root = base / "Library" / "Application Support" / "CodeBuddyExtension"
    elif sys.platform == "win32":
        root = base / "AppData" / "Local" / "CodeBuddyExtension"
    else:
        root = base / ".local" / "share" / "CodeBuddyExtension"
    return root / _AUTH_SUBDIR


def accounts_root() -> Path:
    """平台保存账号槽位的目录（真实 HOME，不受 ``CODEBUDDY_CONFIG_DIR`` 影响）。"""
    return Path.home() / ".codebuddy" / _ACCOUNTS_DIRNAME


def _index_path() -> Path:
    return accounts_root() / _INDEX_FILE


def _logout_markers() -> list[Path]:
    try:
        return list(auth_store_dir().glob("*.logged-out"))
    except OSError:
        return []


def is_logged_out() -> bool:
    """内核是否处于「已登出」状态（登出标记会阻止 session 被加载）。"""
    return bool(_logout_markers())


def _info_files() -> list[Path]:
    try:
        candidates = list(auth_store_dir().glob("*.info"))
    except OSError:
        return []
    files: list[Path] = []
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                files.append(path)
        except OSError:  # noqa: PERF203 - 单个文件不可读不影响其它文件
            continue
    return files


def read_live_auth() -> Path | None:
    """当前内核 session 文件（存在且非空）；登出后可能只剩备份，仍返回最新的一份。"""
    index = load_index()
    recorded = str(index.get("live_file") or "").strip()
    if recorded:
        candidate = auth_store_dir() / recorded
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            pass
    files = _info_files()
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def live_auth_target() -> Path | None:
    """切换账号时要写回的目标路径（即使当前已登出、文件已被改名也能找回）。"""
    index = load_index()
    recorded = str(index.get("live_file") or "").strip()
    if recorded:
        return auth_store_dir() / recorded
    live = read_live_auth()
    return live


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- 索引


def load_index() -> dict[str, Any]:
    """读槽位索引；缺失 / 损坏都当空索引（不能让一次读失败挡住登录状态展示）。"""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": _INDEX_VERSION, "live_file": None, "accounts": []}
    if not isinstance(data, dict):
        return {"version": _INDEX_VERSION, "live_file": None, "accounts": []}
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    return {
        "version": _INDEX_VERSION,
        "live_file": data.get("live_file") if isinstance(data.get("live_file"), str) else None,
        "accounts": [entry for entry in accounts if isinstance(entry, dict) and entry.get("id")],
    }


def save_index(index: dict[str, Any]) -> None:
    """原子写索引（0600）：中途失败也不会留下半截 JSON。"""
    root = accounts_root()
    root.mkdir(parents=True, exist_ok=True)
    target = _index_path()
    temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    temp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, target)


def _write_bytes_atomic(target: Path, blob: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp.write_bytes(blob)
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, target)


# --------------------------------------------------------------------------- 账号元数据


def _account_field(account: dict[str, Any], *names: str) -> str:
    for name in names:
        value = account.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_account(account: dict[str, Any] | None) -> dict[str, Any] | None:
    """把内核状态接口里的账号信息规整成槽位元数据；没有 uid 就不值得存。"""
    if not isinstance(account, dict):
        return None
    uid = _account_field(account, "userId", "uid")
    if not uid:
        return None
    name = _account_field(account, "userName", "nickname", "displayName")
    enterprise = _account_field(account, "enterpriseName")
    enterprise_id = _account_field(account, "enterpriseId")
    label = name or f"账号 {uid[:8]}"
    if enterprise:
        label = f"{label} · {enterprise}"
    slot_id = hashlib.sha256(f"{uid}\0{enterprise_id}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": slot_id,
        "uid": uid,
        "label": label,
        "detail": enterprise or None,
        "enterprise_id": enterprise_id or None,
        "saved_at": time.time(),
    }


# --------------------------------------------------------------------------- 槽位操作


def snapshot_live(account: dict[str, Any] | None) -> str | None:
    """把当前登录 session 存成槽位；拿不到账号信息 / 没有 session 时返回 ``None``。

    同一账号重复登录会覆盖同一槽位（槽位 id 由 uid + 企业 id 决定），不会越攒越多。
    """
    meta = normalize_account(account)
    if meta is None or is_logged_out():
        return None
    live = read_live_auth()
    if live is None:
        return None
    try:
        blob = live.read_bytes()
    except OSError:
        return None
    if not blob:
        return None

    root = accounts_root()
    root.mkdir(parents=True, exist_ok=True)
    slot_file = f"{meta['id']}.info"
    _write_bytes_atomic(root / slot_file, blob)

    index = load_index()
    index["live_file"] = live.name
    entry = {
        **meta,
        "file": slot_file,
        "sha256": hashlib.sha256(blob).hexdigest(),
    }
    others = [item for item in index["accounts"] if item.get("id") != meta["id"]]
    index["accounts"] = [entry, *others]
    save_index(index)
    return meta["id"]


def list_saved() -> list[dict[str, Any]]:
    """已保存的账号（带上 ``active``）；槽位文件丢失 / 损坏的条目会被忽略。"""
    index = load_index()
    live = read_live_auth()
    live_sha = None
    if live is not None and not is_logged_out():
        try:
            live_sha = _sha256(live)
        except OSError:
            live_sha = None
    saved: list[dict[str, Any]] = []
    for entry in index["accounts"]:
        slot = accounts_root() / str(entry.get("file") or "")
        try:
            if not slot.is_file() or slot.stat().st_size <= 0:
                continue
        except OSError:
            continue
        item = dict(entry)
        item["active"] = bool(live_sha and entry.get("sha256") == live_sha)
        saved.append(item)
    saved.sort(key=lambda item: (not item["active"], -float(item.get("saved_at") or 0)))
    return saved


def active_account() -> dict[str, Any] | None:
    for entry in list_saved():
        if entry["active"]:
            return entry
    return None


def activate(account_id: str) -> str:
    """把指定槽位的 session 写回内核，返回账号展示名。

    ``account_id`` 不存在 / 槽位损坏抛 ``KeyError``；没有可写回的路径（从未登录过）
    抛 ``RuntimeError``。
    """
    entry = next((item for item in load_index()["accounts"] if item.get("id") == account_id), None)
    if entry is None:
        raise KeyError(account_id)
    slot = accounts_root() / str(entry.get("file") or "")
    try:
        blob = slot.read_bytes()
    except OSError as exc:
        raise KeyError(account_id) from exc
    if not blob:
        raise KeyError(account_id)
    target = live_auth_target()
    if target is None:
        raise RuntimeError("还没有可切换的登录态，请先登录一次")
    try:
        _write_bytes_atomic(target, blob)
    except OSError as exc:
        raise RuntimeError(f"无法写入登录态（{exc}），账号切换未生效") from exc
    # 登出标记会让内核忽略刚写回的 session，切换时必须清掉；清不掉就等于没切换成功，
    # 不能吞掉（否则界面显示已切换，内核仍按登出处理）。
    for marker in _logout_markers():
        try:
            marker.unlink()
        except FileNotFoundError:
            continue  # 已被别处清掉：幂等
        except OSError as exc:
            raise RuntimeError(f"无法清除登出标记（{exc}），账号切换未生效") from exc
    return str(entry.get("label") or account_id)


def remove(account_id: str) -> None:
    """删除一个槽位；不存在抛 ``KeyError``，文件删不掉抛 ``RuntimeError``。

    当前账号的槽位删掉后只是「不再归档」。删不掉时必须报错 —— 槽位里是可复制的
    登录凭据，静默忽略会让用户以为已经删掉，磁盘上却还留着一份。
    """
    index = load_index()
    entry = next((item for item in index["accounts"] if item.get("id") == account_id), None)
    if entry is None:
        raise KeyError(account_id)
    slot = accounts_root() / str(entry.get("file") or "")
    try:
        slot.unlink()
    except FileNotFoundError:
        pass  # 文件已不在：删除本来就是幂等的
    except OSError as exc:
        raise RuntimeError(f"无法删除账号槽位（{exc}），请检查文件权限") from exc
    index["accounts"] = [item for item in index["accounts"] if item.get("id") != account_id]
    save_index(index)
