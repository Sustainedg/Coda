from __future__ import annotations

import json

from app.agents import AgentContext, get_agent, known_ids
from app.agents.base import AgentRun


def ctx(**overrides) -> AgentContext:
    base = dict(
        agent_id="a1",
        agent_name="demo",
        gateway_url="https://gateway.example/v1",
        api_key="sk-secret",
        model="some-model",
        system_prompt="You are a tester.",
        workspace_dir="/tmp/ws",
        prompt="hello world",
    )
    base.update(overrides)
    return AgentContext(**base)


def test_registry_exposes_first_party_adapters():
    for expected in (
        "claude-code",
        "codex",
        "antigravity-cli",
        "openai-harness",
        "custom-cli",
        "hermes-agent",
        "deepseek-harness",
        "codebuddy-cli",
        "doubao-cli",
    ):
        assert expected in known_ids()


def test_claude_code_invocation_and_auth_styles():
    definition = get_agent("claude-code")
    invocation = definition.build_invocation(ctx())
    assert invocation.argv[:4] == ["claude", "-p", "--output-format", "stream-json"]
    assert "--append-system-prompt" in invocation.argv
    assert invocation.stdin == "hello world"
    assert invocation.env["ANTHROPIC_BASE_URL"] == "https://gateway.example/v1"
    assert invocation.env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"
    assert "ANTHROPIC_API_KEY" not in invocation.env

    # 网关声明 x-api-key（直连 Anthropic 官方）时换用 API_KEY 请求头
    keyed = get_agent("claude-code").build_invocation(ctx(auth_style="x-api-key"))
    assert keyed.env["ANTHROPIC_API_KEY"] == "sk-secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in keyed.env


def test_claude_code_system_prompt_disables_snapshot():
    """注入人格必须同时关掉内核的「系统提示词快照」。

    Claude Code 默认把系统提示词在会话第一次请求时拍成快照，``--resume`` 续接时
    原样重放旧版本、无视本轮传入的人格文本（append 与 replace 都一样）——平台语义
    是每轮按智能体当前人格重新注入，因此两种模式都要带上关闭旗标。
    """
    definition = get_agent("claude-code")

    # append（默认）：注入人格 → 必须带 --system-prompt-snapshot off
    appended = definition.build_invocation(ctx())
    assert "--append-system-prompt" in appended.argv
    assert appended.argv[appended.argv.index("--system-prompt-snapshot") + 1] == "off"

    # replace（完全覆盖）：同样必须带，否则存量对话续聊时换不掉第一次的旧快照
    replaced = definition.build_invocation(ctx(options={"system_prompt_mode": "replace"}))
    assert "--system-prompt" in replaced.argv
    assert "--append-system-prompt" not in replaced.argv
    assert replaced.argv[replaced.argv.index("--system-prompt-snapshot") + 1] == "off"

    # resume 场景：续接 + 人格注入，旗标仍在
    resumed = definition.build_invocation(
        ctx(session_id="s-1", options={"system_prompt_mode": "replace"})
    )
    assert resumed.argv[resumed.argv.index("--resume") + 1] == "s-1"
    assert resumed.argv[resumed.argv.index("--system-prompt-snapshot") + 1] == "off"

    # 没有人格时不带旗标：快照开着与否无从谈起，保持命令行最小
    bare = definition.build_invocation(ctx(system_prompt=None))
    assert "--system-prompt-snapshot" not in bare.argv
    assert "--append-system-prompt" not in bare.argv


def test_claude_code_stream_parsing_dedupes_partial_text():
    definition = get_agent("claude-code")
    context = ctx()
    run = definition.create_run(context)
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "sonnet", "tools": ["Bash"]},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "session_id": "s1", "usage": {"input_tokens": 3, "output_tokens": 4}, "total_cost_usd": 0.01},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    kinds = [e.type.value for e in events]
    assert kinds.count("session") == 1
    assert run.emitted_text == "Hello"  # 不应重复输出整段文本
    assert "tool.call" in kinds and "tool.result" in kinds and "usage" in kinds
    assert run.session_id == "s1"


def test_claude_code_reports_context_usage_from_message_start():
    """窗口占用取 ``message_start.message.usage``；message_delta 只有 output_tokens，不计入。"""
    definition = get_agent("claude-code")
    run = definition.create_run(ctx())
    lines = [
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 5,
                    }
                },
            },
        },
        # 只有输出 token：不能当成窗口输入
        {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 9}}},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    assert [e.type.value for e in events] == ["context.usage"]
    assert events[0].payload["tokens"] == 125


def test_codex_invocation_uses_named_provider_and_resume():
    definition = get_agent("codex")
    invocation = definition.build_invocation(
        ctx(
            options={
                "transport": "exec",
                "provider_name": "mygateway",
                "sandbox_mode": "workspace-write",
            },
            session_id="tid-1",
        )
    )
    assert invocation.argv[:3] == ["codex", "exec", "resume"]
    assert "tid-1" in invocation.argv
    assert "--json" in invocation.argv
    joined = " ".join(invocation.argv)
    assert "model_providers.mygateway.base_url=\"https://gateway.example/v1\"" in joined
    assert invocation.env["OPENAI_API_KEY"] == "sk-secret"


def test_codex_gateway_goes_through_a_named_provider():
    """自定义网关必须落到具名 model_provider 上 —— 新版 Codex 已不再读 ``OPENAI_BASE_URL``。

    同时不下发 ``wire_api``：新版只剩 responses、旧版默认 chat，写死任何一边都会在另一边
    直接报错；``provider_name`` 留空时用内置名 ``coda``。
    """
    definition = get_agent("codex")
    # 存量智能体里由旧默认值写下的 wire_api=chat 必须被忽略，否则新版 Codex 会拒绝启动。
    invocation = definition.build_invocation(
        ctx(options={"transport": "exec", "wire_api": "chat"})
    )
    joined = " ".join(invocation.argv)
    assert "model_provider=coda" in joined
    assert 'model_providers.coda.base_url="https://gateway.example/v1"' in joined
    assert 'model_providers.coda.env_key="OPENAI_API_KEY"' in joined
    assert "wire_api" not in joined

    # 没走网关（官方默认 / 本机登录态）时不注入任何 provider，交给 Codex 自己的配置。
    bare = definition.build_invocation(ctx(options={"transport": "exec"}, gateway_url=None))
    assert "model_provider" not in " ".join(bare.argv)


def test_codex_disables_the_metrics_export_that_stalls_shutdown():
    """退出前的 OTEL 指标导出连不上分析后端会干等满超时（内网约 10 秒），必须关掉。

    未知的 ``-c`` 键在旧版 Codex 上会被忽略，所以这个开关对老版本是安全的。
    """
    definition = get_agent("codex")
    invocation = definition.build_invocation(ctx(options={"transport": "exec"}))
    joined = " ".join(invocation.argv)
    assert 'otel.metrics_exporter="none"' in joined
    # 走官方默认（本机登录态）时同样要关，否则一样会卡。
    assert 'otel.metrics_exporter="none"' in " ".join(
        definition.build_invocation(ctx(options={"transport": "exec"}, gateway_url=None)).argv
    )


