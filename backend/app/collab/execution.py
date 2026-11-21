"""执行边界：协作域里**唯一**接触 ``runtime`` / ``agents`` 的模块。

它做四件事，其余一律不管（编排决策在 ``engine.py``）：

1. 把「节点绑定的智能体 → 适配器定义」解析出来（读库，只读）；
2. 组装 :class:`AgentContext` —— 这里是「不共享记忆」的执行侧落地：
   ``history=[]``、``session_id=None``，每次步骤都是全新会话；
3. 调用 :func:`runtime.runner.stream_run` 拿规范化事件流（取消、超时、命令脱敏全部复用）；
4. 越权审计：对只读/无目录的节点，比对共享工作目录运行前后的指纹。

把这个文件收窄的意义：框架怎么跑智能体、技能怎么物化，将来怎么变，都只在这里适配。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import AgentContext, AgentDefinition, get_agent
from ..agents.events import AgentEvent
from ..credentials import with_notice
from ..models import AgentProfile, Gateway, new_id
from ..runtime.credentials import decrypt_for_profile
from ..runtime.gateway import bind_gateway
from ..runtime.manager import ActiveRun
from ..runtime.mcp import prepare_mcp_home
from ..runtime.runner import stream_run
from ..runtime.skills import prepare_skill_home
from ..settings import get_settings
from .graph import budget_from_storable, effective_workspace_scope
from .schemas import NodeSpec, WorkspaceScope
from .tables import CollabRun

#: 审计快照的体积上限（避免把巨大的工作目录翻一遍）
MAX_AUDIT_FILES = 2000
MAX_HASH_BYTES = 512_000


class StepError(Exception):
    """这一步没配好或没法执行（消息面向用户）。"""


@dataclass(slots=True)
class PreparedStep:
    profile: AgentProfile
    definition: AgentDefinition
    ctx: AgentContext
    workspace: str
    scope: WorkspaceScope
    #: 本次步骤的单步限时（秒）：负数 = 不限，正数 = 按该秒数限死。
    #: 预算里的 0 表示「不限」，在 prepare_step / prepare_chat 时已转成 -1，不会落到
    #: runner 的「0 = 全局默认」哨兵上（见 :func:`_runner_step_timeout`）。
    step_timeout_seconds: int = 0
    #: 需要以 notice 暴露给用户的说明（技能 / MCP 物化过程中的告警与提示）
    warnings: list[str] = field(default_factory=list)
    #: 共享工作目录在运行前的指纹（只读节点才做）
    audit_before: dict[str, Any] = field(default_factory=dict)
    audit_root: str | None = None


class StepRegistry:
    """运行中的步骤句柄表（供取消与「正在跑什么」查询）。

    复用 ``runtime.manager.ActiveRun``：它是执行层对「一次运行」的最小描述，
    同时也是 ``stream_run`` 要求的入参类型，因此不必在协作域再造一个。
    该类的 ``conversation_id`` 在这里承载的是**协作运行 ID**（同一个字段，
    两种运行作用域都用它来标识"这次运行属于谁"）。
    """

    def __init__(self) -> None:
        self._handles: dict[str, ActiveRun] = {}

    def register(
        self, run_id: str, step_id: str, agent_name: str, node_key: str | None = None
    ) -> ActiveRun:
        handle = ActiveRun(id=new_id()[:12], conversation_id=run_id, agent_name=agent_name)
        handle.step_id = step_id  # 动态属性：仅供协作域自己查，不影响执行层
        handle.node_key = node_key or ""  # 动态属性：聊天与工单步骤可分别追踪岗位
        self._handles[step_id] = handle
        return handle

    def finish(self, step_id: str) -> None:
        handle = self._handles.pop(step_id, None)
        if handle is not None:
            handle.finished = True

    def clear(self) -> None:
        """清空整张句柄表（测试隔离用：用例收尾时兜底，防泄漏进下一个用例）。"""
        for handle in self._handles.values():
            handle.finished = True
        self._handles.clear()

    def active_for(self, run_id: str) -> list[ActiveRun]:
        return [h for h in self._handles.values() if h.conversation_id == run_id and not h.finished]

    def list_active(self) -> list[ActiveRun]:
        """本进程里**所有**还在跑的句柄（跨运行，给「正在运行的智能体」汇总用）。"""
        return [h for h in self._handles.values() if not h.finished]

    def active_for_node(self, run_id: str, node_key: str) -> list[ActiveRun]:
        """这个运行下、某个岗位上仍在跑的句柄（聊天通道用来避免同一岗位并发）。"""
        return [h for h in self.active_for(run_id) if getattr(h, "node_key", "") == node_key]

    def cancel_run(self, run_id: str) -> bool:
        handles = self.active_for(run_id)
        for handle in handles:
            handle.cancel_event.set()
            process = handle.process
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:  # 进程已经退出
                    pass
        return bool(handles)

    def cancel_step(self, step_id: str) -> bool:
        handle = self._handles.get(step_id)
        if handle is None:
            return False
        handle.cancel_event.set()
        if handle.process is not None and handle.process.returncode is None:
            try:
                handle.process.kill()
            except ProcessLookupError:
                pass
        return True


#: 工单步骤的句柄表：主流程串行推进，同一时刻最多一个。
registry = StepRegistry()
#: 入口智能体聊天的句柄表：与主流程完全分开，聊天不会挡住工单推进，反之亦然。
chat_registry = StepRegistry()


# --- 工作目录 ---------------------------------------------------------------


def workspace_root(run: CollabRun) -> Path:
    """本次协作任务的工作路径（不落盘）：创建任务时即已确定（任务指定 > 团队默认 > 按运行 ID 分配）。"""
    raw = run.workspace_dir or str(get_settings().workspaces_root / f"collab-{run.id}")
    return Path(raw).expanduser().resolve()


def ensure_workspace(run: CollabRun) -> Path:
    """确保工作路径存在且可用；不可用时给出面向用户的中文错误。"""
    path = workspace_root(run)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StepError(f"工作路径不可用：{path}（{exc}）") from exc
    if not path.is_dir():
        raise StepError(f"工作路径不是目录：{path}")
    return path


def _step_cwd(run: CollabRun, node: NodeSpec) -> tuple[str, WorkspaceScope]:
    """所有岗位都以团队工作路径为 cwd；读写范围由 ``workspace_scope`` 约束。

    路径统一后，智能体之间不会各自落到不同的临时目录；``none`` / ``read`` 仍按原语义
    通过提示词约束并对共享目录做越权审计，违反时不会静默放过。
    """
    scope = effective_workspace_scope(node)
    return str(ensure_workspace(run)), scope


def snapshot_workspace(root: Path) -> dict[str, Any]:
    """工作目录指纹：路径 + 大小 + 修改时间（小文件再算内容哈希）。"""
    entries: dict[str, str] = {}
    truncated = False
    try:
        for index, path in enumerate(sorted(root.rglob("*"))):
            if index >= MAX_AUDIT_FILES:
                truncated = True
                break
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
                if stat.st_size <= MAX_HASH_BYTES:
                    stamp += ":" + hashlib.blake2b(path.read_bytes(), digest_size=8).hexdigest()
                entries[str(path.relative_to(root))] = stamp
            except OSError:
                continue
    except OSError:
        return {"entries": {}, "truncated": False, "error": "无法读取工作目录"}
    return {"entries": entries, "truncated": truncated}


def audit_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """比对两次指纹，找出被改动/新增/删除的文件。"""
    old = before.get("entries") or {}
    new = after.get("entries") or {}
    changed = sorted(
        path
        for path, stamp in new.items()
        if path in old and old[path] != stamp
    )
    added = sorted(path for path in new if path not in old)
    removed = sorted(path for path in old if path not in new)
    violations = added + changed + removed
    return {
        "checked": True,
        "unchanged": not violations,
        "added": added[:50],
        "modified": changed[:50],
        "removed": removed[:50],
        "count": len(violations),
        "truncated": bool(before.get("truncated") or after.get("truncated")),
    }


# --- 步骤准备与执行 ---------------------------------------------------------


def _home_scope(run_id: str, profile_id: str, channel: str) -> str:
    """内核隔离「家」的作用域：运行 × 智能体 × 通道。

    与对话链路同构（一次对话就是「一个智能体的一段隔离配置」）：协作的聊天通道与工单通道
    并行（见 :func:`engine.prepare_chat`），同一个运行里不同岗位也可能共用同一个内核。
    三者合一，既保证「本次运行生效的技能 / MCP == 该岗位分配到的」，也不会互相覆盖隔离目录。
    """
    return f"{run_id}-{profile_id}-{channel}"


@dataclass(slots=True)
class ResolvedProfile:
    """一个岗位解析出来的智能体与运行参数（工单步骤与聊天共用）。"""

    profile: AgentProfile
    definition: AgentDefinition
    options: dict[str, Any]
    model: str | None
    gateway: str | None
    api_key: str | None
    auth_style: str | None
    #: 智能体凭据（解密后的「环境变量名 → 明文」）
    credentials: dict[str, str]
    cwd: str
    scope: WorkspaceScope


async def _resolve_profile(
    session: AsyncSession, *, run: CollabRun, node: NodeSpec
) -> ResolvedProfile:
    """把节点上的智能体与运行参数解析出来（步骤与聊天共用）。"""
    profile = await session.get(AgentProfile, node.agent_profile_id)
    if profile is None or profile.deleted_at is not None:
        raise StepError(f"节点「{node.label or node.key}」绑定的智能体已不存在")
    if not profile.enabled:
        raise StepError(f"智能体「{profile.name}」已停用")

    try:
        definition = get_agent(profile.type_id)
    except KeyError as exc:
        raise StepError(str(exc)) from None

    gateway_row = await session.get(Gateway, profile.gateway_id) if profile.gateway_id else None
    binding = bind_gateway(definition, gateway_row)

    # 节点不再覆盖适配器参数：一个岗位用什么参数，完全由它绑定的智能体决定（与对话一致）。
    options = definition.coerce_options(profile.options or {})

    model = profile.model or definition.resolve_model(None)
    cwd, scope = _step_cwd(run, node)

    return ResolvedProfile(
        profile=profile,
        definition=definition,
        options=options,
        model=model,
        gateway=binding.url,
        api_key=binding.api_key,
        auth_style=binding.auth_style,
        credentials=await decrypt_for_profile(session, profile.id),
        cwd=cwd,
        scope=scope,
    )


async def _build_context(
    session: AsyncSession,
    *,
    resolved: ResolvedProfile,
    home_scope: str,
    prompt: str,
    history: list[dict[str, str]] | None = None,
    node: NodeSpec | None = None,
) -> tuple[AgentContext, list[str]]:
    """组装一次运行的上下文，并把分配到的技能与 MCP 物化进该内核的隔离目录。

    物化走的是与对话链路（``runtime.service.prepare_run``）**完全相同的一条路径**：
    技能先落、MCP 复用同一个「家」，物化告警原样交回调用方，由运行层作为 notice 暴露。
    节点带了协作专属分配（``skill_ids`` / ``mcp_ids`` 非 None）时以节点为准 ——
    只影响本次协作运行，不写回智能体页面的配置。
    """
    profile = resolved.profile
    definition = resolved.definition

    text = prompt
    # 凭据只以环境变量注入（值不进提示词），人格里只加一段「可用凭据」的名字清单。
    system_prompt = with_notice(profile.system_prompt or None, resolved.credentials)
    if system_prompt and not definition.capabilities.native_system_prompt:
        # 适配器不接受独立人格参数时，把人格折进这一轮的输入（同对话链路的做法）
        text = f"[system]\n{system_prompt}\n\n{prompt}"
        system_prompt = None

    ctx = AgentContext(
        agent_id=profile.id,
        agent_name=profile.name,
        options=resolved.options,
        gateway_url=resolved.gateway,
        api_key=resolved.api_key,
        auth_style=resolved.auth_style,
        model=resolved.model,
        system_prompt=system_prompt,
        credentials=resolved.credentials,
        workspace_dir=resolved.cwd,
        # 「不共享记忆」：每次步骤都是全新会话，历史只走工单载荷
        session_id=None,
        history=list(history or []),
        prompt=text,
    )
    # 技能与 MCP 共用一个「家」：先物化技能定下 home_dir，MCP 再复用它（顺序不能反）。
    # 协作专属分配（node.skill_ids / node.mcp_ids 非 None）在这条路径上覆盖默认来源。
    warnings = await prepare_skill_home(
        session, definition, profile, None, ctx,
        scope_id=home_scope, override_ids=node.skill_ids if node else None,
    )
    warnings += await prepare_mcp_home(
        session, definition, profile, None, ctx,
        scope_id=home_scope, override_ids=node.mcp_ids if node else None,
    )
    return ctx, warnings


def _runner_step_timeout(budget_value: int) -> int:
    """把预算里的单步限时翻译成 runner 的语义。

    预算对用户承诺「0 = 不限」（``BudgetSpec.step_timeout_seconds`` 的 ``ge=0`` 只允许非负数），
    而 ``runner`` 的哨兵是：0 = 这次没指定（回落全局默认）、负数 = 不限。
    因此 0 必须在这里转成 -1；否则用户看到「不限」，实际却落到全局 ``run_timeout_seconds``。
    （全局默认如今是「不限时」，但这条翻译仍然必要：部署方一旦给对话配了护栏，
    协作域的单步「不限」不能被它顺带掐断。）
    """
    return -1 if budget_value == 0 else budget_value


async def prepare_step(
    session: AsyncSession,
    *,
    run: CollabRun,
    node: NodeSpec,
    step_id: str,
    prompt: str,
) -> PreparedStep:
    """解析智能体与智能体、组装一次工单步骤的上下文。"""
    resolved = await _resolve_profile(session, run=run, node=node)
    ctx, warnings = await _build_context(
        session,
        resolved=resolved,
        home_scope=_home_scope(run.id, resolved.profile.id, "step"),
        prompt=prompt,
        node=node,
    )

    prepared = PreparedStep(
        profile=resolved.profile,
        definition=resolved.definition,
        ctx=ctx,
        workspace=resolved.cwd,
        scope=resolved.scope,
        step_timeout_seconds=_runner_step_timeout(
            budget_from_storable(run.budget).step_timeout_seconds
        ),
        warnings=warnings,
    )
    if resolved.scope is not WorkspaceScope.WRITE:
        prepared.audit_root = resolved.cwd
        prepared.audit_before = snapshot_workspace(Path(prepared.audit_root))
    return prepared


async def prepare_chat(
    session: AsyncSession,
    *,
    run: CollabRun,
    node: NodeSpec,
    prompt: str,
) -> PreparedStep:
    """组装入口智能体的一次聊天上下文。

    与工单步骤共用智能体 / 适配器解析、技能与 MCP 物化，但**不做越权审计**。聊天历史已经由
    ``prompt.build_chat_prompt`` 折进这一轮正文；这里仍保持 ``session_id=None``，
    避免不同设备 / 重试时悄悄续接到旧服务端会话。
    """
    resolved = await _resolve_profile(session, run=run, node=node)
    ctx, warnings = await _build_context(
        session,
        resolved=resolved,
        home_scope=_home_scope(run.id, resolved.profile.id, "chat"),
        prompt=prompt,
        node=node,
    )
    return PreparedStep(
        profile=resolved.profile,
        definition=resolved.definition,
        ctx=ctx,
        workspace=resolved.cwd,
        scope=resolved.scope,
        step_timeout_seconds=_runner_step_timeout(
            budget_from_storable(run.budget).step_timeout_seconds
        ),
        warnings=warnings,
    )


async def stream_step(prepared: PreparedStep, handle: ActiveRun) -> AsyncIterator[AgentEvent]:
    """把一次步骤变成规范化事件流（取消/超时/脱敏都由执行层负责）。

    单步限时在这一刻落到 handle 上：运行中调预算改的是**下一步**的时限，
    正在跑的这一步按它开跑时的值算。
    """
    handle.timeout_seconds = prepared.step_timeout_seconds
    async for event in stream_run(prepared.definition, prepared.ctx, handle):
        yield event


def finish_audit(prepared: PreparedStep) -> dict[str, Any]:
    """只读节点运行结束后的越权检查。"""
    if not prepared.audit_root:
        return {}
    after = snapshot_workspace(Path(prepared.audit_root))
    return audit_diff(prepared.audit_before, after)


def cancel_run(run_id: str) -> bool:
    """取消这个协作运行下所有还在跑的工单步骤与聊天回复。"""
    main = registry.cancel_run(run_id)
    chat = chat_registry.cancel_run(run_id)
    return main or chat


def cancel_step(step_id: str) -> bool:
    return registry.cancel_step(step_id)


def is_running(run_id: str) -> bool:
    return bool(registry.active_for(run_id))


def is_chat_running(run_id: str) -> bool:
    return bool(chat_registry.active_for(run_id))


def active_runs() -> list[dict[str, Any]]:
    """本进程里正在跑的协作智能体：工单步骤 + 入口智能体的对话回复。

    协作的运行登记在**协作域自己这里**（``registry`` / ``chat_registry``），不在对话域那份
    ``runtime.manager.registry`` 里 —— 所以「正在运行的智能体」只看对话域的话，协作里跑着的
    岗位一个都不会出现。这是一份跨运行的汇总，由协作域自己给出（框架层不反向依赖协作域）。

    ``run_id`` 就是协作运行 id（``ActiveRun.conversation_id`` 在协作域承载的是它）。
    """
    summary: list[dict[str, Any]] = []
    for kind, handles in (("step", registry), ("chat", chat_registry)):
        for handle in handles.list_active():
            snapshot = handle.snapshot()
            summary.append(
                {
                    "run_id": snapshot["conversation_id"],
                    "agent_name": snapshot["agent_name"],
                    "kind": kind,
                    "node_key": getattr(handle, "node_key", "") or None,
                    "elapsed_seconds": snapshot["elapsed_seconds"],
                }
            )
    return summary


def cancel_chat(run_id: str) -> bool:
    return chat_registry.cancel_run(run_id)
