"""应用配置的解析约定：超时相关的环境变量怎么写都不会把服务弄挂。

普通对话默认不限时之后，运维最自然的写法就是「留空」或干脆不配；空字符串对 ``int``
是解析错误，若照旧抛出去，服务会直接起不来。这里把「空 = 不配置 = 不限时」固定住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import settings as settings_module
from app.settings import DataDirError, Settings, _prepare_dirs

ENV = "CODA_RUN_TIMEOUT_SECONDS"


def test_unset_run_timeout_means_unlimited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV, raising=False)
    assert Settings().run_timeout_seconds is None


def test_blank_run_timeout_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    """``CODA_RUN_TIMEOUT_SECONDS=`` 是「不限时」，不是启动故障。"""
    monkeypatch.setenv(ENV, "")
    assert Settings().run_timeout_seconds is None


def test_blank_run_timeout_with_spaces_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV, "   ")
    assert Settings().run_timeout_seconds is None


def test_explicit_run_timeout_is_kept(monkeypatch: pytest.MonkeyPatch):
    """显式配置的护栏照常生效，别被空值兜底顺手抹掉。"""
    monkeypatch.setenv(ENV, "7200")
    assert Settings().run_timeout_seconds == 7200


# ---- 数据目录初始化 -------------------------------------------------------


def test_ensure_dirs_creates_nested_layout_and_is_idempotent(tmp_path: Path):
    """父目录不存在也要一次建好；默认目录与 workspaces 都能实际写入。"""
    data = tmp_path / "nested" / "data"
    workspace = tmp_path / "nested" / "workspaces"
    settings = Settings(data_dir=data, workspace_dir=workspace)

    settings.ensure_dirs()
    assert data.is_dir()
    assert (data / "agent-homes").is_dir()
    assert (data / "skill-libraries").is_dir()
    assert workspace.is_dir()
    # 真实写入验证（不是只看目录存在）
    (data / "probe.txt").write_text("ok", encoding="utf-8")
    (workspace / "probe.txt").write_text("ok", encoding="utf-8")
    # 重复调用必须幂等：不能因为目录已存在而报错
    settings.ensure_dirs()


def test_ensure_dirs_reports_path_and_reason(tmp_path: Path):
    """创建失败要报出具体路径与原因，不能静默失败。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    settings = Settings(data_dir=blocked / "data", workspace_dir=tmp_path / "workspaces")

    with pytest.raises(DataDirError) as excinfo:
        settings.ensure_dirs()

    message = str(excinfo.value)
    assert "无法创建或写入应用数据目录" in message
    assert str(blocked / "data") in message
    assert "CODA_DATA_DIR" in message


def test_prepare_dirs_falls_back_when_default_dir_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """默认目录不可用时回退到家目录（expanduser 动态解析，不写死用户名）。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    fallback = tmp_path / "home" / ".coda" / "data"
    monkeypatch.setattr(settings_module, "_fallback_data_dir", lambda: fallback)
    settings = Settings(data_dir=blocked / "data", workspace_dir=fallback / "workspaces")

    prepared = _prepare_dirs(settings, explicit_data_dir=False)

    assert prepared.data_dir == fallback
    assert fallback.is_dir()
    assert (fallback / "workspaces").is_dir()


def test_prepare_dirs_keeps_explicit_dir_loud(tmp_path: Path):
    """用户显式配置的 CODA_DATA_DIR 不可写时，不回退、不掩盖，直接报错。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    settings = Settings(data_dir=blocked / "data", workspace_dir=tmp_path / "workspaces")

    with pytest.raises(DataDirError):
        _prepare_dirs(settings, explicit_data_dir=True)

    assert settings.data_dir == blocked / "data"


def test_get_settings_treats_dotenv_data_dir_as_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``.env`` 里配的 CODA_DATA_DIR 也算「显式配置」：不可写就报错，不能悄悄回退。

    pydantic-settings 读取 ``.env`` 不会写进 ``os.environ``；只查环境变量会把「在 .env
    里配了数据目录」误判成「没配」，默认位置不可写时静默换到 ``~/.coda/data``，
    用户会以为数据丢了。这里用真实 ``.env`` 走一遍 ``get_settings()``。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"CODA_DATA_DIR={blocked / 'data'}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODA_DATA_DIR", raising=False)
    settings_module.get_settings.cache_clear()
    try:
        with pytest.raises(DataDirError):
            settings_module.get_settings()
    finally:
        # 缓存是进程级的：清掉，别让这次失败污染后面的用例。
        settings_module.get_settings.cache_clear()
