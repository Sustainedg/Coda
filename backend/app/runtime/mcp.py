"""MCP 服务器：统一编目、智能体分配与运行时物化。

各内核已有的 MCP 配置由适配器的 :meth:`AgentDefinition.discover_mcp_servers` 只读扫描，
与用户在「MCP」页新建的**平台通用**服务器一起汇成统一目录（``mcp_servers`` 表）；
智能体能挂哪些只能通过 ``agent_mcp_servers`` 分配。运行时再由适配器声明「把分配到的
服务器写到哪」（:class:`~app.agents.mcp.McpMount`），框架按声明写进该对话的隔离目录。

密钥（``env`` / ``headers`` 的值）只经 :mod:`app.security` 加密保存：接口永远只回键名与
掩码，明文只存在于「解密后注入适配器」的那一瞬间，不得进日志、事件或对话记录。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, all_agents
from ..agents.mcp import (
    MCP_TRANSPORTS,
    PLATFORM_SOURCE,
    TASK_CARD_MCP_SLUG,
    McpMount,
    McpServerSpec,
    safe_slug,
)
from ..auth import issue_pat, pat_hash
from ..builtin_mcp import (
    BUILTIN_SERVERS,
    MANAGED_PLATFORM_TOKEN,
    MANAGED_PUBLIC_BASE_URL,
    TASK_CARD_SCRIPT,
    BuiltinServer,
    builtin_by_key,
)
from ..models import AgentMcpServer, AgentProfile, McpServer, User
from ..security import decrypt, encrypt, mask
from ..settings import get_settings
from .isolation import ensure_home, home_for, note_fresh_home, safe_relative

logger = logging.getLogger("coda")

#: 任务进度卡的内部 server 标识：固定 slug，前端据此过滤工具轨迹。
#: 目录里的展示项（``BuiltinServer.assignable=False``）也用这个 slug，但只展示、
#: 不参与分配；真实运行配置由 ``prepare_mcp_home`` 按轮追加。
#: 常量来自 ``agents/mcp.py``（适配器预允许权限时也要用），避免三处字面量漂移。
INTERNAL_TASK_CARD_SLUG = TASK_CARD_MCP_SLUG

#: MCP 来源的中文标签
SOURCE_LABELS = {
    PLATFORM_SOURCE: "平台新建",
    "user": "内核配置",
}

#: 环境变量名与请求头名要能直接写进内核配置，否则内核读不到。
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEADER_KEY = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


class McpError(ValueError):
    """MCP 操作失败：消息面向用户、可直接展示；``status_code`` 交给 API 层当状态码。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- 密钥 -------------------------------------------------------------------


def _encrypt_pairs(pairs: Mapping[str, str] | None) -> dict[str, str]:
    return {str(key): encrypt(str(value)) or "" for key, value in (pairs or {}).items()}


def _decrypt_pairs(raw: Any) -> dict[str, str]:
    """解开「键 → 密文」；解不开或空值的键直接跳过（绝不抛错中断一次运行）。"""
    out: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        plain = decrypt(str(value) if value else None)
        if plain:
            out[str(key)] = plain
    return out


def mask_pairs(raw: Any) -> dict[str, str | None]:
    """给界面的只读视图：键名照旧，值只给掩码。"""
    return {str(key): mask(decrypt(str(value) if value else None)) for key, value in (raw or {}).items()}


def _clean_pairs(raw: Any, *, kind: str) -> dict[str, str]:
    """校验并规整 ``env`` / ``headers`` 的键（值原样保留）。"""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, Mapping):
        raise McpError(f"{'环境变量' if kind == 'env' else '请求头'}必须是「名字 → 值」的键值对")
    pattern = _ENV_KEY if kind == "env" else _HEADER_KEY
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not pattern.match(name):
            hint = (
                "环境变量名只能是字母、数字与下划线，且以字母或下划线开头（例如 API_KEY）"
                if kind == "env"
                else "请求头名只能由字母、数字与 !#$%&'*+.^_`|~- 组成（例如 Authorization）"
            )
            raise McpError(f"{'环境变量' if kind == 'env' else '请求头'}名「{key}」不合法：{hint}")
        cleaned[name] = str(value)
    return cleaned


def _merge_pairs(
    stored: Any, incoming: Mapping[str, str], *, kind: str
) -> dict[str, str]:
    """把界面提交的键值对合并进已存的那份：留空的已有键保持原值，没列出的键删除。"""
    current = _decrypt_pairs(stored)
    cleaned = _clean_pairs(incoming, kind=kind)
    merged: dict[str, str] = {}
    for key, value in cleaned.items():
        text = str(value)
        if text:
            merged[key] = text
        elif key in current:
            merged[key] = current[key]
    return merged


# --- 校验 -------------------------------------------------------------------


def _normalize_transport(raw: Any) -> str:
    value = str(raw or "stdio").strip().lower()
    if value not in MCP_TRANSPORTS:
        raise McpError(f"传输方式「{raw}」不支持：只能是 stdio、http 或 sse")
    return value


def _normalize_connection(transport: str, command: Any, url: Any) -> tuple[str | None, str | None]:
    command = str(command).strip() if command else None
    url = str(url).strip() if url else None
    if transport == "stdio":
        if not command:
            raise McpError("stdio 传输必须填写启动命令（例如 npx）")
        return command, None
    if not url:
        raise McpError("http / sse 传输必须填写服务器地址")
    if not url.startswith(("http://", "https://")):
        raise McpError("服务器地址必须以 http:// 或 https:// 开头")
    return None, url


