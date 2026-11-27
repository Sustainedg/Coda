"""DeepSeek Harness（``dsh``）适配器：启动参数、隔离目录与事件解析。"""
from __future__ import annotations

import json

from app.agents import AgentContext, get_agent
from app.agents.mcp import McpServerSpec


def make_ctx(tmp_path, **overrides) -> AgentContext:
    base = dict(
        agent_id="a1",
        agent_name="dsh",
        gateway_url="https://gw.example/anthropic",
        api_key="sk-secret",
        model="deepseek-v4-pro",
        system_prompt="你是一名严谨的工程师。",
        workspace_dir=str(tmp_path / "ws"),
        home_dir=str(tmp_path / "dsh-home"),
        prompt="hello dsh",
    )
    base.update(overrides)
    return AgentContext(**base)


def test_invocation_uses_headless_json_and_isolated_home(tmp_path):
    definition = get_agent("deepseek-harness")
    invocation = definition.build_invocation(make_ctx(tmp_path, session_id="session-1"))

    assert invocation.argv[:2] == ["dsh", "headless"]
    assert invocation.argv[-1] == "-"
    assert invocation.stdin == "hello dsh"
    # launcher 的 --patch 必须排在第一个「它不认识的参数」（--json）之前
    assert invocation.argv.index("--patch") < invocation.argv.index("--json")
    assert "--patch" in invocation.argv
    session_at = invocation.argv.index("--session-id")
    assert invocation.argv[session_at + 1] == "session-1"
    assert invocation.argv[invocation.argv.index("--permission-mode") + 1] == "workspace-write"
    assert invocation.argv[invocation.argv.index("--approval-policy") + 1] == "never"

    assert invocation.env["DEEPSEEK_API_KEY"] == "sk-secret"
    assert invocation.env["DEEPSEEK_BASE_URL"] == "https://gw.example/anthropic"
    assert invocation.env["DSH_HOME"] == str(tmp_path / "dsh-home")
    assert invocation.env["DSH_AGENTS_HOME"] == str(tmp_path / "dsh-home" / "agents")

    home = tmp_path / "dsh-home"
    overlay = (home / "coda.patch.yml").read_text(encoding="utf-8")
    assert "agent-default-model" in overlay
    assert "deepseek-v4-pro" in overlay
    assert "includeDefaultRoots: false" in overlay
    assert str(home / "skills") in overlay
    # 人格走 dsh 自己的用户级指令文件，不再拼进用户消息
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "你是一名严谨的工程师。"


def test_invocation_without_session_or_home_still_works(tmp_path):
    definition = get_agent("deepseek-harness")
    invocation = definition.build_invocation(
        make_ctx(tmp_path, session_id=None, home_dir=None, system_prompt="人格")
    )
    assert "--session-id" not in invocation.argv
    assert "--patch" not in invocation.argv
    # 没有隔离目录时退回「人格拼进提示词」
    assert invocation.stdin.startswith("人格\n\n---\n\n")


def test_foreign_session_id_is_not_forwarded(tmp_path):
    """换过内核的对话可能带着别家的会话 id：dsh 不认，直接开新会话即可。"""
    definition = get_agent("deepseek-harness")
    invocation = definition.build_invocation(make_ctx(tmp_path, session_id="sess-42"))
    assert "--session-id" not in invocation.argv


def test_permission_mode_and_extra_args_are_forwarded(tmp_path):
    definition = get_agent("deepseek-harness")
    invocation = definition.build_invocation(
        make_ctx(tmp_path, options={"permission_mode": "danger-full-access", "extra_args": ["--add-dir", "/tmp/x"]})
    )
    at = invocation.argv.index("--permission-mode")
    assert invocation.argv[at + 1] == "danger-full-access"
    assert "--add-dir" in invocation.argv and "/tmp/x" in invocation.argv


def test_parse_line_projects_the_headless_event_stream(tmp_path):
    definition = get_agent("deepseek-harness")
    context = make_ctx(tmp_path)
    run = definition.create_run(context)

    def feed(payload: dict) -> list:
        return list(run.feed(json.dumps(payload)))

    events = []
    events += feed({"type": "session", "sessionId": "session-42", "cwd": "/tmp"})
    events += feed({"type": "thinking", "text": "先看一眼仓库"})
    events += feed({"type": "tool_call", "callId": "c1", "tool": "bash", "input": {"command": "ls"}})
    events += feed({"type": "tool_result", "callId": "c1", "status": "completed", "result": "ok"})
    events += feed({"type": "text", "text": "你好"})
    events += feed(
        {
            "type": "status",
            "phase": "step_end",
            "usage": {
                "inputTokens": 3,
                "outputTokens": 4,
                "totalTokens": 7,
                "cacheReadTokens": 1,
                "cacheWriteTokens": 2,
                "reasoningTokens": 5,
            },
        }
    )
    events += feed({"type": "final", "sessionId": "session-42", "reason": {"kind": "completed"}, "text": "你好"})

    kinds = [event.type.value for event in events]
    assert kinds.count("session") == 1
    assert "thinking.delta" in kinds
    assert "tool.call" in kinds and "tool.result" in kinds
    assert "usage.raw" in kinds
    assert run.emitted_text == "你好"
    assert run.session_id == "session-42"
    usage_event = next(event for event in events if event.type.value == "usage.raw")
    # P4：适配器不再提前翻译，原样保留驼峰字段供 metering 层统一归一化。
    assert usage_event.payload == {
        "inputTokens": 3,
        "outputTokens": 4,
        "totalTokens": 7,
        "cacheReadTokens": 1,
        "cacheWriteTokens": 2,
        "reasoningTokens": 5,
    }


