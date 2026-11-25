/**
 * 协作运行页的纯视图模型：把编排图、步骤与工单折成「谁在哪一步、现在轮到谁」。
 *
 * 桌面端与移动端共用这份计算。主会话只展示用户与入口领导者；其他岗位的完整
 * 对话通过进度卡片点开查看。这里不碰 DOM、不发请求。
 */
import { ACTIONS, EDGE_KINDS, ticketStateMeta } from './collabDraft'
import type { FinishedStep, LiveStep } from './collabStream'
import type {
  CollabEdgeKind,
  CollabEdgePort,
  CollabGraph,
  CollabRunStatus,
  CollabStep,
  CollabTicket,
  AgentProfile,
} from './types'

export type CollabAgentState = 'pending' | 'active' | 'waiting' | 'error' | 'done'

/** 岗位头像的完整三字段：`AgentAvatar` 组件按「自定义图 > 头像包 > emoji」依次回退。 */
export interface CollabAgentFace {
  avatar: string
  avatarPreset?: string | null
  avatarFile?: string | null
}

export interface CollabAgentProgress {
  key: string
  label: string
  avatar: string
  avatarPreset?: string | null
  avatarFile?: string | null
  agentId?: string | null
  duty: string
  isEntry: boolean
  state: CollabAgentState
  stateLabel: string
  stepCount: number
  summary: string
  lastAction?: string
  isLive: boolean
  activeTickets: CollabTicket[]
  /** 编排画布上保存的坐标（画布内坐标）；旧图没拖过时为空，进度图回退自动分层。 */
  x?: number | null
  y?: number | null
}

/** 各状态对应的「粗略进度」底数——后端没有真实百分比，这里只给进度条一个说法。 */
const STATE_PROGRESS: Record<CollabAgentState, number> = {
  pending: 0,
  active: 45,
  waiting: 62,
  error: 30,
  done: 100,
}

/**
 * 粗估一个岗位跑到了几成：已完成 100%、闲置 0%，干活中按跑过的步数往上抬，
 * 最多停在 96%——免得「还在干」看起来像已经收工。
 */
export function agentProgressPercent(node: CollabAgentProgress): number {
  if (node.state === 'done') return 100
  if (node.state === 'pending') return 0
  return Math.min(96, STATE_PROGRESS[node.state] + node.stepCount * 9)
}

export interface CollabFlowEdge {
  id: string
  /** 起点岗位的显示名（给「协作流向」那排标签用） */
  from: string
  to: string
  /** 起点 / 终点的岗位 key（画流程图连线要用，显示名可能重名） */
  fromKey: string
  toKey: string
  kind: CollabEdgeKind
  kindLabel: string
  /** 画布端口方向（只影响外观）；旧图为空时进度图按节点相对位置自动推断。 */
  fromSide?: CollabEdgePort | null
  toSide?: CollabEdgePort | null
}

/** 流程图里的一条连线：把相邻岗位接起来；回流（审查打回 / 返工）单独标记。 */
export interface CollabFlowLink {
  id: string
  fromKey: string
  toKey: string
  kind: CollabEdgeKind
  kindLabel: string
  /** true = 回头箭头：目标在流程里排在源的前面（审查打回、返工），流程图上要单独画。 */
  isLoop: boolean
  /** 画布端口方向（只影响外观）；旧图为空时进度图按节点相对位置自动推断。 */
  fromSide?: CollabEdgePort | null
  toSide?: CollabEdgePort | null
}

/**
 * 把图的边整理成流程图用的连线。
 *
 * 岗位的顺序来自 :func:`orderedCollabNodes`（从入口做广度遍历），所以「目标排在源之后」
 * 就是顺流，反之就是回流。两端节点都在本次运行图里的边才画。
 */
export function buildFlowLinks(
  nodes: { key: string }[],
  edges: CollabFlowEdge[],
): CollabFlowLink[] {
  const rank = new Map(nodes.map((node, index) => [node.key, index]))
  return edges.flatMap((edge) => {
    const from = rank.get(edge.fromKey)
    const to = rank.get(edge.toKey)
    if (from === undefined || to === undefined) return []
    return [{
      id: edge.id,
      fromKey: edge.fromKey,
      toKey: edge.toKey,
      kind: edge.kind,
      kindLabel: edge.kindLabel,
      isLoop: to <= from,
      fromSide: edge.fromSide ?? null,
      toSide: edge.toSide ?? null,
    }]
  })
}

