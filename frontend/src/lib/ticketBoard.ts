/**
 * 工单看板的**纯视图模型**：把一次运行的「工单 + 步骤」折成三栏看板需要的一切。
 *
 * 桌面端（`components/TicketBoard`）与移动端（`mobile/pages/MobileTicketBoardPage`）共用这份计算，
 * 于是「谁把什么交给了谁」「什么算越权」「徽章取哪种色」只有一处定义，两端只各写自己的布局与交互。
 * 本模块不碰 DOM、不发请求、不读全局状态。
 *
 * 数据来源与后端一致：工单来自 `collab_tickets`，流转来自 `collab_steps`（一步一行）；
 * 越权提示直接读 `step.audit` —— 就是后端 `collab/execution.py:audit_diff()` 的返回。
 */
import { ACTIONS, ticketStateMeta } from './collabDraft'
import { stepHasOutput, stepStatusHint } from './collabProgress'
import { summarizeUsage } from './usage'
import type {
  CollabDuty,
  CollabGraph,
  CollabStep,
  CollabTicket,
  CollabTicketAction,
  CollabWorkspaceScope,
  AgentProfile,
} from './types'

/* ------------------------------------------------------------------ *
 * 徽章语义：动作 / 工单状态 / 审计结论
 * ------------------------------------------------------------------ */

/** 时间线上一条动作的徽章取值；`error` 是运行态、`note` 是没有动作的普通记录、`user` 是用户自己。 */
export type BoardEntryTone = CollabTicketAction | 'error' | 'note' | 'user'

/** 图例（右栏「动作徽章」那排）与徽章配色都用这个顺序，改这里就两端一起改。 */
export const BOARD_TONES: BoardEntryTone[] = [
  'delegate', 'submit', 'review', 'done', 'ask_user', 'escalate', 'reject', 'error', 'note', 'user',
]

/** 动作短语来自 `collabDraft.ACTIONS`（形如 `delegate 派发`），这里只取中文那半截。 */
function shortActionLabel(action: CollabTicketAction): string {
  const found = ACTIONS.find((item) => item.value === action)
  if (!found) return action
  return found.label.split(' ').pop() || found.label
}

/** 徽章上的字：短、能一眼分辨。 */
export const BOARD_TONE_LABEL: Record<BoardEntryTone, string> = {
  delegate: shortActionLabel('delegate'),
  submit: shortActionLabel('submit'),
  review: shortActionLabel('review'),
  done: shortActionLabel('done'),
  ask_user: shortActionLabel('ask_user'),
  escalate: shortActionLabel('escalate'),
  reject: shortActionLabel('reject'),
  error: '出错',
  note: '记录',
  user: '你',
}

/** 工单状态徽章（显示词汇的单一来源在 `collabDraft.TICKET_STATES`）。 */
export function boardStateMeta(state: string): { label: string; pill: string } {
  return ticketStateMeta(state)
}

/** 审计结论的档位：`ok` 没动、`info` 正常改动、`violation` 越权、`none` 没有工作目录。 */
export type BoardAuditTone = 'ok' | 'info' | 'violation' | 'none'

export interface BoardAudit {
  checked: boolean
  unchanged: boolean
  count: number
  added: string[]
  modified: string[]
  removed: string[]
}

export interface BoardAuditView {
  tone: BoardAuditTone
  text: string
  files: string[]
}

/* ------------------------------------------------------------------ *
 * 视图模型
 * ------------------------------------------------------------------ */

/** 岗位头像的完整字段（与 `AgentAvatar` 一致：自定义图 > 头像包 > 按 id 兜底）。 */
export interface BoardFace {
  /** 老的 emoji 字段（已下线，仅作兜底挑头像包的种子） */
  avatar: string
  /** 内置头像包编号；有值渲染 /avatars/<它>.webp */
  avatarPreset?: string | null
  /** 自定义头像文件名；有值取 /api/agents/{id}/avatar */
  avatarFile?: string | null
  /** 智能体 id：取自定义图要用 */
  agentId?: string | null
}

