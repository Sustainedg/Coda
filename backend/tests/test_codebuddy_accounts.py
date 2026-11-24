"""CodeBuddy 多账号槽位：存档 / 切换 / 删除 / 登出标记。

这里不启动真正的内核：直接创建一份假的 session 文件，验证平台侧的槽位逻辑。
真正的 session 内容是加密的，平台只做「原始字节复制」，不解析。
"""
from __future__ import annotations

import json

import pytest

from app.agents import get_agent
from app.agents.builtin import codebuddy_accounts as accounts

LIVE_NAME = "Tencent-Cloud.coding-copilot.info"


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "CODEBUDDY_CONFIG_DIR",
        "CODEBUDDY_API_KEY",
        "CODEBUDDY_AUTH_TOKEN",
        "CODEBUDDY_CREDENTIALS_IN_MEMORY",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_live(blob: bytes) -> None:
    root = accounts.auth_store_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / LIVE_NAME).write_bytes(blob)


ACCOUNT_A = {"userId": "u-a", "userName": "Alice", "enterpriseId": "e-1", "enterpriseName": "Tencent"}
ACCOUNT_B = {"userId": "u-b", "userName": "Bob", "enterpriseId": "e-2", "enterpriseName": "Acme"}


def test_snapshot_and_switch_between_accounts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    _write_live(b"session-A")
    slot_a = accounts.snapshot_live(ACCOUNT_A)
    assert slot_a
    assert accounts.active_account()["label"] == "Alice · Tencent"

    # 登录第二个账号：内核覆盖了当前 session，平台再存一份
    _write_live(b"session-B")
    slot_b = accounts.snapshot_live(ACCOUNT_B)
    assert slot_b and slot_b != slot_a
    labels = {item["label"] for item in accounts.list_saved()}
    assert labels == {"Alice · Tencent", "Bob · Acme"}
    assert accounts.active_account()["id"] == slot_b

    # 切回 Alice：session 字节原样写回
    assert accounts.activate(slot_a) == "Alice · Tencent"
    assert (accounts.auth_store_dir() / LIVE_NAME).read_bytes() == b"session-A"
    assert accounts.active_account()["id"] == slot_a

    # 切到 Bob
    accounts.activate(slot_b)
    assert (accounts.auth_store_dir() / LIVE_NAME).read_bytes() == b"session-B"


def test_relogin_same_account_updates_slot_instead_of_duplicating(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_live(b"old")
    slot = accounts.snapshot_live(ACCOUNT_A)

    _write_live(b"new-token")
    assert accounts.snapshot_live(ACCOUNT_A) == slot
    assert len(accounts.list_saved()) == 1
    assert (accounts.accounts_root() / f"{slot}.info").read_bytes() == b"new-token"


def test_switch_clears_logout_marker(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_live(b"session-A")
    slot = accounts.snapshot_live(ACCOUNT_A)

    # 用户在别处登出：内核会改名 session 并留一个登出标记
    marker = accounts.auth_store_dir() / f"{LIVE_NAME}.logged-out"
    marker.write_text("2026-11-23T00:00:00Z", encoding="utf-8")
    assert accounts.is_logged_out() is True

    accounts.activate(slot)
    assert accounts.is_logged_out() is False
    assert (accounts.auth_store_dir() / LIVE_NAME).read_bytes() == b"session-A"


def test_remove_slot(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_live(b"session-A")
    slot = accounts.snapshot_live(ACCOUNT_A)

    accounts.remove(slot)
    assert accounts.list_saved() == []
    with pytest.raises(KeyError):
        accounts.remove(slot)


def test_activate_unknown_account_raises(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(KeyError):
        accounts.activate("does-not-exist")


def test_adapter_exposes_saved_accounts_and_switching(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    definition = get_agent("codebuddy-cli")
    assert definition.supports_accounts() is True

    _write_live(b"session-A")
    slot_a = accounts.snapshot_live(ACCOUNT_A)
    _write_live(b"session-B")
    slot_b = accounts.snapshot_live(ACCOUNT_B)

    status = definition.auth_status()
    assert status.logged_in is True
    assert status.account == "Bob · Acme"
    assert {item.label for item in status.accounts} == {"Alice · Tencent", "Bob · Acme"}
    assert [item.id for item in status.accounts if item.active] == [slot_b]

    definition.activate_account(slot_a)
    assert definition.auth_status().account == "Alice · Tencent"

    definition.remove_account(slot_b)
    assert [item.id for item in definition.auth_status().accounts] == [slot_a]


def test_logout_marker_reports_not_logged_in_but_keeps_saved_accounts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    definition = get_agent("codebuddy-cli")
    _write_live(b"session-A")
    accounts.snapshot_live(ACCOUNT_A)

    (accounts.auth_store_dir() / f"{LIVE_NAME}.logged-out").write_text("x", encoding="utf-8")
    status = definition.auth_status()
    assert status.logged_in is False
    # 已保存的账号仍然列出来，用户可以直接切回去
    assert [item.label for item in status.accounts] == ["Alice · Tencent"]


def test_index_is_private_and_tolerates_corruption(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_live(b"session-A")
    accounts.snapshot_live(ACCOUNT_A)

    index = accounts.accounts_root() / "index.json"
    assert index.stat().st_mode & 0o777 == 0o600
    json.loads(index.read_text(encoding="utf-8"))

    index.write_text("{not json", encoding="utf-8")
    assert accounts.list_saved() == []


def test_remove_fails_loudly_when_slot_cannot_be_deleted(monkeypatch, tmp_path):
    """槽位文件删不掉时必须报错：它是一份可复制的登录凭据，静默成功会留下残留。"""
    _isolate(monkeypatch, tmp_path)
    _write_live(b"session-A")
    slot_id = accounts.snapshot_live(ACCOUNT_A)

    slot = accounts.accounts_root() / f"{slot_id}.info"
    slot.unlink()
    slot.mkdir()  # 同名目录让 unlink 抛 OSError

    with pytest.raises(RuntimeError):
        accounts.remove(slot_id)
    # 索引条目保留，避免「界面已删、磁盘还在」
    assert any(item["id"] == slot_id for item in accounts.load_index()["accounts"])


def test_activate_fails_loudly_when_logout_marker_cannot_be_cleared(monkeypatch, tmp_path):
    """登出标记清不掉时切换必须报错：否则接口说切好了，内核仍按登出处理。"""
    _isolate(monkeypatch, tmp_path)
    _write_live(b"session-A")
    slot_a = accounts.snapshot_live(ACCOUNT_A)
    _write_live(b"session-B")
    accounts.snapshot_live(ACCOUNT_B)

    marker = accounts.auth_store_dir() / "blocking.logged-out"
    marker.mkdir()  # 目录无法 unlink

    with pytest.raises(RuntimeError):
        accounts.activate(slot_a)
