"""自更新（app/selfupdate.py · api/update.py）：版本解析、磁盘操作、API 行为。

不碰真实 GitHub：网络函数（check_latest / _download_asset）用注入的 httpx 客户端
或 mock 替身；磁盘操作在 tmp_path 里搭一个假的托管目录结构。
execv 重启不测（exec 会替换测试进程本身）—— schedule_restart 只测 argv 的剥参逻辑。
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app import selfupdate
from app.selfupdate import (
    SelfUpdateError,
    _extract_sha256_from_notes,
    _normalize_version,
    _safe_extract,
    parse_release,
    version_eq,
    version_gt,
)


def test_desktop_release_version_matches_backend():
    """桌面端与后端必须同版本发布，否则 tag 构建会在发布阶段失败。"""
    root = Path(__file__).resolve().parents[2]
    package = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "desktop/package-lock.json").read_text(encoding="utf-8"))
    expected = selfupdate.__version__.lstrip("V")
    assert package["version"] == expected
    assert lock["version"] == expected
    assert lock["packages"][""]["version"] == expected


# ---------- 版本号解析与比较 ----------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.44", "V0.44"),
        ("V0.44", "V0.44"),
        ("v0.44.1", "V0.44.1"),
        ("main", None),
        ("", None),
        ("vabc", None),
        ("../..", None),
        ("v-1", None),
    ],
)
def test_normalize_version(tag: str, expected: str | None):
    assert _normalize_version(tag) == expected


def test_version_compare():
    assert version_gt("V0.44", "V0.43")
    assert version_gt("V0.44.1", "V0.44")
    assert not version_gt("V0.43", "V0.44")
    assert not version_gt("V0.44", "V0.44")
    assert version_eq("V0.44", "v0.44")
    assert not version_eq("V0.44", "V0.44.1")


def test_version_compare_treats_missing_patch_as_zero():
    """三段式版本：缺省的段补 0，``V0.44`` 与 ``V0.44.0`` 是同一个版本。

    不补 0 时 ``(0, 44) < (0, 44, 0)``，更新器会认为「有新版」却永远装不上同一个版本。
    """
    assert version_eq("V0.44", "V0.44.0")
    assert version_eq("V0.1", "V0.1.0")
    assert not version_gt("V0.44", "V0.44.0")
    assert not version_gt("V0.44.0", "V0.44")
    # 修订号真的更大时仍然算升级。
    assert version_gt("V0.1.1", "V0.1")
    assert version_gt("V0.1.2", "V0.1.1")
    assert not version_gt("V0.1", "V0.1.1")
    # 语义化版本：第二段 1 < 11，所以 V0.1.1 小于 V0.11（不是笔误）。
    assert not version_gt("V0.1.1", "V0.11")


# ---------- Release JSON 解析 ----------


def _release_json(tag: str = "v0.44", *, with_asset: bool = True, asset_name: str = "coda.tar.gz") -> dict:
    assets = []
    if with_asset:
        assets.append({"name": asset_name, "browser_download_url": f"https://x/{asset_name}", "size": 12345})
    return {
        "tag_name": tag,
        "name": "标题",
        "body": "说明",
        "published_at": "2026-11-20T00:00:00Z",
        "assets": assets,
    }


def test_parse_release_ok():
    release = parse_release(_release_json())
    assert release is not None
    assert release.version == "V0.44"
    assert release.tag == "v0.44"
    assert release.asset_size == 12345
    assert release.asset_url.endswith("coda.tar.gz")


@pytest.mark.parametrize(
    "payload",
    [
        {"tag_name": "nightly", "assets": []},  # tag 不是版本号
        {"tag_name": "v0.44", "assets": []},  # 没有产物
        {"tag_name": "v0.44", "assets": [{"name": "other.zip"}]},  # 产物名不对
        {"tag_name": "v0.44"},  # 没有 assets 字段
    ],
)
def test_parse_release_rejects(payload: dict):
    assert parse_release(payload) is None


async def test_check_latest_reports_version_release_without_asset():
    """最新 Release 是版本号但没带产物时，不能静默当成“没有新版”。

    GitHub 的 /releases/latest 会选中这个空 Release；以前 parse_release 返回 None，
    界面只显示当前版本、既不报错也不提示，看起来像检测功能失效。
    """
    payload = _release_json("v0.1.5", with_asset=False)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SelfUpdateError, match="coda.tar.gz"):
            await selfupdate.check_latest(client=client)


@pytest.mark.parametrize(
    ("notes", "expected"),
    [
        ("说明\nsha256: " + "a" * 64, "a" * 64),
        ("SHA256：" + "b" * 64, "b" * 64),  # 中文冒号 + 大写关键字
        ("sha256 " + "c" * 64, None),  # 没有冒号不算（避免误吞正文里的哈希样文本）
        ("没有校验值", None),
        ("", None),
    ],
)
def test_extract_sha256_from_notes(notes: str, expected: str | None):
    assert _extract_sha256_from_notes(notes) == expected


# ---------- 磁盘操作：解压 / 指针 / 清理 / 标记 ----------


def make_archive(entries: list[tuple[str, bytes | str]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries:
            data = content if isinstance(content, bytes) else content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD_ARCHIVE = make_archive(
    [
        ("backend/app/__init__.py", '__version__ = "V0.45"'),
        ("backend/requirements.txt", "fastapi"),
        ("frontend/dist/index.html", "<html>ok</html>"),
    ]
)


def test_safe_extract_good():
    import tempfile

    dest = Path(tempfile.mkdtemp())
    _safe_extract(GOOD_ARCHIVE, dest)
    assert (dest / "backend/app/__init__.py").read_text(encoding="utf-8") == '__version__ = "V0.45"'
    assert (dest / "frontend/dist/index.html").exists()


@pytest.mark.parametrize("evil_name", ["../escape.txt", "/abs/escape.txt", "a/../../escape.txt"])
def test_safe_extract_rejects_escape(evil_name: str, tmp_path: Path):
    archive = make_archive([(evil_name, b"evil")])
    with pytest.raises(SelfUpdateError):
        _safe_extract(archive, tmp_path)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_safe_extract_skips_symlink(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        link = tarfile.TarInfo("evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
        regular = tarfile.TarInfo("backend/app/__init__.py")
        regular.size = 4
        tf.addfile(regular, io.BytesIO(b"x=1)"))
    _safe_extract(buf.getvalue(), tmp_path)
    assert (tmp_path / "backend/app/__init__.py").exists()
    assert not (tmp_path / "evil-link").exists()
    assert not (tmp_path / "evil-link").is_symlink()


def test_safe_extract_rejects_not_tar(tmp_path: Path):
    with pytest.raises(SelfUpdateError):
        _safe_extract(b"not a tarball", tmp_path)


def _make_managed(tmp_path: Path, versions: list[str], current: str) -> Path:
    """搭一个假的托管目录（releases/<版本>/ + current 软链），返回 releases 根。

    软链用相对目标（与生产一致：current → ./V0.43），断言时再 resolve。
    """
    releases = tmp_path / "releases"
    releases.mkdir()
    for version in versions:
        d = releases / version
        (d / "backend/app").mkdir(parents=True)
        (d / "backend/app/__init__.py").write_text(f'__version__ = "{version}"', encoding="utf-8")
        (d / "frontend/dist").mkdir(parents=True)
        (d / "frontend/dist/index.html").write_text("<html/>", encoding="utf-8")
    (releases / "current").symlink_to(current, target_is_directory=True)
    return releases


def test_managed_mode_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    releases = _make_managed(tmp_path, ["V0.43", "V0.44"], "V0.43")
    monkeypatch.setattr(selfupdate, "_app_root", lambda: tmp_path)

    assert selfupdate.managed_root() == (releases / "V0.43").resolve()
    assert selfupdate.deployment_mode() == "managed"
    assert selfupdate.installed_versions() == ["V0.44", "V0.43"]


def test_source_mode_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """没有 current 软链就是源码模式（一切安装 / 回滚动作都应被拒绝）。"""
    monkeypatch.setattr(selfupdate, "_app_root", lambda: tmp_path)  # 空目录，无 releases/
    assert selfupdate.managed_root() is None
    assert selfupdate.deployment_mode() == "source"


def test_switch_current_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    releases = _make_managed(tmp_path, ["V0.43", "V0.44"], "V0.43")
    monkeypatch.setattr(selfupdate, "_app_root", lambda: tmp_path)

    selfupdate._switch_current(releases / "V0.44")
    assert selfupdate.managed_root() == (releases / "V0.44").resolve()
    assert selfupdate.current_link().is_symlink()
    # 没有残留的临时链
    assert not (releases / ".current.tmp").exists()


def test_prune_keeps_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    releases = _make_managed(tmp_path, ["V0.43", "V0.44", "V0.42", "V0.41"], "V0.44")
    monkeypatch.setattr(selfupdate, "_app_root", lambda: tmp_path)

    selfupdate._prune_versions(keep=3, pinned={"V0.44"})
    remaining = selfupdate.installed_versions()
    assert "V0.44" in remaining
    assert "V0.43" in remaining
    assert "V0.42" in remaining
    assert "V0.41" not in remaining


def test_restart_marker_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(selfupdate, "restart_marker_path", lambda: tmp_path / "restart-marker.json")
    selfupdate._write_restart_marker("update", "V0.45")
    marker = selfupdate.read_restart_marker()
    assert marker == {"action": "update", "version": "V0.45", "from_version": selfupdate.__version__, "at": marker["at"]}
    # 读一次即清：界面上只会弹一次结果横幅
    assert selfupdate.read_restart_marker() is None


def test_restart_command_rebuilds_interpreter_argv(monkeypatch: pytest.MonkeyPatch):
    """`python -m uvicorn` 的 sys.argv[0] 是 uvicorn 脚本路径，直接 execv 会 Exec format error。

    restart_command 必须重建为 [sys.executable, "-m", "uvicorn", ...]。
    """
    # 形态一：python -m uvicorn（serve.sh / PyPanel / 容器 entrypoint）
    monkeypatch.setattr(
        sys,
        "argv",
        ["/venv/lib/uvicorn/__main__.py", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--reload"],
    )
    monkeypatch.setattr(sys, "executable", "/x/python")
    argv = selfupdate.restart_command()
    assert argv[0] == "/x/python"  # 解释器，不是脚本路径
    assert argv[1] == "-m" and argv[2] == "uvicorn"
    assert "app.main:app" in argv and "--port" in argv and "--host" in argv
    assert "--reload" not in argv


# ---------- API 层 ----------


@pytest_asyncio.fixture
async def client():
    from app.db import SessionLocal, engine, init_db
    from app.main import app
    from app.models import SQLModel

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_update_status_source_mode(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """测试环境没有 releases/current：应报告源码模式、不允许安装。"""
    response = await client.get("/api/update/status")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "source"
    assert data["can_install"] is False
    assert data["update_available"] is False


async def test_update_check_maps_network_error(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """GitHub 不可达：502 + 面向用户的错误信息（而不是 500）。"""

    async def boom(**kwargs):
        raise SelfUpdateError("访问 GitHub 失败：超时")

    monkeypatch.setattr(selfupdate, "check_latest", boom)
    response = await client.get("/api/update/check")
    assert response.status_code == 502
    assert "GitHub" in response.json()["detail"]


async def test_update_check_ok(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """检测成功：返回完整状态（源码模式下 update_available 仍可为真，用于提示 git 更新）。"""
    release = parse_release(_release_json("v0.99"))
    assert release is not None

    async def fake_check(**kwargs):
        return release

    monkeypatch.setattr(selfupdate, "check_latest", fake_check)
    response = await client.get("/api/update/check")
    assert response.status_code == 200
    data = response.json()
    assert data["latest"]["version"] == "V0.99"
    assert data["update_available"] is True
    assert data["can_install"] is False  # 源码模式


async def test_install_rejected_in_source_mode(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """源码模式点安装：SSE 返回 error 事件而不是 500（check 先被源码模式拦截）。"""
    response = await client.post("/api/update/install")
    assert response.status_code == 200
    events = [line for line in response.text.splitlines() if line.startswith("data: ")]
    # 源码模式下 install_release 拒绝，错误信息里带「源码模式」
    assert any('"update.error"' in line and "源码模式" in line for line in events), events


async def test_rollback_rejected_in_source_mode(client: httpx.AsyncClient):
    response = await client.post("/api/update/rollback")
    assert response.status_code == 200
    events = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert any('"update.error"' in line for line in events)