export interface BoardEntry extends BoardFace {
  id: string
  /** 发起这一轮的节点 key；用户自己的发言是 `user` */
  roleKey: string
  agentLabel: string
  duty: string
  tone: BoardEntryTone
  /** 动作徽章上的字（来自 `BOARD_TONE_LABEL`，这里摊平，方便模板直接用） */
  toneLabel: string
  /** 交接的接收方（只有 delegate 有） */
  targetKey: string
  targetLabel: string
  title: string
  detail: string
  at: string
  tokens: number
  durationMs: number
  /** 该节点在本次协作里的工作目录作用域 */
  scope: CollabWorkspaceScope | null
  audit: BoardAudit | null
  auditView: BoardAuditView | null
  /** 只读节点却改了工作目录 */
  violation: boolean
  artifacts: string[]
  error: string
  /** 返工第几轮（工单当前轮次，用于卡片角标） */
  round: number
}

export interface BoardBriefSection {
  key: string
  label: string
  items: string[]
}

export interface BoardTicket {
  id: string
  title: string
  state: string
  stateLabel: string
  statePill: string
  /** 子工单在列表里的缩进层级（0 = 根工单） */
  depth: number
  parentTicketId: string | null
  holderKey: string
  holderLabel: string
  /** 当前处理人的完整头像（同 `BoardFace`，与智能体设定同步） */
  holderFace: BoardFace
  holderDuty: string
  ownerLabel: string
  round: number
  maxRounds: number
  entries: BoardEntry[]
  brief: BoardBriefSection[]
  reviewComment: string
  verdict: string
  escalation: { from: string; reason: string; need: string; hops: string[] } | null
  summary: string
  artifacts: { key: string; label: string }[]
  stats: { steps: number; handoffs: number; violations: number; tokens: number }
}

export interface BoardTotals {
  tickets: number
  steps: number
  handoffs: number
  violations: number
  tokens: number
}

export interface BoardModel {
  tickets: BoardTicket[]
  totals: BoardTotals
  /** 默认选中：最需要人介入的那张（卡住的 → 返工中 → 待审查 → 根工单 → 第一张） */
  focusedId: string | null
}

export interface BuildBoardArgs {
  tickets: CollabTicket[]
  steps: CollabStep[]
  /** 本次运行的协作图（取节点的作用域 / 岗位） */
  graph?: CollabGraph | null
  agents: AgentProfile[]
  /** 把节点 key 翻成人看的智能体名（各端文案不同：桌面用节点标签，移动端也一样传） */
  nodeLabel: (key: string) => string
}

/** 岗位的默认工作目录作用域（与后端 `NodeSpec.workspace_scope` 的默认值一致）。 */
const DEFAULT_SCOPE_BY_DUTY: Record<string, CollabWorkspaceScope> = {
  coordinator: 'none',
  engineer: 'write',
  reviewer: 'read',
}

/** 节点的生效作用域：显式配置优先，否则按岗位取默认；都没有就返回 null（不显示这一栏）。 */
export function boardScopeOf(
  node: { duty?: CollabDuty | string; workspace_scope?: CollabWorkspaceScope | null } | undefined,
  duty: string,
): CollabWorkspaceScope | null {
  if (node?.workspace_scope) return node.workspace_scope
  return DEFAULT_SCOPE_BY_DUTY[node?.duty ?? duty] ?? null
}

/**
 * 把一步的审计结论翻成界面上一句话。
 *
 * 判定顺序：没作用域 → 不显示；没采集到指纹 → 中性；**只读节点改了东西 → 越权（红）**；
 * 可写节点的改动 → 中性提示。只读判定只看作用域，不看岗位，避免「审查者配了 write」被误标。
 */
export function auditViewOf(
  scope: CollabWorkspaceScope | null,
  audit: BoardAudit | null,
): BoardAuditView | null {
  if (!scope) return null
  // 协调者这类节点拿不到共享工作目录，但会分到一个一次性空目录 —— 它同样有指纹审计，
  // 只是写进去不算越权，所以这里只作中性说明，不吓人。
  if (scope === 'none') {
    if (!audit || !audit.checked) return { tone: 'none', text: '未分配共享工作目录', files: [] }
    if (audit.unchanged) return { tone: 'ok', text: '未分配共享工作目录 · 一次性目录未被改动', files: [] }
    return {
      tone: 'info',
      text: `一次性目录 ${audit.count} 个文件变更（不算越权）`,
      files: [...audit.added, ...audit.modified, ...audit.removed],
    }
  }

  const scopeLabel = scope === 'read' ? '只读节点' : '可写节点'
  if (!audit || !audit.checked) return { tone: 'none', text: `${scopeLabel} · 未采集工作目录指纹`, files: [] }
  if (audit.unchanged) return { tone: 'ok', text: `${scopeLabel} · 工作目录未被改动`, files: [] }

  const files = [...audit.added, ...audit.modified, ...audit.removed]
  if (scope === 'read') return { tone: 'violation', text: `越权：只读节点改动了 ${audit.count} 个文件`, files }
  return { tone: 'info', text: `工作目录 ${audit.count} 个文件变更`, files }
}

