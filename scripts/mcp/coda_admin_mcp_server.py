#!/usr/bin/env python3
"""Coda 系统管理 MCP 服务器（stdio 传输）。

一个随平台分发的「平台通用」MCP 服务器：把平台的 REST API（智能体 / 协作组 /
技能 / MCP 编目）包成一组管理工具。分配到它的智能体就能在对话里**以主人身份**
管理这个平台 —— 查看与创建智能体、编排协作组、安装 / 删除技能、增删改 MCP 服务器。

配置全部来自环境变量（在「MCP」页新建 / 编辑服务器时填写；值经平台加密落库，
运行时由内核注入本进程，绝不会出现在日志或对话里）：

* ``CODA_BASE_URL``   平台地址（如 ``http://127.0.0.1:8010`` 或 ``…:8010/api``）
* ``CODA_TOKEN``      个人访问令牌（PAT，在「设置 → 账号」生成；也可用登录 JWT，
                           但会过期，推荐 PAT）
* ``CODA_MODE``       权限模式：``ro``（默认，只读，写操作直接拒绝）/ ``rw``（可读写）
* ``CODA_TIMEOUT``    单次请求超时秒数（默认 60）

设计要点（与 image_mcp_server.py 同一套纪律）：

* 只用标准库：stdio 传输里 stdout 是协议端口，日志一律走 stderr。
* 单文件内的职责分层（自上而下，单向依赖）：

      协议层（JSON-RPC 收发与分发）
        → 工具层（25 个管理工具的编排与 ro 拦截）
            → 客户端层（带 Bearer PAT 调平台 REST API）
                → 配置层（读环境变量、拼 URL）

* 所有写操作都要 ``confirm: true`` 才会执行 —— 让「删除技能」这类破坏性动作必须
  是模型的显式决定，而不是顺嘴一说。
* 平台的权限体系原样生效：PAT 属于哪个用户，工具就只能看到 / 动到那个用户的资产
  （管理员的全局可见）。本服务器不额外绕过或放宽任何校验。
* 工具结果只回**摘要**（名字 / id / 状态），列表详情让模型按需再查 —— 防一次
  tools/call 把几十 KB 的 JSON 灌进上下文。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

SERVER_NAME = "coda-admin"
SERVER_VERSION = "1.0.0"
#: MCP 协议版本；initialize 时优先回显客户端请求的版本
PROTOCOL_VERSION = "2024-11-05"

ENV_BASE_URL = "CODA_BASE_URL"
ENV_TOKEN = "CODA_TOKEN"
ENV_MODE = "CODA_MODE"
ENV_TIMEOUT = "CODA_TIMEOUT"

DEFAULT_TIMEOUT = 60.0
MODE_RW = "rw"
MODE_RO = "ro"


# --- 配置层 -------------------------------------------------------------------


class ConfigError(RuntimeError):
    """配置缺失：消息面向用户，说明缺什么、去哪里补。"""


def load_config() -> dict[str, Any]:
    """读取环境变量里的平台配置；缺 base_url / token 不在这里报错，
    留到真正调用时才提示 —— 服务器本身要先能启动，错误才能在对话里看到。"""
    timeout = DEFAULT_TIMEOUT
    raw_timeout = (os.environ.get(ENV_TIMEOUT) or "").strip()
    if raw_timeout:
        try:
            timeout = max(1.0, float(raw_timeout))
        except ValueError:
            print(
                f"{ENV_TIMEOUT} 配置不成秒数（{raw_timeout}），按默认 {DEFAULT_TIMEOUT:.0f}s 处理",
                file=sys.stderr,
            )
    mode = (os.environ.get(ENV_MODE) or MODE_RO).strip().lower()
    if mode not in (MODE_RW, MODE_RO):
        print(
            f"{ENV_MODE} 只支持 {MODE_RW} / {MODE_RO}（收到 {mode}），按只读 {MODE_RO} 处理",
            file=sys.stderr,
        )
        mode = MODE_RO
    return {
        "base_url": api_root(os.environ.get(ENV_BASE_URL, "")),
        "token": os.environ.get(ENV_TOKEN, ""),
        "mode": mode,
        "timeout": timeout,
    }


def api_root(raw: str) -> str:
    """把用户填的地址归一成 REST API 根：补协议、补 ``/api``。

    ``http://host:8010`` / ``http://host:8010/`` / ``http://host:8010/api`` /
    ``http://host:8010/api/`` 都收敛到 ``http://host:8010/api``。
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    if not (value.startswith("http://") or value.startswith("https://")):
        value = f"http://{value}"
    if value.endswith("/api"):
        return value
    return f"{value}/api"