/** 相邻两个岗位之间那条直接连线（没有就是虚线「流程继续」，说明中间经过别的岗位）。 */
export function linkBetween(
  links: CollabFlowLink[],
  fromKey: string,
  toKey: string,
): CollabFlowLink | undefined {
  return links.find((link) => link.fromKey === fromKey && link.toKey === toKey && !link.isLoop)
}

/** 流程图里的一行（一个层级）：入口这一层只有领导者一个人，往下每层是本层的干活的人。 */
export interface CollabFlowRow<T> {
  rank: number
  nodes: T[]
}

/**
 * 按「谁向谁派活」把人排成一行行：入口是第 0 行（就它一个人），它派活的人在第 1 行，
 * 再下一层在第 2 行 —— 于是「领导者一人一行、下面干活的并排一行」自然就出来了。
 *
 * 只认顺着推进方向的边（就是 :func:`buildFlowLinks` 里 `isLoop === false` 的那些）：
 * 往回走的边（审查打回、返工）不改变层级，否则一行会被硬拽到流程末尾去。
 * 没接进流程的孤立岗位放到第 1 行，不与领导者挤在同一行。
 */
export function buildFlowRows<T extends { key: string }>(
  nodes: T[],
  edges: CollabFlowEdge[],
): CollabFlowRow<T>[] {
  const index = new Map(nodes.map((node, position) => [node.key, position]))
  const rank = new Map(nodes.map((node) => [node.key, 0]))
  const outgoing = new Map<string, CollabFlowEdge[]>()
  for (const edge of edges) {
    if (!index.has(edge.fromKey) || !index.has(edge.toKey)) continue
    const list = outgoing.get(edge.fromKey) ?? []
    list.push(edge)
    outgoing.set(edge.fromKey, list)
  }
  // 顺着「用户能看懂的推进顺序」走一遍就够了：朝后的边只会指向更靠后的岗位，轮到它时层级已经算定。
  nodes.forEach((node) => {
    const from = index.get(node.key)!
    const level = rank.get(node.key)!
    for (const edge of outgoing.get(node.key) ?? []) {
      const to = index.get(edge.toKey)!
      if (to <= from) continue
      rank.set(edge.toKey, Math.max(rank.get(edge.toKey)!, level + 1))
    }
  })
  const rows: CollabFlowRow<T>[] = []
  nodes.forEach((node, position) => {
    const level = position > 0 && rank.get(node.key) === 0 ? 1 : rank.get(node.key)!
    const row = rows.find((item) => item.rank === level)
    if (row) row.nodes.push(node)
    else rows.push({ rank: level, nodes: [node] })
  })
  return rows.sort((left, right) => left.rank - right.rank)
}

/** 相邻两行之间那条箭头要写的动作（派发 / 审查 / 验收…），同一行里最多写三条。 */
export function rowLinkLabels(
  links: CollabFlowLink[],
  rows: CollabFlowRow<{ key: string }>[],
  rowIndex: number,
): string[] {
  if (rowIndex <= 0 || rowIndex >= rows.length) return []
  const above = new Set(rows[rowIndex - 1].nodes.map((node) => node.key))
  const below = new Set(rows[rowIndex].nodes.map((node) => node.key))
  const labels: string[] = []
  for (const link of links) {
    if (link.isLoop || !above.has(link.fromKey) || !below.has(link.toKey)) continue
    if (!labels.includes(link.kindLabel)) labels.push(link.kindLabel)
  }
  return labels.slice(0, 3)
}

interface BuildProgressArgs {
  graph?: CollabGraph | null
  entryKey?: string | null
  steps: CollabStep[]
  tickets: CollabTicket[]
  agents: AgentProfile[]
  /** 并行波次里可能同时有多个岗位在流式输出；单个（串行场景）或数组都接受 */
  live?: LiveStep | LiveStep[] | null
  streamed?: FinishedStep[]
  runStatus: CollabRunStatus
}

