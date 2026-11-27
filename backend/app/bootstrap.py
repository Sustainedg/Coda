"""首次启动播种：为每个可用内核建一条官方网关 + 一个默认智能体，避免界面空空如也。

多用户系统启用时，这里还负责：

* :func:`seed_initial_admin` —— 按环境变量建首任管理员；
* :func:`active_admin` —— 取当前启用中的管理员（老库已有用户时回填用）；
* :func:`backfill_ownership` —— 把老库（原本单用户、所有数据无归属）的存量行
  ``owner_id`` 回填给管理员，用 ``.ownership_backfilled`` 标记防重复。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .auth import hash_password
from .models import (
    AgentProfile,
    ApiKey,
    Conversation,
    Credential,
    Gateway,
    McpServer,
    UsageRecord,
    User,
)
from .security import encrypt
from .settings import get_settings

if TYPE_CHECKING:  # 仅供类型标注：播种由入口组合根传入适配器，避免运行期反向依赖 agents
    from .agents.base import AgentDefinition

#: 默认智能体的人格：(显示名, 系统提示词)。
#: **不带 emoji 头像** —— 头像统一由下面的 `STARTER_AVATAR_PRESETS` 从内置头像包里指定。
STARTER_PERSONAS = {
    "claude-code": (
        "资深工程师",
        "你是一名严谨的资深软件工程师。动手前先通读仓库，简要说明取舍，"
        "优先给出最小且经过验证的改动。",
    ),
    "codex": (
        "结对编程搭子",
        "你是务实的结对编程伙伴。每次改动尽量小而可评审，改完立刻运行相关测试，"
        "并总结改了什么。",
    ),
    "openai-harness": (
        "网关通用助手",
        "你是通过自定义网关接入的通用助手。回答要精确，"
        "遇到含糊的需求先请求澄清。",
    ),
    "deepseek-harness": (
        "DeepSeek 工程搭子",
        "你是 DeepSeek Harness 驱动的工程搭子。先读代码、再动手，"
        "改动尽量小而可验证，并在结束时说明改了什么。",
    ),
}

#: 默认智能体领取的头像包编号。
#: emoji 头像已下线，播种时直接发一张 3D 黏土风人物 / 卡通图。
STARTER_AVATAR_PRESETS = {
    "claude-code": "pack-005",
    "codex": "pack-013",
    "openai-harness": "pack-021",
    "deepseek-harness": "pack-029",
}

#: 没登记的适配器兜底：头像包第一张。
DEFAULT_STARTER_AVATAR_PRESET = "pack-001"


async def seed_if_empty(session: AsyncSession, definitions: Sequence[AgentDefinition]) -> None:
    """按传入的适配器声明播种「官方网关 + 默认智能体」（适配器列表由入口组合根提供）。"""
    settings = get_settings()
    marker = settings.data_dir / ".seeded"
    if marker.exists():
        return
    existing = (await session.execute(select(AgentProfile))).scalars().all()
    if existing:
        marker.write_text("skipped\n", encoding="utf-8")
        return

    created: list[tuple[Gateway, AgentDefinition, str | None]] = []
    for definition in definitions:
        if not definition.auto_provision or not definition.availability().installed:
            continue
        defaults = definition.system_defaults()
        base_url = defaults.get("gateway_url") or definition.default_gateway
        if not base_url:
            # 没有官方地址的内核（例如自定义 CLI）：由用户自己建网关
            continue
        model = defaults.get("model") or definition.default_model
        models = [model] if model else list(definition.default_models)
        gateway = Gateway(
            name=f"{definition.name} 官方",
            kind="official",
            type_id=definition.id,
            protocol=definition.protocol,
            auth_style=definition.default_auth_style,
            base_url=base_url,
            api_key_encrypted=encrypt(defaults.get("api_key")),
            selected_models=models,
            model_catalog=models,
        )
        session.add(gateway)
        created.append((gateway, definition, model))

    if not created:
        return
    await session.commit()
    for gateway, _definition, _model in created:
        await session.refresh(gateway)

    for index, (gateway, definition, model) in enumerate(created):
        name, prompt = STARTER_PERSONAS.get(
            definition.id, (f"{definition.name} 助手", "你是一个乐于助人的助手。")
        )
        session.add(
            AgentProfile(
                name=name,
                # emoji 头像已下线：老字段留空，头像由内置头像包提供。
                avatar="",
                avatar_preset=STARTER_AVATAR_PRESETS.get(
                    definition.id, DEFAULT_STARTER_AVATAR_PRESET
                ),
                tagline=f"由 {definition.name} 官方链路驱动",
                description=definition.description,
                system_prompt=prompt,
                type_id=definition.id,
                gateway_id=gateway.id,
                model=model,
                options=definition.coerce_options({}),
                enabled=True,
                # 播种出来的第一个就是默认智能体：装好点「新建对话」就能直接聊。
                is_default=index == 0,
            )
        )
    await session.commit()
    marker.write_text("done\n", encoding="utf-8")


async def ensure_default_agent(session: AsyncSession) -> None:
    """保证「总有一颗五角星」：老库升级上来时补一次。

    只在「一个默认都没有、但已经有智能体」时动手，并优先挑启用的那个；
    用户自己点出来的星永远不会被这里覆盖。
    """
    has_default = (
        await session.execute(
            select(AgentProfile.id)
            .where(AgentProfile.is_default == True, AgentProfile.deleted_at.is_(None))  # noqa: E712
            .limit(1)
        )
    ).first()
    if has_default is not None:
        return
    rows = (
        await session.execute(
            select(AgentProfile)
            .where(AgentProfile.deleted_at.is_(None))
            .order_by(AgentProfile.enabled.desc(), AgentProfile.created_at)
        )
    ).scalars().all()
    if not rows:
        return
    rows[0].is_default = True
    session.add(rows[0])
    await session.commit()


async def seed_initial_admin(session: AsyncSession) -> User | None:
    """按环境变量建首任管理员；已存在任意用户时不生效。

    返回建出的管理员，或 ``None``（未配置环境变量 / 已有用户）。
    不写标记文件：判断依据是「表里有没有用户」，比文件标记更可靠。
    """
    settings = get_settings()
    if not settings.admin_username or not settings.admin_password:
        return None
    has_any = (await session.execute(select(User.id).limit(1))).first()
    if has_any is not None:
        return None
    user = User(
        username=settings.admin_username,
        display_name=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def active_admin(session: AsyncSession) -> User | None:
    """取一个启用中的管理员；老库回填需要在「已有用户、但还没回填」时也能找到它。"""
    return (
        await session.execute(
            select(User)
            .where(User.role == "admin", User.is_active == True)  # noqa: E712
            .order_by(User.created_at)
            .limit(1)
        )
    ).scalars().first()


async def migrate_legacy_runtime_ids(
    session: AsyncSession, definitions: Sequence[AgentDefinition]
) -> None:
    """把存量智能体从已下线的内核迁到替代它的适配器（幂等）。

    旧 id → 新适配器的映射来自适配器自己的 ``legacy_ids`` 声明，框架不认识具体内核。
    迁移时顺带切断只属于旧内核的官方链路；新内核不接管模型（``default_model`` 为空，
    由内核按账号能力自决）时，旧模型名也一并清掉 —— 否则会作为非法参数传下去。
    """
    new_by_old = {
        old_id: definition
        for definition in definitions
        for old_id in definition.legacy_ids
    }
    if not new_by_old:
        return
    rows = (
        await session.execute(
            select(AgentProfile).where(AgentProfile.type_id.in_(new_by_old))
        )
    ).scalars().all()
    if not rows:
        return
    for profile in rows:
        definition = new_by_old[profile.type_id]
        # 旧内核的官方链路不属于新内核：断掉，运行时回落到「官方默认」。
        if profile.gateway_id:
            gateway = await session.get(Gateway, profile.gateway_id)
            if (
                gateway is not None
                and gateway.kind == "official"
                and gateway.type_id == profile.type_id
            ):
                profile.gateway_id = None
        if definition.default_model is None:
            profile.model = None
        profile.type_id = definition.id
        session.add(profile)
    await session.commit()


async def backfill_ownership(session: AsyncSession, admin: User) -> None:
    """把老库存量行的 ``owner_id`` 回填给管理员。

    用 ``.ownership_backfilled`` 标记防重复；回填 ``owner_id IS NULL``（老库）与
    ``owner_id = '__system__'``（鉴权关闭期间以虚拟管理员身份创建）的行，已有真实归属
    （新创建的）不动。幂等。
    """
    settings = get_settings()
    marker = settings.data_dir / ".ownership_backfilled"
    if marker.exists():
        return
    owner_id = admin.id
    # 逐表回填无归属（NULL）与虚拟管理员名下（__system__）的行。用原生 UPDATE 避免整行加载进 ORM。
    # usage_records 是后加的一手账本：鉴权关闭期间写的行 owner_id 为空，要一起回填，
    # 否则开启鉴权后非管理员看不到自己的历史用量。
    # 注意 ``usage_pricing``：它的 ``owner_id IS NULL`` 是「平台级」语义，不是老库无主，
# **不能**回填给管理员，否则全局价会变成管理员私有。
    for model in (Gateway, AgentProfile, Conversation, Credential, McpServer, UsageRecord, ApiKey):
        table_name = model.__tablename__  # type: ignore[attr-defined]
        await session.execute(
            # SQLModel 的表名稳定，这里用 text 走原生 SQL 最直接。
            text(
                f"UPDATE {table_name} SET owner_id = :owner "
                "WHERE owner_id IS NULL OR owner_id = '__system__'"
            ).bindparams(owner=owner_id)
        )
    # 协作域表 + IM 域表（同样带 owner_id；漏掉 IM 的话，鉴权关闭期间建的连接
    # 打开鉴权后会永远看不到，且没人能接管）
    for table_name in ("collab_teams", "collab_runs", "im_connections"):
        await session.execute(
            text(
                f"UPDATE {table_name} SET owner_id = :owner "
                "WHERE owner_id IS NULL OR owner_id = '__system__'"
            ).bindparams(owner=owner_id)
        )
    await session.commit()
    marker.write_text("done\n", encoding="utf-8")
