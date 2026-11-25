"""对话标题：用「设置」里指定的智能体 + 模型，把首条消息概括成一个短标题。

为什么要起一次真实运行：智能体可能是任意内核（Claude Code / Codex / Gemini CLI / 进程内 API），
唯一与内核无关的说法就是「像一轮普通对话那样把这句话交给它」。因此这里复用
:func:`runtime.runner.stream_run` 执行一次**一次性**运行（取消 / 超时 / 脱敏都沿用它），
只收集 ``message.delta`` 的文本当作标题，不落任何消息、不注册进运行表。

它是**尽力而为**的：任何一步失败都只记一条日志，保留 ``prepare_run`` 写的首行兜底标题 ——
起标题失败绝不该影响用户正常对话（铁律 11 的例外是「后台增强」，不是静默吞业务错误）。

配置（存在平台设置的键值表里，见 ``api/system.py``）：

* ``title_agent_profile_id``：用哪个智能体起标题；为空表示跟随「默认智能体」；
* ``title_model``：用哪个模型；为空表示用该智能体的默认模型。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, get_agent
from ..db import SessionLocal
from ..models import AgentProfile, Conversation, Gateway, PlatformSetting
from ..settings import get_settings
from .credentials import decrypt_for_profile
from .gateway import bind_gateway
from .manager import ActiveRun
from .mcp import prepare_mcp_home
from .runner import stream_run
from .skills import prepare_skill_home

logger = logging.getLogger(__name__)

#: 平台设置里存标题用哪个智能体 / 哪个模型的键名。
AUTO_SETTING_KEY = "title_auto"
AGENT_SETTING_KEY = "title_agent_profile_id"
MODEL_SETTING_KEY = "title_model"
#: 上一次自动起标题失败的原因与时间（诊断用：界面在「设置 → 对话」里显示，成功一次就清掉）。
ERROR_SETTING_KEY = "title_last_error"
ERROR_AT_SETTING_KEY = "title_last_error_at"

#: 标题上限：侧边栏与顶栏都是一行，超过就截断。
MAX_TITLE_CHARS = 24
#: 一次标题生成的时长上限；比一轮对话短得多，超时即放弃（保留兜底标题）。
TITLE_TIMEOUT_SECONDS = 60
#: 喂给智能体的首条消息上限，避免把超长正文塞进这次调用。
MAX_PROMPT_CHARS = 2000

#: 正在跑的标题任务：持有引用防止被 GC 提前回收（asyncio 的官方建议）。
_tasks: set[asyncio.Task] = set()


@dataclass(slots=True)
class TitleSettings:
    """标题生成：是否开启、用哪个智能体、哪个模型（智能体为空即「跟随默认智能体」）。"""

    enabled: bool = True
    agent_profile_id: str | None = None
    model: str | None = None


async def _read_setting(session: AsyncSession, key: str) -> str | None:
    row = (
        await session.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    ).scalar_one_or_none()
    return row.value if row and row.value else None


async def _write_setting(session: AsyncSession, key: str, value: str | None) -> None:
    row = (
        await session.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(PlatformSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def read_settings(session: AsyncSession) -> TitleSettings:
    auto = await _read_setting(session, AUTO_SETTING_KEY)
    return TitleSettings(
        # 键不存在（或为空）= 默认开启：默认就按「默认智能体的默认模型」自动起标题。
        enabled=(auto or "1") != "0",
        agent_profile_id=await _read_setting(session, AGENT_SETTING_KEY),
        model=await _read_setting(session, MODEL_SETTING_KEY),
    )


async def write_settings(
    session: AsyncSession,
    enabled: bool,
    agent_profile_id: str | None,
    model: str | None,
) -> TitleSettings:
    settings = TitleSettings(
        enabled=bool(enabled),
        agent_profile_id=(agent_profile_id or "").strip() or None,
        model=(model or "").strip() or None,
    )
    await _write_setting(session, AUTO_SETTING_KEY, "1" if settings.enabled else "0")
    await _write_setting(session, AGENT_SETTING_KEY, settings.agent_profile_id)
    await _write_setting(session, MODEL_SETTING_KEY, settings.model)
    return settings


@dataclass(slots=True)
class TitleDiagnostics:
    """上一次自动起标题的失败（界面上给用户一个自查的出口，见 `api/system.py`）。"""

    last_error: str | None = None
    last_error_at: str | None = None


async def read_diagnostics(session: AsyncSession) -> TitleDiagnostics:
    return TitleDiagnostics(
        last_error=await _read_setting(session, ERROR_SETTING_KEY),
        last_error_at=await _read_setting(session, ERROR_AT_SETTING_KEY),
    )


async def _record_error(message: str) -> None:
    """记下这次失败，让「设置 → 对话」能显示出来（尽力而为：记录本身失败只记日志）。"""
    try:
        async with SessionLocal() as session:
            await _write_setting(session, ERROR_SETTING_KEY, message[:500])
            await _write_setting(
                session, ERROR_AT_SETTING_KEY, datetime.now(timezone.utc).isoformat()
            )
    except Exception:  # noqa: BLE001 - 记录诊断失败不能反过来弄崩后台任务
        logger.warning("记录标题生成失败原因时出错", exc_info=True)


async def _clear_error(session: AsyncSession) -> None:
    await _write_setting(session, ERROR_SETTING_KEY, None)
    await _write_setting(session, ERROR_AT_SETTING_KEY, None)


def schedule(conversation_id: str, base_title: str, user_text: str) -> None:
    """把一次标题生成丢到后台（不阻塞发消息）。没有可用的智能体时静默跳过。"""
    if not user_text.strip():
        return
    task = asyncio.create_task(generate(conversation_id, base_title, user_text))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_for_pending() -> None:
    """等所有在跑的标题任务收尾（测试用；线上不需要——它们各自独立完成）。"""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def cancel_pending() -> None:
    """取消所有在跑的标题任务（测试收尾用，避免后台任务跨用例占着数据库）。"""
    pending = list(_tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def generate(conversation_id: str, base_title: str, user_text: str) -> None:
    """生成并写回一条对话的标题；失败一律保留兜底标题，不打扰用户。

    读配置的会话**在起子进程之前就关掉**：一次真实运行可能跑十几秒，不能让一个数据库
    会话（及其事务）挂在那里，否则会把别的写入挡在门外（SQLite 会直接报 database is locked）。

    **失败不抛给用户，但必须留痕**（铁律 11）：原因写进平台设置，界面在「设置 → 对话」
    显示「上次起标题失败：…」，成功一次就清掉 —— 用户既能自查，也不会被打扰。
    """
    try:
        async with SessionLocal() as session:
            settings = await read_settings(session)
            if not settings.enabled:
                return
            # 标题用谁起的必须是这条对话的主人的智能体：公共设置里指定的那颗星若不是
            # 他的，就在他自己名下退回默认智能体（否则会把别人的密钥带进这次运行）。
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                return
            resolved = await _resolve(session, settings, conversation.owner_id)
            if resolved is None:
                return
            profile, gateway, model = resolved
            definition = get_agent(profile.type_id)
            ctx = await _build_context(session, definition, profile, gateway, model, user_text)

        try:
            text = await _collect(definition, ctx)
        finally:
            _cleanup_scratch(ctx.workspace_dir)
        title = _clean(text)
        if not title:
            # 跑通了却没吐标题（模型只回了思考 / 空串）：这是真失败，得让用户看见。
            await _record_error("内核没有返回可用的标题（回复为空或只有思考）")
            return

        async with SessionLocal() as session:
            # 只在标题仍是当初那一条时才写回：用户中途改过名 / 另一条路径已经生成过，就别覆盖。
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None or (conversation.title or "") != base_title:
                # 这一轮其实**跑通了**，只是不该覆盖：上一次的失败记录也该清掉，别留着过期告警。
                await _clear_error(session)
                return
            # 只改标题，**不动 updated_at**：换名字不是一次「活动」，不该把这条对话顶到列表最前面
            # （列表按 updated_at 排序，标已读也不动它）。
            conversation.title = title
            session.add(conversation)
            await session.commit()
            # 这一次成功了：把上一次的失败记录清掉，界面上不再挂着过期的告警。
            await _clear_error(session)
    except Exception as exc:  # noqa: BLE001 - 后台增强：失败保留兜底标题，不打扰用户
        logger.warning("自动生成对话标题失败，保留兜底标题", exc_info=True)
        await _record_error(f"{exc.__class__.__name__}：{exc}" if str(exc) else exc.__class__.__name__)


async def _resolve(
    session: AsyncSession, settings: TitleSettings, owner_id: str | None = None
) -> tuple[AgentProfile, Gateway | None, str | None] | None:
    """挑出这次用来起标题的智能体 / 网关 / 模型。

    配置里指定的智能体被删了 / 停用了，就退回「默认智能体」；一个都没有则放弃。
    智能体钉了「非当前登录」的内核账号时也返回 ``None``：后台起标题不切号，宁可跳过。
    """
    from .service import pick_default_profile  # 局部导入：避免与 service 形成模块级循环

    profile: AgentProfile | None = None
    if settings.agent_profile_id:
        candidate = await session.get(AgentProfile, settings.agent_profile_id)
        if (
            candidate is not None
            and candidate.deleted_at is None
            and candidate.enabled
            and (owner_id is None or candidate.owner_id == owner_id)
        ):
            profile = candidate
    if profile is None:
        profile = await pick_default_profile(session, owner_id)
    if profile is None:
        return None
    gateway = await session.get(Gateway, profile.gateway_id) if profile.gateway_id else None
    try:
        definition = get_agent(profile.type_id)
    except KeyError:
        # 适配器没注册就没法起标题：说清楚是哪一颗内核跳过了，不静默（铁律 11）。
        logger.info(
            "标题智能体「%s」的内核 %s 没有注册，跳过自动起标题", profile.name, profile.type_id
        )
        return None
    # 只有「指定了账号 + 内核支持多账号」才值得探一次当前登录账号。
    active_account = (
        await _probe_active_account(definition)
        if profile.account_id and definition.supports_accounts()
        else None
    )
    if _pinned_account_is_inactive(active_account, profile, gateway):
        logger.info(
            "标题智能体「%s」指定的内核账号不是当前登录账号，跳过自动起标题"
            "（后台任务不切号，避免与前台对话串号或误扣别的账号额度）",
            profile.name,
        )
        return None
    model = settings.model or profile.model or None
    return profile, gateway, model


async def _probe_active_account(definition: AgentDefinition) -> str | None:
    """探测内核当前登录的账号 id；探测不到（不支持 / 失败）返回 None。

    ``auth_status`` 是同步实现、可能起子进程，放进线程池，别冻住事件循环。
    """
    try:
        status = await asyncio.to_thread(definition.auth_status)
    except Exception as exc:  # noqa: BLE001 - 标题是后台增强，探测失败不能拿去弄崩对话
        # 契约要求 auth_status 尽力而为、不抛错；真抛了是适配器 bug，必须留痕（铁律 11）。
        logger.warning(
            "探测内核 %s 的登录账号失败，起标题时按「无法判断」处理：%s", definition.id, exc
        )
        return None
    accounts = status.accounts if status is not None else None
    return next((account.id for account in accounts or [] if account.active), None)


def _pinned_account_is_inactive(
    active_account: str | None, profile: AgentProfile, gateway: Gateway | None
) -> bool:
    """智能体钉的账号确实不是当前登录账号时返回 True（此时跳过自动起标题）。

    只在**能确认**真实登录账号、且与指定槽位不同的时候跳过：探测失败 / 内核没暴露
    当前账号时无从判断，照常起标题（仍沿用当前登录账号，不会改动机器登录态）。
    自定义网关自带地址与密钥，不看内核登录态，自然也不受账号槽位影响。
    """
    if not profile.account_id:
        return False
    if gateway is not None and gateway.kind != "official":
        return False
    return bool(active_account and active_account != profile.account_id)


async def _build_context(
    session: AsyncSession,
    definition,
    profile: AgentProfile,
    gateway: Gateway | None,
    model: str | None,
    user_text: str,
) -> AgentContext:
    binding = bind_gateway(definition, gateway)
    options = definition.coerce_options(profile.options or {})
    if model:
        options.setdefault("model", model)
    credentials = await decrypt_for_profile(session, profile.id)
    scratch = _scratch_dir()
    ctx = AgentContext(
        agent_id=profile.id,
        agent_name=profile.name,
        options=options,
        gateway_url=binding.url,
        api_key=binding.api_key,
        auth_style=binding.auth_style,
        model=model,
        # 不要人格：只需要一句标题，别让智能体的人格把这次调用带偏。
        system_prompt=None,
        credentials=credentials,
        workspace_dir=str(scratch),
        session_id=None,
        history=[],
        prompt=_prompt(user_text),
    )
    # 技能与 MCP 照常按「严格仅分配」物化：哪怕只是一次起标题，也不要让内核读到本机
    # 未分配的个人 / 插件技能（铁律 13）。用独立作用域，别污染对话自己的隔离目录。
    scope = f"{profile.id}-title"
    await prepare_skill_home(session, definition, profile, None, ctx, scope_id=scope)
    await prepare_mcp_home(session, definition, profile, None, ctx, scope_id=scope)
    return ctx


def _scratch_dir() -> Path:
    """标题运行的工作目录：一个专用空目录，跑完即删，别把文件写进对话的工作目录。

    每次调用一个唯一子目录：并发的标题任务互不打扰，清理也只动自己那一个。
    """
    base = get_settings().data_dir / "title-scratch"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="title-", dir=base))


def _cleanup_scratch(workspace: str | None) -> None:
    if not workspace:
        return
    try:
        base = (get_settings().data_dir / "title-scratch").resolve()
        target = Path(workspace).resolve()
        # 只删自己那一个临时目录，绝不碰别处（`is_relative_to` 挡住越界路径）。
        if target != base and target.is_relative_to(base):
            shutil.rmtree(target, ignore_errors=True)
    except (OSError, RuntimeError, ValueError) as exc:
        # 临时目录清不掉不影响这次对话（下次启动会因为目录已存在而另建一个），但要说一声，别静默。
        logger.warning("清理标题运行的临时目录失败：%s（%s）", workspace, exc)


async def _collect(definition, ctx: AgentContext) -> str:
    """跑完这一次，把正文增量拼起来（只看正文，思考与工具调用不算）。"""
    chunks: list[str] = []
    handle = ActiveRun(id="title", conversation_id=ctx.agent_id, agent_name=ctx.agent_name)
    async with asyncio.timeout(TITLE_TIMEOUT_SECONDS):
        async for event in stream_run(definition, ctx, handle):
            if event.type.value == "message.delta" and event.text:
                chunks.append(event.text)
    return "".join(chunks)


def _prompt(user_text: str) -> str:
    excerpt = user_text.strip()[:MAX_PROMPT_CHARS]
    return (
        "请为下面这段对话起一个简洁的中文标题，用来在对话列表里一眼认出它说的是什么。\n"
        f"要求：不超过 {MAX_TITLE_CHARS} 个字；不要引号、书名号、句号；不要换行；"
        "只输出标题本身，不要任何解释或前后缀。\n\n"
        f"用户的第一句话：\n{excerpt}"
    )


def _clean(raw: str) -> str:
    """把模型的输出收拾成一行标题：去句末标点 / 去包裹 / 去前缀 / 只留一行 / 超长截断。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0].strip()
    # 先去掉句末标点，再去包裹 —— 先去的顺序反了会漏掉「"标题"。」这种带标点的包裹。
    text = text.strip("。.！!？?；;，,、 \t")
    for open_, close in (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("《", "》"), ("『", "』")):
        if text.startswith(open_) and text.endswith(close) and len(text) > 2:
            text = text[1:-1].strip()
    text = text.lstrip("#").strip()
    # 去掉「标题：xxx」这类前缀
    for prefix in ("标题：", "标题:", "对话标题：", "对话标题:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.strip("。.！!？?；;，,、 \t")
    return text[:MAX_TITLE_CHARS]


__all__ = [
    "AGENT_SETTING_KEY",
    "MODEL_SETTING_KEY",
    "AUTO_SETTING_KEY",
    "MAX_TITLE_CHARS",
    "TitleSettings",
    "read_settings",
    "write_settings",
    "schedule",
    "generate",
    "wait_for_pending",
    "cancel_pending",
]
