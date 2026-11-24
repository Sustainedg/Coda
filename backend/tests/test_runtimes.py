"""运行时（CLI 内核）管理：适配器契约、安装器执行与接口行为。

安装/卸载会改本机环境，所以测试**绝不**通过接口真的去跑 npm：执行层一律用
``sh -c echo`` 之类的无害命令验证；接口层面只覆盖 404 / 409 / 422 这些「不执行」的分支。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import httpx
import pytest
import pytest_asyncio

from app.agents import AgentContext, AgentDefinition, AgentRun, Runtime, all_agents, get_agent
from app.agents.base import RuntimeProvisioner
from app.agents.builtin.util import npm_cli, npm_install_argv, npm_prepare_argv
from app.db import engine, init_db
from app.main import app
from app.models import SQLModel
from app.runtime import provisioning


class _FakeRuntime(AgentDefinition):
    """仅用于测试的运行时定义：命令全是无害的 shell 回显。"""

    id = "fake-runtime"
    name = "假运行时"
    description = "测试用的运行时。"
    runtime = Runtime.CLI
    executable = "sh"
    provisioner = RuntimeProvisioner(
        manager="test",
        package="fake-runtime",
        install_argv=["sh", "-c", "echo 安装完成"],
        update_argv=["sh", "-c", "echo 更新完成"],
        uninstall_argv=["sh", "-c", "echo 卸载完成"],
        latest_argv=["sh", "-c", "echo 9.9.9"],
        homepage="https://example.com",
    )

    def build_invocation(self, ctx: AgentContext):  # pragma: no cover - 契约占位
        raise NotImplementedError

    def parse_line(self, line: str, run: AgentRun) -> Iterable[Any]:  # pragma: no cover
        return ()


class _NoCommandRuntime(_FakeRuntime):
    """声明了 provisioner，但每个动作都没有命令。"""

    id = "fake-empty"
    provisioner = RuntimeProvisioner(manager="test")


@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _collect(definition: AgentDefinition, action: str) -> list[dict]:
    return [event async for event in provisioning.stream_provision(definition, action)]  # type: ignore[arg-type]


# --- 适配器契约 -------------------------------------------------------------


def test_cli_kernels_declare_a_provisioner():
    """三个 CLI 内核都要声明安装方式，否则「运行时」页无从下手。"""
    for type_id in ("claude-code", "codex", "hermes-agent", "deepseek-harness"):
        provisioner = get_agent(type_id).provisioner
        assert provisioner is not None, f"{type_id} 未声明 provisioner"
        assert provisioner.manager and provisioner.package, f"{type_id} 缺少包管理器/包名"
        for action in ("install", "update", "uninstall"):
            assert provisioner.supports(action), f"{type_id} 缺少「{action}」命令"
        assert provisioner.latest_argv, f"{type_id} 缺少「查询最新版本」命令"


def test_antigravity_installs_from_the_official_script():
    """Antigravity 是原生二进制：走官方安装脚本 + 自更新，没有 npm 那套清扫流程。"""
    definition = get_agent("antigravity-cli")
    provisioner = definition.provisioner
    assert provisioner is not None
    assert definition.executable == "agy"
    for action in ("install", "update", "uninstall"):
        assert provisioner.supports(action), f"antigravity-cli 缺少「{action}」命令"
    install = provisioner.argv_for("install")
    assert install[:2] == ["bash", "-c"]
    assert "antigravity.google/cli/install.sh" in install[2]
    # 自更新：没有「查询最新版本」子命令，界面不该出现「检查更新」。
    assert provisioner.latest_argv == []
    # 账号型登录：助手脚本随包存在。
    login = provisioner.argv_for("login")
    assert Path(login[-1]).name == "antigravity_login.py"
    assert Path(login[-1]).exists()


def test_api_kernels_are_not_managed():
    """进程内 API 内核与自定义 CLI 不该被平台改环境。"""
    for type_id in ("openai-harness", "custom-cli"):
        assert get_agent(type_id).provisioner is None, f"{type_id} 不应声明 provisioner"
        assert get_agent(type_id).provisioning()["supported"] is False


def test_npm_kernels_clean_up_leftovers_before_acting():
    """npm 的中转目录残留会让安装 / 更新 / 卸载永久失败，三个 npm 内核都要声明清扫命令。"""
    for type_id in ("claude-code", "codex", "deepseek-harness"):
        provisioner = get_agent(type_id).provisioner
        assert provisioner is not None and provisioner.prepare_argv, f"{type_id} 缺少「清扫」命令"


def test_npm_kernels_install_through_the_conflict_aware_wrapper():
    """安装 / 更新走包装命令：本机既有的同名可执行文件会让 npm 直接失败在 EEXIST。

    包装脚本由 Node 执行（而不是 ``sh -c``）：Windows 上没有 ``sh``，
    桌面版必须能在那台机器上装内核 —— 见 ``builtin/util.py`` 的模块注释。
    """
    kernels = (
        ("claude-code", "@anthropic-ai/claude-code"),
        ("codex", "@openai/codex"),
        ("deepseek-harness", "@deepseek-ai/dsh"),
    )
    for type_id, package in kernels:
        definition = get_agent(type_id)
        provisioner = definition.provisioner
        assert provisioner is not None
        for action, spec in (
            ("install", package),
            ("update", f"{package}@latest"),
        ):
            argv = provisioner.argv_for(action)
            assert argv[:2] == ["node", "-e"], f"{type_id} 的「{action}」应走 Node 包装命令"
            script = argv[2]
            assert f'"{spec}"' in script, f"{type_id} 的「{action}」没跑预期的 npm 命令"
            assert "'install', '-g'" in script, f"{type_id} 的「{action}」没有走 npm 全局安装"
            assert f'"{definition.executable}"' in script, f"{type_id} 的包装命令没对上可执行文件名"


def test_npm_prepare_command_only_targets_its_own_leftovers():
    """清扫命令只删自己那个包的残留目录，不碰别的东西。"""
    scoped = npm_prepare_argv("@openai/codex")
    assert scoped[:2] == ["node", "-e"]
    script = scoped[2]
    assert "npm" in script and "'root', '-g'" in script
    assert '["@openai", "codex"]' in script
    # 普通包没有作用域目录（scope 为空串）
    plain = npm_prepare_argv("aider")
    assert '["", "aider"]' in plain[2]


def test_npm_cli_is_cross_platform():
    """直连 npm 的动作（卸载 / 查最新版本）在 Windows 上要经 cmd.exe 启动 npm.cmd。"""
    assert npm_cli("view", "@openai/codex", "version") == [
        "npm",
        "view",
        "@openai/codex",
        "version",
    ]
    assert npm_cli("view", "@openai/codex", "version", platform="win32") == [
        "cmd",
        "/d",
        "/s",
        "/c",
        "npm",
        "view",
        "@openai/codex",
        "version",
    ]


def test_provisioning_descriptor_is_serialisable():
    for definition in all_agents():
        data = definition.provisioning()
        assert set(data) == {
            "supported", "manager", "package", "homepage", "note", "commands",
            "installed", "version", "path", "checked_at",
        }, f"{definition.id} 的 provisioning() 字段集发生变化"


# --- 执行层 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_provision_runs_and_reports_success():
    events = await _collect(_FakeRuntime(), "install")
    assert events[0]["type"] == "provision.started"
    assert "sh" in events[0]["command"]
    output = "".join(e.get("text", "") for e in events if e["type"] == "provision.output")
    assert "安装完成" in output
    final = events[-1]
    assert final["type"] == "provision.finished"
    assert final["ok"] is True and final["status"] == "ok" and final["exit_code"] == 0


@pytest.mark.asyncio
async def test_stream_provision_reports_failure():
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(manager="test", install_argv=["sh", "-c", "exit 3"])
    events = await _collect(definition, "install")
    final = events[-1]
    assert final["type"] == "provision.finished"
    assert final["ok"] is False and final["status"] == "error" and final["exit_code"] == 3


def _texts(events: list[dict]) -> str:
    return "".join(e.get("text", "") for e in events if e["type"] == "provision.output")


#: 假 npm：只认 prefix/root/install 三个子命令，把「装好的包」写成一条链接。
_NPM_STUB = """#!/bin/sh
case "$1 $2" in
  "prefix -g") echo "$FAKE_PREFIX" ;;
  "root -g") echo "$FAKE_PREFIX/lib/node_modules" ;;
  "install -g")
    [ "${FAKE_INSTALL_FAIL:-0}" = "1" ] && exit 7
    mkdir -p "$FAKE_PREFIX/lib/node_modules/pkg"
    ln -sf ../lib/node_modules/pkg/cli.js "$FAKE_PREFIX/bin/claude"
    ;;
  *) exit 1 ;;