const ACTIVE_TICKET_STATES = new Set([
  'open',
  'working',
  'waiting_children',
  'review',
  'rework',
  'escalated',
  'blocked',
])

const ACTION_LABELS = Object.fromEntries(ACTIONS.map((item) => [item.value, item.label]))
const EDGE_LABELS = Object.fromEntries(EDGE_KINDS.map((item) => [item.value, item.label]))

/** 从入口节点做一次广度遍历，得到用户能看懂的推进顺序；孤立节点随后补上。 */
export function orderedCollabNodes(graph: CollabGraph, entryKey?: string | null) {
  const byKey = new Map(graph.nodes.map((node) => [node.key, node]))
  const order = new Map(graph.nodes.map((node, index) => [node.key, index]))
  const adjacency = new Map<string, string[]>()
  for (const edge of graph.edges) {
    const next = adjacency.get(edge.from_key) ?? []
    next.push(edge.to_key)
    adjacency.set(edge.from_key, next)
  }

  const queue = entryKey && byKey.has(entryKey) ? [entryKey] : graph.nodes.slice(0, 1).map((node) => node.key)
  const seen = new Set<string>()
  const keys: string[] = []
  while (queue.length > 0) {
    const key = queue.shift()!
    if (seen.has(key) || !byKey.has(key)) continue
    seen.add(key)
    keys.push(key)
    const next = (adjacency.get(key) ?? []).slice().sort((left, right) => (order.get(left) ?? 0) - (order.get(right) ?? 0))
    queue.push(...next)
  }
  for (const node of graph.nodes) {
    if (!seen.has(node.key)) keys.push(node.key)
  }
  return keys.map((key) => byKey.get(key)!).filter(Boolean)
}

function latestStep(steps: CollabStep[]): CollabStep | undefined {
  return steps.length > 0 ? steps[steps.length - 1] : undefined
}

function snippet(text: string | null | undefined, limit = 72): string {
  const oneLine = (text ?? '').replace(/\s+/g, ' ').trim()
  return oneLine.length > limit ? `${oneLine.slice(0, limit)}…` : oneLine
}

/** 已落库的一步是否已经有可展示的产出（正文或结构化部件）。 */
export function stepHasOutput(step: {
  content?: string | null
  parts?: unknown[] | null
  parts_count?: number
}): boolean {
  // 详情接口不再携带 parts 本体（懒加载），因此「有没有过程」要看 parts_count。
  return Boolean((step.content ?? '').trim()) || (step.parts?.length ?? 0) > 0 || (step.parts_count ?? 0) > 0
}

/**
 * 一步还没产出时，给出一句人话说明它当前的状态。
 *
 * 关键场景：智能体正在跑（`streaming`）但输出只在跑完后才落库，此时点开抽屉若渲染空气泡，
 * 用户会以为「显示处理中却什么都没发生」；中断/出错也要说清楚，避免看起来像卡死。
 */
export function stepStatusHint(status: string, hasOutput: boolean): string | null {
  if (hasOutput) return null
  if (status === 'streaming') return '正在处理中……这一步的输出会在跑完后显示。'
  if (status === 'interrupted') return '这一步被中断，没有留下输出；可在顶栏点「继续」让它重跑。'
  if (status === 'error') return '这一步出错了，没有产出。'
  if (status === 'cancelled') return '这一步被取消。'
  return '这一步没有可展示的输出。'
}

