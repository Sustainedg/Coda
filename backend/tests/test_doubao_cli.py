"""豆包 CLI 适配器：启动参数、登录态探测与 stream-json 解析。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterable

import pytest

from app.agents import AgentContext, AgentDefinition, AuthStatus, Runtime, get_agent
from app.agents.base import RuntimeProvisioner
from app.runtime import provisioning


def make_ctx(tmp_path, **overrides) -> AgentContext:
    base = dict(
        agent_id="a1",
        agent_name="豆包",
        model="doubao-pro",
        system_prompt="你是一名严谨的工程师。",
        workspace_dir=str(tmp_path / "ws"),
        home_dir=str(tmp_path / "doubao-home"),
        prompt="hello doubao",
        options={"permission_mode": "always_allow", "effort": "medium"},
    )
    base.update(overrides)
    return AgentContext(**base)


def test_invocation_uses_stream_json_stdin_and_isolated_home(tmp_path):
    definition = get_agent("doubao-cli")
    invocation = definition.build_invocation(make_ctx(tmp_path, session_id="conv-1"))

    # -C 必须排在业务参数之前；提示词走 stdin（-p -）。
    assert invocation.argv[:4] == ["doubao", "-C", str(tmp_path / "ws"), "--output-format"]
    assert invocation.argv[4] == "stream-json"
    assert invocation.argv[-2:] == ["-p", "-"]
    assert invocation.argv[invocation.argv.index("--model") + 1] == "doubao-pro"
    assert invocation.argv[invocation.argv.index("--effort") + 1] == "medium"
    assert invocation.argv[invocation.argv.index("--permission-mode") + 1] == "always_allow"
    assert invocation.argv[invocation.argv.index("--resume") + 1] == "conv-1"
    # 账号型内核：不注入任何网关/密钥。
    assert "OPENAI_API_KEY" not in invocation.env
    assert invocation.env["CI"] == "1"
    assert invocation.env["HOME"] == str(tmp_path / "doubao-home")
    assert "XDG_DATA_HOME" in invocation.env
    # 人格拼进提示词，而不是写用户的 AGENTS.md。
    assert invocation.stdin.startswith("你是一名严谨的工程师。")
    assert invocation.stdin.endswith("hello doubao")


def test_invocation_bypass_flag_replaces_permission_mode(tmp_path):
    definition = get_agent("doubao-cli")
    invocation = definition.build_invocation(
        make_ctx(tmp_path, options={"bypass_sandbox": True})
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in invocation.argv
    assert "--permission-mode" not in invocation.argv


def test_stream_json_parsing_dedupes_text_tools_and_results(tmp_path):
    definition = get_agent("doubao-cli")
    run = definition.create_run(make_ctx(tmp_path))
    lines = [
        {"type": "system", "subtype": "init", "session_id": "conv-1"},
        {
            "type": "stream_event",
            "session_id": "conv-1",
            "message_id": "m1",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "你"},
            },
        },
        {
            "type": "stream_event",
            "session_id": "conv-1",
            "message_id": "m1",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "好"},
            },
        },
        # 同一条消息的完成态：不应重复整段文本。
        {
            "type": "assistant",
            "session_id": "conv-1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "text", "text": "你好"}],
            },
        },
        # 参数还没流完的 tool_use：先不报。
        {
            "type": "assistant",
            "session_id": "conv-1",
            "message": {
                "id": "m2",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input_status": "unavailable",
                    }
                ],
            },
        },
        # 参数完整：报一次；重复的同 id 不再报。
        {
            "type": "assistant",
            "session_id": "conv-1",
            "message": {
                "id": "m2",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                        "input_status": "available",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "session_id": "conv-1",
            "message": {
                "id": "m2",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                        "input_status": "available",
                    }
                ],
            },
        },
        {
            "type": "user",
            "session_id": "conv-1",
            "message": {
                "id": "m2:tool_result:t1",
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "a.txt"}],
                        "isError": False,
                    }
                ],
            },
        },
        {
            "type": "user",
            "session_id": "conv-1",
            "message": {
                "id": "m2:tool_result:t1",
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "repeat"}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "conv-1",
            "result": {"type": "text", "content": "你好"},
        },
    ]
    events = [event for line in lines for event in run.feed(json.dumps(line))]
    kinds = [event.type.value for event in events]

    assert kinds.count("session") == 1
    assert run.emitted_text == "你好"  # 增量与完成态没有重复
    assert kinds.count("tool.call") == 1
    assert kinds.count("tool.result") == 1
    assert run.session_id == "conv-1"
    call = next(event for event in events if event.type.value == "tool.call")
    assert call.name == "Bash"
    assert call.payload["input"] == {"command": "ls"}


def test_result_error_and_permission_denials_are_visible(tmp_path):
    definition = get_agent("doubao-cli")
    run = definition.create_run(make_ctx(tmp_path))
    payload = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "session_id": "conv-1",
        "errors": ["工具执行失败"],
        "permission_denials": [{"tool_name": "Bash", "prompt": "写入文件"}],
    }
    events = list(run.feed(json.dumps(payload)))
    kinds = [event.type.value for event in events]
    assert "error" in kinds
    assert any(event.type.value == "log" and event.payload.get("level") == "warn" for event in events)


def test_auth_status_parses_login_required_and_success(tmp_path, monkeypatch):
    from app.agents.builtin import doubao_cli

    definition = get_agent("doubao-cli")
    monkeypatch.setattr(doubao_cli.shutil, "which", lambda _: "/usr/bin/doubao")

    def fake_run(argv, **kwargs):
        assert argv[1:] == ["auth", "status"]
        return subprocess.CompletedProcess(argv, 0, stdout="已登录\n用户 ID：1024\n", stderr="")

    monkeypatch.setattr(doubao_cli.subprocess, "run", fake_run)
    status = definition.auth_status()
    assert status.logged_in is True
    assert "1024" in (status.account or "")

    monkeypatch.setattr(
        doubao_cli.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="login_required: 当前未登录，请运行 `doubao login` 登录"
        ),
    )
    status = definition.auth_status()
    assert status.logged_in is False
    assert "登录" in (status.detail or "")


@pytest.mark.asyncio
async def test_login_action_turns_qr_image_into_data_url(tmp_path):
    """登录命令按通用约定输出 QR_IMAGE=<路径>，框架转成 data URL 并回带登录态。"""
    qr = tmp_path / "qr.png"
    qr.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    class FakeLoginRuntime(AgentDefinition):
        id = "fake-login"
        name = "假登录内核"
        description = "测试用"
        runtime = Runtime.CLI
        executable = None
        provisioner = RuntimeProvisioner(
            manager="test",
            login_argv=["sh", "-c", f"echo QR_IMAGE={qr}"],
        )

        def build_invocation(self, ctx):  # pragma: no cover - 契约占位
            raise NotImplementedError

        def parse_line(self, line, run) -> Iterable:  # pragma: no cover
            return ()

        def auth_status(self) -> AuthStatus:
            return AuthStatus(required=True, logged_in=True, detail="已登录")

    events = [event async for event in provisioning.stream_provision(FakeLoginRuntime(), "login")]
    qr_events = [event for event in events if event["type"] == "provision.qr"]
    assert len(qr_events) == 1
    assert qr_events[0]["data_url"].startswith("data:image/png;base64,")
    final = events[-1]
    assert final["type"] == "provision.finished"
    assert final["ok"] is True
    assert final["auth"]["logged_in"] is True


@pytest.mark.asyncio
async def test_provision_job_replays_qr_on_reconnect():
    """刷新 / 重连后还能把二维码补回来，用户不必重新点一次登录。"""
    job = provisioning.ProvisionJob("doubao-cli", "豆包 CLI", "login")
    job.add_qr("data:image/png;base64,AAA")
    job.finish("ok", auth={"required": True, "logged_in": True})
    events = [event async for event in provisioning.stream_job(job)]
    assert any(event["type"] == "provision.qr" for event in events)
    assert events[-1]["type"] == "provision.finished"
    assert events[-1]["auth"]["logged_in"] is True


def test_skill_mount_materializes_only_assigned_skills(tmp_path):
    definition = get_agent("doubao-cli")
    mount = definition.skill_mount(make_ctx(tmp_path))
    assert mount.home_name == "doubao-home"
    assert mount.skills_subdir == ".agents/skills"
    assert definition.logo == "/icons/kernels/doubao.png"
    assert definition.logo_style == "image"
    assert definition.provisioner.to_dict()["commands"]["login"] == [
        "doubao",
        "login",
        "--qr=png",
    ]


def test_link_macos_data_links_doubao_cli_and_mssdk(tmp_path, monkeypatch):
    """macOS 隔离 HOME 里 ``doubao_cli`` 与 ``mssdk`` 都要软链到真实目录。

    漏链 ``mssdk``（设备身份的安全存储）会让每次运行在隔离目录重建设备身份，
    报「无法访问用于保存设备身份的安全存储」，对话起不来——真机上踩过的坑。
    """
    from app.agents.builtin import doubao_cli

    real_home = tmp_path / "real-home"
    isolated_home = tmp_path / "isolated-home"
    for name in ("doubao_cli", "mssdk"):
        (real_home / "Library" / "Application Support" / name).mkdir(parents=True)

    monkeypatch.setattr(doubao_cli.sys, "platform", "darwin")
    monkeypatch.setattr(doubao_cli.os, "environ", {**os.environ, "HOME": str(real_home)})

    doubao_cli._link_macos_data(str(isolated_home))
    support = isolated_home / "Library" / "Application Support"
    for name in ("doubao_cli", "mssdk"):
        target = support / name
        assert target.is_symlink(), f"缺少软链：{target}"
        assert target.resolve() == (real_home / "Library" / "Application Support" / name).resolve()


def test_link_macos_data_skips_existing_targets(tmp_path, monkeypatch):
    """已存在的目标（早建过的软链 / 用户自建目录）不覆盖，也不抛错。"""
    from app.agents.builtin import doubao_cli

    real_home = tmp_path / "real-home"
    isolated_home = tmp_path / "isolated-home"
    (real_home / "Library" / "Application Support" / "doubao_cli").mkdir(parents=True)
    (real_home / "Library" / "Application Support" / "mssdk").mkdir(parents=True)
    preexisting = isolated_home / "Library" / "Application Support" / "mssdk"
    preexisting.mkdir(parents=True)
    (preexisting / "keep.txt").write_text("设备身份占位", encoding="utf-8")

    monkeypatch.setattr(doubao_cli.sys, "platform", "darwin")
    monkeypatch.setattr(doubao_cli.os, "environ", {**os.environ, "HOME": str(real_home)})

    doubao_cli._link_macos_data(str(isolated_home))  # 不抛错即通过
    assert (preexisting / "keep.txt").read_text(encoding="utf-8") == "设备身份占位"
    assert (isolated_home / "Library" / "Application Support" / "doubao_cli").is_symlink()


def test_link_macos_data_noop_outside_darwin(tmp_path, monkeypatch):
    """非 darwin 平台（Linux 走 XDG_DATA_HOME、Windows 走 LOCALAPPDATA）不建任何链接。"""
    from app.agents.builtin import doubao_cli

    monkeypatch.setattr(doubao_cli.sys, "platform", "linux")
    doubao_cli._link_macos_data(str(tmp_path / "isolated-home"))
    assert not (tmp_path / "isolated-home" / "Library").exists()


def test_link_windows_data_links_real_data_dirs(tmp_path, monkeypatch):
    """Windows：隔离目录里要把真实数据目录接进来。

    Windows 原生程序按「用户文件夹」解析数据根（``SHGetFolderPath`` /
    ``SHGetKnownFolderPath``），而它优先认进程里的 ``USERPROFILE``。隔离 HOME 改了
    ``USERPROFILE`` 之后，原生程序就会去隔离目录里另起一份数据：找不到真实登录态，
    还会重建设备身份（报「无法访问用于保存设备身份的安全存储」）。
    """
    from app.agents.builtin import doubao_cli

    real_local = tmp_path / "AppData" / "Local"
    (real_local / "doubao_cli").mkdir(parents=True)
    (real_local / "mssdk").mkdir(parents=True)
    isolated_home = tmp_path / "isolated-home"
    linked: list[tuple[Path, Path]] = []

    monkeypatch.setattr(doubao_cli.sys, "platform", "win32")
    monkeypatch.setattr(
        doubao_cli.os, "environ", {**os.environ, "LOCALAPPDATA": str(real_local)}
    )

    doubao_cli._link_windows_data(
        str(isolated_home), link_dir=lambda link, target: linked.append((link, target))
    )

    # 隔离目录里这两个名字都要接到真实目录（否则原生程序会去另起一份数据）。
    assert [(link.name, target) for link, target in linked] == [
        ("doubao_cli", real_local / "doubao_cli"),
        ("mssdk", real_local / "mssdk"),
    ]
    assert all(link.parent == isolated_home / "AppData" / "Local" for link, _ in linked)


def test_link_windows_data_noop_without_localappdata(tmp_path, monkeypatch):
    """拿不到 ``LOCALAPPDATA`` 时不乱建目录：让内核自己报「无法创建应用数据目录」。"""
    from app.agents.builtin import doubao_cli

    monkeypatch.setattr(doubao_cli.sys, "platform", "win32")
    without_local = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
    monkeypatch.setattr(doubao_cli.os, "environ", without_local)

    doubao_cli._link_windows_data(str(tmp_path / "isolated-home"))

    assert not (tmp_path / "isolated-home" / "AppData").exists()


def test_windows_env_keeps_localappdata_and_drops_xdg(tmp_path, monkeypatch):
    """Windows 上原生程序不认 ``XDG_DATA_HOME``，认的是「用户文件夹」。

    所以 Windows 分支不该再设 ``XDG_DATA_HOME``（那是 Linux 的旋钮，设了也白设），
    而要把真实 ``LOCALAPPDATA`` 原样传下去，让它找到真实的数据与登录态。
    """
    from app.agents.builtin import doubao_cli

    definition = get_agent("doubao-cli")
    real_local = tmp_path / "AppData" / "Local"
    (real_local / "doubao_cli").mkdir(parents=True)

    monkeypatch.setattr(doubao_cli.sys, "platform", "win32")
    monkeypatch.setattr(
        doubao_cli.os, "environ", {**os.environ, "LOCALAPPDATA": str(real_local)}
    )

    ctx = make_ctx(tmp_path, home_dir=str(tmp_path / "doubao-home"))
    invocation = definition.build_invocation(ctx)

    assert "XDG_DATA_HOME" not in invocation.env
    assert invocation.env["USERPROFILE"] == ctx.home_dir
    assert invocation.env["LOCALAPPDATA"] == str(real_local)


def test_posix_env_still_points_xdg_data_home_at_real_dir(tmp_path, monkeypatch):
    """Linux 保持原样：``XDG_DATA_HOME`` 指向真实 ``~/.local/share``（登录态共享）。"""
    from app.agents.builtin import doubao_cli

    definition = get_agent("doubao-cli")
    monkeypatch.setattr(doubao_cli.sys, "platform", "linux")
    monkeypatch.setattr(doubao_cli.os, "environ", {**os.environ, "HOME": str(tmp_path / "home")})

    ctx = make_ctx(tmp_path, home_dir=str(tmp_path / "doubao-home"))
    invocation = definition.build_invocation(ctx)

    assert invocation.env["XDG_DATA_HOME"] == str(tmp_path / "home" / ".local" / "share")
    assert invocation.env["HOME"] == ctx.home_dir


def test_auth_argv_on_windows_passes_one_command_string(monkeypatch):
    """Windows 上探测命令要走 ``cmd.exe``，且与真实运行用同一套拼法。

    平台里 npm 垫片只有 ``subprocess.list2cmdline`` 拼整串这一种形态
    （``runtime/runner.py:_spawn_argv`` 及其单测钉着它）。探测另起一种拼法，
    就会在引号 / 空格上出现两套行为：装得下却探不到登录态。
    """
    from app.agents.builtin import doubao_cli

    monkeypatch.setattr(doubao_cli.sys, "platform", "win32")
    exe = r"C:\Users\John Doe\AppData\Roaming\Coda\node\global\doubao.cmd"

    argv = doubao_cli._auth_argv(exe)

    assert argv[:4] == [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
    assert argv[4] == subprocess.list2cmdline([exe, "auth", "status"])
