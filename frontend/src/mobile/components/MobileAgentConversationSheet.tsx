/** 移动端岗位内部实时会话：全屏底部弹层，线程复用共享的 `CollabAgentThread`。 */
import { useMemo } from 'react'
import { CollabAgentThread } from '../../components/CollabAgentThread'
import { AgentAvatar } from '../../components/AgentAvatar'
import { DUTIES } from '../../lib/collabDraft'
import type { FinishedStep, LiveStep } from '../../lib/collabStream'
import type { CollabAgentProgress } from '../../lib/collabProgress'
import type { AuthUser, CollabStep, CollabTicket } from '../../lib/types'
import { MobileSheet } from './MobileSheet'

interface Props {
  node: CollabAgentProgress
  roster: CollabAgentProgress[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  live: LiveStep | null
  streamed: FinishedStep[]
  onClose: () => void
  /** 点工单卡片 / 标签时打开完整工单 */
  onOpenTicket?: (ticketId: string) => void
  /** 当前登录用户：自己发的消息画真头像（不传退回 🙋） */
  user?: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null
}

export function MobileAgentConversationSheet({
  node, roster, steps, tickets, live, streamed, onClose, onOpenTicket, user,
}: Props) {
  const dutyLabel = useMemo(
    () => DUTIES.find((item) => item.value === node.duty)?.label ?? node.duty,
    [node.duty],
  )

  return (
    <MobileSheet title={`${node.label} · 实时对话`} onClose={onClose} full bodyClassName="m-collab-agent-body">
      <div className="m-collab-agent-meta">
        <AgentAvatar
          avatar={node.avatar}
          avatarPreset={node.avatarPreset}
          avatarFile={node.avatarFile}
          agentId={node.agentId}
          size={34}
        />
        <div>
          <div className="m-collab-agent-name">{node.label}</div>
          <div className="dim small">
            {dutyLabel} · {node.stepCount} 步 · {node.stateLabel}
            {node.isEntry ? ' · 入口领导者' : ' · 只读内部对话'}
          </div>
        </div>
      </div>
      {node.activeTickets.length > 0 && (
        <div className="row wrap" style={{ gap: 6 }}>
          {node.activeTickets.map((ticket) => (
            onOpenTicket ? (
              <button
                type="button"
                className="collab-agent-ticket-chip"
                key={ticket.id}
                onClick={() => onOpenTicket(ticket.id)}
              >
                <span className="pill">{ticket.title}</span>
              </button>
            ) : (
              <span className="pill" key={ticket.id}>
                当前工单：{ticket.title}
              </span>
            )
          ))}
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
        className="m-thread m-collab-agent-thread"
        empty={(
          <div className="m-empty">
            <div className="m-empty-icon">
              <AgentAvatar
                avatar={node.avatar}
                avatarPreset={node.avatarPreset}
                avatarFile={node.avatarFile}
                agentId={node.agentId}
                size={48}
              />
            </div>
            {node.state === 'active' ? (
              <>
                <p>「{node.label}」正在处理中…</p>
                <p className="dim small">输出会在这一步跑完后显示。</p>
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
    </MobileSheet>
  )
}