def test_codex_invocation_uses_flags_the_cli_still_accepts():
    """`codex exec` 0.4x 起不再有 --full-auto；`exec resume` 也不认 -s/-C（只认配置覆盖）。"""
    definition = get_agent("codex")
    options = {"transport": "exec", "full_auto": True, "sandbox_mode": "danger-full-access"}

    fresh = definition.build_invocation(ctx(options=options))
    assert "--full-auto" not in fresh.argv
    assert 'approval_policy="never"' in fresh.argv
    assert fresh.argv[fresh.argv.index("-s") + 1] == "danger-full-access"

    resumed = definition.build_invocation(ctx(options=options, session_id="tid-1"))
    assert resumed.argv[:3] == ["codex", "exec", "resume"]
    assert "-s" not in resumed.argv and "-C" not in resumed.argv
    assert 'sandbox_mode="danger-full-access"' in resumed.argv
    assert 'approval_policy="never"' in resumed.argv


def test_codex_parses_modern_and_legacy_events():
    definition = get_agent("codex")
    run = definition.create_run(ctx(options={"transport": "exec"}))
    run.state["saw_delta"] = True
    lines = [
        {"type": "thread.started", "thread_id": "th1"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "full text"}},
        {"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": "ls -la"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "aggregated_output": "files", "exit_code": 0}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 6}},
        {"id": "0", "msg": {"type": "agent_message", "message": "legacy"}},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    types = [e.type.value for e in events]
    assert "session" in types and "tool.call" in types and "tool.result" in types and "usage" in types
    assert run.emitted_text == "legacy"  # 已有增量时抑制网关补发的整段文本
    assert run.session_id == "th1"


def test_antigravity_invocation_resumes_and_prepends_system_prompt():
    definition = get_agent("antigravity-cli")
    invocation = definition.build_invocation(ctx(session_id="sess-9"))
    assert "--conversation" in invocation.argv and "sess-9" in invocation.argv
    # 提示词走 stdin：-p "" 收尾，内容（含人格）从 stdin 喂进去。
    assert invocation.argv[-2:] == ["-p", ""]
    assert invocation.stdin.startswith("You are a tester.")
    assert invocation.files == {}
    # 账号型内核：平台不注入密钥 / 网关。
    assert "GEMINI_API_KEY" not in invocation.env


def test_antigravity_parses_gemini_and_claude_stream_shapes():
    """Antigravity 是 Gemini 谱系，但事件形状未公开：两种常见形状都要能解析。"""
    definition = get_agent("antigravity-cli")
    run = definition.create_run(ctx(prompt="hi"))

    init = list(run.feed('{"type":"init","session_id":"s-1","model":"m"}\n'))
    assert any(event.type == "session" for event in init)

    gemini_text = list(run.feed('{"type":"message","role":"assistant","content":"hello"}\n'))
    assert any(event.type == "message.delta" and event.text == "hello" for event in gemini_text)

    claude_text = list(
        run.feed('{"type":"assistant","message":{"content":[{"type":"text","text":"world"}]}}\n')
    )
    assert any(event.type == "message.delta" and event.text == "world" for event in claude_text)

    tool = list(
        run.feed(
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","id":"t1","name":"read","input":{"path":"a"}}]}}\n'
        )
    )
    assert any(event.type == "tool.call" and event.name == "read" for event in tool)


def test_custom_cli_declarative_args_and_plain_text():
    definition = get_agent("custom-cli")
    context = ctx(
        options={"command": "echo", "args": ["-n", "{model}:{prompt}"], "output_format": "text"},
        model="m1",
    )
    run: AgentRun = definition.create_run(context)
    invocation = run.build_invocation()
    assert invocation.argv == ["echo", "-n", "m1:hello world"]
    events = list(run.feed("first"))
    assert events[0].text.startswith("first")


def test_custom_cli_placeholder_survives_unknown_keys():
    definition = get_agent("custom-cli")
    context = ctx(options={"command": "tool", "args": ["{unknown}"]})
    invocation = definition.create_run(context).build_invocation()
    assert invocation.argv == ["tool", "{unknown}"]


def test_harness_builds_transcript_messages():
    definition = get_agent("openai-harness")
    context = ctx(history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
    messages = definition.build_messages(context)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert definition.chat_completions_url(context) == "https://gateway.example/v1/chat/completions"


def _write_claude_settings(tmp_path, settings: dict, local: dict | None = None) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    if local is not None:
        (claude_dir / "settings.local.json").write_text(json.dumps(local), encoding="utf-8")


def _isolate_anthropic_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_claude_code_reads_real_system_config(monkeypatch, tmp_path):
    _write_claude_settings(
        tmp_path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "http://10.0.0.1:3065",
                "ANTHROPIC_AUTH_TOKEN": "sk-local-secret",
                "ANTHROPIC_API_KEY": "",  # 空值应被忽略
            },
            "model": "my-model-v1",
        },
        local={"env": {"ANTHROPIC_MODEL": "local-model"}},
    )
    _isolate_anthropic_env(monkeypatch, tmp_path)

    defaults = get_agent("claude-code").system_defaults()
    assert defaults["gateway_url"] == "http://10.0.0.1:3065"
    assert defaults["model"] == "local-model"  # settings.local.json 覆盖 settings.json
    assert defaults["api_key"] == "sk-local-secret"


def test_unmodified_fields_fall_back_to_real_system_config(monkeypatch, tmp_path):
    _write_claude_settings(
        tmp_path,
        {"env": {"ANTHROPIC_BASE_URL": "http://gw.local:1", "ANTHROPIC_MODEL": "real-model"}},
    )
    _isolate_anthropic_env(monkeypatch, tmp_path)
    definition = get_agent("claude-code")

    # 等于内置默认值 / 为空 → 视为未自定义，回落到本机真实配置
    assert definition.resolve_gateway(definition.default_gateway) == "http://gw.local:1"
    assert definition.resolve_model(definition.default_model) == "real-model"
    assert definition.resolve_gateway(None) == "http://gw.local:1"
    # 显式覆盖的值保持不变
    assert definition.resolve_gateway("https://mine.example") == "https://mine.example"
    assert definition.resolve_model("mine-model") == "mine-model"


def test_adapters_without_real_config_keep_their_defaults(monkeypatch, tmp_path):
    _isolate_anthropic_env(monkeypatch, tmp_path)
    definition = get_agent("codex")
    assert definition.system_defaults() == {}
    assert definition.resolve_gateway(definition.default_gateway) == definition.default_gateway


