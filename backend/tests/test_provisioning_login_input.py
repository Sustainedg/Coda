"""双向登录（OAuth 授权码）的框架能力与 Antigravity 适配器测试。

覆盖三件事：

1. 登录命令打印 ``INPUT_PROMPT=`` 时，框架产出 ``provision.input`` 事件；
2. 界面通过 ``ProvisionJob.send_input()`` 把文本写回登录子进程；
3. Antigravity 适配器声明了可视化登录助手，并能尽力而为地探测登录态。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

import pytest

from app.agents import AgentContext, get_agent
from app.agents.base import AgentDefinition, AuthStatus, Runtime, RuntimeProvisioner
from app.runtime import provisioning


class FakeInputRuntime(AgentDefinition):
    """登录命令先要输入、拿到后回显长度再退出。"""

    id = "fake-login-input"
    name = "假双向登录内核"
    description = "测试用"
    runtime = Runtime.CLI
    executable = None

    def __init__(self, script: Path) -> None:
        super().__init__()
        self.provisioner = RuntimeProvisioner(
            manager="test",
            login_argv=[sys.executable or "python3", str(script)],
        )

    def build_invocation(self, ctx: AgentContext):  # pragma: no cover - 契约占位
        raise NotImplementedError

    def parse_line(self, line: str, run) -> Iterable:  # pragma: no cover
        return ()

    def auth_status(self) -> AuthStatus:
        return AuthStatus(required=True, logged_in=False, detail="尚未登录")


@pytest.mark.asyncio
async def test_login_input_prompt_and_roundtrip(tmp_path):
    """``INPUT_PROMPT=`` 转成事件；提交的文本能写回登录进程。"""
    script = tmp_path / "login.py"
    script.write_text(
        "import sys\n"
        "print('正在准备登录', flush=True)\n"
        "print('INPUT_PROMPT=请粘贴授权码', flush=True)\n"
        "code = sys.stdin.readline().strip()\n"
        "print(f'收到授权码长度 {len(code)}', flush=True)\n"
    )

    job = await provisioning.run_provision_background(FakeInputRuntime(script), "login")

    events: list[dict] = []
    submitted = False
    async for event in provisioning.stream_job(job):
        events.append(event)
        if event["type"] == "provision.input" and not submitted:
            assert event["prompt"] == "请粘贴授权码"
            assert await job.send_input("abc12345") is True
            submitted = True
        if event["type"] == "provision.finished":
            break

    assert submitted, "没有收到输入请求"
    assert any(
        event["type"] == "provision.output" and "收到授权码长度 8" in event.get("text", "")
        for event in events
    )
    assert events[-1]["type"] == "provision.finished"


@pytest.mark.asyncio
async def test_send_input_without_login_process_is_rejected():
    """没有进行中的登录进程时，写入输入必须明确失败而不是静默吞掉。"""
    job = provisioning.ProvisionJob("fake", "假内核", "login")
    assert await job.send_input("code") is False


def test_login_helper_never_imports_posix_only_modules_at_top_level():
    """``agents/registry`` 会自动 import ``builtin/`` 下所有模块：登录助手顶层不得依赖 pty。

    ``pty`` / ``termios`` 是 POSIX-only 模块，放在模块顶层会让 Windows 后端启动即崩。
    """
    helper = Path(__file__).parent.parent / "app" / "agents" / "builtin" / "antigravity_login.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name not in {"pty", "termios"} for alias in node.names), (
                "antigravity_login.py 顶层不得 import pty / termios（Windows 启动会失败）"
            )
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in {"pty", "termios"}, (
                "antigravity_login.py 顶层不得 import pty / termios（Windows 启动会失败）"
            )


def test_antigravity_declares_visual_login_helper():
    """Antigravity 适配器声明了登录助手，且助手脚本随包存在。"""
    definition = get_agent("antigravity-cli")
    login_argv = definition.provisioner.argv_for("login")
    assert login_argv, "Antigravity 应声明可代跑的登录命令"
    assert Path(login_argv[-1]).name == "antigravity_login.py"
    assert Path(login_argv[-1]).exists()


def test_antigravity_auth_status_scans_credentials_by_content(tmp_path, monkeypatch):
    """登录态不靠文件名：扫描数据目录里带 token 的小 JSON。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    app_dir = tmp_path / ".gemini" / "antigravity-cli"
    app_dir.mkdir(parents=True)

    definition = get_agent("antigravity-cli")
    assert definition.auth_status().logged_in is False

    (app_dir / "auth-state.json").write_text(
        '{"tokens": {"access_token": "a", "refresh_token": "r"},'
        ' "email": "someone@example.com"}',
        encoding="utf-8",
    )

    status = definition.auth_status()
    assert status.logged_in is True
    assert status.account == "someone@example.com"


def test_antigravity_is_account_based():
    """账号型内核：平台不注入密钥 / 网关，也不会播种官方网关。"""
    definition = get_agent("antigravity-cli")
    assert definition.api_key_env is None
    assert definition.base_url_env is None
    assert definition.default_gateway is None
    invocation = definition.build_invocation(
        AgentContext(agent_id="a1", agent_name="demo", workspace_dir="/tmp/ws", prompt="hi")
    )
    assert invocation.env.get("GEMINI_API_KEY") is None
