/** 岗位内部实时对话面板：进度页内嵌与独立抽屉共用同一套外壳，线程本身复用 `CollabAgentThread`。 */
import { useMemo } from 'react'
import { DUTIES, ticketStateMeta } from '../lib/collabDraft'
import type { FinishedStep, LiveStep } from '../lib/collabStream'
import type { CollabAgentProgress } from '../lib/collabProgress'
import type { AuthUser, CollabStep, CollabTicket } from '../lib/types'
import { CollabAgentThread } from './CollabAgentThread'
import { AgentAvatar } from './AgentAvatar'

interface Props {
  node: CollabAgentProgress
  roster: CollabAgentProgress[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  live: LiveStep | null
  streamed: FinishedStep[]
  onClose: () => void
  closeLabel?: string
  variant?: 'embedded' | 'drawer'
  /** 点工单卡片 / 标签时打开完整工单 */
  onOpenTicket?: (ticketId: string) => void
  /** 当前登录用户：自己发的消息画真头像（不传退回 🙋） */
  user?: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null
}

export function AgentConversationPanel({
  node, roster, steps, tickets, live, streamed, onClose, closeLabel = '关闭', variant = 'drawer',
  onOpenTicket, user,
}: Props) {
  const liveHere = live?.nodeKey === node.key ? live : null
  const dutyLabel = useMemo(
    () => DUTIES.find((item) => item.value === node.duty)?.label ?? node.duty,
    [node.duty],
  )

  return (
    <div className={`collab-agent-panel ${variant}`} aria-label={`${node.label}的实时对话`}>
      {/* 与普通对话共用同一套顶栏：头像 + 名字 + 次要信息，右边是状态与返回。 */}
      <header className="chat-topbar collab-agent-panel-head">
        <AgentAvatar
          avatar={node.avatar}
          avatarPreset={node.avatarPreset}
          avatarFile={node.avatarFile}
          agentId={node.agentId}
        />
        <div className="collab-agent-panel-title">
          <div className="collab-agent-panel-name">{node.label} · 实时对话</div>
          <div className="collab-agent-panel-meta">
            {dutyLabel} · {node.stepCount} 步 · {node.stateLabel}
            {node.isEntry ? ' · 入口领导者' : ' · 只读内部对话'}
          </div>
        </div>
        {liveHere && <span className="pill brand collab-agent-live-pill">实时输出中</span>}
        <button className="btn sm ghost collab-agent-panel-close" onClick={onClose}>
          {closeLabel}
        </button>
      </header>

      {node.activeTickets.length > 0 && (
        <div className="collab-agent-tickets">
          {node.activeTickets.map((ticket) => {
            const state = ticketStateMeta(ticket.state)
            return onOpenTicket ? (
              <button
                type="button"
                className="collab-agent-ticket-chip"
                key={ticket.id}
                title={`查看工单：${ticket.title}`}
                onClick={() => onOpenTicket(ticket.id)}
              >
                <span className={`pill ${state.pill}`}>{state.label}</span>
                <span className="collab-agent-ticket-chip-title">{ticket.title}</span>
              </button>
            ) : (
              <span className="pill" key={ticket.id}>当前工单：{ticket.title}</span>
            )
          })}
        </div>
      )}

      <CollabAgentThread
        node={node}
        roster={roster}
        steps={steps}
        tickets={tickets}
        live={live}
        streamed={streamed}
        onOpenTicket={onOpenTicket}
        user={user}
        className="collab-agent-thread"
        empty={(
          <div className="collab-agent-empty">
            <div className="collab-agent-empty-icon">
              <AgentAvatar
                avatar={node.avatar}
                avatarPreset={node.avatarPreset}
                avatarFile={node.avatarFile}
                agentId={node.agentId}
                size={44}
              />
            </div>
            {node.state === 'active' ? (
              <>
                <p>「{node.label}」正在处理中…</p>
                <p className="dim small">输出会在这一步跑完后显示；由本页发起时这里也会实时刷出。</p>
              </>
            ) : (
              <>
                <p>这个岗位还没有开始工作</p>
                <p className="dim small">协作推进到它时，这里会实时出现输出。</p>
              </>
            )}
          </div>
        )}
      />
    </div>
  )
}
