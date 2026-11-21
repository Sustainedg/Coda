/**
 * 协作岗位的实时对话线程（共享组件）。
 * 桌面端岗位面板与移动端岗位弹层都用它：内部把「岗位出入的步骤 / 工单 / 流式输出」
 * 装配成统一的 `ConversationThread` 条目，气泡外观与单聊、协作主对话完全一致。
 */
import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { api } from '../lib/api'
import { ACTIONS, ticketStateMeta } from '../lib/collabDraft'
import type { FinishedStep, LiveStep } from '../lib/collabStream'
import { stepHasOutput, stepStatusHint, type CollabAgentProgress } from '../lib/collabProgress'
import { buildAgentThread, ticketBriefSummary } from '../lib/collabThread'
import type { AuthUser, CollabStep, CollabTicket } from '../lib/types'
import { ConversationThread, type ConversationThreadItem } from './ConversationThread'
import type { RenderMessage } from './MessageView'

interface Props {
  node: CollabAgentProgress
  roster: CollabAgentProgress[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  live: LiveStep | null
  streamed: FinishedStep[]
  /** 容器类名：桌面端用 `collab-agent-thread`，移动端用 `m-collab-agent-thread` */
  className?: string
  empty?: ReactNode
  /** 点工单卡片 / 工单标签时打开完整工单（不传就只展示摘要） */
  onOpenTicket?: (ticketId: string) => void
  /** 当前登录用户：自己发的消息画真头像（不传退回 🙋） */
  user?: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null
}

const ACTION_LABELS = Object.fromEntries(ACTIONS.map((item) => [item.value, item.label]))

const STEP_STATUS_LABELS: Record<string, string> = {
  streaming: '进行中',
  interrupted: '已中断',
  error: '出错',
  cancelled: '已取消',
}

/** 压缩成一行，卡片上只留摘要；完整内容点开看。 */
function oneLine(text: string | null | undefined, limit = 56): string {
  const line = (text ?? '').replace(/\s+/g, ' ').trim()
  return line.length > limit ? `${line.slice(0, limit)}…` : line
}

/** 对话里的工单卡：标题 + 状态 + 处理人 + 要点条数；点开才是完整工单。 */
function TicketCard({
  ticket, holderLabel, onOpen,
}: {
  ticket: CollabTicket
  holderLabel: string
  onOpen?: (ticketId: string) => void
}) {
  const state = ticketStateMeta(ticket.state)
  const goal = oneLine(ticket.brief?.goal)
  const summary = ticketBriefSummary(ticket)
  const body = (
    <>
      <span className="collab-ticket-card-head">
        <span className="collab-ticket-card-icon" aria-hidden>📋</span>
        <span className="collab-ticket-card-title">{ticket.title}</span>
        <span className={`pill ${state.pill}`}>{state.label}</span>
      </span>
      <span className="collab-ticket-card-meta">
        <span>处理人：{holderLabel}</span>
        <span>返工 {ticket.round}/{ticket.max_rounds}</span>
        {goal && <span className="collab-ticket-card-goal">{goal}</span>}
        {summary && <span>{summary}</span>}
      </span>
      {onOpen && <span className="collab-ticket-card-more">查看完整工单 →</span>}
    </>
  )
  if (!onOpen) return <div className="collab-ticket-card static">{body}</div>
  return (
    <button type="button" className="collab-ticket-card" onClick={() => onOpen(ticket.id)}>
      {body}
    </button>
  )
}

/** 气泡上方的工单标签：可点时打开完整工单，不可点时保持原来的纯文本。 */
function TicketChip({
  ticket, onOpen,
}: {
  ticket: CollabTicket
  onOpen?: (ticketId: string) => void
}) {
  if (!onOpen) return <span>工单：{ticket.title}</span>
  return (
    <button type="button" className="collab-ticket-chip" title={`查看工单：${ticket.title}`} onClick={() => onOpen(ticket.id)}>
      📋 {ticket.title}
    </button>
  )
}

export function CollabAgentThread({
  node, roster, steps, tickets, live, streamed, className, empty, onOpenTicket, user,
}: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  // 同一步骤可能同时出现在落库详情与流式结果里（跨设备刷新、刚跑完还没落库）：
  // 流式结果才是完整的，落库那行这时往往还是「streaming + 空内容」。
  // 按 stepId 让流式结果顶掉落库行，避免线程里只剩「正在处理中……跑完后显示」。
  const streamedIds = useMemo(() => new Set(streamed.map((step) => step.stepId)), [streamed])
  const superseded = (stepId: string) => stepId === live?.stepId || streamedIds.has(stepId)
  const ownStreamed = useMemo(
    () => streamed.filter((step) => step.nodeKey === node.key),
    [streamed, node.key],
  )
  const liveHere = live?.nodeKey === node.key ? live : null
  const ticketById = useMemo(
    () => Object.fromEntries(tickets.map((ticket) => [ticket.id, ticket])),
    [tickets],
  )
  const labelOf = (key: string) => roster.find((item) => item.key === key)?.label ?? key
  const avatarOf = (key: string) => roster.find((item) => item.key === key)?.avatar ?? '🤖'
  // 本岗位被流式结果顶替的步骤不再进线程模型，否则历史里会多一个空的占位气泡。
  const threadSteps = useMemo(
    () => steps.filter((step) => step.role_key !== node.key || !superseded(step.id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [steps, node.key, live?.stepId, streamedIds],
  )
  const thread = useMemo(
    () => buildAgentThread({ nodeKey: node.key, isEntry: node.isEntry, steps: threadSteps, tickets, labelOf, avatarOf }),
    // labelOf/avatarOf 只是 roster 的查询，依赖 roster 即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [node.key, node.isEntry, threadSteps, tickets, roster],
  )

  const liveMessage: RenderMessage | null = liveHere
    ? {
        id: `live-${liveHere.stepId}`,
        role: 'assistant',
        content: liveHere.agent.text,
        parts: liveHere.agent.parts,
        status: liveHere.agent.error ? 'error' : 'streaming',
        error: liveHere.agent.error,
        usage: liveHere.agent.usage,
        streaming: true,
        logs: liveHere.agent.logs,
      }
    : null

  const threadItems: ConversationThreadItem[] = []
  for (const entry of thread) {
    if (entry.kind === 'user') {
      threadItems.push({
        id: entry.id,
        message: { id: entry.id, role: 'user', content: entry.text ?? '', parts: [], status: 'complete' },
      })
      continue
    }

    if (entry.kind === 'handoff') {
      threadItems.push({
        id: entry.id,
        custom: (
          <div className="bubble-row user collab-agent-handoff-row">
            <div className="avatar user">{entry.fromAvatar}</div>
            <div className="bubble">
              <div className="bubble-head">
                <span>{entry.fromLabel}</span>
                <span>→ {node.label}</span>
                <span className="pill brand">交接</span>
              </div>
              <div className="collab-agent-handoff">
                {entry.text && <div className="collab-agent-handoff-text">{entry.text}</div>}
                {entry.ticket && (
                  <TicketCard
                    ticket={entry.ticket}
                    holderLabel={labelOf(entry.ticket.holder_key)}
                    onOpen={onOpenTicket}
                  />
                )}
              </div>
            </div>
          </div>
        ),
      })
      continue
    }

    const step = entry.step!
    const hint = stepStatusHint(step.status, stepHasOutput(step))
    const ticket = step.ticket_id ? ticketById[step.ticket_id] : undefined
    threadItems.push({
      id: entry.id,
      className: step.status === 'streaming' ? 'live' : undefined,
      meta: (
        <>
          <span>#{step.sequence}</span>
          {step.action && <span className="pill">{ACTION_LABELS[step.action] ?? step.action}</span>}
          {ticket && <TicketChip ticket={ticket} onOpen={onOpenTicket} />}
          {step.status !== 'complete' && (
            <span className={`pill ${step.status === 'error' || step.status === 'interrupted' ? 'err' : ''}`}>
              {STEP_STATUS_LABELS[step.status] ?? step.status}
            </span>
          )}
        </>
      ),
      custom: hint ? (
        <div className={`collab-agent-placeholder${step.status === 'streaming' ? ' live' : ''}`}>{hint}</div>
      ) : undefined,
      message: hint ? undefined : {
        id: step.id,
        role: 'assistant',
        content: step.content,
        parts: step.parts,
        status: step.status,
        error: step.error,
        usage: step.usage,
        // 详情不再携带 parts 本体：点开「过程」折叠块时按需拉这一步的完整流水
        loadParts: () => api.getCollabStepParts(step.run_id, step.id).then((r) => r.parts),
        partsCount: step.parts_count ?? 0,
      },
      avatar: node.avatar,
      avatarPreset: node.avatarPreset,
      avatarFile: node.avatarFile,
      agentId: node.agentId,
      label: node.label,
    })
  }

  for (const step of ownStreamed) {
    const ticket = step.ticketId ? ticketById[step.ticketId] : undefined
    threadItems.push({
      id: step.stepId,
      className: step.status === 'streaming' ? 'live' : undefined,
      meta: (
        <>
          {step.action && <span className="pill">{ACTION_LABELS[step.action] ?? step.action}</span>}
          {ticket && <TicketChip ticket={ticket} onOpen={onOpenTicket} />}
          {step.status !== 'complete' && <span className="pill">{step.status}</span>}
        </>
      ),
      message: {
        id: step.stepId,
        role: 'assistant',
        content: step.agent.text,
        parts: step.agent.parts,
        status: step.error ? 'error' : step.status,
        error: step.error ?? step.agent.error,
        usage: step.agent.usage,
        logs: step.agent.logs,
      },
      avatar: node.avatar,
      avatarPreset: node.avatarPreset,
      avatarFile: node.avatarFile,
      agentId: node.agentId,
      label: node.label,
    })
  }

  if (liveMessage) {
    threadItems.push({
      id: liveMessage.id,
      className: 'live',
      meta: <span className="pill brand">实时输出</span>,
      message: liveMessage,
      avatar: node.avatar,
      avatarPreset: node.avatarPreset,
      avatarFile: node.avatarFile,
      agentId: node.agentId,
      label: node.label,
    })
  }

  const liveText = liveHere?.agent.text ?? ''
  const liveParts = liveHere?.agent.parts.length ?? 0
  // 内容变化（新步骤、流式追加、切换岗位）时把线程滚到底部。
  useEffect(() => {
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }, [node.key, threadItems.length, liveText, liveParts])

  return (
    <ConversationThread
      centered
      className={className}
      threadRef={scrollRef}
      items={threadItems}
      user={user}
      empty={thread.length === 0 && ownStreamed.length === 0 && !liveMessage ? empty : undefined}
    />
  )
}