/* ------------------------------------------------------------------ *
 * 原始字段的宽容解析（后端契约是宽松的，界面不能因此崩）
 * ------------------------------------------------------------------ */

function asStringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean)
  if (typeof value === 'string' && value.trim()) return [value]
  return []
}

function asAudit(value: unknown): BoardAudit | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  if (record.checked !== true) return null
  const added = asStringList(record.added)
  const modified = asStringList(record.modified)
  const removed = asStringList(record.removed)
  const count = typeof record.count === 'number' ? record.count : added.length + modified.length + removed.length
  return {
    checked: true,
    unchanged: record.unchanged === true || count === 0,
    count,
    added,
    modified,
    removed,
  }
}

/**
 * 产出物：协议允许写字符串，后端会把它规整成 `{kind: 'file', path}`；
 * 两种都要认，不能让界面出现 `[object Object]`。
 */
function asArtifacts(value: unknown): { key: string; label: string }[] {
  if (!Array.isArray(value)) return []
  const items: { key: string; label: string }[] = []
  value.forEach((item, index) => {
    if (typeof item === 'string' && item.trim()) {
      items.push({ key: `${index}-${item}`, label: item })
      return
    }
    if (item && typeof item === 'object') {
      const record = item as Record<string, unknown>
      const path = record.path ?? record.name ?? record.url
      if (typeof path === 'string' && path.trim()) {
        const kind = typeof record.kind === 'string' ? record.kind : ''
        items.push({ key: `${index}-${path}`, label: kind && kind !== 'file' ? `${path}（${kind}）` : path })
      }
    }
  })
  return items
}

/** 一步里调用过的工具名（后端已从 parts 提取好、去重限量——看板不展示完整工具流水）。 */
function toolNamesOf(step: CollabStep): string[] {
  return step.tool_names ?? []
}

/* ------------------------------------------------------------------ *
 * 格式化（时间线右上/底部的次要信息）
 * ------------------------------------------------------------------ */

/**
 * 一步的 token 用量：交给 `lib/usage.ts` 归一化（各内核字段名不同，直接加起来会重复计数）。
 * 口径与顶栏、进度卡一致，看板不另立一套。
 */
export function usageTotal(usage: Record<string, unknown> | null | undefined): number {
  return summarizeUsage([usage]).total
}

/** `13,411 tok`；没有用量时返回空串，让调用方决定显示什么。 */
export function formatTokens(tokens: number): string {
  return tokens > 0 ? `${tokens.toLocaleString('en-US')} tok` : ''
}

/** `1.6 min` / `12.4 s`；没跑出时长就返回空串。 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return ''
  return ms >= 60000 ? `${(ms / 60000).toFixed(1)} min` : `${(ms / 1000).toFixed(1)} s`
}

/** ISO 时间 → `14:02`（只给时间线上的一眼定位，完整时间在别处）。 */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function stepDurationMs(step: CollabStep): number {
  const start = step.started_at ? new Date(step.started_at).getTime() : NaN
  const end = step.finished_at ? new Date(step.finished_at).getTime() : NaN
  if (Number.isNaN(start) || Number.isNaN(end)) return 0
  return Math.max(0, end - start)
}

function snippet(text: string | null | undefined, limit: number): string {
  const oneLine = (text ?? '').replace(/\s+/g, ' ').trim()
  return oneLine.length > limit ? `${oneLine.slice(0, limit)}…` : oneLine
}

/* ------------------------------------------------------------------ *
 * 组装
 * ------------------------------------------------------------------ */

interface StepContext {
  nodeLabel: (key: string) => string
  faceOf: (key: string) => BoardFace
  dutyOf: (key: string) => string
  scopeOf: (key: string) => CollabWorkspaceScope | null
  round: number
}