# --- 客户端层 -----------------------------------------------------------------


class ApiError(RuntimeError):
    """平台 API 返回了错误状态码：消息给模型看，包含状态码与平台的原话。"""


def call_api(
    config: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    """以 PAT 调平台 REST API，返回解析后的 JSON。

    401 单独给出可操作的提示（PAT 吊销 / 用错令牌是部署里最常见的故障）。
    """
    if not config["base_url"]:
        raise ConfigError(
            f"缺少 {ENV_BASE_URL}：请到「MCP」页编辑这个服务器，填平台地址"
            "（如 http://127.0.0.1:8010）"
        )
    if not config["token"]:
        raise ConfigError(
            f"缺少 {ENV_TOKEN}：内置的系统管理服务器由平台自动填写（启动 / 注册 / 令牌变更时）；"
            "若这是手工新建的服务器，请到「设置 → 账号」生成个人访问令牌并填进环境变量"
        )
    url = f"{config['base_url']}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method.upper())
    request.add_header("Authorization", f"Bearer {config['token']}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 401:
            raise ApiError(
                "平台拒绝了令牌（401）：个人访问令牌可能已被吊销或填错。"
                "内置的系统管理服务器在令牌失效时平台会自动补发（重新生成令牌 / 改口令后即刻生效）；"
                "若仍不通，请到「MCP」页编辑这个服务器确认令牌，或到「设置 → 账号」重新生成。"
            ) from None
        raise ApiError(f"平台返回 {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"连不上平台（{exc.reason}）：请检查 {ENV_BASE_URL} 是否正确、服务是否在运行") from None
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        raise ApiError("平台响应不是合法 JSON，可能与地址配置不符（填到了非 API 路径上）") from None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """尽力从错误响应里挖出平台的中文错误信息（FastAPI 的 detail 字段）。"""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        return str(payload.get("detail") or payload)
    except Exception:  # noqa: BLE001 - 错误体读不出来就用状态码兜底
        return exc.reason or ""


# --- 工具层 -------------------------------------------------------------------


def _require_rw(config: dict[str, Any]) -> None:
    """ro 模式下拦截一切写操作，错误信息告诉用户去哪里开。"""
    if config["mode"] == MODE_RO:
        raise ApiError(
            f"这个服务器配置为只读模式（{ENV_MODE}=ro），拒绝执行写操作。"
            f"如需放开，请到「MCP」页把 {ENV_MODE} 改为 {MODE_RW}。"
        )


def _need_confirm(arguments: dict[str, Any]) -> None:
    """破坏性 / 写操作的显式确认：模型必须传 confirm=true 才执行。"""
    if arguments.get("confirm") is not True:
        raise ApiError(
            "这个操作需要确认：请在参数里显式传 confirm=true 再调用"
            "（想清楚后果 —— 它会真实改动平台数据）。"
        )


def _brief(row: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """从平台的行里摘一小撮字段，防大 JSON 灌爆上下文。"""
    return {key: row.get(key) for key in fields if key in row}


AGENT_BRIEF = ("id", "name", "type_id", "type_name", "model", "enabled", "is_default", "group_name")
SKILL_BRIEF = ("id", "slug", "name", "type_id", "type_name", "source_kind", "available")
MCP_BRIEF = ("id", "slug", "name", "transport", "type_id", "source_kind", "enabled", "available")
TEAM_BRIEF = ("id", "name", "description", "entry_key", "node_count", "updated_at")
GATEWAY_BRIEF = ("id", "name", "kind", "type_id", "base_url")
TYPE_BRIEF = ("id", "name", "runtime", "vendor", "installed")


def _list_out(items: list[dict[str, Any]], fields: Sequence[str], label: str) -> str:
    if not items:
        return f"没有任何{label}。"
    lines = [f"共 {len(items)} 项："]
    for row in items:
        lines.append(f"- {_fmt(_brief(row, fields))}")
    return "\n".join(lines)


def _fmt(brief: dict[str, Any]) -> str:
    """把摘要字典排成一行紧凑的键值串（None 与空值跳过）。"""
    parts = [f"{key}={value}" for key, value in brief.items() if value not in (None, "", [])]
    return "  ".join(parts)


def _pick_ids(raw: Any) -> list[str]:
    """从工具参数里取 id 列表：既接受字符串数组，也接受「逗号分隔」的宽松写法。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


TOOL_HANDLERS: dict[str, Any] = {}


def tool(name: str, description: str, schema: dict[str, Any], *, mutating: bool = False):
    """登记一个工具：``mutating=True`` 的自动叠加 ro 拦截与 confirm 要求。"""

    def decorate(func):
        TOOL_HANDLERS[name] = {
            "description": description,
            "schema": schema,
            "handler": func,
            "mutating": mutating,
        }
        return func

    return decorate


# --- 智能体工具 ---------------------------------------------------------------


@tool(
    "agent_list",
    "列出当前用户的所有智能体（管理员可见全部）。返回 id、名字、内核类型、模型、启用状态。",
    {"type": "object", "properties": {}},
)
def agent_list(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    data = call_api(config, "GET", "/agents")
    return _list_out(data.get("items", []), AGENT_BRIEF, "智能体")


@tool(
    "agent_get",
    "查看一个智能体的完整配置（人格提示词、网关、模型、选项、已分配的技能与 MCP）。"
    "name_or_id 填智能体名字或 id（先按 id 精确匹配，再按名字唯一匹配）。",
    {
        "type": "object",
        "properties": {"name_or_id": {"type": "string", "description": "智能体 id 或名字"}},
        "required": ["name_or_id"],
    },
)
def agent_get(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    agent = _resolve_agent(arguments.get("name_or_id"), config)
    return json.dumps(agent, ensure_ascii=False, indent=2)


def _resolve_agent(name_or_id: Any, config: dict[str, Any]) -> dict[str, Any]:
    """按 id 或名字找一个智能体；找不到给可操作的错误（附现有列表，方便模型自查）。"""
    key = str(name_or_id or "").strip()
    if not key:
        raise ApiError("请提供智能体的 id 或名字（name_or_id）。")
    items = call_api(config, "GET", "/agents").get("items", [])
    for row in items:
        if row.get("id") == key:
            return row
    matches = [row for row in items if row.get("name") == key]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ApiError(
            f"找不到智能体「{key}」。现有智能体：{'、'.join(row.get('name', '?') for row in items) or '（无）'}"
        )
    raise ApiError(
        f"叫「{key}」的智能体不止一个，请改用 id：{'、'.join(row['id'] for row in matches)}"
    )


@tool(
    "agent_create",
    "创建一个新智能体。type_id 从 agent_types_list 查；gateway_id 从 gateway_list 查"
    "（不填则用官方链路）；model 必须从网关的模型列表里选。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "智能体名字（必填）"},
            "type_id": {"type": "string", "description": "内核类型 id（必填，见 agent_types_list）"},
            "system_prompt": {"type": "string", "description": "人格提示词（可选）"},
            "tagline": {"type": "string", "description": "一句话简介（可选）"},
            "description": {"type": "string", "description": "详细介绍（可选）"},
            "avatar": {"type": "string", "description": "头像 emoji（默认 🛠️）"},
            "gateway_id": {"type": "string", "description": "网关 id（可选，见 gateway_list）"},
            "model": {"type": "string", "description": "模型名（从网关模型列表选）"},
            "group_name": {"type": "string", "description": "分组名（可选）"},
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "分配的技能 id 列表（可选，见 skill_list）",
            },
            "mcp_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "分配的 MCP 服务器 id 列表（可选，见 mcp_list）",
            },
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name", "type_id", "confirm"],
    },
    mutating=True,
)
def agent_create(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    body = {key: value for key, value in arguments.items() if key not in ("confirm",)}
    skill_ids = _pick_ids(body.pop("skill_ids", None))
    mcp_ids = _pick_ids(body.pop("mcp_ids", None))
    agent = call_api(config, "POST", "/agents", body=body)
    # 创建成功后再补分配（平台的创建接口不收这两个字段；分配走整表替换接口）。
    if skill_ids:
        call_api(config, "PUT", f"/agents/{agent['id']}/skills", body={"skill_ids": skill_ids})
    if mcp_ids:
        call_api(config, "PUT", f"/agents/{agent['id']}/mcp-servers", body={"mcp_ids": mcp_ids})
    return f"已创建智能体「{agent['name']}」（id={agent['id']}）。"


@tool(
    "agent_update",
    "修改一个智能体的配置（人格、网关、模型、分组、启停、默认星标等）。"
    "只传要改的字段，未传的保持不变。name_or_id 填 id 或名字。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "智能体 id 或名字"},
            "name": {"type": "string"},
            "system_prompt": {"type": "string"},
            "tagline": {"type": "string"},
            "description": {"type": "string"},
            "avatar": {"type": "string"},
            "gateway_id": {"type": "string"},
            "model": {"type": "string"},
            "enabled": {"type": "boolean"},
            "is_default": {"type": "boolean", "description": "设为 / 取消默认智能体星标"},
            "group_name": {"type": "string"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def agent_update(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    agent = _resolve_agent(arguments.get("name_or_id"), config)
    body = {
        key: value
        for key, value in arguments.items()
        if key not in ("confirm", "name_or_id") and value is not None
    }
    if not body:
        raise ApiError("没有给出任何要修改的字段。")
    updated = call_api(config, "PATCH", f"/agents/{agent['id']}", body=body)
    return f"已更新智能体「{updated['name']}」。"


@tool(
    "agent_set_skills",
    "整表替换一个智能体分配到的技能（不在列表里的会解除分配）。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "智能体 id 或名字"},
            "skill_ids": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string", "description": "逗号分隔也行"},
                ],
                "description": "技能 id 列表（见 skill_list）",
            },
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "skill_ids", "confirm"],
    },
    mutating=True,
)
def agent_set_skills(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    agent = _resolve_agent(arguments.get("name_or_id"), config)
    call_api(
        config,
        "PUT",
        f"/agents/{agent['id']}/skills",
        body={"skill_ids": _pick_ids(arguments.get("skill_ids"))},
    )
    return f"已更新智能体「{agent['name']}」的技能分配。"


@tool(
    "agent_set_mcp",
    "整表替换一个智能体分配到的 MCP 服务器（不在列表里的会解除分配）。"
    "注意别把这个「系统管理」服务器自己解除掉，否则当前对话的工具会失效。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "智能体 id 或名字"},
            "mcp_ids": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string", "description": "逗号分隔也行"},
                ],
                "description": "MCP 服务器 id 列表（见 mcp_list）",
            },
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "mcp_ids", "confirm"],
    },
    mutating=True,
)
def agent_set_mcp(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    agent = _resolve_agent(arguments.get("name_or_id"), config)
    call_api(
        config,
        "PUT",
        f"/agents/{agent['id']}/mcp-servers",
        body={"mcp_ids": _pick_ids(arguments.get("mcp_ids"))},
    )
    return f"已更新智能体「{agent['name']}」的 MCP 分配。"


@tool(
    "agent_delete",
    "软删除一个智能体（历史对话仍可读；它从所有可选列表消失）。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "智能体 id 或名字"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def agent_delete(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    agent = _resolve_agent(arguments.get("name_or_id"), config)
    call_api(config, "DELETE", f"/agents/{agent['id']}")
    return f"已删除智能体「{agent['name']}」。"


# --- 协作组工具 ---------------------------------------------------------------


@tool(
    "team_list",
    "列出当前用户的所有协作组（多智能体编排：谁是指挥、岗位怎么流转）。",
    {"type": "object", "properties": {}},
)
def team_list(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    data = call_api(config, "GET", "/collab/teams")
    return _list_out(data.get("items", []), TEAM_BRIEF, "协作组")


@tool(
    "team_get",
    "查看一个协作组的完整编排（岗位节点、流转边、预算、校验结果）。name_or_id 填 id 或名字。",
    {
        "type": "object",
        "properties": {"name_or_id": {"type": "string", "description": "协作组 id 或名字"}},
        "required": ["name_or_id"],
    },
)
def team_get(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    team = _resolve_team(arguments.get("name_or_id"), config)
    return json.dumps(team, ensure_ascii=False, indent=2)


def _resolve_team(name_or_id: Any, config: dict[str, Any]) -> dict[str, Any]:
    key = str(name_or_id or "").strip()
    if not key:
        raise ApiError("请提供协作组的 id 或名字（name_or_id）。")
    items = call_api(config, "GET", "/collab/teams").get("items", [])
    for row in items:
        if row.get("id") == key:
            return call_api(config, "GET", f"/collab/teams/{key}")
    matches = [row for row in items if row.get("name") == key]
    if len(matches) == 1:
        return call_api(config, "GET", f"/collab/teams/{matches[0]['id']}")
    if not matches:
        raise ApiError(
            f"找不到协作组「{key}」。现有协作组：{'、'.join(row.get('name', '?') for row in items) or '（无）'}"
        )
    raise ApiError(f"叫「{key}」的协作组不止一个，请改用 id。")


@tool(
    "team_create",
    "创建一个协作组。最简形态：name + nodes（至少一个岗位：key、agent_profile_id、"
    "duty），entry_key 不填默认用第一个节点。岗位上的智能体必须是当前用户已有的"
    "（见 agent_list）。duty 可选：ceo（指挥）/ engineer（实施）/ reviewer（审查）。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名字（必填）"},
            "description": {"type": "string"},
            "nodes": {
                "type": "array",
                "description": "岗位列表：{key, agent_profile_id, duty, label, instructions}",
                "items": {"type": "object"},
            },
            "edges": {
                "type": "array",
                "description": "流转边：{from_key, to_key, kind}；kind 可选 assign/review",
                "items": {"type": "object"},
            },
            "entry_key": {"type": "string", "description": "入口岗位 key（默认第一个节点）"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name", "nodes", "confirm"],
    },
    mutating=True,
)
def team_create(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    nodes = _coerce_nodes(arguments.get("nodes"))
    if not nodes:
        raise ApiError("nodes 不能为空：至少给协作组一个岗位。")
    edges = _coerce_edges(arguments.get("edges"), nodes)
    body = {
        "name": arguments.get("name"),
        "nodes": nodes,
        "edges": edges,
        "entry_key": arguments.get("entry_key") or nodes[0]["key"],
    }
    if arguments.get("description"):
        body["description"] = arguments["description"]
    team = call_api(config, "POST", "/collab/teams", body=body)
    return f"已创建协作组「{team['name']}」（id={team['id']}，{len(nodes)} 个岗位）。"


def _coerce_nodes(raw: Any) -> list[dict[str, Any]]:
    """把模型给的节点列表整成平台要的形状，宽松字段名映射（agent/agent_id → agent_profile_id）。"""
    nodes: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("id") or "").strip()
        agent = str(
            item.get("agent_profile_id")
            or item.get("agent")
            or item.get("agent_id")
            or ""
        ).strip()
        if not key or not agent:
            raise ApiError(f"岗位缺少 key 或 agent_profile_id：{_fmt(item)}")
        node: dict[str, Any] = {"key": key, "agent_profile_id": agent}
        for field in ("duty", "label", "instructions", "capability_scope"):
            if item.get(field):
                node[field] = item[field]
        nodes.append(node)
    return nodes


def _coerce_edges(raw: Any, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known = {node["key"] for node in nodes}
    edges: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        edge = {
            "from_key": item.get("from_key") or item.get("from"),
            "to_key": item.get("to_key") or item.get("to"),
        }
        if not edge["from_key"] or not edge["to_key"]:
            raise ApiError(f"边缺少 from_key / to_key：{_fmt(item)}")
        for side in ("from_key", "to_key"):
            if edge[side] not in known:
                raise ApiError(
                    f"边引用了不存在的岗位「{edge[side]}」；现有岗位：{'、'.join(sorted(known))}"
                )
        if item.get("kind"):
            edge["kind"] = item["kind"]
        if item.get("label"):
            edge["label"] = item["label"]
        edges.append(edge)
    return edges


@tool(
    "team_update",
    "修改一个协作组（名字 / 描述 / 整图替换）。graph 是整图替换：nodes 与 edges "
    "都要给全（不是增量修改）。不带 graph 只改名字描述。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "协作组 id 或名字"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "nodes": {"type": "array", "items": {"type": "object"}, "description": "整图替换时的岗位列表"},
            "edges": {"type": "array", "items": {"type": "object"}, "description": "整图替换时的流转边"},
            "entry_key": {"type": "string"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def team_update(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    team = _resolve_team(arguments.get("name_or_id"), config)
    body: dict[str, Any] = {}
    if arguments.get("name"):
        body["name"] = arguments["name"]
    if arguments.get("description") is not None:
        body["description"] = arguments["description"]
    if arguments.get("nodes"):
        nodes = _coerce_nodes(arguments.get("nodes"))
        edges = _coerce_edges(arguments.get("edges"), nodes)
        body["graph"] = {
            "nodes": nodes,
            "edges": edges,
        }
        if arguments.get("entry_key"):
            body["entry_key"] = arguments["entry_key"]
        elif team.get("entry_key"):
            body["entry_key"] = team["entry_key"]
    if not body:
        raise ApiError("没有给出任何要修改的字段。")
    updated = call_api(config, "PUT", f"/collab/teams/{team['id']}", body=body)
    return f"已更新协作组「{updated['name']}」。"


@tool(
    "team_delete",
    "删除一个协作组（不影响其中的智能体；运行历史按平台规则保留）。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "协作组 id 或名字"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def team_delete(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    team = _resolve_team(arguments.get("name_or_id"), config)
    call_api(config, "DELETE", f"/collab/teams/{team['id']}")
    return f"已删除协作组「{team['name']}」。"


# --- 技能工具 -----------------------------------------------------------------


@tool(
    "skill_list",
    "列出平台技能目录（可按内核类型 / 来源 / 关键词过滤）。技能必须分配给智能体才生效。",
    {
        "type": "object",
        "properties": {
            "type_id": {"type": "string", "description": "按内核类型过滤（可选）"},
            "q": {"type": "string", "description": "关键词过滤（可选）"},
        },
    },
)
def skill_list(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    query = {k: arguments[k] for k in ("type_id", "q") if arguments.get(k)}
    data = call_api(config, "GET", "/skills", query=query or None)
    return _list_out(data.get("items", []), SKILL_BRIEF, "技能")


@tool(
    "skill_get",
    "查看一个技能的详情（SKILL.md 内容、适用场景、被哪些智能体使用）。name_or_id 填 id 或 slug。",
    {
        "type": "object",
        "properties": {"name_or_id": {"type": "string"}},
        "required": ["name_or_id"],
    },
)
def skill_get(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    row = _resolve_skill(arguments.get("name_or_id"), config)
    detail = call_api(config, "GET", f"/skills/{row['id']}")
    body = detail.get("body") or ""
    preview = body[:2000] + ("\n…（内容过长已截断）" if len(body) > 2000 else "")
    return (
        f"技能「{detail['name']}」（{detail['id']}）\n"
        f"来源：{detail.get('source_label') or detail.get('source_kind')}；"
        f"适用：{detail.get('when_to_use') or '（未声明）'}\n"
        f"使用中的智能体：{len(detail.get('agents') or [])} 个\n"
        f"--- SKILL.md ---\n{preview}"
    )


def _resolve_skill(name_or_id: Any, config: dict[str, Any]) -> dict[str, Any]:
    key = str(name_or_id or "").strip()
    if not key:
        raise ApiError("请提供技能的 id 或 slug（name_or_id）。")
    items = call_api(config, "GET", "/skills").get("items", [])
    for row in items:
        if row.get("id") == key or row.get("slug") == key:
            return row
    raise ApiError(
        f"找不到技能「{key}」。可先用 skill_list 按关键词搜一下。"
    )


@tool(
    "skill_rescan",
    "重新扫描各内核的技能目录（刚在内核里装了新技能、平台目录里还没有时用）。",
    {"type": "object", "properties": {}},
)
def skill_rescan(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    data = call_api(config, "POST", "/skills/rescan", body={})
    return (
        f"扫描完成：新增 {data.get('added', 0)}、更新 {data.get('updated', 0)}、"
        f"清理已消失 {data.get('removed', 0)}、解除分配 {data.get('unassigned', 0)}、"
        f"未扫描到 {data.get('missing', 0)}。"
    )


@tool(
    "skill_delete",
    "删除一个技能（同时断开所有智能体的分配）。内核自带的技能不可删。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def skill_delete(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    row = _resolve_skill(arguments.get("name_or_id"), config)
    call_api(config, "DELETE", f"/skills/{row['id']}")
    return f"已删除技能「{row['name']}」及其所有分配。"


# --- MCP 工具 ------------------------------------------------------------------


@tool(
    "mcp_list",
    "列出平台 MCP 服务器目录（平台通用的 + 各内核配置扫描来的）。",
    {
        "type": "object",
        "properties": {
            "type_id": {"type": "string", "description": "按内核类型过滤（可选）"},
            "q": {"type": "string", "description": "关键词过滤（可选）"},
        },
    },
)
def mcp_list(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    query = {k: arguments[k] for k in ("type_id", "q") if arguments.get(k)}
    data = call_api(config, "GET", "/mcp-servers", query=query or None)
    return _list_out(data.get("items", []), MCP_BRIEF, "MCP 服务器")


@tool(
    "mcp_create",
    "新建一个「平台通用」MCP 服务器（stdio 命令或 http url，二选一）。"
    "创建后还要用 agent_set_mcp 把它分配给智能体才会生效。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "服务器名字（必填）"},
            "command": {"type": "string", "description": "stdio：启动命令"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "stdio：命令参数"},
            "url": {"type": "string", "description": "http/sse：服务器地址"},
            "transport": {"type": "string", "description": "stdio（默认）/ http / sse"},
            "description": {"type": "string"},
            "env": {"type": "object", "description": "环境变量（值会加密保存）"},
            "headers": {"type": "object", "description": "请求头（值会加密保存）"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name", "confirm"],
    },
    mutating=True,
)
def mcp_create(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    command = arguments.get("command")
    url = arguments.get("url")
    if not command and not url:
        raise ApiError("stdio 服务器要给 command，http/sse 服务器要给 url —— 至少填一个。")
    body = {key: value for key, value in arguments.items() if key != "confirm" and value}
    row = call_api(config, "POST", "/mcp-servers", body=body)
    return f"已创建 MCP 服务器「{row['name']}」（id={row['id']}）。记得用 agent_set_mcp 分配给智能体。"


@tool(
    "mcp_update",
    "修改一个 MCP 服务器（名字 / 描述 / 启停 / 命令 / url / env）。"
    "注意：env 是整表替换 —— 只列要保留的键；值留空表示保持原值不变，没列的键会被删除。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "MCP 服务器 id 或名字"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "enabled": {"type": "boolean"},
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "url": {"type": "string"},
            "env": {"type": "object"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def mcp_update(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    row = _resolve_mcp(arguments.get("name_or_id"), config)
    body = {
        key: value
        for key, value in arguments.items()
        if key not in ("confirm", "name_or_id") and value is not None
    }
    if not body:
        raise ApiError("没有给出任何要修改的字段。")
    updated = call_api(config, "PATCH", f"/mcp-servers/{row['id']}", body=body)
    return f"已更新 MCP 服务器「{updated['name']}」。"


@tool(
    "mcp_delete",
    "删除一个平台通用 MCP 服务器（软删除）。仍被智能体引用时会返回 409 —— 先用 "
    "agent_set_mcp 解除分配。内核扫描来的服务器不可删。",
    {
        "type": "object",
        "properties": {
            "name_or_id": {"type": "string", "description": "MCP 服务器 id 或名字"},
            "confirm": {"type": "boolean", "description": "必须传 true 才会执行"},
        },
        "required": ["name_or_id", "confirm"],
    },
    mutating=True,
)
def mcp_delete(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    _require_rw(config)
    _need_confirm(arguments)
    row = _resolve_mcp(arguments.get("name_or_id"), config)
    call_api(config, "DELETE", f"/mcp-servers/{row['id']}")
    return f"已删除 MCP 服务器「{row['name']}」。"


def _resolve_mcp(name_or_id: Any, config: dict[str, Any]) -> dict[str, Any]:
    key = str(name_or_id or "").strip()
    if not key:
        raise ApiError("请提供 MCP 服务器的 id 或名字（name_or_id）。")
    items = call_api(config, "GET", "/mcp-servers").get("items", [])
    for row in items:
        if row.get("id") == key or row.get("slug") == key or row.get("name") == key:
            return row
    raise ApiError(f"找不到 MCP 服务器「{key}」。可先用 mcp_list 查一下。")


# --- 辅助工具 ------------------------------------------------------------------


@tool(
    "agent_types_list",
    "列出平台支持的内核类型（创建智能体前先看这个：拿 type_id 与可用模型）。",
    {"type": "object", "properties": {}},
)
def agent_types_list(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    data = call_api(config, "GET", "/agent-types")
    return _list_out(data.get("items", []), TYPE_BRIEF, "内核类型")


@tool(
    "gateway_list",
    "列出可用网关（创建智能体时填 gateway_id 用；官方链路 + 用户自定义）。",
    {"type": "object", "properties": {}},
)
def gateway_list(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    data = call_api(config, "GET", "/gateways")
    rows = []
    for row in data.get("items", []):
        brief = _brief(row, GATEWAY_BRIEF)
        models = row.get("selected_models") or []
        if models:
            brief["models"] = "、".join(models[:6]) + ("…" if len(models) > 6 else "")
        rows.append(brief)
    if not rows:
        return "没有任何网关。"
    lines = [f"共 {len(rows)} 个网关："]
    for brief in rows:
        lines.append(f"- {_fmt(brief)}")
    return "\n".join(lines)


# --- 协议层 -------------------------------------------------------------------


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_response(msg_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
    """工具调用的标准回应：文本内容 + isError 标记（不抛协议错误，让模型看到原因）。"""
    content = [{"type": "text", "text": text}]
    result: dict[str, Any] = {"content": content}
    if is_error:
        result["isError"] = True
    return _result(msg_id, result)


def handle_message(message: Any) -> dict[str, Any] | None:
    """分发一条 JSON-RPC 消息；通知（无 id）静默忽略，返回 None 表示不回应。"""
    if not isinstance(message, dict):
        return _error(None, -32600, "请求必须是 JSON 对象")
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if msg_id is None:
        # notifications/initialized、notifications/cancelled 等通知：无需回应
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(
            msg_id,
            {
                "tools": [
                    {
                        "name": name,
                        "description": spec["description"],
                        "inputSchema": spec["schema"],
                    }
                    for name, spec in TOOL_HANDLERS.items()
                ]
            },
        )
    if method == "tools/call":
        name = str(params.get("name") or "")
        spec = TOOL_HANDLERS.get(name)
        if spec is None:
            return _error(msg_id, -32602, f"未知工具：{name}")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text = spec["handler"](arguments, load_config())
        except (ConfigError, ApiError, ValueError) as exc:
            return _tool_response(msg_id, f"操作失败：{exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - 兜底：不让未知异常拖垮整个服务器
            print(f"管理工具 {name} 未预期的异常：{exc!r}", file=sys.stderr)
            return _tool_response(msg_id, f"执行 {name} 时发生未预期的错误：{exc}", is_error=True)
        return _tool_response(msg_id, text)
    return _error(msg_id, -32601, f"未知方法：{method}")


def serve() -> int:
    """主循环：逐行读 stdin（每行一条 JSON-RPC 消息），回应写回 stdout。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "收到无法解析的 JSON 行")
        else:
            try:
                reply = handle_message(message)
            except Exception as exc:  # noqa: BLE001 - 协议层兜底，同上
                print(f"处理消息时未预期的异常：{exc!r}", file=sys.stderr)
                reply = _error(None, -32603, f"内部错误：{exc}")
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