def test_claude_code_root_bypass_sets_sandbox_env(monkeypatch):
    """以 root 勾 bypassPermissions 时必须声明 IS_SANDBOX，否则内核直接拒绝启动。"""
    from app.agents.builtin import claude_code

    definition = get_agent("claude-code")

    monkeypatch.setattr(claude_code, "_is_root", lambda: True)
    invocation = definition.build_invocation(ctx(options={"permission_mode": "bypassPermissions"}))
    assert "--dangerously-skip-permissions" in invocation.argv
    assert invocation.env["IS_SANDBOX"] == "1"
    # 运行日志里要有一次明示，否则用户看到内核拒绝启动会一头雾水
    run = definition.create_run(ctx(options={"permission_mode": "bypassPermissions"}))
    notes = [e for e in run.feed("") if e.type.value == "log" and "IS_SANDBOX" in (e.text or "")]
    assert len(notes) == 1

    # 常规权限模式不受影响
    plain = definition.build_invocation(ctx(options={"permission_mode": "acceptEdits"}))
    assert "IS_SANDBOX" not in plain.env

    # 非 root 也不用补
    monkeypatch.setattr(claude_code, "_is_root", lambda: False)
    assert "IS_SANDBOX" not in definition.build_invocation(
        ctx(options={"permission_mode": "bypassPermissions"})
    ).env

    # 用户自己配了 env.IS_SANDBOX 就以用户为准
    monkeypatch.setattr(claude_code, "_is_root", lambda: True)
    custom = definition.build_invocation(
        ctx(options={"permission_mode": "bypassPermissions", "env": {"IS_SANDBOX": "0"}})
    )
    assert custom.env["IS_SANDBOX"] == "0"


# --- Codex app-server（JSON-RPC）-----------------------------------------


def _app_server_run(**overrides):
    """建一个走 app-server 的 Codex 运行（默认选项里只覆盖 transport）。"""
    definition = get_agent("codex")
    options = {"transport": "app-server", "sandbox_mode": "workspace-write"}
    options.update(overrides.pop("options", {}))
    return definition, definition.create_run(ctx(options=options, **overrides))


def _request(run) -> dict:
    outbound = run.take_outbound()
    assert len(outbound) == 1, outbound
    return json.loads(outbound[0])


def test_codex_app_server_invocation_speaks_json_rpc_over_stdio():
    """app-server 模式：argv 是常驻服务，stdin 只放第一帧 initialize 且保持打开。"""
    definition = get_agent("codex")
    invocation = definition.build_invocation(ctx())
    assert invocation.argv[:3] == ["codex", "app-server", "--listen"]
    assert "stdio://" in invocation.argv
    assert invocation.stdin_hold is True
    assert "--json" not in invocation.argv
    frame = json.loads(invocation.stdin)
    assert frame["method"] == "initialize" and frame["id"] == 1
    assert frame["params"]["clientInfo"]["name"] == "coda"

    # 逐字流式来自 app-server；exec 仍是可选的老路径（写完 stdin 就关）。
    legacy = definition.build_invocation(ctx(options={"transport": "exec"}))
    assert legacy.argv[:2] == ["codex", "exec"]
    assert legacy.stdin_hold is False


def test_codex_app_server_handshake_then_streams_deltas():
    definition, run = _app_server_run()

    # initialize 的响应到齐后，适配器把 thread/start 排进队列（由运行层代写）。
    assert list(run.feed(json.dumps({"id": 1, "result": {"userAgent": "x"}}))) == []
    thread_request = _request(run)
    assert thread_request["method"] == "thread/start"
    assert thread_request["params"]["cwd"] == "/tmp/ws"
    assert thread_request["params"]["model"] == "some-model"
    assert thread_request["params"]["sandbox"] == "workspace-write"
    assert thread_request["params"]["approvalPolicy"] == "never"

    events = list(run.feed(json.dumps({"id": 2, "result": {"thread": {"id": "th-1"}}})))
    assert [e.type.value for e in events] == ["session"]
    assert run.session_id == "th-1"
    turn_request = _request(run)
    assert turn_request["method"] == "turn/start"
    assert turn_request["params"]["threadId"] == "th-1"
    assert turn_request["params"]["input"] == [{"type": "text", "text": "hello world"}]

    # 逐字增量：一个 delta 一个事件。
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": "m1", "delta": "你"},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["message.delta"]
    assert run.emitted_text == "你"

    # 已有增量的条目不再补发整段文本，避免内容翻倍。
    assert (
        list(
            run.feed(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {"item": {"type": "agentMessage", "id": "m1", "text": "你好"}},
                    }
                )
            )
        )
        == []
    )

    # 思考走 summaryTextDelta。
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": "r1", "delta": "想一下"},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["thinking.delta"]

    # token 用量是 camelCase，翻译成与 exec 一致的键名。
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 5,
                                "cachedInputTokens": 1,
                                "cacheWriteInputTokens": 0,
                                "outputTokens": 6,
                                "reasoningOutputTokens": 2,
                                "totalTokens": 11,
                            }
                        }
                    },
                }
            )
        )
    )
    # 用量（整轮累计）与上下文快照（单次请求）成对上报，两者语义不同。
    assert [e.type.value for e in events] == ["usage", "context.usage"]
    assert events[0].payload["input_tokens"] == 5
    assert events[0].payload["total_tokens"] == 11
    assert events[1].payload["tokens"] == 5

    # 本轮结束：适配器声明收工，运行层据此关 stdin。
    assert (
        list(
            run.feed(
                json.dumps(
                    {"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "completed"}}}
                )
            )
        )
        == []
    )
    assert run.done is True


def test_codex_app_server_falls_back_to_the_finished_message():
    """服务端没给增量（旧版 app-server）时，item/completed 要补上整段文本。"""
    _, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {"item": {"type": "agentMessage", "id": "m9", "text": "完整回复"}},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["message.delta"]
    assert run.emitted_text == "完整回复"


def test_codex_app_server_maps_tool_items():
    _, run = _app_server_run()
    started = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/started",
                    "params": {
                        "item": {"type": "commandExecution", "id": "c1", "command": "ls -la"}
                    },
                }
            )
        )
    )
    assert [e.type.value for e in started] == ["tool.call"]
    assert started[0].name == "shell"

    done = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "id": "c1",
                            "status": "completed",
                            "exitCode": 0,
                            "aggregatedOutput": "files",
                        }
                    },
                }
            )
        )
    )
    assert [e.type.value for e in done] == ["tool.result"]
    assert done[0].payload["output"] == "files"
    assert done[0].payload["is_error"] is False


def test_codex_app_server_resumes_the_snapshot_thread():
    definition, run = _app_server_run(session_id="th-old")
    list(run.feed(json.dumps({"id": 1, "result": {}})))
    request = _request(run)
    assert request["method"] == "thread/resume"
    assert request["params"]["threadId"] == "th-old"

    # 选「每轮重放完整对话记录」时不能恢复服务端线程，否则历史会重复。
    _, replayed = _app_server_run(
        session_id="th-old", options={"resume_style": "transcript"}
    )
    list(replayed.feed(json.dumps({"id": 1, "result": {}})))
    assert _request(replayed)["method"] == "thread/start"


