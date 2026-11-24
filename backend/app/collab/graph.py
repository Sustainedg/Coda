"""编排图的查询与校验（纯函数：只依赖 ``schemas`` 与标准库，可脱离数据库单测）。

校验分两层含义：

* **能跑起来**（errors）：必须有唯一入口、每个节点可达、审查边必须与回边成对…
* **可能不是你想要的**（warnings）：比如某个节点从入口走不到、审查者没有可打回的对象。

保存草稿时不拦 errors（编辑器要能边画边存），发起运行时才拒绝。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from .schemas import (
    BudgetSpec,
    Duty,
    EdgeKind,
    EdgeSpec,
    GraphIssue,
    GraphSpec,
    GraphValidation,
    NodeSpec,
    TicketAction,
    WorkspaceScope,
)

#: 各岗位默认能发起的动作。
#: 三个岗位都默认带上 escalate：卡住了就往发起人上报，是「不静默、不硬撑」的最后一条退路
#: （真实事故：实施者只能 done，被阻塞后唯一出口是把失败写进结论，上游只能反复重派）。
DUTY_OUTPUT_CONTRACT: dict[Duty, tuple[TicketAction, ...]] = {
    Duty.COORDINATOR: (
        TicketAction.DELEGATE,
        TicketAction.ASK_USER,
        TicketAction.ESCALATE,
        TicketAction.DONE,
        TicketAction.REJECT,
    ),
    Duty.ENGINEER: (
        TicketAction.SUBMIT,
        TicketAction.DELEGATE,
        TicketAction.ESCALATE,
        TicketAction.REJECT,
    ),
    Duty.REVIEWER: (TicketAction.REVIEW, TicketAction.ESCALATE, TicketAction.REJECT),
}

#: 各岗位默认的工作目录范围：协调者拿不到目录，审查者只读
DUTY_WORKSPACE_SCOPE: dict[Duty, WorkspaceScope] = {
    Duty.COORDINATOR: WorkspaceScope.NONE,
    Duty.ENGINEER: WorkspaceScope.WRITE,
    Duty.REVIEWER: WorkspaceScope.READ,
}

#: 每种边的两端允许的岗位：(起点岗位集合, 终点岗位集合)
#: 注意 rework 的终点允许协调者：审查意见可以先交回上级，再由上级组织返工
#: （需求里的原话就是「审查意见交回给 CTO，CTO 继续交给 SE 完善」）。
EDGE_DUTIES: dict[EdgeKind, tuple[tuple[Duty, ...], tuple[Duty, ...]]] = {
    EdgeKind.ASSIGN: ((Duty.COORDINATOR, Duty.ENGINEER), (Duty.COORDINATOR, Duty.ENGINEER)),
    EdgeKind.REVIEW: ((Duty.ENGINEER,), (Duty.REVIEWER,)),
    EdgeKind.REWORK: ((Duty.REVIEWER,), (Duty.ENGINEER, Duty.COORDINATOR)),
}


# --- 存储形态互转 -----------------------------------------------------------


def to_storable(graph: GraphSpec) -> dict[str, Any]:
    """把图序列化成可落库的纯 JSON（枚举转成字符串）。"""
    return graph.model_dump(mode="json")


def from_storable(data: Any) -> GraphSpec:
    """把库里的 JSON 还原成图；历史数据形状不对时抛 ``ValueError``。"""
    if not isinstance(data, dict):
        raise ValueError("编排图数据不是对象")
    return GraphSpec.model_validate(data)


def has_graph_snapshot(data: Any) -> bool:
    """这份运行快照是否真的能跑（至少有一个节点）。

    空图 / 脏数据一律当作「还没有快照」：运行刚建好时协作组可能还是空草稿，
    用户补完图后应当能直接开跑，而不是被一张空快照钉死。
    """
    return isinstance(data, dict) and bool(data.get("nodes"))


def run_graph_source(run_graph: Any, team_graph: Any) -> Any:
    """这次运行该用哪张图：优先运行自己的快照，没有快照的老运行才回退协作组现图。"""
    return run_graph if has_graph_snapshot(run_graph) else team_graph


def budget_to_storable(budget: BudgetSpec) -> dict[str, Any]:
    return budget.model_dump(mode="json")


def budget_from_storable(data: Any) -> BudgetSpec:
    if not isinstance(data, dict):
        return BudgetSpec()
    return BudgetSpec.model_validate(data)


# --- 查询 -------------------------------------------------------------------


def node_map(graph: GraphSpec) -> dict[str, NodeSpec]:
    return {node.key: node for node in graph.nodes}


def entry_node(graph: GraphSpec, entry_key: str | None) -> NodeSpec | None:
    if not entry_key:
        return None
    return node_map(graph).get(entry_key)


def outgoing(graph: GraphSpec, key: str, kind: EdgeKind | None = None) -> list[EdgeSpec]:
    return [e for e in graph.edges if e.from_key == key and (kind is None or e.kind is kind)]


def incoming(graph: GraphSpec, key: str, kind: EdgeKind | None = None) -> list[EdgeSpec]:
    return [e for e in graph.edges if e.to_key == key and (kind is None or e.kind is kind)]


def allowed_assignees(graph: GraphSpec, key: str) -> list[str]:
    """该节点允许把工单交给谁（``assign`` 出边）。"""
    return sorted({e.to_key for e in outgoing(graph, key, EdgeKind.ASSIGN)})


def reviewer_of(graph: GraphSpec, key: str) -> str | None:
    """该节点提交后由谁审查（``review`` 出边；一张图里只允许一条）。"""
    edges = outgoing(graph, key, EdgeKind.REVIEW)
    return edges[0].to_key if edges else None


def rework_targets(graph: GraphSpec, key: str) -> list[str]:
    """审查者可以把工单打回给谁（``rework`` 出边）。"""
    return sorted({e.to_key for e in outgoing(graph, key, EdgeKind.REWORK)})


def rework_edge(graph: GraphSpec, from_key: str, to_key: str) -> EdgeSpec | None:
    """审查者 → 打回目标的那条边（用来读边级的返工上限）。"""
    for edge in graph.edges:
        if edge.kind is EdgeKind.REWORK and edge.from_key == from_key and edge.to_key == to_key:
            return edge
    return None


def effective_output_contract(
    node: NodeSpec, *, is_entry: bool, escalated: bool = False
) -> tuple[TicketAction, ...]:
    """该节点本轮真正允许的动作。

    三条规则只在这里给出，提示词与运行时校验才会一致：

    * ``ask_user`` 只有入口节点能用（用户只和入口智能体对话）——非入口节点即使在编辑器里
      勾了它也要剔除，避免「提示词说能做、运行时又报只有入口能提问」的自相矛盾；
    * ``escalate`` 反过来：**非入口节点无条件保留**。它是「我搞不定」的保底出路，
      不能被节点契约配成死路（真实事故：实施者的生效动作只剩 done，卡住后只能硬撑）；
      入口节点是链路顶端，没有更上级可报，由 ``ask_user`` 收口，所以剔除；
    * ``escalated``（这张工单是被下游上报上来的）时剔除 ``delegate``：上抛件只能
      「自己解决 / 继续往上 / 交给用户」，不许把阻塞换个说法再往下压
      （真实事故：CTO 把「跑不了」改写成「静态核对」又派给码农，白转一轮）。
    """
    contract = (
        tuple(node.output_contract) if node.output_contract else DUTY_OUTPUT_CONTRACT[node.duty]
    )
    if is_entry:
        return tuple(action for action in contract if action is not TicketAction.ESCALATE)
    if TicketAction.ESCALATE not in contract:
        contract = (*contract, TicketAction.ESCALATE)
    contract = tuple(action for action in contract if action is not TicketAction.ASK_USER)
    if escalated:
        contract = tuple(action for action in contract if action is not TicketAction.DELEGATE)
    return contract


def effective_workspace_scope(node: NodeSpec) -> WorkspaceScope:
    return node.workspace_scope or DUTY_WORKSPACE_SCOPE[node.duty]


def effective_max_rounds(node: NodeSpec, budget: BudgetSpec) -> int:
    return min(node.max_rounds, budget.max_rounds_per_ticket) if node.max_rounds else budget.max_rounds_per_ticket


# --- 校验 -------------------------------------------------------------------


def _reachable(graph: GraphSpec, start: str) -> set[str]:
    seen = {start}
    queue: deque[str] = deque([start])
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.from_key, []).append(edge.to_key)
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _has_assign_cycle(graph: GraphSpec, keys: set[str]) -> list[str]:
    """返回参与 ``assign`` 环的节点 key（去重）。"""
    adjacency: dict[str, list[str]] = {key: [] for key in keys}
    for edge in graph.edges:
        if edge.kind is EdgeKind.ASSIGN and edge.from_key in adjacency and edge.to_key in adjacency:
            adjacency[edge.from_key].append(edge.to_key)

    color: dict[str, int] = {}  # 0=未见 1=在栈上 2=已完成
    cyclic: set[str] = set()

    def visit(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                if color.get(node) == 2:
                    continue
                color[node] = 1
                path.append(node)
            neighbors = adjacency.get(node, [])
            if index < len(neighbors):
                stack.append((node, index + 1))
                nxt = neighbors[index]
                if color.get(nxt) == 1:
                    cyclic.update(path[path.index(nxt):])
                elif color.get(nxt) != 2:
                    stack.append((nxt, 0))
            else:
                color[node] = 2
                if path and path[-1] == node:
                    path.pop()

    for key in sorted(keys):
        if color.get(key) is None:
            visit(key)
    return sorted(cyclic)


def validate_graph(
    graph: GraphSpec, entry_key: str | None, known_agent_profile_ids: set[str] | None = None
) -> GraphValidation:
    errors: list[GraphIssue] = []
    warnings: list[GraphIssue] = []

    if not graph.nodes:
        errors.append(GraphIssue(code="graph.empty", message="编排图还没有节点，先拖一个智能体进来"))
        return GraphValidation.build(errors, warnings)

    nodes = node_map(graph)
    if len(nodes) != len(graph.nodes):
        seen: set[str] = set()
        for node in graph.nodes:
            if node.key in seen:
                errors.append(
                    GraphIssue(code="node.duplicate_key", message=f"节点 key 重复：{node.key}", node_key=node.key)
                )
            seen.add(node.key)

    if known_agent_profile_ids is not None:
        for node in graph.nodes:
            if node.agent_profile_id not in known_agent_profile_ids:
                errors.append(
                    GraphIssue(
                        code="node.agent_missing",
                        message=f"节点「{node.label or node.key}」绑定的智能体已经不存在了，请重新选择",
                        node_key=node.key,
                    )
                )

    edge_ids: set[str] = set()
    for edge in graph.edges:
        if edge.id in edge_ids:
            errors.append(
                GraphIssue(code="edge.duplicate_id", message=f"边 ID 重复：{edge.id}", edge_id=edge.id)
            )
        edge_ids.add(edge.id)

        if edge.from_key == edge.to_key:
            errors.append(
                GraphIssue(
                    code="edge.self_loop",
                    message=f"边不能自己连自己：{edge.from_key}",
                    node_key=edge.from_key,
                    edge_id=edge.id,
                )
            )
            continue

        start = nodes.get(edge.from_key)
        end = nodes.get(edge.to_key)
        if start is None or end is None:
            missing = edge.from_key if start is None else edge.to_key
            errors.append(
                GraphIssue(
                    code="edge.dangling",
                    message=f"边指向了不存在的节点：{missing}",
                    edge_id=edge.id,
                )
            )
            continue

        allowed_starts, allowed_ends = EDGE_DUTIES[edge.kind]
        if start.duty not in allowed_starts or end.duty not in allowed_ends:
            errors.append(
                GraphIssue(
                    code="edge.duty_mismatch",
                    message=(
                        f"「{start.label or start.key}」（{start.duty.value}）→"
                        f"「{end.label or end.key}」（{end.duty.value}）不允许这条 {edge.kind.value} 边"
                    ),
                    edge_id=edge.id,
                )
            )

    # 入口
    entry = entry_node(graph, entry_key)
    if not entry_key:
        errors.append(GraphIssue(code="entry.missing", message="还没有指定入口节点（唯一能和用户对话的智能体）"))
    elif entry is None:
        errors.append(GraphIssue(code="entry.not_found", message=f"入口节点不存在：{entry_key}"))
    elif entry.duty is not Duty.COORDINATOR:
        errors.append(
            GraphIssue(
                code="entry.duty",
                message=f"入口节点「{entry.label or entry.key}」必须是协调者（coordinator）",
                node_key=entry.key,
            )
        )

    # 每个节点都要有入边（入口除外），且入口之外都要与入口连通
    reachable = _reachable(graph, entry.key) if entry else set()
    duty_names = {Duty.COORDINATOR: "协调者", Duty.ENGINEER: "实施者", Duty.REVIEWER: "审查者"}
    for node in graph.nodes:
        # 入口节点由「用户」驱动，本来就不需要入边。
        if entry is not None and node.key == entry.key:
            continue
        if not incoming(graph, node.key):
            errors.append(
                GraphIssue(
                    code="node.orphan",
                    message=f"节点「{node.label or node.key}」没有任何入边，工单流不到它",
                    node_key=node.key,
                )
            )
        if entry is not None and node.key != entry.key and node.key not in reachable:
            warnings.append(
                GraphIssue(
                    code="node.unreachable",
                    message=f"节点「{node.label or node.key}」从入口开始走不到，可能白配了",
                    node_key=node.key,
                )
            )

    if not any(node.duty is Duty.ENGINEER for node in graph.nodes):
        errors.append(GraphIssue(code="graph.no_engineer", message="至少要有一个实施者节点，否则没人真正干活"))

    # 审查闭环：有 review 边就必须有退路（rework），有 rework 边就必须有来路（review）。
    # 打回目标允许不是提交者本人 —— 例如「审查员把意见交回 CTO，由 CTO 再派给 SE」。
    # 只有一条退路时引擎按它自动打回，不需要 rework_to；有多条退路才要求显式指定（不猜）。
    review_edges = [e for e in graph.edges if e.kind is EdgeKind.REVIEW]
    rework_edges = [e for e in graph.edges if e.kind is EdgeKind.REWORK]
    for edge in review_edges:
        exits = [r for r in rework_edges if r.from_key == edge.to_key]
        if not exits:
            errors.append(
                GraphIssue(
                    code="edge.review_without_rework",
                    message=(
                        f"「{edge.to_key}」会审查「{edge.from_key}」的产出，但它没有任何 rework 出边"
                        "：审查发现问题时无路可退"
                    ),
                    node_key=edge.to_key,
                    edge_id=edge.id,
                )
            )
        for exit_edge in exits:
            if exit_edge.to_key != edge.from_key and len(rework_targets(graph, edge.to_key)) > 1:
                warnings.append(
                    GraphIssue(
                        code="edge.rework_target_not_submitter",
                        message=(
                            f"打回目标「{exit_edge.to_key}」不是提交者「{edge.from_key}」："
                            "该审查者有多条退路，给出 changes_requested 时必须用 rework_to 指明打给谁"
                        ),
                        edge_id=exit_edge.id,
                    )
                )
    for edge in rework_edges:
        if not any(x.to_key == edge.from_key for x in review_edges):
            errors.append(
                GraphIssue(
                    code="edge.rework_without_review",
                    message=f"rework 回边（{edge.from_key} → {edge.to_key}）没有对应的 review 边，打回的对象不对",
                    edge_id=edge.id,
                )
            )

    for node in graph.nodes:
        if (
            entry is not None
            and node.key != entry.key
            and TicketAction.ASK_USER in node.output_contract
        ):
            warnings.append(
                GraphIssue(
                    code="node.ask_user_not_entry",
                    message=(
                        f"「{node.label or node.key}」不是入口节点，勾选的 ask_user 会被忽略："
                        "只有入口节点能向用户提问"
                    ),
                    node_key=node.key,
                )
            )
        if node.duty is Duty.REVIEWER and len(rework_targets(graph, node.key)) > 1:
            warnings.append(
                GraphIssue(
                    code="node.multiple_rework_targets",
                    message=(
                        f"审查者「{node.label or node.key}」有多条打回路径，"
                        "给出 changes_requested 时必须在工单里用 rework_to 指明目标"
                    ),
                    node_key=node.key,
                )
            )

    # assign 边不允许成环（要循环必须走 review/rework 这套显式闭环）
    for key in _has_assign_cycle(graph, set(nodes)):
        errors.append(
            GraphIssue(
                code="edge.assign_cycle",
                message=f"assign 边形成了环（包含「{key}」）：派发方向必须能收敛，返工请用 review/rework 边",
                node_key=key,
            )
        )

    # 岗位与边的相容性（这里只做提示，硬约束由 EDGE_DUTIES 覆盖）
    for node in graph.nodes:
        if node.duty is Duty.REVIEWER and not incoming(graph, node.key, EdgeKind.REVIEW):
            warnings.append(
                GraphIssue(
                    code="node.reviewer_unused",
                    message=f"审查者「{node.label or node.key}」没有收到任何 review 边，它没有东西可审",
                    node_key=node.key,
                )
            )
        if node.duty is Duty.REVIEWER and not rework_targets(graph, node.key):
            warnings.append(
                GraphIssue(
                    code="node.reviewer_no_rework",
                    message=f"审查者「{node.label or node.key}」没有 rework 出边，发现问题时无法打回",
                    node_key=node.key,
                )
            )

    return GraphValidation.build(errors, warnings)