async def _slug_conflict(
    session: AsyncSession, slug: str, *, keep_id: str | None = None, owner_id: str | None = None
) -> bool:
    """平台通用服务器之间不允许标识重名（重名写进内核配置会互相覆盖）。

    多用户下重名检查在「同一用户」范围内：不同用户各自叫 ``github`` 的服务器互不冲突。
    """
    statement = select(McpServer.id).where(
        McpServer.type_id.is_(None),
        McpServer.slug == slug,
        McpServer.deleted_at.is_(None),
    )
    if owner_id is not None:
        statement = statement.where(McpServer.owner_id == owner_id)
    for (mcp_id,) in (await session.execute(statement)).all():
        if mcp_id != keep_id:
            return True
    return False


# --- 编目 -------------------------------------------------------------------


# --- 内置服务器播种 ---------------------------------------------------------


async def _builtin_row(
    session: AsyncSession, owner_id: str | None, key: str
) -> McpServer | None:
    """取某个用户名下的内置服务器（软删除的不算）。"""
    statement = select(McpServer).where(
        McpServer.builtin_key == key,
        McpServer.deleted_at.is_(None),
    )
    if owner_id is None:
        statement = statement.where(McpServer.owner_id.is_(None))
    else:
        statement = statement.where(McpServer.owner_id == owner_id)
    return (await session.execute(statement)).scalars().first()


async def _builtin_slug(session: AsyncSession, base: str, owner_id: str | None) -> str:
    """给内置服务器挑一个不冲突的标识（用户可能已经建过同名服务器）。"""
    candidate = base
    index = 1
    while await _slug_conflict(session, candidate, owner_id=owner_id):
        index += 1
        candidate = f"{base}-{index}"
    return candidate


async def _managed_env(
    session: AsyncSession,
    preset: BuiltinServer,
    owner_id: str | None,
    existing_env: Mapping[str, str],
    token_cache: dict[str, str],
) -> tuple[dict[str, str], set[str]]:
    """按声明算出平台托管参数的当前值。

    哪些参数由平台托管、取什么值，全部来自 :attr:`BuiltinParam.managed_source`；
    框架层不认识具体的内置服务器，也不硬编码参数名。地址只在缺失时按部署配置补上；
    令牌只在「配置里没有 / 已不属于该用户」时签发一份新的，已有且仍有效的值原样保留
    （包括用户手工换成的自己的 PAT）。令牌只存哈希，明文只写进这份加密配置。

    返回 ``(要写入的参数值, 本次新签发的令牌参数键)``：后者只是给调用方报个数
    （「确实补发过令牌」），地址这类托管参数不算在内。

    ``token_cache`` 是**这一轮结算**里该用户当前生效的明文令牌：同一个用户的多个
    内置服务器共用一份平台令牌（哈希只存一列），谁先签发谁说了算，后面的直接复用 ——
    否则第二个服务器签发新令牌会顶掉哈希，把第一个服务器那份作废。
    """
    values: dict[str, str] = {}
    issued: set[str] = set()
    user: User | None = None
    issued_token: str | None = None
    for param in preset.params:
        if not param.managed:
            continue
        if param.managed_source == MANAGED_PUBLIC_BASE_URL:
            if not (existing_env.get(param.key) or "").strip():
                values[param.key] = get_settings().public_base_url_value
            continue
        if param.managed_source != MANAGED_PLATFORM_TOKEN:
            continue
        if owner_id is None:
            # 鉴权关闭：没有用户可归属，令牌留空；脚本会给出可操作的提示。
            continue
        if issued_token is not None:
            # 同一个服务器声明了多个令牌参数时共用一份平台令牌，别互相覆盖。
            values[param.key] = issued_token
            continue
        if user is None:
            user = await session.get(User, owner_id)
        if user is None:
            continue
        token = (existing_env.get(param.key) or "").strip()
        # 已有令牌仍属于这个用户（平台专用令牌，或用户手工换成的自己的 PAT）就保留。
        if token and pat_hash(token) in (user.builtin_mcp_pat_hash, user.pat_hash):
            issued_token = token
            token_cache.setdefault(owner_id, token)
            continue
        # 这一轮里已经给这个用户签发过令牌了：直接复用，别再签一份把哈希顶掉。
        cached = token_cache.get(owner_id)
        if cached:
            issued_token = cached
            values[param.key] = cached
            continue
        raw, digest = issue_pat()
        user.builtin_mcp_pat_hash = digest
        user.builtin_mcp_pat_issued_at = datetime.now(timezone.utc)
        user.updated_at = user.builtin_mcp_pat_issued_at
        session.add(user)
        values[param.key] = raw
        issued.add(param.key)
        issued_token = raw
        token_cache[owner_id] = raw
    return values, issued


async def _settle_builtin(
    session: AsyncSession,
    preset: BuiltinServer,
    owner_id: str | None,
    row: McpServer,
    token_cache: dict[str, str] | None = None,
) -> tuple[bool, int]:
    """结算一条内置服务器：重算平台托管参数并写回加密配置。

    返回 ``(这一行有没有改动, 本次补发了几份令牌)``。不自己提交 —— 提交时机由调用方
    决定（播种是批量提交；凭据联动是跟着用户的变更一起提交）。
    """
    cache = token_cache if token_cache is not None else {}
    existing_env = _decrypt_pairs(row.env_encrypted)
    managed, issued = await _managed_env(session, preset, owner_id, existing_env, cache)
    raw_env = dict(row.env_encrypted or {})
    changed = False
    # 用户没填过的参数先落成默认值（界面能看到要填哪些项）。
    for key, default in preset.env_defaults().items():
        if key not in raw_env:
            raw_env[key] = encrypt(default) or ""
            changed = True
    # 平台托管的参数由平台说了算，覆盖默认值。
    for key, value in managed.items():
        if value and value != existing_env.get(key):
            raw_env[key] = encrypt(value) or ""
            changed = True
    if raw_env != dict(row.env_encrypted or {}):
        row.env_encrypted = raw_env
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        changed = True
    return changed, len(issued)