function entryFromStep(step: CollabStep, context: StepContext): BoardEntry {
  const roleKey = step.role_key || 'unknown'
  const isUser = roleKey === 'user'
  const scope = isUser ? null : context.scopeOf(roleKey)
  const audit = asAudit(step.audit)
  const view = auditViewOf(scope, audit)

  // 动作判定：运行出错优先（用户要先看到失败），其次是用户自己的发言，最后才是工单动作。
  const known = ACTIONS.some((item) => item.value === step.action)
  const tone: BoardEntryTone = step.error || step.status === 'error'
    ? 'error'
    : isUser
      ? 'user'
      : known
        ? (step.action as CollabTicketAction)
        : 'note'

  const gist = snippet(step.gist, 160)
  const content = snippet(step.content, 240)
  // 有摘要就用摘要当标题、正文当补充；没有摘要（纯文本轮）就把正文提上来。
  const title = gist || content || stepStatusHint(step.status, stepHasOutput(step)) || '（这一步没有输出）'
  const detail = gist && content && !content.startsWith(gist.replace(/…$/, '')) ? content : ''

  const targetKey = step.target_key ?? ''
  const face = isUser ? { avatar: '🙋' } : context.faceOf(roleKey)
  return {
    id: step.id,
    roleKey,
    agentLabel: isUser ? '你' : context.nodeLabel(roleKey) || step.profile_name || roleKey,
    ...face,
    duty: context.dutyOf(roleKey),
    tone,
    toneLabel: BOARD_TONE_LABEL[tone],
    targetKey,
    targetLabel: targetKey ? context.nodeLabel(targetKey) || targetKey : '',
    title,
    detail,
    at: clockTime(step.started_at),
    tokens: usageTotal(step.usage),
    durationMs: stepDurationMs(step),
    scope,
    audit,
    auditView: view,
    violation: view?.tone === 'violation',
    artifacts: toolNamesOf(step),
    error: step.error ?? '',
    round: context.round,
  }
}

const BRIEF_FIELDS: { key: string; label: string }[] = [
  { key: 'goal', label: '目标' },
  { key: 'requirements', label: '要求' },
  { key: 'acceptance', label: '验收标准' },
  { key: 'constraints', label: '约束' },
  { key: 'deliverables', label: '交付物' },
]

/** 子工单的缩进层级：跟着 `parent_ticket_id` 往下走（防环，最多 4 层）。 */
function depthOf(ticket: CollabTicket, byId: Map<string, CollabTicket>): number {
  let depth = 0
  let cursor = ticket.parent_ticket_id ?? null
  const seen = new Set<string>([ticket.id])
  while (cursor && byId.has(cursor) && !seen.has(cursor) && depth < 4) {
    seen.add(cursor)
    depth += 1
    cursor = byId.get(cursor)!.parent_ticket_id ?? null
  }
  return depth
}