def test_codex_app_server_declines_approvals_instead_of_hanging():
    """没有人工确认入口：审批一律自动拒绝，并把这件事告诉用户。"""
    _, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "id": 7,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "rm -rf /"},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["log"]
    assert json.loads(run.take_outbound()[0]) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"decision": "decline"},
    }


def test_codex_app_server_reports_failed_turn_and_bad_response():
    _, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {"id": "t1", "status": "failed", "error": {"message": "模型崩了"}}
                    },
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["error"]
    assert events[0].text == "模型崩了"
    assert run.done is True

    _, broken = _app_server_run()
    events = list(broken.feed(json.dumps({"id": 1, "error": {"message": "不认识这个客户端"}})))
    assert [e.type.value for e in events] == ["error"]
    assert broken.done is True


def test_codex_app_server_ignores_dirty_lines():
    """合法 JSON 但不是对象（数组 / 数字 / 字符串）也不能把整轮运行带崩。"""
    for line in ("[1, 2, 3]", "42", '"oops"'):
        _, run = _app_server_run()
        events = list(run.feed(line))
        assert [e.type.value for e in events] == ["log"], line


def test_hermes_agent_invocation_uses_stream_json_and_oneshot():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(system_prompt=None))
    assert invocation.argv[0] == "hermes"
    assert "chat" in invocation.argv
    assert "--oneshot" in invocation.argv
    assert "--format" in invocation.argv
    fmt_idx = invocation.argv.index("--format")
    assert invocation.argv[fmt_idx + 1] == "stream-json"
    assert "-q" in invocation.argv
    q_idx = invocation.argv.index("-q")
    assert invocation.argv[q_idx + 1] == "hello world"
    assert invocation.cwd == "/tmp/ws"


def test_hermes_agent_routes_the_gateway_through_cli_flags():
    """Hermes 不读 ``OPENROUTER_BASE_URL``：自定义端点只能走命令行，否则它会拿网关密钥
    去请求真 OpenRouter（HTTP 401，用户看到的就是「没有回复」）。"""
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx())
    joined = " ".join(invocation.argv)
    assert "--provider custom" in joined
    assert "--base-url https://gateway.example/v1" in joined
    assert "--api-key sk-secret" in joined
    # 端点不能再退回会被内核忽略的环境变量
    assert "OPENROUTER_BASE_URL" not in invocation.env
    assert definition.base_url_env is None

    # 没选自定义网关（官方链路）：平台不接管路由，交给内核自己的配置
    official = definition.build_invocation(ctx(gateway_url=None, api_key=None))
    assert "--provider" not in official.argv
    assert "--api-key" not in official.argv


def test_hermes_agent_user_provider_applies_without_gateway():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(
        ctx(gateway_url=None, api_key=None, options={"provider": "anthropic"})
    )
    joined = " ".join(invocation.argv)
    assert "--provider anthropic" in joined


def test_hermes_agent_session_resume():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(session_id="hermes-sess-1"))
    assert "--session" not in invocation.argv
    s_idx = invocation.argv.index("--resume")
    assert invocation.argv[s_idx + 1] == "hermes-sess-1"


def test_hermes_agent_yolo_is_declared():
    """配置里写着「yolo · 自动批准所有工具调用」，就必须真的传 --yolo：
    非交互子进程里没有审批提示可答，工具调用会一直卡住。"""
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(options={"approval_mode": "yolo"}))
    assert "--yolo" in invocation.argv


def test_hermes_agent_system_prompt_prepend():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx())
    q_idx = invocation.argv.index("-q")
    prompt = invocation.argv[q_idx + 1]
    assert prompt.startswith("You are a tester.")


def test_hermes_agent_system_prompt_soul_file(tmp_path):
    """人格走内核自己的身份文件 ``SOUL.md``（``config.yaml`` 没有 system_prompt 这个键，
    写进去等于静默丢弃人格）。"""
    definition = get_agent("hermes-agent")
    home = tmp_path / "hermes-home"
    invocation = definition.build_invocation(
        ctx(home_dir=str(home), options={"system_prompt_mode": "soul"})
    )
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "You are a tester."
    # 人格没进提示词，避免重复注入
    assert invocation.argv[invocation.argv.index("-q") + 1] == "hello world"

    # 存量配置里的旧值 "config" 按同一条路径处理（否则人格继续被静默丢掉）
    legacy = tmp_path / "legacy-home"
    definition.build_invocation(
        ctx(home_dir=str(legacy), options={"system_prompt_mode": "config"})
    )
    assert (legacy / "SOUL.md").is_file()


def test_hermes_agent_shares_runtime_with_the_isolated_home(tmp_path, monkeypatch):
    """隔离家目录要复用内核的真实运行时，否则每条新对话都要重装一遍依赖。"""
    real = tmp_path / "real-hermes"
    (real / "tools").mkdir(parents=True)
    (real / "installs").mkdir()
    (real / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(real))

    home = tmp_path / "isolated"
    home.mkdir()
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(home_dir=str(home)))

    assert invocation.env["HERMES_HOME"] == str(home)
    for name in ("tools", "installs"):
        assert (home / name).is_symlink(), name
        assert (home / name).resolve() == (real / name).resolve()
    # 用户数据不共享：技能目录仍留在隔离家目录里
    assert not (home / "skills").exists()