async def ensure_builtin_servers(session: AsyncSession, owner_id: str | None) -> int:
    """确保某个用户名下有全部内置 MCP 服务器；返回本次新建的条数。

    幂等：已存在的那条只刷新「平台维护的连接方式」与新增的参数键，用户填过的参数值
    原样保留。因此版本升级（脚本路径 / 解释器变化）后调用一次就能对齐。
    """
    created = 0
    changed = False
    # 这一轮里每个用户当前生效的平台令牌（哈希只存一列，多个服务器共用一份）。
    token_cache: dict[str, str] = {}
    for preset in BUILTIN_SERVERS:
        row = await _builtin_row(session, owner_id, preset.key)
        if row is None:
            now = datetime.now(timezone.utc)
            row = McpServer(
                id=f"mcp-{os.urandom(8).hex()}",
                name=preset.name,
                slug=await _builtin_slug(session, preset.slug, owner_id),
                description=preset.description,
                type_id=None,
                source_kind=PLATFORM_SOURCE,
                transport="stdio",
                command=preset.command,
                args=list(preset.args),
                # 托管参数与业务参数都由下面的结算补齐：这里先留空，避免两处默认值打架。
                env_encrypted={},
                headers_encrypted={},
                enabled=True,
                available=True,
                builtin_key=preset.key,
                discovered_at=now,
                updated_at=now,
                owner_id=owner_id,
            )
            session.add(row)
            created += 1
        else:
            # 连接方式由代码说了算：升级换了脚本位置 / 解释器时跟着更新。
            if (row.command or "") != preset.command or list(row.args or []) != list(preset.args):
                row.command = preset.command
                row.args = list(preset.args)
                row.updated_at = datetime.now(timezone.utc)
                changed = True
        row_changed, _issued = await _settle_builtin(
            session, preset, owner_id, row, token_cache
        )
        changed = changed or row_changed

    if created or changed:
        await session.commit()
    return created


async def refresh_builtin_tokens(session: AsyncSession, owner_id: str | None) -> int:
    """凭据变动后**立刻**给内置 MCP 补发平台令牌；返回本次补发的令牌份数。

    什么时候需要它：用户的凭据一变（重新生成 / 吊销 PAT、改口令、管理员重置口令），
    内置「系统管理」MCP 配置里那份 ``CODA_TOKEN`` 可能已经失效了。以前只能等下次启动
    播种时才补上，中间这段时间系统管理 MCP 会一直收到 401。这里把补发动作从「启动时」
    提前到「凭据一变时」，让它当场恢复。

    判定口径与启动播种完全一致（都走 :func:`_managed_env`）：

    * 配置里的令牌仍然属于这个用户（平台专用令牌 / 用户自己的 PAT）→ 原样保留，不轮换；
    * 令牌已经不属于这个用户（凭据刚被换掉）→ 补发一份新的并写回加密配置。

    鉴权关闭（``owner_id`` 为空）时没有可归属的令牌，直接跳过。
    """
    if owner_id is None:
        return 0
    issued = 0
    changed = False
    # 这一轮里每个用户当前生效的平台令牌（多个内置服务器共用一份）。
    token_cache: dict[str, str] = {}
    for preset in BUILTIN_SERVERS:
        row = await _builtin_row(session, owner_id, preset.key)
        if row is None:
            # 这一条内置服务器还没播种（用户刚建、还没跑过平台）：由播种负责补齐。
            continue
        row_changed, count = await _settle_builtin(
            session, preset, owner_id, row, token_cache
        )
        changed = changed or row_changed
        issued += count
    if changed:
        await session.commit()
    if issued:
        logger.info("内置 MCP 令牌已补发 %s 份（用户 %s）", issued, owner_id)
    return issued