/** 计算一张协作图在本次运行里的进度。 */
export function buildCollabProgress({
  graph,
  entryKey,
  steps,
  tickets,
  agents,
  live,
  streamed = [],
  runStatus,
}: BuildProgressArgs): { nodes: CollabAgentProgress[]; edges: CollabFlowEdge[] } {
  if (!graph || graph.nodes.length === 0) return { nodes: [], edges: [] }
  const liveSteps = live == null ? [] : Array.isArray(live) ? live : [live]

  const agentsById = new Map(agents.map((agent) => [agent.id, agent]))
  const nodes = orderedCollabNodes(graph, entryKey).map((node): CollabAgentProgress => {
    // 同一步骤可能先在 SSE 里跑完（`streamed`），落库详情却还是空空的 streaming 行。
    // 以流式结果为准：剔除被它顶替的落库行，否则岗位会一直显示「正在处理中」。
    const streamedIds = new Set(streamed.map((step) => step.stepId))
    const ownSteps = steps.filter((step) => step.role_key === node.key && !streamedIds.has(step.id))
    const ownStreamed = streamed.filter((step) => step.nodeKey === node.key)
    const last = latestStep(ownSteps)
    const lastStreamed = ownStreamed.length > 0 ? ownStreamed[ownStreamed.length - 1] : undefined
    const stepCount = ownSteps.length + ownStreamed.length
    const activeTickets = tickets.filter(
      (ticket) => ticket.holder_key === node.key && ACTIVE_TICKET_STATES.has(ticket.state),
    )
    const isLive = liveSteps.some((step) => step.nodeKey === node.key)
    const blockedTicket = activeTickets.find((ticket) => ticket.state === 'blocked')
    const activeTicket = blockedTicket ?? activeTickets[0]
    // 运行已不在进行中，但这一步还停在 streaming / 被中断：说明它没跑完（例如服务重启）。
    // 否则界面会一直显示「处理中」，点开却看不到任何输出。
    const stalled =
      !isLive && runStatus !== 'running' && (last?.status === 'streaming' || last?.status === 'interrupted')

    let state: CollabAgentState = 'pending'
    let stateLabel = '未开始'
    if (isLive) {
      state = 'active'
      stateLabel = '实时输出中'
    } else if (blockedTicket) {
      state = 'waiting'
      stateLabel = '等你决定'
    } else if (stalled) {
      state = 'error'
      stateLabel = last?.status === 'interrupted' ? '已中断' : '已停止'
    } else if (activeTicket) {
      state = 'active'
      stateLabel = ticketStateMeta(activeTicket.state).label
    } else if (last?.status === 'error' || lastStreamed?.status === 'error') {
      state = 'error'
      stateLabel = '需处理'
    } else if (stepCount > 0 && runStatus === 'done') {
      state = 'done'
      stateLabel = '已完成'
    } else if (stepCount > 0) {
      state = 'done'
      stateLabel = '已参与'
    }

    const summary = isLive
      ? '正在生成实时输出，点开可查看'
      : lastStreamed?.agent.error ||
        snippet(lastStreamed?.agent.text) ||
        last?.error ||
        last?.gist ||
        (last?.status === 'streaming' ? '正在处理中，点开可查看' : activeTicket?.title) ||
        (stepCount > 0 ? '本轮工作已落库' : '尚未参与本次协作')

    return {
      key: node.key,
      label: node.label || node.key,
      avatar: agentsById.get(node.agent_profile_id)?.avatar ?? '',
      avatarPreset: agentsById.get(node.agent_profile_id)?.avatar_preset ?? null,
      avatarFile: agentsById.get(node.agent_profile_id)?.avatar_file ?? null,
      agentId: node.agent_profile_id,
      duty: node.duty,
      isEntry: node.key === entryKey,
      state,
      stateLabel,
      stepCount,
      summary,
      lastAction: lastStreamed?.action
        ? ACTION_LABELS[lastStreamed.action] ?? lastStreamed.action
        : last?.action
          ? ACTION_LABELS[last.action] ?? last.action
          : undefined,
      isLive,
      activeTickets,
      x: node.x,
      y: node.y,
    }
  })

  const labels = new Map(nodes.map((node) => [node.key, node.label]))
  const edges = graph.edges.map((edge) => ({
    id: edge.id,
    from: labels.get(edge.from_key) ?? edge.from_key,
    to: labels.get(edge.to_key) ?? edge.to_key,
    fromKey: edge.from_key,
    toKey: edge.to_key,
    kind: edge.kind,
    kindLabel: EDGE_LABELS[edge.kind] ?? edge.kind,
    fromSide: edge.from_side ?? null,
    toSide: edge.to_side ?? null,
  }))
  return { nodes, edges }
}