esac
"""


def _npm_sandbox(tmp_path, monkeypatch) -> Any:
    """搭一个假的 npm 全局前缀，让包装命令整条链路真跑一遍（不碰本机的 npm）。"""
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib/node_modules").mkdir(parents=True)
    stub = tmp_path / "npm"
    stub.write_text(_NPM_STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PREFIX", str(prefix))
    return prefix


def _install_definition() -> AgentDefinition:
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(
        manager="npm",
        package="@anthropic-ai/claude-code",
        install_argv=npm_install_argv("@anthropic-ai/claude-code", bin_name="claude"),
    )
    return definition


@pytest.mark.asyncio
async def test_npm_install_moves_a_foreign_bin_aside(tmp_path, monkeypatch):
    """占位指向别处（另一个 node 装的那份）时，先挪到备份名，再让 npm 建立自己的链接。"""
    prefix = _npm_sandbox(tmp_path, monkeypatch)
    foreign = tmp_path / "appcenter-node/claude"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    (prefix / "bin/claude").symlink_to(foreign)

    events = await _collect(_install_definition(), "install")

    assert events[-1]["ok"] is True
    assert "挪到" in _texts(events)
    assert os.readlink(prefix / "bin/claude") == "../lib/node_modules/pkg/cli.js"
    assert os.readlink(prefix / "bin/.claude.coda-bak") == str(foreign)


@pytest.mark.asyncio
async def test_npm_install_restores_the_foreign_bin_when_npm_fails(tmp_path, monkeypatch):
    """npm 自己失败时把占位放回原位 —— 不留「命令没了、包也没装上」的半截状态。"""
    prefix = _npm_sandbox(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_INSTALL_FAIL", "1")
    foreign = tmp_path / "appcenter-node/claude"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    (prefix / "bin/claude").symlink_to(foreign)

    events = await _collect(_install_definition(), "install")

    assert events[-1]["ok"] is False and events[-1]["exit_code"] == 7
    assert "放回原位" in _texts(events)
    assert os.readlink(prefix / "bin/claude") == str(foreign)
    assert not (prefix / "bin/.claude.coda-bak").exists()


@pytest.mark.asyncio
async def test_npm_install_keeps_a_link_that_is_already_ours(tmp_path, monkeypatch):
    """占位本来就指向 npm 全局目录（换链接不会冲突）时不动它。"""
    prefix = _npm_sandbox(tmp_path, monkeypatch)
    ours = prefix / "lib/node_modules/@anthropic-ai/claude-code"
    ours.mkdir(parents=True)
    (ours / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    # npm 自己建的是相对链接，解析后落在全局目录里
    (prefix / "bin/claude").symlink_to("../lib/node_modules/@anthropic-ai/claude-code/cli.js")

    events = await _collect(_install_definition(), "install")

    assert events[-1]["ok"] is True
    assert "挪到" not in _texts(events)
    assert not (prefix / "bin/.claude.coda-bak").exists()


@pytest.mark.asyncio
async def test_prepare_command_runs_before_the_action():
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(
        manager="test",
        install_argv=["sh", "-c", "echo 开始安装"],
        prepare_argv=["sh", "-c", "echo 清理残留"],
    )
    events = await _collect(definition, "install")
    output = _texts(events)
    assert "清理残留" in output and "开始安装" in output
    assert output.index("清理残留") < output.index("开始安装")
    assert events[-1]["ok"] is True


@pytest.mark.asyncio
async def test_failing_prepare_command_does_not_abort_the_action():
    """清扫只是清场：它自己失败了（退出码非 0）不影响动作本身的成败。"""
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(
        manager="test",
        install_argv=["sh", "-c", "echo 安装完成"],
        prepare_argv=["sh", "-c", "echo 清不掉 && exit 7"],
    )
    events = await _collect(definition, "install")
    output = _texts(events)
    assert "安装完成" in output
    assert "清扫未完成（退出码 7），继续执行安装" in output
    assert events[-1]["ok"] is True and events[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_missing_prepare_command_is_only_a_warning():
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(
        manager="test",
        install_argv=["sh", "-c", "echo 安装完成"],
        prepare_argv=["coda-does-not-exist"],
    )
    events = await _collect(definition, "install")
    output = _texts(events)
    assert "清扫未完成（未找到命令" in output
    assert events[-1]["ok"] is True


@pytest.mark.asyncio
async def test_missing_command_error_echoes_the_declared_manager():
    """命令没起来时只回显**适配器声明**的 manager，不猜具体包管理器、不假设部署形态。

    回归背景：macOS 打包版曾把内置 Node 运行时的**根目录**（而不是 `bin/`）塞进 PATH，
    于是 npm 内核的安装都失败在「未找到命令 node」。但修复不能把 node / npm 的知识写进
    框架层（铁律 1：包管理器是适配器的知识），也不能在共享后端里写「桌面版路径」——
    同一个后端还跑在 Docker 里。提示只负责说清「是哪套安装方式、该检查 PATH」。
    """
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(
        manager="npm",
        install_argv=["coda-node-does-not-exist"],
    )
    events = await _collect(definition, "install")
    error = events[-1]["error"]
    assert "未找到命令 `coda-node-does-not-exist`" in error
    assert "管理方式：npm" in error
    assert "resources/node-runtime" not in error, "部署形态的知识不该进框架层"
    assert events[-1]["ok"] is False


@pytest.mark.asyncio
async def test_stream_provision_without_command_is_explicit():
    events = await _collect(_NoCommandRuntime(), "install")
    assert events[-1]["type"] == "provision.finished"
    assert events[-1]["ok"] is False
    assert "未声明" in events[-1]["error"]


@pytest.mark.asyncio
async def test_stream_provision_without_provisioner_is_explicit():
    events = await _collect(get_agent("custom-cli"), "install")
    assert events[-1]["ok"] is False
    assert "不由平台管理" in events[-1]["error"]


@pytest.mark.asyncio
async def test_latest_version_reads_last_line():
    result = await provisioning.latest_version(_FakeRuntime())
    assert result["supported"] is True
    assert result["latest"] == "9.9.9"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_latest_version_reports_failure_as_data():
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(manager="test", latest_argv=["sh", "-c", "exit 7"])
    result = await provisioning.latest_version(definition)
    assert result["supported"] is True
    assert result["latest"] is None
    assert result["error"]


@pytest.mark.asyncio
async def test_latest_version_unsupported_without_command():
    result = await provisioning.latest_version(_NoCommandRuntime())
    assert result["supported"] is False and result["latest"] is None


# --- 取消与超时（铁律 10）----------------------------------------------------


def _sleeper() -> RuntimeProvisioner:
    """一条「打印 PID 后长睡」的无害命令：用来验证取消 / 超时真的杀掉了子进程。"""
    return RuntimeProvisioner(
        manager="test",
        install_argv=["sh", "-c", "echo PID=$$; exec sleep 30"],
    )


def _pid_from(events: list[dict]) -> int:
    for event in events:
        match = re.search(r"PID=(\d+)", event.get("text") or "")
        if match:
            return int(match.group(1))
    raise AssertionError("没有从输出里拿到子进程 PID")


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """等一个进程（含被 init 接管的孤儿）真正消失；僵尸会被内核很快回收。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    """进程是否还活着。僵尸算死：容器里的 PID 1 不一定回收孤儿，kill(0) 会一直 succeed。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            state = handle.read().rsplit(") ", 1)[1][0]
        if state == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


@pytest.mark.asyncio
async def test_closing_the_stream_kills_the_child_process():
    """客户端断开 → 生成器被关闭 → ``finally`` 必须杀掉子进程（铁律 10）。"""
    definition = _FakeRuntime()
    definition.provisioner = _sleeper()

    stream = provisioning.stream_provision(definition, "install")
    events = [await stream.__anext__(), await stream.__anext__()]
    assert [event["type"] for event in events] == ["provision.started", "provision.output"]
    pid = _pid_from(events)
    assert _alive(pid), "子进程还没起来，取消用例不成立"

    await stream.aclose()  # 等同于客户端断开时框架关闭事件流

    assert not _alive(pid), "关闭事件流后子进程仍在运行——取消没有生效"


@pytest.mark.asyncio
async def test_timeout_kills_the_child_and_reports_error(monkeypatch: pytest.MonkeyPatch):
    """超过 ``provision_timeout_seconds`` 必须杀掉子进程，并以 finished/error 收尾。"""
    definition = _FakeRuntime()
    definition.provisioner = _sleeper()
    monkeypatch.setattr(
        provisioning,
        "get_settings",
        lambda: SimpleNamespace(provision_timeout_seconds=1),
    )

    events = [event async for event in provisioning.stream_provision(definition, "install")]

    assert events[-1]["type"] == "provision.finished"
    assert events[-1]["ok"] is False and events[-1]["status"] == "error"
    assert any("上限" in (event.get("text") or "") for event in events), "超时应当明确告诉用户"
    assert not _alive(_pid_from(events)), "超时后子进程仍在运行"


# --- 接口 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runtimes_reports_status_and_provisioning(client: httpx.AsyncClient):
    payload = (await client.get("/api/runtimes")).json()
    items = {item["id"]: item for item in payload["items"]}
    assert {"claude-code", "codex", "antigravity-cli", "hermes-agent"} <= set(items)
    claude = items["claude-code"]
    assert claude["provisioning"]["supported"] is True
    assert claude["provisioning"]["manager"] == "npm"
    assert claude["provisioning"]["commands"]["install"]
    assert isinstance(claude["agents"], int)
    # 界面按这个路径渲染官方 Logo，必须随接口一起下发（不能只在前端硬编码）
    assert claude["logo"] == "/icons/kernels/claude.webp"
    assert claude["logo_style"] == "image"
    assert all(item["logo"] for item in payload["items"])
    assert items["openai-harness"]["provisioning"]["supported"] is False
    # 登录态探测与「有没有可代跑的登录命令」是两件事：只靠 API 密钥的内核不探测，
    # 账号型内核（豆包 / CodeBuddy）要把登录状态与可执行的登录动作都摆给管理员看。
    assert claude["auth_supported"] is False
    assert items["codebuddy-cli"]["auth_supported"] is True
    assert items["codebuddy-cli"]["provisioning"]["commands"]["login"]
    assert items["doubao-cli"]["auth_supported"] is True


@pytest.mark.asyncio
async def test_unknown_runtime_is_404(client: httpx.AsyncClient):
    assert (await client.post("/api/runtimes/does-not-exist/install")).status_code == 404
    assert (await client.post("/api/runtimes/does-not-exist/check")).status_code == 404
    assert (await client.get("/api/runtimes/does-not-exist/auth")).status_code == 404
    assert (await client.post("/api/runtimes/does-not-exist/login")).status_code == 404


@pytest.mark.asyncio
async def test_runtime_auth_is_not_applicable_for_api_key_kernels(client: httpx.AsyncClient):
    """不需要登录的内核返回 supported=False，不触发任何子进程探测。"""
    payload = (await client.get("/api/runtimes/claude-code/auth")).json()
    assert payload == {
        "supported": False,
        "required": False,
        "logged_in": None,
        "detail": None,
        "account": None,
    }


@pytest.mark.asyncio
async def test_runtime_account_switch_api(client: httpx.AsyncClient, monkeypatch, tmp_path):
    """多账号：切回已保存的账号后，登录态接口与 session 文件都要跟着变。"""
    from app.agents.builtin import codebuddy_accounts as accounts

    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CODEBUDDY_CONFIG_DIR", "CODEBUDDY_API_KEY", "CODEBUDDY_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    root = accounts.auth_store_dir()
    root.mkdir(parents=True)
    live = root / "Tencent-Cloud.coding-copilot.info"
    live.write_bytes(b"session-A")
    slot_a = accounts.snapshot_live({"userId": "u-a", "userName": "Alice"})
    live.write_bytes(b"session-B")
    accounts.snapshot_live({"userId": "u-b", "userName": "Bob"})

    payload = (await client.get("/api/runtimes/codebuddy-cli/auth")).json()
    assert payload["logged_in"] is True
    assert payload["account"] == "Bob"
    assert {item["label"] for item in payload["accounts"]} == {"Alice", "Bob"}

    switched = await client.post(f"/api/runtimes/codebuddy-cli/accounts/{slot_a}/activate")
    assert switched.status_code == 200
    assert switched.json()["account"] == "Alice"
    assert live.read_bytes() == b"session-A"

    removed = await client.delete(f"/api/runtimes/codebuddy-cli/accounts/{slot_a}")
    assert removed.status_code == 200
    assert [item["label"] for item in removed.json()["accounts"]] == ["Bob"]

    assert (
        await client.post("/api/runtimes/codebuddy-cli/accounts/does-not-exist/activate")
    ).status_code == 404


@pytest.mark.asyncio
async def test_runtime_account_api_rejects_single_account_kernels(client: httpx.AsyncClient):
    assert (
        await client.post("/api/runtimes/claude-code/accounts/x/activate")
    ).status_code == 409
    assert (await client.delete("/api/runtimes/claude-code/accounts/x")).status_code == 409


@pytest.mark.asyncio
async def test_runtime_auth_hides_saved_accounts_from_members(
    client: httpx.AsyncClient, monkeypatch, tmp_path
):
    """账号清单是机器级（管理员）资产：普通成员只看登录态，看不到这台机器登录过哪些账号。"""
    from app.agents.builtin import codebuddy_accounts as accounts
    from app.settings import get_settings

    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CODEBUDDY_CONFIG_DIR", "CODEBUDDY_API_KEY", "CODEBUDDY_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    root = accounts.auth_store_dir()
    root.mkdir(parents=True)
    (root / "session.info").write_bytes(b"session-A")
    accounts.snapshot_live({"userId": "u-a", "userName": "Alice"})

    # 打开鉴权：首任注册者是管理员，第二个是普通成员。
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    admin = (
        await client.post(
            "/api/auth/register", json={"username": "root", "password": "root-pass-123"}
        )
    ).json()
    member = (
        await client.post(
            "/api/auth/register", json={"username": "alice", "password": "alice-pass-123"}
        )
    ).json()

    def headers(user: dict) -> dict:
        return {"Authorization": f"Bearer {user['access_token']}"}

    admin_view = (
        await client.get("/api/runtimes/codebuddy-cli/auth", headers=headers(admin))
    ).json()
    assert admin_view["logged_in"] is True
    assert [item["label"] for item in admin_view["accounts"]] == ["Alice"]

    member_view = (
        await client.get("/api/runtimes/codebuddy-cli/auth", headers=headers(member))
    ).json()
    assert member_view["logged_in"] is True
    assert member_view["accounts"] is None


@pytest.mark.asyncio
async def test_unmanaged_runtime_is_rejected_without_running(client: httpx.AsyncClient):
    response = await client.post("/api/runtimes/custom-cli/install")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_uninstall_requires_confirmation(client: httpx.AsyncClient):
    """未确认的卸载会在建流之前被拒——不会真的执行 npm。"""
    response = await client.post("/api/runtimes/claude-code/uninstall", json={"confirm": False})
    assert response.status_code == 422
    assert "确认" in response.json()["detail"]


@pytest.mark.asyncio
async def test_provision_endpoint_streams_sse(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """整条 SSE 链路：接口把安装器的 provision.* 事件翻成事件流后收尾 stream.closed。"""
    from app.api import runtimes as runtimes_api

    monkeypatch.setattr(runtimes_api, "get_agent", lambda _type_id: _FakeRuntime())
    async with client.stream("POST", "/api/runtimes/fake-runtime/install", json={"confirm": True}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "event: provision.started" in body
    assert "安装完成" in body
    assert "event: provision.finished" in body
    assert "event: stream.closed" in body


@pytest.mark.asyncio
async def test_check_endpoint_returns_latest(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from app.api import runtimes as runtimes_api

    monkeypatch.setattr(runtimes_api, "get_agent", lambda _type_id: _FakeRuntime())
    payload = (await client.post("/api/runtimes/fake-runtime/check")).json()
    assert payload["supported"] is True and payload["latest"] == "9.9.9"


@pytest.mark.asyncio
async def test_timeout_kills_the_wrapped_child_process(monkeypatch: pytest.MonkeyPatch):
    """安装命令是「包装脚本 → 再起 npm」：超时必须连子进程一起杀。

    只杀直接子进程会把 npm 留成孤儿，它会继续写 npm 全局目录 —— 下一次安装
    撞上 ENOTEMPTY（prepare_argv 的存在就是为了清这个，但源头要堵住）。
    """
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(
        manager="test",
        install_argv=["sh", "-c", "sleep 30 & echo CHILD=$!; wait"],
    )
    monkeypatch.setattr(
        provisioning,
        "get_settings",
        lambda: SimpleNamespace(provision_timeout_seconds=1),
    )

    events = [event async for event in provisioning.stream_provision(definition, "install")]

    assert events[-1]["status"] == "error"
    match = re.search(r"CHILD=(\d+)", _texts(events))
    assert match, _texts(events)
    assert await _wait_dead(int(match.group(1))), "包装脚本的子进程还在跑 —— 超时没有杀进程组"