/** 把运行详情折成看板模型；两端都调这一个函数。 */
export function buildBoardModel({ tickets, steps, graph, agents, nodeLabel }: BuildBoardArgs): BoardModel {
  const nodes = new Map((graph?.nodes ?? []).map((node) => [node.key, node]))
  const agentsById = new Map(agents.map((agent) => [agent.id, agent]))

  // 头像跟智能体设定走：自定义上传图 > 内置头像包 > 按 id 兜底（与 `AgentAvatar` 同一套优先级）。
  // 只传 emoji 的话，用户在智能体页换了头像，工单看板这边还是老 emoji —— 就是「没同步」。
  const faceOf = (key: string): BoardFace => {
    const agentId = nodes.get(key)?.agent_profile_id ?? null
    const agent = agentId ? agentsById.get(agentId) : undefined
    return {
      avatar: agent?.avatar ?? '',
      avatarPreset: agent?.avatar_preset ?? null,
      avatarFile: agent?.avatar_file ?? null,
      agentId,
    }
  }
  const dutyOf = (key: string): string => nodes.get(key)?.duty ?? ''
  const scopeOf = (key: string): CollabWorkspaceScope | null =>
    boardScopeOf(nodes.get(key), nodes.get(key)?.duty ?? 'engineer')

  const byId = new Map(tickets.map((ticket) => [ticket.id, ticket]))
  const ordered = [...tickets].sort((left, right) =>
    left.created_at < right.created_at ? -1 : left.created_at > right.created_at ? 1 : 0,
  )

  const boardTickets: BoardTicket[] = ordered.map((ticket) => {
    const own = steps
      .filter((step) => step.ticket_id === ticket.id)
      .sort((left, right) => left.sequence - right.sequence)
    const context: StepContext = {
      nodeLabel, faceOf, dutyOf, scopeOf, round: ticket.round,
    }
    const entries = own.map((step) => entryFromStep(step, context))
    const state = boardStateMeta(ticket.state)
    const brief = ticket.brief ?? {}
    const escalation = ticket.escalation

    return {
      id: ticket.id,
      title: ticket.title,
      state: ticket.state,
      stateLabel: state.label,
      statePill: state.pill,
      depth: depthOf(ticket, byId),
      parentTicketId: ticket.parent_ticket_id ?? null,
      holderKey: ticket.holder_key,
      holderLabel: ticket.holder_key === 'user' ? '你' : nodeLabel(ticket.holder_key) || ticket.holder_key,
      holderFace: ticket.holder_key === 'user' ? { avatar: '🙋' } : faceOf(ticket.holder_key),
      holderDuty: dutyOf(ticket.holder_key),
      ownerLabel: nodeLabel(ticket.owner_key) || ticket.owner_key,
      round: ticket.round,
      maxRounds: ticket.max_rounds,
      entries,
      brief: BRIEF_FIELDS.map((field) => ({
        key: field.key,
        label: field.label,
        items: asStringList((brief as Record<string, unknown>)[field.key]),
      })).filter((section) => section.items.length > 0),
      reviewComment: ticket.review_comment ?? '',
      verdict: ticket.verdict ?? '',
      escalation: escalation
        ? {
            from: nodeLabel(String(escalation.from ?? '')) || String(escalation.from ?? ''),
            reason: escalation.reason ?? '',
            need: escalation.need ?? '',
            hops: (escalation.hops ?? []).map((hop) => nodeLabel(hop) || hop),
          }
        : null,
      summary: typeof ticket.result?.summary === 'string' ? ticket.result.summary : '',
      artifacts: asArtifacts(ticket.result?.artifacts),
      stats: {
        steps: entries.length,
        handoffs: entries.filter((entry) => entry.targetKey).length,
        violations: entries.filter((entry) => entry.violation).length,
        tokens: entries.reduce((sum, entry) => sum + entry.tokens, 0),
      },
    }
  })

  const totals: BoardTotals = {
    tickets: boardTickets.length,
    steps: boardTickets.reduce((sum, ticket) => sum + ticket.stats.steps, 0),
    handoffs: boardTickets.reduce((sum, ticket) => sum + ticket.stats.handoffs, 0),
    violations: boardTickets.reduce((sum, ticket) => sum + ticket.stats.violations, 0),
    tokens: boardTickets.reduce((sum, ticket) => sum + ticket.stats.tokens, 0),
  }

  // 默认落在最需要人介入的那张：卡住的 → 返工中 → 待审查 → 根工单 → 第一张。
  const byState = (state: string) => boardTickets.find((ticket) => ticket.state === state)
  const focused =
    byState('blocked') ?? byState('rework') ?? byState('review') ??
    boardTickets.find((ticket) => !ticket.parentTicketId) ?? boardTickets[0]

  return { tickets: boardTickets, totals, focusedId: focused?.id ?? null }
}

/** 左栏筛选：进行中 / 已完成 / 全部。 */
export type BoardFilter = 'all' | 'active' | 'done'

export function filterTickets(tickets: BoardTicket[], filter: BoardFilter, keyword: string): BoardTicket[] {
  const needle = keyword.trim().toLowerCase()
  return tickets.filter((ticket) => {
    if (filter === 'active' && (ticket.state === 'done' || ticket.state === 'rejected')) return false
    if (filter === 'done' && ticket.state !== 'done') return false
    if (!needle) return true
    return (
      ticket.title.toLowerCase().includes(needle) ||
      ticket.holderLabel.toLowerCase().includes(needle) ||
      ticket.stateLabel.toLowerCase().includes(needle)
    )
  })
}