async def retire_orphan_builtins(session: AsyncSession) -> int:
    """软删除已下线的内置 MCP 服务器（``builtin_key`` 不在当前预设里的历史行）。

    内置服务器会随版本改名 / 下线（ArkSwarm 更名 Coda 时 ``arkswarm-admin`` 改成了
    ``coda-admin``）。老库里留下的旧 key 行有三个麻烦：播种逻辑只按当前 key 精确查，
    永远认领不到它；列表接口只按归属过滤，照样把它列出来；而 ``builtin_key`` 非空
    又让删除接口拒绝删除。三者叠加，它就成了删不掉、也不更新的常驻幽灵。

    启动时统一收尾：先断开智能体分配，再软删除。幂等，老库升级上来跑一次即干净。
    """
    active_keys = {preset.key for preset in BUILTIN_SERVERS}
    rows = (
        await session.execute(
            select(McpServer).where(
                McpServer.builtin_key.is_not(None),
                McpServer.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    orphans = [row for row in rows if row.builtin_key not in active_keys]
    if not orphans:
        return 0
    now = datetime.now(timezone.utc)
    ids = [row.id for row in orphans]
    # 外键是 RESTRICT：先解除分配，否则软删除虽不删行，但残留分配会让界面显示断链。
    await session.execute(
        delete(AgentMcpServer).where(AgentMcpServer.mcp_server_id.in_(ids))
    )
    for row in orphans:
        row.deleted_at = now
        row.updated_at = now
        row.enabled = False
        session.add(row)
    await session.commit()
    logger.info("已下线 %s 个失效的内置 MCP 服务器：%s", len(ids), ", ".join(ids))
    return len(ids)


async def ensure_builtin_servers_for_all(session: AsyncSession) -> int:
    """启动时为所有启用中的用户补齐内置服务器；返回本次新建的条数。

    鉴权关闭时资产归属记 ``None``（与服务端的 ``owner_id_for`` 口径一致），
    只播种一条共享的内置服务器。
    """
    # 先清掉历史遗留的失效内置项，再按当前预设播种；两步都幂等。
    await retire_orphan_builtins(session)
    if not get_settings().auth_enabled:
        return await ensure_builtin_servers(session, None)
    user_ids = (
        await session.execute(select(User.id).where(User.is_active == True))  # noqa: E712
    ).scalars().all()
    created = 0
    for user_id in user_ids:
        created += await ensure_builtin_servers(session, str(user_id))
    return created


async def builtin_param_warnings(
    session: AsyncSession,
    profile: AgentProfile,
    *,
    override_ids: list[str] | None = None,
) -> list[str]:
    """运行前提示：已分配的内置服务器还有必填参数没填。

    只提示、不阻断运行 —— 用户可能只是想先聊两句；真正调用工具时脚本自己会给出
    可操作的中文错误。``override_ids`` 是协作域的节点级分配（None = 跟随智能体页面）。
    """
    ids = override_ids if override_ids is not None else await agent_mcp_ids(session, profile.id)
    if not ids:
        return []
    rows = (
        await session.execute(select(McpServer).where(McpServer.id.in_(ids)))
    ).scalars().all()
    warnings: list[str] = []
    for row in rows:
        preset = builtin_by_key(row.builtin_key)
        if preset is None:
            continue
        missing = preset.missing_required(_decrypt_pairs(row.env_encrypted))
        if missing:
            warnings.append(
                f"内置 MCP「{row.name}」还缺必填参数：{'、'.join(missing)}；"
                "请到「MCP」页编辑它并补全后再使用"
            )
    return warnings


def discover_catalog() -> dict[str, McpServerSpec]:
    """聚合所有适配器扫描到的 MCP 服务器；单个适配器出错不影响其余。"""
    catalog: dict[str, McpServerSpec] = {}
    for definition in all_agents():
        try:
            specs = definition.discover_mcp_servers() or []
        except Exception as exc:  # noqa: BLE001 - 扫描失败不能拖垮整个目录
            logger.warning("扫描智能体 %s 的 MCP 配置失败：%s", definition.id, exc)
            continue
        for spec in specs:
            mcp_id = spec.source_id
            if mcp_id and mcp_id not in catalog:
                catalog[mcp_id] = spec
    return catalog


async def rescan(session: AsyncSession, owner_id: str | None = None) -> dict[str, int]:
    """重新扫描各内核的 MCP 配置，并同步到统一目录。

    配置里不再存在的服务器只标记 ``available=False``，不删除记录，避免智能体分配断链。
    平台新建的服务器（``type_id`` 为空）不参与重扫。扫描到的服务器归属到发起扫描的用户。
    """
    discovered = discover_catalog()
    rows = {
        row.id: row
        for row in (
            await session.execute(select(McpServer).where(McpServer.type_id.is_not(None)))
        )
        .scalars()
        .all()
    }
    now = datetime.now(timezone.utc)
    added = updated = 0

    for mcp_id, spec in discovered.items():
        row = rows.get(mcp_id)
        if row is None:
            session.add(
                McpServer(
                    id=mcp_id,
                    name=spec.name,
                    slug=spec.slug,
                    description=spec.description or "",
                    type_id=spec.type_id,
                    source_kind=spec.source_kind,
                    transport=spec.transport,
                    command=spec.command,
                    args=list(spec.args),
                    url=spec.url,
                    env_encrypted=_encrypt_pairs(spec.env),
                    headers_encrypted=_encrypt_pairs(spec.headers),
                    available=True,
                    discovered_at=now,
                    updated_at=now,
                    owner_id=owner_id,
                )
            )
            added += 1
            continue
        changed = (
            row.name != spec.name
            or (row.description or "") != (spec.description or "")
            or row.transport != spec.transport
            or (row.command or "") != (spec.command or "")
            or list(row.args or []) != list(spec.args)
            or (row.url or "") != (spec.url or "")
            or _decrypt_pairs(row.env_encrypted) != dict(spec.env)
            or _decrypt_pairs(row.headers_encrypted) != dict(spec.headers)
        )
        if changed or not row.available:
            row.name = spec.name
            row.slug = spec.slug
            row.description = spec.description or ""
            row.source_kind = spec.source_kind
            row.transport = spec.transport
            row.command = spec.command
            row.args = list(spec.args)
            row.url = spec.url
            row.env_encrypted = _encrypt_pairs(spec.env)
            row.headers_encrypted = _encrypt_pairs(spec.headers)
            row.available = True
            row.updated_at = now
            updated += 1

    missing = 0
    for mcp_id, row in rows.items():
        if mcp_id not in discovered and row.available:
            row.available = False
            row.updated_at = now
            missing += 1

    await session.commit()
    return {"added": added, "updated": updated, "missing": missing, "total": len(discovered)}


async def create_row(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    transport: str = "stdio",
    command: str | None = None,
    args: Sequence[Any] | None = None,
    url: str | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    enabled: bool = True,
    owner_id: str | None = None,
) -> McpServer:
    """新建一个「平台通用」的 MCP 服务器（可分配给任意内核的智能体）。"""
    label = (name or "").strip()
    if not label:
        raise McpError("请给这个 MCP 服务器起个名字")
    key = safe_slug(slug or label)
    if key is None:
        raise McpError("服务器标识不合法：请使用字母、数字、下划线、点或连字符")
    if await _slug_conflict(session, key, owner_id=owner_id):
        raise McpError(f"已经有一个叫「{key}」的 MCP 服务器了，请换一个名字", status_code=409)
    normalized_transport = _normalize_transport(transport)
    normalized_command, normalized_url = _normalize_connection(normalized_transport, command, url)
    now = datetime.now(timezone.utc)
    row = McpServer(
        id=f"mcp-{os.urandom(8).hex()}",
        name=label,
        slug=key,
        description=(description or "").strip(),
        type_id=None,
        source_kind=PLATFORM_SOURCE,
        transport=normalized_transport,
        command=normalized_command,
        args=[str(item) for item in (args or [])],
        url=normalized_url,
        env_encrypted=_encrypt_pairs(_clean_pairs(env, kind="env")),
        headers_encrypted=_encrypt_pairs(_clean_pairs(headers, kind="headers")),
        enabled=bool(enabled),
        available=True,
        discovered_at=now,
        updated_at=now,
        owner_id=owner_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_row(
    session: AsyncSession,
    row: McpServer,
    *,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    transport: str | None = None,
    command: str | None = None,
    args: Sequence[Any] | None = None,
    url: str | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    enabled: bool | None = None,
    owner_id: str | None = None,
) -> McpServer:
    """更新一个平台通用的 MCP 服务器（扫描来的服务器不可改，调用方先拦）。"""
    _ensure_editable(row)
    if row.builtin_key:
        _ensure_builtin_connection(row, transport=transport, command=command, args=args, url=url)
    label = (name or row.name).strip()
    if not label:
        raise McpError("请给这个 MCP 服务器起个名字")
    key = safe_slug(slug or label)
    if key is None:
        raise McpError("服务器标识不合法：请使用字母、数字、下划线、点或连字符")
    if await _slug_conflict(session, key, keep_id=row.id, owner_id=owner_id):
        raise McpError(f"已经有一个叫「{key}」的 MCP 服务器了，请换一个名字", status_code=409)
    normalized_transport = _normalize_transport(transport if transport is not None else row.transport)
    normalized_command, normalized_url = _normalize_connection(
        normalized_transport,
        command if command is not None else row.command,
        url if url is not None else row.url,
    )
    row.name = label
    row.slug = key
    if description is not None:
        row.description = description.strip()
    row.transport = normalized_transport
    row.command = normalized_command
    row.url = normalized_url
    if args is not None:
        row.args = [str(item) for item in args]
    # 界面回显的是掩码，不能把掩码当新值写回去，所以约定：
    # 列出来的键保留、值留空＝保持原值不变、没列出来的键删除、新键必须给值。
    if env is not None:
        row.env_encrypted = _encrypt_pairs(_merge_pairs(row.env_encrypted, env, kind="env"))
    if headers is not None:
        row.headers_encrypted = _encrypt_pairs(
            _merge_pairs(row.headers_encrypted, headers, kind="headers")
        )
    if enabled is not None:
        row.enabled = bool(enabled)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    if row.builtin_key:
        # 内置服务器：平台托管的参数（地址 / 令牌）由平台说了算。
        # 用户删掉令牌值想轮换时，当场补发一份新的 —— 不必等下次启动播种。
        #
        # 令牌哈希在用户上只有一列，同一用户的多个内置服务器共用一份平台令牌。
        # 只结算当前行会在换发新令牌时把别的服务器的令牌顶掉，所以有归属用户时
        # 整轮结算（共享一份 token_cache），与播种 / 凭据联动走同一条逻辑。
        preset = builtin_by_key(row.builtin_key)
        if preset is not None:
            if row.owner_id is not None:
                await refresh_builtin_tokens(session, row.owner_id)
            else:
                # 鉴权关闭：没有可归属的令牌，只需要补齐平台地址这类托管参数。
                changed, _issued = await _settle_builtin(session, preset, row.owner_id, row)
                if changed:
                    await session.commit()
            await session.refresh(row)
    return row


async def delete_row(session: AsyncSession, row: McpServer) -> None:
    """软删除一个平台通用的 MCP 服务器（历史对话与协作历史照旧可读）。

    内置服务器不提供删除：它随平台分发，删了下次启动还会被播种回来。
    """
    _ensure_editable(row)
    if row.builtin_key:
        raise McpError("内置 MCP 服务器不能删除；不需要时停用即可", status_code=403)
    row.deleted_at = datetime.now(timezone.utc)
    row.updated_at = row.deleted_at
    session.add(row)
    await session.commit()


def _ensure_editable(row: McpServer) -> None:
    preset = builtin_by_key(row.builtin_key)
    if preset is not None and not preset.assignable:
        raise McpError(
            f"「{row.name}」是平台内部能力，由运行层按轮自动挂载，不能修改",
            status_code=403,
        )
    if row.source_kind != PLATFORM_SOURCE or row.type_id is not None:
        raise McpError(
            f"「{row.name}」是从内核配置扫描出来的，平台不接管它的内容；"
            "请到该内核自己的配置里修改，或新建一个平台通用的服务器",
            status_code=403,
        )


def _ensure_builtin_connection(
    row: McpServer,
    *,
    transport: str | None,
    command: str | None,
    args: Sequence[Any] | None,
    url: str | None,
) -> None:
    """内置服务器的连接方式由平台维护，只允许改参数。

    界面上这些字段也是只读的；这里再挡一次，避免通过 API 把内置脚本替换成别的命令。
    """
    if transport is not None and _normalize_transport(transport) != row.transport:
        raise McpError("内置 MCP 的连接方式由平台维护，不能修改传输方式", status_code=403)
    if command is not None and str(command).strip() != (row.command or ""):
        raise McpError("内置 MCP 的启动命令由平台维护，不能修改", status_code=403)
    if args is not None and [str(item) for item in args] != list(row.args or []):
        raise McpError("内置 MCP 的启动参数由平台维护，不能修改", status_code=403)
    if url is not None:
        raise McpError("内置 MCP 使用 stdio 传输，不能填写服务器地址", status_code=403)


# --- 查询 -------------------------------------------------------------------


async def list_rows(
    session: AsyncSession,
    *,
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    query: str | None = None,
    owner_id: str | None = None,
    include_shared: bool = True,
    builtin_owner_id: str | None = None,
    scope_builtins: bool = False,
) -> list[McpServer]:
    statement = (
        select(McpServer)
        .where(McpServer.deleted_at.is_(None))
        .order_by(McpServer.type_id, McpServer.name)
    )
    if owner_id is not None:
        if include_shared:
            # 自己的 + 扫描来的（type_id 非空，内核配置里的共享服务器）
            statement = statement.where(
                (McpServer.owner_id == owner_id) | (McpServer.type_id.is_not(None))
            )
        else:
            statement = statement.where(McpServer.owner_id == owner_id)
    if scope_builtins:
        # 内置服务器按用户各播种一份：列表里只回当前用户自己的那一份。
        # 管理员仍可看别人的自定义服务器与扫描项，但内置项不跨用户展示
        # （否则 3 个用户 × 2 个内置项会变成 6 条，且 PAT 配置容易串号）。
        if builtin_owner_id is None:
            statement = statement.where(
                McpServer.builtin_key.is_(None) | McpServer.owner_id.is_(None)
            )
        else:
            statement = statement.where(
                McpServer.builtin_key.is_(None) | (McpServer.owner_id == builtin_owner_id)
            )
    if type_id:
        statement = statement.where(McpServer.type_id == type_id)
    if source_kind:
        statement = statement.where(McpServer.source_kind == source_kind)
    if available is not None:
        statement = statement.where(McpServer.available == available)  # noqa: E712
    rows = list((await session.execute(statement)).scalars().all())
    if query:
        needle = query.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.name.lower()
            or needle in row.slug.lower()
            or needle in (row.description or "").lower()
        ]
    return rows


async def get_row(session: AsyncSession, mcp_id: str) -> McpServer | None:
    row = await session.get(McpServer, mcp_id)
    if row is None or row.deleted_at is not None:
        return None
    return row


async def usage_counts(session: AsyncSession, owner_id: str | None = None) -> dict[str, int]:
    """每个 MCP 服务器被多少个**未删除**的智能体使用。

    ``owner_id`` 非空时只统计该用户的智能体（成员看自己的用量，管理员传 ``None`` 看全局）。
    """
    statement = (
        select(AgentMcpServer.mcp_server_id)
        .join(AgentProfile, AgentProfile.id == AgentMcpServer.agent_profile_id)
        .where(AgentProfile.deleted_at.is_(None))
    )
    if owner_id is not None:
        statement = statement.where(AgentProfile.owner_id == owner_id)
    rows = (await session.execute(statement)).scalars().all()
    counts: dict[str, int] = {}
    for mcp_id in rows:
        counts[mcp_id] = counts.get(mcp_id, 0) + 1
    return counts


async def agents_using(session: AsyncSession, mcp_id: str) -> list[AgentProfile]:
    statement = (
        select(AgentProfile)
        .join(AgentMcpServer, AgentMcpServer.agent_profile_id == AgentProfile.id)
        .where(AgentMcpServer.mcp_server_id == mcp_id, AgentProfile.deleted_at.is_(None))
        .order_by(AgentProfile.created_at)
    )
    return list((await session.execute(statement)).scalars().all())


# --- 分配 -------------------------------------------------------------------


async def agent_mcp_ids(session: AsyncSession, profile_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(AgentMcpServer)
            .where(AgentMcpServer.agent_profile_id == profile_id)
            .order_by(AgentMcpServer.position)
        )
    ).scalars().all()
    return [row.mcp_server_id for row in rows]


async def set_agent_mcp_servers(
    session: AsyncSession,
    profile: AgentProfile,
    mcp_ids: list[str],
    *,
    owner_id: str | None = None,
    builtin_owner_id: str | None = None,
) -> list[str]:
    """整表替换智能体的 MCP 分配；返回实际写入的 id 列表。

    只接受「可分配给这个内核」的服务器：平台通用的永远可以，扫描来的必须同内核。
    """
    wanted = list(dict.fromkeys(mcp_ids))
    slugs: dict[str, str] = {}
    for mcp_id in wanted:
        row = await get_row(session, mcp_id)
        if row is None:
            raise ValueError(f"MCP 服务器不存在：{mcp_id}")
        preset = builtin_by_key(row.builtin_key)
        if preset is not None and not preset.assignable:
            raise ValueError(
                f"「{row.name}」是平台内部能力，运行时会按轮自动挂载，无需分配"
            )
        # 平台通用服务器只能分配自己的；扫描来的（type_id 非空）是各内核配置的共享目录。
        if owner_id is not None and row.type_id is None and row.owner_id != owner_id:
            raise ValueError(f"MCP 服务器不存在或无权使用：{mcp_id}")
        # 内置服务器（尤其「系统管理」）按用户各有一份：只能分配当前用户自己的。
        if row.builtin_key is not None and row.owner_id != builtin_owner_id:
            raise ValueError(f"MCP 服务器不存在或无权使用：{mcp_id}")
        if row.type_id is not None and row.type_id != profile.type_id:
            raise ValueError(
                f"MCP 服务器「{row.name}」来自 {row.type_id} 内核的配置，"
                f"与该智能体的内核（{profile.type_id}）不匹配"
            )
        if row.slug in slugs:
            raise ValueError(
                f"分配的服务器里有重名的标识「{row.slug}」"
                f"（{slugs[row.slug]} 与 {row.name}），请只保留一个"
            )
        slugs[row.slug] = row.name

    for link in (
        await session.execute(
            select(AgentMcpServer).where(AgentMcpServer.agent_profile_id == profile.id)
        )
    ).scalars().all():
        await session.delete(link)
    await session.flush()

    for position, mcp_id in enumerate(wanted):
        session.add(
            AgentMcpServer(agent_profile_id=profile.id, mcp_server_id=mcp_id, position=position)
        )
    await session.commit()
    return wanted


async def mcp_servers_for_agent(
    session: AsyncSession,
    profile: AgentProfile,
    *,
    override_ids: list[str] | None = None,
) -> list[McpServerSpec]:
    """智能体在本轮运行中真正生效的 MCP 服务器（只取可分配、可用且启用中的）。

    ``override_ids``：协作域的节点级分配（None = 跟随智能体页面的分配；
    给了列表就以它为准）。安全边界与默认路径完全一致：可分配、可用、启用中、同内核、同归属。
    """
    ids = override_ids if override_ids is not None else await agent_mcp_ids(session, profile.id)
    if not ids:
        return []
    rows = (await session.execute(select(McpServer).where(McpServer.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    specs: list[McpServerSpec] = []
    for mcp_id in ids:
        row = by_id.get(mcp_id)
        if row is None or row.deleted_at is not None or not row.available or not row.enabled:
            continue
        preset = builtin_by_key(row.builtin_key)
        if preset is not None and not preset.assignable:
            # 平台内部能力由运行层按 context 注入，不经过普通分配（历史误分配也忽略）。
            continue
        if row.type_id is not None and row.type_id != profile.type_id:
            continue
        # 归属与「智能体分配」是同一套口径（见 set_agent_mcp_servers）：平台通用服务器
        # 只能引用自己的，内置服务器按用户各播种一份也只能引用自己那一份。节点级覆盖
        # 不能绕过它 —— 否则协作图能借别人的服务器（env / headers 含密钥）带进一次运行。
        if profile.owner_id is not None:
            if row.type_id is None and row.owner_id != profile.owner_id:
                continue
            if row.builtin_key is not None and row.owner_id != profile.owner_id:
                continue
        specs.append(spec_from_row(row))
    return specs


def spec_from_row(row: McpServer) -> McpServerSpec:
    """把库里的行翻译成适配器要的 :class:`McpServerSpec`（这里是唯一解密的地方）。"""
    return McpServerSpec(
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        transport=row.transport,
        command=row.command,
        args=list(row.args or []),
        url=row.url,
        env=_decrypt_pairs(row.env_encrypted),
        headers=_decrypt_pairs(row.headers_encrypted),
        type_id=row.type_id,
        source_kind=row.source_kind,
    )


def read_view(row: McpServer, *, used_by: int = 0, type_name: str | None = None) -> dict[str, Any]:
    """给界面的只读视图：密钥只给键名与掩码，永远没有明文。"""
    builtin = builtin_by_key(row.builtin_key)
    internal = builtin is not None and not builtin.assignable
    masked = mask_pairs(row.env_encrypted)
    note = row.note
    if internal and not note:
        note = "平台内部能力：运行时按轮自动挂载，无需分配或编辑。"
    # 普通服务器只回真正有值的键（空值键由 _decrypt_pairs 过滤掉，保持原有界面口径）；
    # 内置服务器的参数键即使还没填值也要回给界面：用户得先看到「平台地址 / 令牌」
    # 这些待填项，才知道该配什么。
    env_keys = set(_decrypt_pairs(row.env_encrypted).keys())
    if builtin is not None:
        env_keys.update(param.key for param in builtin.params)
    return {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description or "",
        "type_id": row.type_id,
        "type_name": type_name,
        "source_kind": row.source_kind,
        "source_label": "平台内置" if builtin is not None else SOURCE_LABELS.get(
            row.source_kind, row.source_kind
        ),
        "transport": row.transport,
        "command": row.command,
        "args": list(row.args or []),
        "url": row.url,
        "env_keys": sorted(env_keys),
        "env_preview": masked,
        "header_keys": sorted(_decrypt_pairs(row.headers_encrypted).keys()),
        "header_preview": mask_pairs(row.headers_encrypted),
        "enabled": bool(row.enabled),
        "available": bool(row.available),
        "note": note,
        # 平台内部能力（任务进度卡）只在目录里展示：不能分配、不能编辑。
        "assignable": True if builtin is None else builtin.assignable,
        "editable": (
            row.source_kind == PLATFORM_SOURCE
            and row.type_id is None
            and not internal
        ),
        "used_by_agents": used_by,
        #: 内置服务器的稳定标识与参数说明；普通服务器为 None / 空数组。
        "builtin_key": row.builtin_key,
        "builtin_params": builtin.params_view(masked) if builtin is not None else [],
    }


# --- 物化 -------------------------------------------------------------------


def _task_card_endpoint() -> str:
    """内部写卡接口地址（stdio 子进程与服务同机）。"""
    base = get_settings().public_base_url_value.rstrip("/")
    if base.endswith("/api"):
        return f"{base}/internal/task-cards/update"
    return f"{base}/api/internal/task-cards/update"


def _task_card_spec() -> McpServerSpec:
    """平台内部的任务进度卡 server；令牌不在这里，靠家目录里的凭据文件。"""
    return McpServerSpec(
        name="任务进度卡",
        slug=INTERNAL_TASK_CARD_SLUG,
        description="平台内置：更新当前对话的任务进度卡",
        transport="stdio",
        command=sys.executable or "python3",
        args=[str(TASK_CARD_SCRIPT)],
        env={
            "CODA_TASK_CARD_API": _task_card_endpoint(),
            "CODA_TASK_CARD_TIMEOUT": "10",
        },
    )


async def prepare_mcp_home(
    session: AsyncSession,
    definition: AgentDefinition,
    profile: AgentProfile,
    conversation: Any,
    ctx: AgentContext,
    *,
    scope_id: str | None = None,
    override_ids: list[str] | None = None,
    task_card_context_id: str | None = None,
) -> list[str]:
    """把分配到的 MCP 服务器写进该内核的隔离目录，并把结果写回 ``ctx``。

    ``scope_id`` 是隔离目录的作用域（默认用对话 id；协作运行会传运行 id）。
    ``override_ids`` 是协作域的节点级 MCP 分配（None = 跟随智能体页面）。
    ``task_card_context_id`` 非空时追加平台内部的任务卡 server（只对声明了
    ``Capabilities.mcp`` 的内核生效）。运行配置只写进隔离目录；「MCP」页里那条
    只是不可分配的展示项，不参与这里的物化。
    返回需要提示给用户的说明/告警（由运行层作为 ``log`` 事件暴露）。
    """
    ctx.mcp_servers = await mcp_servers_for_agent(session, profile, override_ids=override_ids)
    internal_task_card = bool(task_card_context_id) and definition.capabilities.mcp
    if internal_task_card:
        # 用户若恰好建了同名服务器，内部能力优先（配置里的键会互相覆盖）。
        ctx.mcp_servers = [
            spec for spec in ctx.mcp_servers if spec.slug != INTERNAL_TASK_CARD_SLUG
        ]
        ctx.mcp_servers.append(_task_card_spec())
    mount = definition.mcp_mount(ctx)
    if mount is None:
        if internal_task_card:
            ctx.mcp_servers = [
                spec for spec in ctx.mcp_servers if spec.slug != INTERNAL_TASK_CARD_SLUG
            ]
        return []
    # 内置服务器已分配但参数没填：提前提示，别等模型调用工具时才撞墙。
    # 只有真的分配了服务器才查库（没分配时不必多一次 SELECT，运行链路保持轻）；
    # 单测会以 ``None`` 会话 / 资料直接调这个函数（只验证物化计划），这里也跳过提示。
    warnings: list[str] = []
    if ctx.mcp_servers and session is not None and profile is not None:
        warnings = await builtin_param_warnings(session, profile, override_ids=override_ids)
    # 只有当配置真的要写进「内核隔离配置目录」时才建这个家：技能与 MCP 共用同一个目录，
    # 技能物化（prepare_skill_home）已经定过目录时以它为准。只写工作目录的内核
    # （例如 Gemini CLI）不需要这个目录，凭空建一个空目录会被 note_fresh_home
    # 误判成「新家」而清掉会话。
    if mount.files or ctx.home_dir:
        home = (
            Path(ctx.home_dir)
            if ctx.home_dir
            else home_for(scope_id or conversation.id, mount.home_name)
        )
        if internal_task_card:
            # 内核启动 MCP 子进程时的 cwd 是对话工作目录，不是内核家目录；
            # 这里把「家目录」告诉 server，让它按固定相对路径找凭据。
            # 只暴露目录，不含令牌；令牌仍在 0600 的 auth.json 里。
            for spec in ctx.mcp_servers:
                if spec.slug == INTERNAL_TASK_CARD_SLUG:
                    spec.env["CODA_TASK_CARD_HOME"] = str(home)
            # 上面改了 env，配置内容需要按新值重新渲染一次。
            mount = definition.mcp_mount(ctx) or mount
        first_run = not home.exists()
        warnings += ensure_home(home)
        warnings += _write_files(home, mount.files, label="内核配置")
        ctx.home_dir = str(home)
        warnings += note_fresh_home(ctx, first_run=first_run)
    elif internal_task_card:
        # 该内核只把 MCP 配置写进工作目录、没有隔离家目录：内部 server 无处安放。
        ctx.mcp_servers = [
            spec for spec in ctx.mcp_servers if spec.slug != INTERNAL_TASK_CARD_SLUG
        ]
    if mount.workspace_files:
        if ctx.workspace_dir:
            warnings += _write_files(
                Path(ctx.workspace_dir), mount.workspace_files, label="项目配置"
            )
        else:
            warnings.append("这个内核的 MCP 配置要写进工作目录，但本轮没有工作目录，已跳过")
    return [*mount.notes, *warnings]


def _write_files(base: Path, files: Mapping[str, str], *, label: str) -> list[str]:
    """把「相对路径 → 内容」写进某个目录（内容可能含密钥，一律收紧权限）。"""
    warnings: list[str] = []
    for relative, content in (files or {}).items():
        target_rel = safe_relative(relative)
        if target_rel is None:
            warnings.append(f"{label}的写入路径不安全，已跳过：{relative}")
            continue
        target = base / target_rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            os.chmod(target, 0o600)
        except OSError as exc:
            warnings.append(f"写入{label} {relative} 失败：{exc}")
    return warnings


__all__ = ["McpError", "McpMount", "McpServerSpec", "SOURCE_LABELS"]