def test_final_text_is_emitted_when_no_streaming_text_arrived(tmp_path):
    definition = get_agent("deepseek-harness")
    run = definition.create_run(make_ctx(tmp_path))
    events = list(run.feed(json.dumps({"type": "final", "text": "只有汇总", "reason": None})))
    assert [event.text for event in events if event.type.value == "message.delta"] == ["只有汇总"]


def test_error_event_marks_the_run_as_failed(tmp_path):
    definition = get_agent("deepseek-harness")
    run = definition.create_run(make_ctx(tmp_path))
    events = list(run.feed(json.dumps({"type": "error", "message": "boom"})))
    assert events[0].type.value == "error"
    assert run.state["is_error"] is True


def test_mcp_mount_renders_client_rows(tmp_path):
    definition = get_agent("deepseek-harness")
    context = make_ctx(
        tmp_path,
        mcp_servers=[
            McpServerSpec(
                name="GitHub",
                slug="GitHub",
                transport="stdio",
                command="npx",
                args=["-y", "server-github"],
                env={"GITHUB_TOKEN": "t"},
            ),
            McpServerSpec(
                name="Web",
                slug="web",
                transport="http",
                url="https://mcp.example/mcp",
                headers={"Authorization": "Bearer x"},
            ),
            McpServerSpec(name="旧式 SSE", slug="sse", transport="sse", url="https://sse.example"),
        ],
    )
    mount = definition.mcp_mount(context)
    content = mount.files["coda-mcp.patch.yml"]
    assert "- insert:" in content
    assert "@deepseek-ai/dsh-mcp-client" in content
    assert 'serverName: "GitHub"' in content
    assert "transport: stdio" in content
    assert 'command: "npx"' in content
    assert "transport: streamable-http" in content
    assert 'url: "https://mcp.example/mcp"' in content
    # SSE 不支持：跳过并给出说明，而不是写一条内核读不懂的配置
    assert "sse.example" not in content
    assert any("SSE" in note for note in mount.notes)
    assert mount.home_name == "dsh-home"


def test_mcp_mount_without_servers_writes_an_empty_patch(tmp_path):
    definition = get_agent("deepseek-harness")
    mount = definition.mcp_mount(make_ctx(tmp_path, mcp_servers=[]))
    # 必须覆盖旧文件，否则取消分配后上一轮的服务器会继续生效
    assert mount.files["coda-mcp.patch.yml"].strip() == "[]"


def test_discovers_host_mcp_servers(tmp_path, monkeypatch):
    """本机 dsh 的 profile patch 里的两种 MCP 写法都要能只读扫描出来。"""
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    profile = tmp_path / "profiles" / "headless"
    profile.mkdir(parents=True)
    (profile / "cordis.patch.yml").write_text(
        """
- id: mcp
  name: '@deepseek-ai/dsh-mcp-manager'
  config:
    servers:
      github:
        transport: stdio
        command: npx
        args: ['-y', 'server-github']
- name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: web
    transport: streamable-http
    url: https://mcp.example/mcp
""",
        encoding="utf-8",
    )
    specs = get_agent("deepseek-harness").discover_mcp_servers()
    by_name = {spec.name: spec for spec in specs}
    assert by_name["github"].transport == "stdio"
    assert by_name["github"].args == ["-y", "server-github"]
    assert by_name["web"].transport == "http"
    assert by_name["web"].url == "https://mcp.example/mcp"


def test_skills_share_the_isolated_home(tmp_path):
    definition = get_agent("deepseek-harness")
    mount = definition.skill_mount(make_ctx(tmp_path))
    assert mount is not None
    assert mount.home_name == "dsh-home"
    assert mount.skills_subdir == "skills"


def test_escape_hatch_templates_are_hidden_from_new_agents():
    for type_id in ("custom-cli", "openai-harness"):
        assert get_agent(type_id).selectable is False, f"{type_id} 不应出现在新建智能体的内核下拉里"
    assert get_agent("deepseek-harness").selectable is True
    assert get_agent("claude-code").selectable is True


def test_descriptor_exposes_selectable(tmp_path):
    data = get_agent("custom-cli").descriptor()
    assert data["selectable"] is False
    assert get_agent("deepseek-harness").descriptor()["selectable"] is True