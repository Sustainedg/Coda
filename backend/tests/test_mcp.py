"""MCP 服务器：解析、统一编目、智能体分配与运行时物化。

MCP 的相关约定见 AGENTS.md §3（契约）与 §4（扩展手册）。
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

import app.runtime.mcp as mcp_service
from app.agents import get_agent
from app.agents.base import AgentContext, AgentDefinition, Capabilities, Runtime
from app.agents.mcp import (
    McpMount,
    McpServerSpec,
    parse_json_servers,
    parse_toml_servers,
    render_toml_servers,
    safe_slug,
    strip_toml_table,
    to_json_config,
    to_json_servers,
)
from app.db import SessionLocal
from app.models import AgentMcpServer, AgentProfile, McpServer
from app.settings import get_settings
from helpers import make_gateway, make_profile


def context(**overrides) -> AgentContext:
    data: dict = {"agent_id": "a1", "agent_name": "测试智能体", "prompt": "hi"}
    data.update(overrides)
    return AgentContext(**data)


def spec(name: str, **overrides) -> McpServerSpec:
    data: dict = {"name": name, "slug": safe_slug(name) or name, "command": "npx"}
    data.update(overrides)
    return McpServerSpec(**data)


# --- 解析与格式化 ------------------------------------------------------------


def test_safe_slug_rejects_empty_and_sanitises():
    assert safe_slug("context7") == "context7"
    assert safe_slug("My Server") == "My-Server"
    assert safe_slug("   ") is None
    assert safe_slug("../逃逸") == "逃逸"  # 斜杠会被替换掉，标识本身没有路径含义
    assert safe_slug("通用工具") == "通用工具"


def test_parse_json_servers_reads_stdio_and_http():
    raw = {
        "mcpServers": {
            "files": {"command": "npx", "args": ["-y", "server"], "env": {"TOKEN": "x"}},
            "remote": {"type": "http", "url": "https://mcp.example/sse", "headers": {"A": "b"}},
        },
        "其它键": 1,
    }
    found = {item.slug: item for item in parse_json_servers(raw, type_id="claude-code")}
    assert found["files"].transport == "stdio"
    assert found["files"].env == {"TOKEN": "x"}
    assert found["files"].source_id == "claude-code:files"
    assert found["remote"].transport == "http"
    assert found["remote"].url == "https://mcp.example/sse"
    assert found["remote"].headers == {"A": "b"}

    assert parse_json_servers({"mcpServers": "坏数据"}, type_id="claude-code") == []
    assert parse_json_servers(None, type_id="claude-code") == []


def test_parse_toml_servers_and_render_round_trip():
    text = '''
model = "gpt-5-codex"

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
[mcp_servers.context7.env]
TOKEN = "secret"
'''
    found = parse_toml_servers(text, type_id="codex")
    assert [item.slug for item in found] == ["context7"]
    assert found[0].args == ["-y", "@upstash/context7-mcp"]
    assert found[0].env == {"TOKEN": "secret"}

    # 损坏的 TOML 只当没有配置，不允许抛错。
    assert parse_toml_servers("这不是 TOML = [", type_id="codex") == []

    rendered = render_toml_servers([spec("remote", transport="http", command=None, url="https://x/y")])
    parsed = tomllib.loads(f"{text}\n{rendered}")
    assert parsed["mcp_servers"]["remote"]["url"] == "https://x/y"


def test_strip_toml_table_removes_host_mcp_config_only():
    text = '''model = "gpt-5-codex"

[mcp_servers.context7]
command = "npx"

[projects."/tmp/ws"]
trust_level = "trusted"

[mcp_servers.context7.env]
TOKEN = "secret"
'''
    stripped = strip_toml_table(text)
    assert "mcp_servers" not in stripped
    assert "context7" not in stripped
    assert "secret" not in stripped
    assert 'model = "gpt-5-codex"' in stripped
    assert '[projects."/tmp/ws"]' in stripped

    # 顶层 dotted key 形式也要剥掉。
    dotted = 'mcp_servers.foo.command = "npx"\nmodel = "x"\n'
    assert strip_toml_table(dotted).strip() == 'model = "x"'


# --- 适配器声明 --------------------------------------------------------------


def test_claude_mcp_mount_is_strict_and_scans_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"host": {"command": "npx", "args": ["host"]}}}),
        encoding="utf-8",
    )
    definition = get_agent("claude-code")

    found = {item.slug: item for item in definition.discover_mcp_servers()}
    assert found["host"].command == "npx"
    assert found["host"].type_id == "claude-code"

    home = tmp_path / "home"
    ctx = context(home_dir=str(home), mcp_servers=[spec("files")])
    mount = definition.mcp_mount(ctx)
    assert mount.home_name == "claude-home"
    payload = json.loads(mount.files["mcp.json"])
    assert list(payload["mcpServers"]) == ["files"]

    invocation = definition.build_invocation(ctx)
    assert invocation.env["CLAUDE_CONFIG_DIR"] == str(home)
    # 隔离目录里还没有写文件时不能声明 --mcp-config（内核会因为文件不存在直接报错）。
    assert "--mcp-config" not in invocation.argv

    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(mount.files["mcp.json"], encoding="utf-8")
    invocation = definition.build_invocation(ctx)
    assert "--strict-mcp-config" in invocation.argv
    index = invocation.argv.index("--mcp-config")
    assert invocation.argv[index + 1] == str(home / "mcp.json")


def test_codex_mcp_mount_replaces_host_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex"
    root.mkdir()
    (root / "config.toml").write_text(
        '[projects."/tmp/ws"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers.host]\ncommand = "npx"\n',
        encoding="utf-8",
    )
    definition = get_agent("codex")

    found = {item.slug: item for item in definition.discover_mcp_servers()}
    assert set(found) == {"host"}

    ctx = context(home_dir=str(tmp_path / "home"), mcp_servers=[spec("assigned")])
    mount = definition.mcp_mount(ctx)
    content = mount.files["config.toml"]
    assert "host" not in content
    assert '[projects."/tmp/ws"]' in content
    parsed = tomllib.loads(content)
    assert list(parsed["mcp_servers"]) == ["assigned"]
    assert parsed["projects"]["/tmp/ws"]["trust_level"] == "trusted"


class _WorkspaceOnlyMcpRuntime(AgentDefinition):
    """只把 MCP 配置写进工作目录的假内核。

    Gemini CLI 已由 Antigravity CLI 取代（后者本地 MCP 配置面尚未确认），
    这里用假内核单独覆盖框架的 ``workspace_files`` 物化路径，避免把测试绑在
    某个真实内核的私有力配置格式上。
    """

    id = "fake-workspace-mcp"
    name = "假工作目录 MCP 内核"
    description = "测试用"
    runtime = Runtime.CLI
    executable = None
    provisioner = None
    capabilities = Capabilities(mcp=True)

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        try:
            raw = json.loads((Path.home() / ".fake" / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return parse_json_servers(raw, type_id=self.id, source_kind="user")

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        return McpMount(
            home_name="fake-home",
            workspace_files={
                ".gemini/settings.json": json.dumps(
                    {"mcpServers": to_json_servers(ctx.mcp_servers)}, ensure_ascii=False
                )
            },
        )

    def build_invocation(self, ctx: AgentContext):  # pragma: no cover - 契约占位
        raise NotImplementedError

    def parse_line(self, line: str, run) -> tuple:  # pragma: no cover - 契约占位
        return ()


def test_workspace_mcp_mount_writes_project_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".fake").mkdir()
    (tmp_path / ".fake" / "settings.json").write_text(
        json.dumps({"mcpServers": {"host": {"command": "npx"}}}), encoding="utf-8"
    )
    definition = _WorkspaceOnlyMcpRuntime()
    assert [item.slug for item in definition.discover_mcp_servers()] == ["host"]

    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = context(
        home_dir=str(tmp_path / "home"),
        workspace_dir=str(workspace),
        mcp_servers=[spec("assigned")],
    )
    mount = definition.mcp_mount(ctx)
    payload = json.loads(mount.workspace_files[".gemini/settings.json"])
    assert list(payload["mcpServers"]) == ["assigned"]


async def test_workspace_only_mount_does_not_create_an_isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """只把配置写进工作目录的内核：不该凭空建「家」，更不该因此把会话当新的一轮。"""
    definition = _WorkspaceOnlyMcpRuntime()

    async def fake_servers(_session, _profile, **_kwargs):
        return [spec("assigned")]

    monkeypatch.setattr(mcp_service, "mcp_servers_for_agent", fake_servers)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = context(workspace_dir=str(workspace), session_id="会话-1")

    warnings = await mcp_service.prepare_mcp_home(None, definition, None, None, ctx)

    assert warnings == []
    assert ctx.home_dir is None, "只写工作目录的物化不该给适配器塞一个空的隔离目录"
    assert ctx.session_id == "会话-1", "没有隔离目录就谈不上「新家」，不能把会话当新的一轮"
    payload = json.loads((workspace / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert list(payload["mcpServers"]) == ["assigned"]


# --- 接口与端到端 ------------------------------------------------------------


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把编目结果换成两个「扫描出来的」服务器，避免依赖开发机上的真实配置。"""
    specs = [
        McpServerSpec(
            name="github",
            slug="github",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "从内核配置读到的值"},
            type_id="codex",
            source_kind="user",
        ),
        McpServerSpec(
            name="filesystem",
            slug="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/data"],
            type_id="claude-code",
            source_kind="user",
        ),
    ]

    def fake() -> dict[str, McpServerSpec]:
        return {item.source_id or item.slug: item for item in specs}

    monkeypatch.setattr(mcp_service, "discover_catalog", fake)
    return specs