def test_hermes_agent_anchors_runtime_to_the_real_store(tmp_path, monkeypatch):
    """运行时要用真实路径：内核会拿它跟 sys.executable 比对，不一致就重写共享启动器。

    隔离家目录是临时的；一旦内核用 ``<隔离家>/tools/...`` 重写
    ``hermes-agent/.hermes/bin/hermes``，目录删掉后每一轮对话都会
    ``exec: ... not found``。
    """
    real = tmp_path / "real-hermes"
    entry = "python-3.14.9-linux-x64"
    python = real / "tools" / entry / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (real / "tools" / "facts.json").write_text(
        json.dumps({"packages": {"python": {"entry": entry}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(real))

    home = tmp_path / "isolated"
    home.mkdir()
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(home_dir=str(home)))

    assert invocation.env["HERMES_RUNTIME_DIR"] == str(real / "tools")


def test_hermes_agent_leaves_runtime_dir_unset_without_a_store(tmp_path, monkeypatch):
    """没有真实运行时就不要瞎指路径：让内核按自己的默认值走。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-hermes"))
    home = tmp_path / "isolated"
    home.mkdir()
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(home_dir=str(home)))
    assert "HERMES_RUNTIME_DIR" not in invocation.env


def test_hermes_agent_ignores_malformed_runtime_facts(tmp_path, monkeypatch):
    """``facts.json`` 形状不对也不能让一轮对话起不来（尽力而为）。"""
    real = tmp_path / "real-hermes"
    (real / "tools").mkdir(parents=True)
    (real / "tools" / "facts.json").write_text('{"packages": []}', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(real))

    home = tmp_path / "isolated"
    home.mkdir()
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(home_dir=str(home)))
    assert "HERMES_RUNTIME_DIR" not in invocation.env


def test_hermes_agent_safe_mode_flag():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(options={"approval_mode": "safe"}))
    assert "--safe-mode" in invocation.argv


def test_hermes_agent_home_dir_env():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(home_dir="/tmp/hermes-home"))
    assert invocation.env["HERMES_HOME"] == "/tmp/hermes-home"


def test_hermes_agent_stream_parsing():
    """真实样本：``hermes chat --oneshot --format stream-json`` 的输出。"""
    definition = get_agent("hermes-agent")
    run = definition.create_run(ctx())
    lines = [
        {"type": "system", "subtype": "init", "model": "deepseek-v4.1-flash", "session_id": "", "timestamp": 1},
        {"type": "tool_use", "name": "search_files", "tool_call_id": "call_00_x", "input": {"path": ".", "pattern": "*"}, "timestamp": 2},
        {"type": "tool_result", "name": "search_files", "tool_call_id": "call_00_x", "output": '{"total_count": 1}', "is_error": False, "timestamp": 3},
        {"type": "text", "text": "你好", "timestamp": 4},
        {"type": "text", "text": "，我是卡卡", "timestamp": 5},
        {
            "type": "result",
            "session_id": "local-abc",
            "exit_code": 0,
            "text": "你好，我是卡卡",
            "tokens": {"input": 25091, "output": 2, "total": 25093, "cache_read": 2048, "cache_write": 0},
            "duration_ms": 2023,
            "timestamp": 6,
        },
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    kinds = [e.type.value for e in events]
    # init 里的 session_id 是空的，真正的会话 id 在 result 里补发一次
    assert kinds.count("session") == 1
    assert run.session_id == "local-abc"
    assert run.emitted_text == "你好，我是卡卡"  # 不应与 result.text 重复
    calls = [e for e in events if e.type.value == "tool.call"]
    results = [e for e in events if e.type.value == "tool.result"]
    assert calls[0].call_id == results[0].call_id == "call_00_x"
    usage_events = [e for e in events if e.type.value == "usage"]
    # 用量要归一成平台口径，否则界面上的 token 统计恒为 0
    assert usage_events[0].payload["input_tokens"] == 25091
    assert usage_events[0].payload["output_tokens"] == 2
    assert usage_events[0].payload["cache_read_input_tokens"] == 2048


def test_hermes_agent_result_text_is_a_fallback():
    """内核只发一条汇总（没有 text 增量）时，正文不能被丢掉。"""
    definition = get_agent("hermes-agent")
    run = definition.create_run(ctx())
    payload = {
        "type": "result",
        "session_id": "local-fb",
        "exit_code": 0,
        "text": "只有汇总正文",
        "tokens": {"input": 1, "output": 2, "total": 3},
    }
    events = list(run.feed(json.dumps(payload)))
    assert run.emitted_text == "只有汇总正文"
    assert [e.type.value for e in events].count("message.delta") == 1


def test_hermes_agent_surfaces_nonzero_exit():
    definition = get_agent("hermes-agent")
    run = definition.create_run(ctx())
    payload = {
        "type": "result",
        "session_id": "local-err",
        "exit_code": 1,
        "text": "OpenRouter rejected your API key",
    }
    events = list(run.feed(json.dumps(payload)))
    kinds = [e.type.value for e in events]
    assert "error" in kinds
    assert run.state.get("is_error") is True


# --- 文件访问范围（file_access_scope）-----------------------------------------


def test_claude_code_workspace_only_injects_permission_blocks():
    """「仅工作目录」：物化 settings.json 时注入目录级读写封锁。"""
    definition = get_agent("claude-code")
    context = ctx(options={"file_access_scope": "workspace_only"})
    mount = definition.skill_mount(context)
    permissions = mount.patch["settings.json"]["permissions"]
    assert permissions["blockReadsOutsideWorkingDirectories"] is True
    assert permissions["blockFileToolReadsOutsideWorkingDirectories"] is True
    assert permissions["blockWritesOutsideWorkingDirectories"] is True
    assert any("仅工作目录" in note or "工作目录" in note for note in mount.notes)


def test_claude_code_unrestricted_keeps_settings_clean():
    """「不限制」是默认值：不注入任何目录封锁。"""
    definition = get_agent("claude-code")
    mount = definition.skill_mount(ctx())
    assert "permissions" not in mount.patch["settings.json"]
    assert not any("文件访问范围" in note for note in mount.notes)


def test_codex_workspace_only_overrides_sandbox_mode():
    """「仅工作目录」覆盖用户单独选的沙箱模式，强制 workspace-write。"""
    definition = get_agent("codex")
    options = {
        "transport": "exec",
        "file_access_scope": "workspace_only",
        "sandbox_mode": "danger-full-access",  # 用户选了危险档，也要被压回
    }
    fresh = definition.build_invocation(ctx(options=options))
    assert fresh.argv[fresh.argv.index("-s") + 1] == "workspace-write"

    resumed = definition.build_invocation(ctx(options=options, session_id="tid-1"))
    assert 'sandbox_mode="workspace-write"' in resumed.argv
    assert 'sandbox_mode="danger-full-access"' not in resumed.argv


def test_codex_unrestricted_respects_user_sandbox_choice():
    definition = get_agent("codex")
    options = {"transport": "exec", "sandbox_mode": "read-only"}
    fresh = definition.build_invocation(ctx(options=options))
    assert fresh.argv[fresh.argv.index("-s") + 1] == "read-only"


def test_antigravity_sandbox_flag_follows_option():
    """勾选沙箱才带 --sandbox；默认不限制文件访问。"""
    definition = get_agent("antigravity-cli")
    on = definition.build_invocation(ctx(options={"sandbox": True})).argv
    assert "--sandbox" in on
    off = definition.build_invocation(ctx()).argv
    assert "--sandbox" not in off


# --- CodeBuddy CLI --------------------------------------------------------


def _isolate_codebuddy_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "CODEBUDDY_CONFIG_DIR",
        "CODEBUDDY_MODEL",
        "CODEBUDDY_API_KEY",
        "CODEBUDDY_AUTH_TOKEN",
        "CODEBUDDY_INTERNET_ENVIRONMENT",
        "CODEBUDDY_IS_SANDBOX",
        # 这两个会改变 auth_status() 的结论：本机设了就提前清掉，测试才可复现
        "CODEBUDDY_CREDENTIALS_IN_MEMORY",
        "CODEBUDDY_LOGIN_METHOD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_codebuddy_invocation_uses_stream_json_and_bypass():
    definition = get_agent("codebuddy-cli")
    invocation = definition.build_invocation(ctx())
    assert invocation.argv[:5] == [
        "codebuddy",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert "--include-partial-messages" in invocation.argv
    mode_at = invocation.argv.index("--permission-mode")
    assert invocation.argv[mode_at + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in invocation.argv
    assert "--append-system-prompt" in invocation.argv
    assert invocation.argv[invocation.argv.index("--model") + 1] == "some-model"
    assert invocation.stdin == "hello world"
    # 网关密钥按适配器声明的变量名注入；网关地址绝不能塞进 CODEBUDDY_BASE_URL
    # （内核会把它拼成 <base>/v2/chat/completions，那是它自家协议）。
    assert invocation.env["CODEBUDDY_API_KEY"] == "sk-secret"
    assert "CODEBUDDY_BASE_URL" not in invocation.env
    assert invocation.env["DISABLE_TELEMETRY"] == "1"
    assert invocation.env["CI"] == "1"


def test_codebuddy_permission_mode_and_system_prompt_variants():
    definition = get_agent("codebuddy-cli")

    # 非 bypass 模式不带跳过审批的旗标
    plain = definition.build_invocation(ctx(options={"permission_mode": "acceptEdits"}))
    assert "--dangerously-skip-permissions" not in plain.argv
    assert plain.argv[plain.argv.index("--permission-mode") + 1] == "acceptEdits"

    replaced = definition.build_invocation(ctx(options={"system_prompt_mode": "replace"}))
    assert "--system-prompt" in replaced.argv
    assert "--append-system-prompt" not in replaced.argv


def test_codebuddy_resume_and_tool_lists():
    definition = get_agent("codebuddy-cli")
    invocation = definition.build_invocation(
        ctx(session_id="s-1", options={"allowed_tools": "Bash,Read"})
    )
    assert invocation.argv[invocation.argv.index("--resume") + 1] == "s-1"
    assert invocation.argv[invocation.argv.index("--allowedTools") + 1] == "Bash"
    assert "Read" in invocation.argv


def test_codebuddy_writes_models_json_for_custom_gateway(tmp_path):
    """自定义网关只能走 models.json：完整 /chat/completions 路径 + 环境变量引用密钥。"""
    definition = get_agent("codebuddy-cli")
    home = tmp_path / "home"
    definition.build_invocation(ctx(home_dir=str(home)))

    payload = json.loads((home / "models.json").read_text(encoding="utf-8"))
    model = payload["models"][0]
    assert model["id"] == "some-model"
    assert model["url"] == "https://gateway.example/v1/chat/completions"
    assert model["apiKey"] == "${CODEBUDDY_API_KEY}"
    assert payload["availableModels"] == ["some-model"]


def test_codebuddy_clears_stale_models_json_without_gateway(tmp_path):
    """切回官方链路时必须删掉上一次的 models.json，否则会继续打到旧网关上。"""
    definition = get_agent("codebuddy-cli")
    home = tmp_path / "home"
    home.mkdir()
    (home / "models.json").write_text("{}", encoding="utf-8")

    definition.build_invocation(ctx(gateway_url=None, api_key=None, home_dir=str(home)))
    assert not (home / "models.json").exists()


def test_codebuddy_isolates_config_dir_and_enforces_strict_mcp(tmp_path):
    definition = get_agent("codebuddy-cli")
    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    invocation = definition.build_invocation(ctx(home_dir=str(home)))
    assert invocation.env["CODEBUDDY_CONFIG_DIR"] == str(home)
    assert "--strict-mcp-config" in invocation.argv
    assert invocation.argv[invocation.argv.index("--mcp-config") + 1] == str(home / "mcp.json")


def test_codebuddy_root_bypass_sets_sandbox_env(monkeypatch):
    """root + bypassPermissions 要声明 CODEBUDDY_IS_SANDBOX，否则危险命令会再弹确认。"""
    from app.agents.builtin import codebuddy_cli

    definition = get_agent("codebuddy-cli")
    options = {"permission_mode": "bypassPermissions"}

    monkeypatch.setattr(codebuddy_cli, "_is_root", lambda: True)
    invocation = definition.build_invocation(ctx(options=options))
    assert invocation.env["CODEBUDDY_IS_SANDBOX"] == "1"

    run = definition.create_run(ctx(options=options))
    notes = [
        e
        for e in run.feed("")
        if e.type.value == "log" and "CODEBUDDY_IS_SANDBOX" in (e.text or "")
    ]
    assert len(notes) == 1

    plain = definition.build_invocation(ctx(options={"permission_mode": "acceptEdits"}))
    assert "CODEBUDDY_IS_SANDBOX" not in plain.env

    monkeypatch.setattr(codebuddy_cli, "_is_root", lambda: False)
    assert "CODEBUDDY_IS_SANDBOX" not in definition.build_invocation(ctx(options=options)).env

    # 用户自己显式配了就听用户的
    monkeypatch.setattr(codebuddy_cli, "_is_root", lambda: True)
    custom = definition.build_invocation(ctx(options={**options, "env": {"CODEBUDDY_IS_SANDBOX": "0"}}))
    assert custom.env["CODEBUDDY_IS_SANDBOX"] == "0"


def test_codebuddy_stream_parsing_matches_claude_shape():
    definition = get_agent("codebuddy-cli")
    run = definition.create_run(ctx())
    lines = [
        {"type": "system", "subtype": "init", "session_id": "cb1", "model": "default-model", "tools": ["Bash"], "mcp_servers": []},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "想一下"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]}},
        {"type": "system", "subtype": "usage_update", "usage": {"input_tokens": 1}},
        {"type": "result", "subtype": "success", "session_id": "cb1", "usage": {"input_tokens": 3, "output_tokens": 4}, "total_cost_usd": 0},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    kinds = [e.type.value for e in events]
    assert kinds.count("session") == 1
    assert run.emitted_text == "Hello"  # 已有逐字增量时不重复输出整段
    assert run.thinking == "想一下"
    assert "tool.call" in kinds and "tool.result" in kinds and "usage" in kinds
    assert run.session_id == "cb1"
    # 内核特有的 usage_update 不产生错误事件
    assert "error" not in kinds


def test_codebuddy_skill_roots_discovery_and_mount(monkeypatch, tmp_path):
    _isolate_codebuddy_env(monkeypatch, tmp_path)
    cfg = tmp_path / ".codebuddy"
    (cfg / "skills" / "demo").mkdir(parents=True)
    (cfg / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示技能\n---\n", encoding="utf-8"
    )
    (cfg / "skills" / ".skillhub" / "bought").mkdir(parents=True)
    (cfg / "skills" / ".skillhub" / "bought" / "SKILL.md").write_text(
        "---\nname: bought\ndescription: 市场技能\n---\n", encoding="utf-8"
    )
    (cfg / ".credentials.json").write_text("{}", encoding="utf-8")

    definition = get_agent("codebuddy-cli")
    specs = definition.discover_skills()
    by_slug = {spec.slug: spec for spec in specs}
    assert {"demo", "bought"} <= set(by_slug)
    assert by_slug["bought"].source_kind == "market"

    mount = definition.skill_mount(ctx())
    assert mount.home_name == "codebuddy-home"
    carried = {dest for _src, dest in mount.carry}
    # 隔离家目录必须带上登录态，否则官方链路会变成「未登录」
    assert {".credentials.json", "settings.json", "settings.local.json"} <= carried

    assert definition.skill_invocation("Skill", {"skill": "demo"}) == "demo"
    assert definition.skill_invocation("Bash", {"command": "ls"}) is None


def test_codebuddy_mcp_discovery_and_mount(monkeypatch, tmp_path):
    _isolate_codebuddy_env(monkeypatch, tmp_path)
    cfg = tmp_path / ".codebuddy"
    cfg.mkdir(parents=True)
    (cfg / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "server-fs"]}}}
        ),
        encoding="utf-8",
    )

    definition = get_agent("codebuddy-cli")
    assert [spec.slug for spec in definition.discover_mcp_servers()] == ["fs"]

    # 工作目录自带 .mcp.json 时要提示它不会生效（本次运行只认平台分配的那份）
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text("{}", encoding="utf-8")
    mount = definition.mcp_mount(ctx(workspace_dir=str(workspace)))
    assert mount.home_name == "codebuddy-home"
    assert "mcpServers" in mount.files["mcp.json"]
    assert any(".mcp.json" in note for note in mount.notes)


def test_codebuddy_system_defaults_reads_local_settings(monkeypatch, tmp_path):
    _isolate_codebuddy_env(monkeypatch, tmp_path)
    cfg = tmp_path / ".codebuddy"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(
        json.dumps({"model": "hy3", "env": {"CODEBUDDY_BASE_URL": "http://ignored.example"}}),
        encoding="utf-8",
    )

    defaults = get_agent("codebuddy-cli").system_defaults()
    assert defaults["model"] == "hy3"
    # 本机 CODEBUDDY_BASE_URL 不能当成平台网关地址回填
    assert "gateway_url" not in defaults


def test_codebuddy_network_environment_only_applies_to_official_path(monkeypatch, tmp_path):
    """官方链路要按站点注入 CODEBUDDY_INTERNET_ENVIRONMENT；自定义网关与它无关。"""
    _isolate_codebuddy_env(monkeypatch, tmp_path)
    definition = get_agent("codebuddy-cli")

    # 默认不注入：跟随内核自己的站点选择（国际站）
    assert "CODEBUDDY_INTERNET_ENVIRONMENT" not in definition.build_invocation(ctx()).env

    official = definition.build_invocation(
        ctx(gateway_url=None, api_key=None, options={"network_environment": "internal"})
    )
    assert official.env["CODEBUDDY_INTERNET_ENVIRONMENT"] == "internal"

    # 绑了自定义网关时请求直接打第三方，站点选择没有意义
    custom = definition.build_invocation(ctx(options={"network_environment": "internal"}))
    assert "CODEBUDDY_INTERNET_ENVIRONMENT" not in custom.env

    # 用户自己配的同名环境变量优先
    manual = definition.build_invocation(
        ctx(
            gateway_url=None,
            api_key=None,
            options={
                "network_environment": "internal",
                "env": {"CODEBUDDY_INTERNET_ENVIRONMENT": "ioa"},
            },
        )
    )
    assert manual.env["CODEBUDDY_INTERNET_ENVIRONMENT"] == "ioa"


def test_codebuddy_auth_status_reports_credential_sources(monkeypatch, tmp_path):
    """登录态只做「配置层」判断：环境变量凭据 → 未知；本机凭据缓存 → 已登录。"""
    _isolate_codebuddy_env(monkeypatch, tmp_path)
    definition = get_agent("codebuddy-cli")
    cfg = tmp_path / ".codebuddy"

    status = definition.auth_status()
    assert status.required is True
    assert status.logged_in is False
    assert "登录" in (status.detail or "")

    # MCP / 插件的 OAuth 缓存不是账号登录态（它是另一份存储）
    cfg.mkdir(parents=True)
    (cfg / ".credentials.json").write_text(
        json.dumps({"mcp-server": {"accessToken": "x"}}), encoding="utf-8"
    )
    assert definition.auth_status().logged_in is False

    # 真正的账号 session 落在 Data/Public/auth/*.info（跟随真实 HOME）
    from app.agents.builtin.codebuddy_cli import auth_store_dir

    store = auth_store_dir()
    store.mkdir(parents=True)
    (store / "abc.info").write_text("{}", encoding="utf-8")
    assert definition.auth_status().logged_in is True

    # 空文件只是占位，不能当成登录态
    (store / "abc.info").write_text("", encoding="utf-8")
    assert definition.auth_status().logged_in is False

    # 环境变量凭据：平台不联网校验，结论是「未知」但要说清来源
    monkeypatch.setenv("CODEBUDDY_API_KEY", "sk-official")
    status = definition.auth_status()
    assert status.logged_in is None
    assert "CODEBUDDY_API_KEY" in (status.detail or "")

    # 写进 settings.json 的 env 同样要认
    monkeypatch.delenv("CODEBUDDY_API_KEY")
    (cfg / "settings.json").write_text(
        json.dumps({"env": {"CODEBUDDY_AUTH_TOKEN": "oauth-token"}}), encoding="utf-8"
    )
    status = definition.auth_status()
    assert status.logged_in is None
    assert "CODEBUDDY_AUTH_TOKEN" in (status.detail or "")

    # apiKeyHelper（企业 OAuth 自动刷新）也算「已配置」
    (cfg / "settings.json").write_text(
        json.dumps({"apiKeyHelper": "/opt/bin/get-token.sh"}), encoding="utf-8"
    )
    status = definition.auth_status()
    assert status.logged_in is None
    assert "apiKeyHelper" in (status.detail or "")


def test_codebuddy_declares_visual_login_helper():
    """内核没有 `login` 子命令：登录按钮由 ``--serve`` API 助手实现，输出二维码。"""
    from pathlib import Path

    definition = get_agent("codebuddy-cli")
    assert definition.supports_auth() is True
    assert definition.provisioner is not None
    assert definition.provisioner.supports("login") is True
    argv = definition.provisioner.argv_for("login")
    assert len(argv) == 2
    assert Path(argv[1]).is_file()
    assert Path(argv[1]).name == "codebuddy_login.py"


# —— 各内核的上下文快照 / 原生压缩：逐内核对齐，不靠 claude_code 一个兜底 ——


def test_codebuddy_parses_compact_boundary():
    """CodeBuddy 的 ``-p`` 会话在 /compact 后发 system/compact_boundary，与 Claude Code 同协议。"""
    definition = get_agent("codebuddy-cli")
    run = definition.create_run(ctx())
    line = json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "session_id": "cb-1",
            "compact_metadata": {
                "trigger": "manual",
                "pre_tokens": 180_000,
                "post_tokens": 12_000,
                "messages_summarized": 25,
                "internal_telemetry": "drop-me",
            },
        }
    )
    events = list(run.feed(line))
    kinds = [e.type.value for e in events]
    assert "context.compacted" in kinds
    compacted = next(e for e in events if e.type.value == "context.compacted")
    assert compacted.payload["trigger"] == "manual"
    assert compacted.payload["pre_tokens"] == 180_000
    assert compacted.payload["post_tokens"] == 12_000
    assert "internal_telemetry" not in compacted.payload, "白名单以外的字段不得透传"


def test_codebuddy_autocompact_flag_passthrough():
    """配置了 autocompact 才透传 --autocompact；未配置保持命令行不变。"""
    definition = get_agent("codebuddy-cli")
    argv = definition.build_invocation(ctx(options={"autocompact": "auto"})).argv
    assert argv[argv.index("--autocompact") + 1] == "auto"

    argv = definition.build_invocation(ctx(options={"autocompact": "400k"})).argv
    assert argv[argv.index("--autocompact") + 1] == "400k"

    plain = definition.build_invocation(ctx()).argv
    assert "--autocompact" not in plain, "未配置时不得注入，否则会覆盖内核默认"


def test_codex_thread_compacted_notification():
    """老版本 app-server 的 thread/compacted（Deprecated）也要接住。"""
    definition, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {"method": "thread/compacted", "params": {"threadId": "t1", "turnId": "turn1"}}
            )
        )
    )
    assert [e.type.value for e in events] == ["context.compacted"]


def test_codex_context_compaction_item_maps_to_compacted_event():
    """新协议：item/completed 的 contextCompaction 条目 = 内核压缩完成。"""
    definition, run = _app_server_run()
    # started 时压缩还没落地：不上报，避免过早失效快照。
    started = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/started",
                    "params": {"item": {"id": "c1", "type": "contextCompaction"}},
                }
            )
        )
    )
    assert started == []

    completed = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {"item": {"id": "c1", "type": "contextCompaction"}},
                }
            )
        )
    )
    assert [e.type.value for e in completed] == ["context.compacted"]


def test_claude_code_reports_kernel_window_from_autocompact_state():
    """内核自报的上下文窗口（顶层 autocompact_state 帧）要单独上报。"""
    definition = get_agent("claude-code")
    run = definition.create_run(ctx())
    line = json.dumps(
        {
            "type": "autocompact_state",
            "value": {
                "enabled": True,
                "effective_window": 200_000,
                "threshold": 160_000,
                "enforced": False,
                "source": "settings",
            },
            "uuid": "u1",
            "session_id": "s1",
        }
    )
    events = list(run.feed(line))
    # 帧里带 session_id：会话事件先来，再是窗口事件。
    assert [e.type.value for e in events] == ["session", "context.window"]
    assert events[1].payload["tokens"] == 200_000

    # 字段缺失或非法时不发事件（不能让坏数据污染上限）。
    for bad in ({}, {"value": {}}, {"value": {"effective_window": 0}}, {"value": "x"}):
        run2 = definition.create_run(ctx())
        assert list(run2.feed(json.dumps({"type": "autocompact_state", **bad}))) == []


def test_codex_reports_kernel_window_from_model_context_window():
    """Codex 的 tokenUsage 带 modelContextWindow = 模型真正的上下文窗口。"""
    definition, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 5,
                                "cachedInputTokens": 0,
                                "cacheWriteInputTokens": 0,
                                "outputTokens": 6,
                                "reasoningOutputTokens": 0,
                                "totalTokens": 11,
                            },
                            "modelContextWindow": 272_000,
                        }
                    },
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["usage", "context.usage", "context.window"]
    assert events[2].payload["tokens"] == 272_000


def test_claude_code_reports_context_usage_from_message_delta():
    """窗口占用真正的来源：message_delta.usage（2.1.327 起 message_start 常为全 0）。

    实测（真实内核 + 工具循环，两次请求）：
      message_start  input=0    cache_read=0
      message_delta  input=75   cache_read=14848   ← 本次请求的窗口占用
      message_start  input=0    cache_read=0
      message_delta  input=232  cache_read=14848   ← 第二次请求（多了工具结果）
      result         input=307  cache_read=29696   ← 整轮累计，不能当窗口占用
    """
    definition = get_agent("claude-code")
    run = definition.create_run(ctx())
    lines = [
        # message_start 的 usage 全 0：取不到快照（不能报错、也不能当成 0 上报）
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "message_delta",
                "usage": {
                    "input_tokens": 232,
                    "output_tokens": 18,
                    "cache_read_input_tokens": 14848,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    assert [e.type.value for e in events] == ["context.usage"]
    # 输入侧求和：232 + 14848（缓存命中也是窗口的一部分）
    assert events[0].payload["tokens"] == 232 + 14848


def test_claude_code_message_delta_without_input_tokens_is_not_reported():
    """老内核的 message_delta 只有 output_tokens：不能当成窗口占用上报。"""
    definition = get_agent("claude-code")
    run = definition.create_run(ctx())
    events = list(
        run.feed(
            json.dumps(
                {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 9}}}
            )
        )
    )
    assert events == []


def test_codebuddy_auth_required_error_is_translated_to_chinese():
    """内核的「未登录」英文报错要翻成中文，并指明去哪儿登录。

    Windows 上内核找不到本机登录态时抛的是这句英文；平台不翻译的话，
    用户只看到一句看不懂的话，也不知道该去「内核管理」登录。
    """
    definition = get_agent("codebuddy-cli")
    run = definition.create_run(ctx())
    raw = "Authentication required. Please use /login command to sign in to your account"
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": raw}]}},
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": [raw]},
    ]

    events = [e for line in lines for e in run.feed(json.dumps(line))]

    assert raw not in run.emitted_text  # 英文原文不再直接透给用户
    errors = [e for e in events if e.type.value == "error"]
    assert errors, "未登录必须产出错误事件"
    assert "内核管理" in errors[0].text
    assert raw not in errors[0].text


def test_codebuddy_other_errors_keep_kernel_text():
    """其他错误原样透出：内核自己的报错信息通常比平台翻译更准。"""
    definition = get_agent("codebuddy-cli")
    run = definition.create_run(ctx())
    raw = "workspace is not writable"

    events = list(run.feed(json.dumps(
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": [raw]}
    )))

    errors = [e for e in events if e.type.value == "error"]
    assert [e.text for e in errors] == [raw]
