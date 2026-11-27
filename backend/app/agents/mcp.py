"""MCP 服务器（Model Context Protocol）的共享数据结构、解析与格式化。

MCP 服务器是「外部工具服务」：一个本地进程（stdio）或一个 HTTP 端点，内核按配置连上去，
把它的工具当成自己的工具用。与技能一样，平台只做**统一编目 + 分配**：

* 适配器用 :meth:`AgentDefinition.discover_mcp_servers` 只读扫描本机已有的 MCP 配置，
  把它们送进统一目录（来源记在 ``type_id`` 上）；
* 用户在「MCP」页维护「平台通用」的服务器（来源 ``type_id`` 为空，可分配给任意内核的智能体）；
* 运行时由适配器用 :meth:`AgentDefinition.mcp_mount` 声明「把分配到的服务器写到哪里」，
  框架按声明物化进该内核的隔离配置目录。

「哪个内核把 MCP 配置放在哪、长什么形状」是适配器的知识，框架不硬编码任何路径或键名。
本模块是叶子层：适配器与框架都依赖它，它只依赖标准库。
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 支持的传输方式
MCP_TRANSPORTS = ("stdio", "http", "sse")

#: 平台自己新建（而不是从内核配置里扫描出来）的来源标识
PLATFORM_SOURCE = "manual"

#: 平台内部的任务进度卡 server 标识与工具完整名（适配器预允许权限时用）。
#: 它不进 ``BUILTIN_SERVERS``，也不允许用户分配 / 编辑。
TASK_CARD_MCP_SLUG = "coda-task-card"
TASK_CARD_MCP_TOOL = f"mcp__{TASK_CARD_MCP_SLUG}__update_task_card"

#: 服务器标识（写进内核配置的键名）允许的字符：字母 / 数字 / 下划线 / 点 / 连字符，
#: 中文名也算合法字符（它只是个配置键，写文件时会带引号）。
_SLUG_ALLOWED = re.compile(r"[^\w.-]+", re.UNICODE)


def safe_slug(raw: str) -> str | None:
    """把用户起的名字规整成安全的服务器标识；无法规整时返回 ``None``。

    标识只用作内核配置里的键名（JSON / TOML 都会带引号），所以中文名也照收；
    真正会被替换掉的是空白、引号、括号这类会破坏配置结构的字符。
    """
    text = _SLUG_ALLOWED.sub("-", (raw or "").strip()).strip("-._")
    if not text:
        return None
    return text[:64]


@dataclass(slots=True)
class McpServerSpec:
    """一个 MCP 服务器的只读描述；编目、分配与物化都以它为单位。"""

    name: str
    slug: str
    description: str = ""
    #: stdio / http / sse
    transport: str = "stdio"
    #: stdio：可执行文件；http / sse：目标地址
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    #: 启动命令的环境变量（stdio）与请求头（http / sse）；值可能含密钥，界面上只回掩码
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    #: 来源内核 id；``None`` 表示「平台通用」（可分配给任意内核）
    type_id: str | None = None
    source_kind: str = PLATFORM_SOURCE

    @property
    def source_id(self) -> str | None:
        """扫描出来的服务器的稳定标识 ``<内核 id>:<slug>``；平台通用服务器没有。"""
        return f"{self.type_id}:{self.slug}" if self.type_id else None


@dataclass(slots=True)
class McpMount:
    """把「分配到的 MCP 服务器」交给某个内核时要写的文件。

    框架只按这份声明执行，不关心是哪个内核：

    * ``files``：写进该对话的内核隔离配置目录（相对路径 → 内容），每次运行都重建，
      取消分配后立刻消失；
    * ``workspace_files``：写进工作目录（相对路径 → 内容）——给那些只认「项目级配置」
      的内核（例如 Gemini CLI）；
    * ``notes``：需要提示给用户的一次性说明。

    ``home_name`` 必须与同一适配器 :meth:`AgentDefinition.skill_mount` 声明的一致：
    技能与 MCP 共用同一个「家」。
    """

    home_name: str
    files: dict[str, str] = field(default_factory=dict)
    workspace_files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --- 解析（内核配置 → 统一描述）---------------------------------------------


def parse_json_servers(
    raw: Any,
    *,
    type_id: str,
    source_kind: str = "user",
    key: str = "mcpServers",
) -> list[McpServerSpec]:
    """从内核的 JSON 配置里解析 ``mcpServers``（Claude Code / Gemini CLI 的形状）。"""
    if not isinstance(raw, dict):
        return []
    servers = raw.get(key)
    if not isinstance(servers, dict):
        return []
    specs: list[McpServerSpec] = []
    for name, entry in servers.items():
        spec = entry_to_spec(str(name), entry, type_id=type_id, source_kind=source_kind)
        if spec is not None:
            specs.append(spec)
    return specs


def parse_toml_servers(
    text: str,
    *,
    type_id: str,
    source_kind: str = "user",
    table: str = "mcp_servers",
) -> list[McpServerSpec]:
    """从内核的 TOML 配置里解析 ``[mcp_servers.*]``（Codex 的形状）。

    配置损坏（或不是 TOML）时返回空列表：只读扫描必须尽力而为，不得抛错。
    """
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    servers = data.get(table)
    if not isinstance(servers, dict):
        return []
    specs: list[McpServerSpec] = []
    for name, entry in servers.items():
        spec = entry_to_spec(str(name), entry, type_id=type_id, source_kind=source_kind)
        if spec is not None:
            specs.append(spec)
    return specs


def entry_to_spec(
    name: str, entry: Any, *, type_id: str, source_kind: str = "user"
) -> McpServerSpec | None:
    """把内核配置里的一条服务器记录翻译成 :class:`McpServerSpec`。"""
    if not isinstance(entry, Mapping):
        return None
    slug = safe_slug(name)
    if slug is None:
        return None
    env = _string_map(entry.get("env"))
    headers = _string_map(entry.get("headers"))
    command = entry.get("command")
    url = entry.get("url") or entry.get("httpUrl") or entry.get("serverUrl")
    transport = _transport(entry, command=command, url=url)
    if command is None and url is None:
        return None
    return McpServerSpec(
        name=name,
        slug=slug,
        description=str(entry.get("description") or ""),
        transport=transport,
        command=str(command) if command else None,
        args=[str(item) for item in _as_list(entry.get("args"))],
        url=str(url) if url else None,
        env=env,
        headers=headers,
        type_id=type_id,
        source_kind=source_kind,
    )


def _transport(entry: Mapping[str, Any], *, command: Any, url: Any) -> str:
    declared = str(entry.get("type") or "").strip().lower()
    if declared in ("sse",):
        return "sse"
    if declared in ("http", "streamable", "streamable-http", "streamable_http"):
        return "http"
    if command:
        return "stdio"
    return "http" if url else "stdio"


def _string_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value not in (None, "")}


def _as_list(raw: Any) -> list[Any]:
    if isinstance(raw, str):
        return [part for part in raw.split() if part]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


# --- 格式化（统一描述 → 内核配置）-------------------------------------------


def to_json_servers(specs: Iterable[McpServerSpec]) -> dict[str, dict[str, Any]]:
    """内核通用的 ``mcpServers`` JSON 形状（Claude Code 与 Gemini CLI 都吃这一份）。"""
    return {spec.slug: to_json_entry(spec) for spec in specs}


def to_json_entry(spec: McpServerSpec) -> dict[str, Any]:
    if spec.transport == "stdio":
        entry: dict[str, Any] = {"command": spec.command or "", "args": list(spec.args)}
        if spec.env:
            entry["env"] = dict(spec.env)
        return entry
    entry = {"type": "sse" if spec.transport == "sse" else "http", "url": spec.url or ""}
    if spec.headers:
        entry["headers"] = dict(spec.headers)
    return entry


def to_json_config(specs: Iterable[McpServerSpec], *, key: str = "mcpServers") -> str:
    """一整个内核配置文件的 JSON 文本（``{"mcpServers": {...}}``）。"""
    return json.dumps({key: to_json_servers(specs)}, ensure_ascii=False, indent=2) + "\n"


def render_toml_servers(
    specs: Iterable[McpServerSpec], *, table: str = "mcp_servers"
) -> str:
    """把服务器渲染成 TOML 片段（Codex 的 ``[mcp_servers.<name>]``）。

    键名一律用带引号的形式（``[mcp_servers."foo"]``），免去「哪些字符能当裸键」的麻烦。
    """
    chunks: list[str] = []
    for spec in specs:
        head = f"{table}.{_toml_string(spec.slug)}"
        lines = [f"[{head}]"]
        if spec.transport == "stdio":
            lines.append(f"command = {_toml_string(spec.command or '')}")
            if spec.args:
                lines.append(f"args = [{', '.join(_toml_string(arg) for arg in spec.args)}]")
            if spec.env:
                lines.append(f"[{head}.env]")
                lines += [f"{_toml_string(k)} = {_toml_string(v)}" for k, v in spec.env.items()]
        else:
            lines.append(f"url = {_toml_string(spec.url or '')}")
            if spec.headers:
                lines.append(f"[{head}.http_headers]")
                lines += [
                    f"{_toml_string(k)} = {_toml_string(v)}" for k, v in spec.headers.items()
                ]
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def strip_toml_table(text: str, *, table: str = "mcp_servers") -> str:
    """删掉 TOML 文本里的 ``[<table>]`` / ``[<table>.x]`` 表（含表内所有行）。

    用来把「本机自带的 MCP 配置」从要携带的内核配置里剥掉，保证内核只看得到分配到的服务器。
    """
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            name = stripped.lstrip("[").split("]")[0].strip().strip('"')
            skipping = name == table or name.startswith(f"{table}.")
            if skipping:
                continue
            kept.append(line)
            continue
        if skipping:
            continue
        key = stripped.split("=", 1)[0].strip().strip('"') if "=" in stripped else ""
        if key == table or key.startswith(f"{table}."):
            continue
        kept.append(line)
    body = "\n".join(kept).strip("\n")
    return f"{body}\n" if body else ""


def _toml_string(value: Any) -> str:
    # TOML 的基本字符串与 JSON 的转义规则在这些常见字符上一致，直接复用更不容易写错。
    return json.dumps(str(value), ensure_ascii=False)