async def make_agent(client: httpx.AsyncClient, type_id: str = "custom-cli") -> dict:
    gateway = await make_gateway(client)
    return await make_profile(
        client,
        name=f"{type_id} 智能体",
        type_id=type_id,
        gateway_id=gateway["id"],
        model=None,
        options={"command": "echo"} if type_id == "custom-cli" else {},
    )


async def test_mcp_api_create_list_update_and_guards(client: httpx.AsyncClient):
    # 启动播种的内置服务器会自动出现在目录里；这里只清掉它们再验证「新建」链路。
    async with SessionLocal() as session:
        for row in (await session.execute(select(McpServer))).scalars().all():
            await session.delete(row)
        await session.commit()
    assert (await client.get("/api/mcp-servers")).json()["items"] == []

    created = await client.post(
        "/api/mcp-servers",
        json={
            "name": "filesystem",
            "description": "本地文件系统",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
            "env": {"FS_TOKEN": "super-secret-token"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["transport"] == "stdio"
    assert body["type_id"] is None
    assert body["source_label"] == "平台新建"
    assert body["editable"] is True
    assert body["env_keys"] == ["FS_TOKEN"]
    assert body["env_preview"]["FS_TOKEN"] != "super-secret-token"
    assert "super-secret-token" not in created.text

    # 库里存的必须是密文。
    async with SessionLocal() as session:
        row = await session.get(McpServer, body["id"])
        assert row is not None and row.env_encrypted["FS_TOKEN"] != "super-secret-token"

    # 重名标识会被挡住（重名写进内核配置会互相覆盖）。
    duplicate = await client.post(
        "/api/mcp-servers", json={"name": "filesystem", "command": "npx"}
    )
    assert duplicate.status_code == 409

    # 校验：传输方式、stdio 缺命令、http 缺地址。
    bad_transport = await client.post(
        "/api/mcp-servers", json={"name": "x", "transport": "websocket", "command": "npx"}
    )
    assert bad_transport.status_code == 422
    missing_command = await client.post("/api/mcp-servers", json={"name": "y", "transport": "stdio"})
    assert missing_command.status_code == 422
    missing_url = await client.post("/api/mcp-servers", json={"name": "z", "transport": "http"})
    assert missing_url.status_code == 422

    updated = await client.patch(
        f"/api/mcp-servers/{body['id']}", json={"name": "文件系统", "enabled": False}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "文件系统"
    assert updated.json()["enabled"] is False
    # 值留空＝不换：掩码没被当成新值写回去。
    assert updated.json()["env_keys"] == ["FS_TOKEN"]

    assert (await client.delete(f"/api/mcp-servers/{body['id']}")).status_code == 204
    assert (await client.get(f"/api/mcp-servers/{body['id']}")).status_code == 404


async def test_mcp_rescan_and_plain_servers_are_read_only(
    client: httpx.AsyncClient, catalog: list[McpServerSpec]
):
    result = (await client.post("/api/mcp-servers/rescan")).json()
    assert result == {"added": 2, "updated": 0, "missing": 0, "total": 2}

    items = (await client.get("/api/mcp-servers")).json()["items"]
    assert [item["slug"] for item in items] == ["filesystem", "github"]
    assert next(item for item in items if item["slug"] == "github")["editable"] is False
    assert next(item for item in items if item["slug"] == "github")["source_label"] == "内核配置"

    detail = (await client.get("/api/mcp-servers/codex:github")).json()
    # 扫描来的密钥同样是密文，接口只回掩码。
    assert detail["env_keys"] == ["GITHUB_TOKEN"]
    assert detail["env_preview"]["GITHUB_TOKEN"] != "从内核配置读到的值"

    # 扫描来的服务器不可在平台上改 / 删。
    assert (await client.patch("/api/mcp-servers/codex:github", json={"name": "x"})).status_code == 403
    assert (await client.delete("/api/mcp-servers/codex:github")).status_code == 403

    # 配置里消失后只标记失效，不删记录。
    catalog.remove(next(item for item in catalog if item.slug == "github"))
    assert (await client.post("/api/mcp-servers/rescan")).json()["missing"] == 1
    items = (await client.get("/api/mcp-servers")).json()["items"]
    assert next(item for item in items if item["slug"] == "github")["available"] is False


async def test_agent_mcp_assignment_rules(client: httpx.AsyncClient, catalog: list[McpServerSpec]):
    await client.post("/api/mcp-servers/rescan")
    platform = (
        await client.post(
            "/api/mcp-servers",
            json={"name": "通用工具", "transport": "stdio", "command": "npx", "args": ["-y", "x"]},
        )
    ).json()

    codex_agent = await make_agent(client, "codex")
    claude_agent = await make_agent(client, "claude-code")
    assert codex_agent["mcp_ids"] == []

    response = await client.put(
        f"/api/agents/{codex_agent['id']}/mcp-servers",
        json={"mcp_ids": ["codex:github", platform["id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["mcp_ids"] == ["codex:github", platform["id"]]
    assert {brief["slug"] for brief in response.json()["mcp_servers"]} == {"github", "通用工具"}

    # 平台通用的服务器可以分给任意内核；扫描来的只能给同内核。
    assert (
        await client.put(
            f"/api/agents/{claude_agent['id']}/mcp-servers", json={"mcp_ids": [platform["id"]]}
        )
    ).status_code == 200
    mismatch = await client.put(
        f"/api/agents/{claude_agent['id']}/mcp-servers", json={"mcp_ids": ["codex:github"]}
    )
    assert mismatch.status_code == 422
    assert "不匹配" in mismatch.json()["detail"]

    # 同一次分配里两个重名标识会互相覆盖，直接拒绝。
    clash = await client.post(
        "/api/mcp-servers", json={"name": "另一个", "slug": "github", "command": "npx"}
    )
    assert clash.status_code == 201
    clash = await client.put(
        f"/api/agents/{codex_agent['id']}/mcp-servers",
        json={"mcp_ids": ["codex:github", clash.json()["id"]]},
    )
    assert clash.status_code == 422
    assert "重名" in clash.json()["detail"]

    # 详情里能看到使用中的智能体；被引用时不允许删除。
    detail = (await client.get(f"/api/mcp-servers/{platform['id']}")).json()
    assert sorted(item["name"] for item in detail["agents"]) == ["claude-code 智能体", "codex 智能体"]
    assert (await client.delete(f"/api/mcp-servers/{platform['id']}")).status_code == 409

    # 复制智能体时一并复制分配；换内核时丢弃内核专属，保留平台通用。
    clone = (await client.post(f"/api/agents/{codex_agent['id']}/duplicate")).json()
    assert clone["mcp_ids"] == ["codex:github", platform["id"]]
    retyped = (await client.patch(f"/api/agents/{codex_agent['id']}", json={"type_id": "antigravity-cli"})).json()
    assert retyped["mcp_ids"] == [platform["id"]]


async def test_run_materializes_assigned_mcp_into_isolated_home(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    # 本用例只验证「启动子进程之前已经物化」，不需要真实内核跑完一轮；
    # 给它一个很短的运行上限，免得本机装了 codex 时把测试拖成无限等待。
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 5)
    root = tmp_path / "codex"
    root.mkdir()
    (root / "config.toml").write_text(
        '[projects."/tmp/ws"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers.host]\ncommand = "npx"\nargs = ["host-only"]\n',
        encoding="utf-8",
    )

    host_specs = get_agent("codex").discover_mcp_servers()
    monkeypatch.setattr(
        mcp_service, "discover_catalog", lambda: {item.source_id or "": item for item in host_specs}
    )
    await client.post("/api/mcp-servers/rescan")

    agent = await make_agent(client, "codex")
    await client.put(f"/api/agents/{agent['id']}/mcp-servers", json={"mcp_ids": ["codex:host"]})
    conversation = (
        await client.post(
            "/api/conversations", json={"agent_profile_id": agent["id"], "title": "MCP 验证"}
        )
    ).json()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"}
    )
    # 物化发生在启动子进程之前：不管本机装没装 codex，隔离配置都已经写好了。
    await response.aread()

    home = get_settings().agent_homes_root / conversation["id"] / "codex-home"
    content = (home / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    assert list(parsed["mcp_servers"]) == ["host"]
    assert parsed["mcp_servers"]["host"]["args"] == ["host-only"]
    assert parsed["projects"]["/tmp/ws"]["trust_level"] == "trusted"
    # 隔离目录里的配置文件必须收紧权限（可能含 MCP 密钥）。
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600

    # 取消分配后，隔离配置里也要立刻消失（每轮重建）。
    await client.put(f"/api/agents/{agent['id']}/mcp-servers", json={"mcp_ids": []})
    await (await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"reset_session": True, "content": "再问"}
    )).aread()
    parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert "mcp_servers" not in parsed


# --- 内置服务器：开箱即用 + 参数由用户填写 -------------------------------------


async def test_builtin_mcp_servers_are_seeded_editable_and_parameterized(
    client: httpx.AsyncClient,
):
    """内置「图片生成 / 系统管理 / 任务进度卡」自动播种：命令由平台维护，参数键回给界面。"""
    async with SessionLocal() as session:
        assert await mcp_service.ensure_builtin_servers(session, None) == 3
        # 幂等：重复调用不会再建。
        assert await mcp_service.ensure_builtin_servers(session, None) == 0
        rows = {
            row.builtin_key: row
            for row in (
                await session.execute(select(McpServer).where(McpServer.builtin_key.is_not(None)))
            ).scalars().all()
        }
        assert set(rows) == {"image-gen", "coda-admin", "task-card"}
        for row in rows.values():
            assert row.source_kind == "manual"
            assert row.command and row.args
            assert Path(row.args[0]).is_file()  # 脚本必须随包存在，否则内置服务器起不来
            view = mcp_service.read_view(row)
            assert view["source_label"] == "平台内置"
        # 图片生成 / 系统管理可编辑；任务进度卡是平台内部能力，只在目录里展示。
        assert mcp_service.read_view(rows["image-gen"])["editable"] is True
        assert mcp_service.read_view(rows["coda-admin"])["editable"] is True
        task_card = mcp_service.read_view(rows["task-card"])
        assert task_card["editable"] is False
        assert task_card["assignable"] is False

        image = mcp_service.read_view(rows["image-gen"])
        assert image["env_keys"] == [
            "IMAGE_GEN_API_KEY",
            "IMAGE_GEN_GATEWAY",
            "IMAGE_GEN_MODEL",
            "IMAGE_GEN_TIMEOUT",
        ]
        labels = {item["key"]: item["label"] for item in image["builtin_params"]}
        assert labels["IMAGE_GEN_GATEWAY"] == "网关地址"
        assert labels["IMAGE_GEN_MODEL"] == "模型名称"
        assert all(item["masked"] is None for item in image["builtin_params"])

        admin = mcp_service.read_view(rows["coda-admin"])
        admin_labels = {item["key"]: item["label"] for item in admin["builtin_params"]}
        assert admin_labels["CODA_BASE_URL"] == "平台地址"
        assert admin_labels["CODA_TOKEN"] == "平台访问令牌"
        assert admin["env_preview"]["CODA_MODE"]
        # 系统管理 MCP 的平台地址由平台自动填写；鉴权关闭时没有用户可归属令牌。
        assert admin["env_preview"]["CODA_BASE_URL"]
        assert {item["key"]: item["managed"] for item in admin["builtin_params"]}[
            "CODA_BASE_URL"
        ] is True
        assert {item["key"]: item["managed"] for item in admin["builtin_params"]}[
            "CODA_TOKEN"
        ] is True

    # 列表接口同样带上参数说明；内置服务器开箱即出现在目录里。
    items = (await client.get("/api/mcp-servers")).json()["items"]
    builtins = {item["builtin_key"] for item in items if item["builtin_key"]}
    assert builtins == {"image-gen", "coda-admin", "task-card"}
    image_item = next(item for item in items if item["builtin_key"] == "image-gen")
    assert image_item["env_preview"]["IMAGE_GEN_GATEWAY"] is None
    assert image_item["builtin_params"][0]["required"] is True


async def test_internal_task_card_builtin_is_visible_but_not_assignable(
    client: httpx.AsyncClient,
):
    """任务进度卡在 MCP 页可见，但由运行层按轮自动挂载：不能编辑、不能分配。"""
    async with SessionLocal() as session:
        await mcp_service.ensure_builtin_servers(session, None)
        rows = {
            row.builtin_key: row
            for row in (
                await session.execute(select(McpServer).where(McpServer.builtin_key.is_not(None)))
            ).scalars().all()
        }
        task_row = rows["task-card"]

    items = (await client.get("/api/mcp-servers")).json()["items"]
    task = next(item for item in items if item["builtin_key"] == "task-card")
    assert task["assignable"] is False
    assert task["editable"] is False
    # 能编辑的只有图片生成 / 系统管理
    assert (
        await client.patch(f"/api/mcp-servers/{task['id']}", json={"enabled": False})
    ).status_code == 403

    gateway = await make_gateway(client)
    agent = await make_profile(client, gateway_id=gateway["id"])
    assigned = await client.put(
        f"/api/agents/{agent['id']}/mcp-servers", json={"mcp_ids": [task["id"]]}
    )
    assert assigned.status_code == 422
    assert "平台内部能力" in assigned.text

    # 历史误分配（直接写库）也不会被运行时加载。
    async with SessionLocal() as session:
        profile = await session.get(AgentProfile, agent["id"])
        session.add(
            AgentMcpServer(agent_profile_id=profile.id, mcp_server_id=task_row.id)
        )
        await session.commit()
        specs = await mcp_service.mcp_servers_for_agent(session, profile)
    assert all(item.slug != "coda-task-card" for item in specs)


async def test_builtin_mcp_accepts_params_but_platform_owns_connection(
    client: httpx.AsyncClient,
):
    async with SessionLocal() as session:
        await mcp_service.ensure_builtin_servers(session, None)
    row = next(
        item
        for item in (await client.get("/api/mcp-servers")).json()["items"]
        if item["builtin_key"] == "image-gen"
    )

    patched = await client.patch(
        f"/api/mcp-servers/{row['id']}",
        json={
            "env": {
                "IMAGE_GEN_GATEWAY": "https://gw-test-8.example.com",
                "IMAGE_GEN_MODEL": "model-x7",
                "IMAGE_GEN_API_KEY": "",
                "IMAGE_GEN_TIMEOUT": "",
            }
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["env_preview"]["IMAGE_GEN_GATEWAY"]
    assert body["env_preview"]["IMAGE_GEN_MODEL"]
    # 明文绝不回显（用参数说明里不会出现的值做断言）。
    assert "gw-test-8.example.com" not in patched.text
    assert "model-x7" not in patched.text

    # 连接方式（命令 / 参数 / 传输方式）由平台维护，不能通过接口替换成别的程序。
    assert (
        await client.patch(f"/api/mcp-servers/{row['id']}", json={"command": "bash"})
    ).status_code == 403
    assert (
        await client.patch(f"/api/mcp-servers/{row['id']}", json={"args": ["x.py"]})
    ).status_code == 403
    assert (await client.delete(f"/api/mcp-servers/{row['id']}")).status_code == 403


async def test_retire_orphan_builtins_removes_renamed_legacy_rows(
    client: httpx.AsyncClient,
):
    """更名后留下的旧内置记录会被启动清理收走：软删除 + 断开分配，且不误伤当前预设。"""
    async with SessionLocal() as session:
        await mcp_service.ensure_builtin_servers(session, None)

        profile = AgentProfile(name="测试智能体", type_id="custom-cli")
        session.add(profile)
        orphan = McpServer(
            id="mcp-legacy-arkswarm",
            name="系统管理",
            slug="arkswarm-admin",
            description="旧版内置系统管理",
            type_id=None,
            source_kind="manual",
            transport="stdio",
            command="/app/venv/bin/python",
            args=["/app/releases/V0.1.2/scripts/mcp/arkswarm_admin_mcp_server.py"],
            env_encrypted={},
            headers_encrypted={},
            enabled=True,
            available=True,
            builtin_key="arkswarm-admin",
        )
        session.add(orphan)
        # 先落库再建分配：没有 relationship 声明时，SQLAlchemy 不保证父表先插。
        await session.commit()
        session.add(
            AgentMcpServer(agent_profile_id=profile.id, mcp_server_id=orphan.id)
        )
        await session.commit()

        assert await mcp_service.retire_orphan_builtins(session) == 1

        refreshed = await session.get(McpServer, orphan.id)
        assert refreshed is not None
        assert refreshed.deleted_at is not None
        assert refreshed.enabled is False
        # 外键是 RESTRICT：清理前必须先断开智能体分配。
        assigned = (
            await session.execute(
                select(AgentMcpServer).where(
                    AgentMcpServer.mcp_server_id == orphan.id
                )
            )
        ).scalars().first()
        assert assigned is None

        # 当前预设一个都不能误伤。
        live = {
            row.builtin_key
            for row in (
                await session.execute(
                    select(McpServer).where(
                        McpServer.builtin_key.is_not(None),
                        McpServer.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
        }
        assert live == {"image-gen", "coda-admin", "task-card"}

        # 幂等：再跑一次没有可清的了。
        assert await mcp_service.retire_orphan_builtins(session) == 0
