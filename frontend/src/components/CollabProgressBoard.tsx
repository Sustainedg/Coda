/**
 * 协作进度（覆盖层）：把这次协作画成一张**分层流程图**，每个岗位是一个节点（头像 + 昵称）。
 *
 * 与移动端同一套交互形态：不再另开全屏页面，而是**盖在对话时间线上**（顶栏与输入框保持
 * 可用 —— 输入框仍能和入口智能体对话，Esc 收起覆盖层回到时间线）。
 *
 * 图上不放任何标题与说明：顶栏的「协作进度」按钮就是开合开关，状态徽标与图例已能自解释。
 * 工位按层级摆（谁向谁派活）：入口领导者独占第一行，往下每行是它直接派活的人，
 * 并排干活的人在同一行 —— 「上面一个、下面一排」的团队结构在图上就能看出来。
 * 节点头像右上角的状态徽标区分状态（绿=执行中、黄=等你决定、红=出错、灰=等待、勾=完成），
 * 点节点弹出「现在具体在做什么」的详情卡，还能跳到它在本次协作里的实时对话。
 * 回流（打回 / 返工）在图上单独画成往回走的虚线，不参与分层。
 */
import { useEffect, useState } from 'react'
import { liveOfNode, type FinishedStep, type LiveStep } from '../lib/collabStream'
import { type CollabFlowEdge, type CollabAgentProgress } from '../lib/collabProgress'
import type { AuthUser, CollabStep, CollabTicket } from '../lib/types'
import { CollabFlowMap } from './CollabFlowMap'
import { AgentConversationPanel } from './AgentConversationPanel'

interface Props {
  nodes: CollabAgentProgress[]
  edges: CollabFlowEdge[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  /** 并行波次里可能同时有多个岗位在流式输出；岗位视角用 liveOfNode 取 */
  live: LiveStep[]
  streamed: FinishedStep[]
  onClose: () => void
  /** 点岗位对话里的工单卡片时打开完整工单 */
  onOpenTicket?: (ticketId: string) => void
  /** 上面还压着工单看板时，Esc 交给看板处理，别把进度覆盖层一起关掉 */
  escapeDisabled?: boolean
  /** 当前登录用户：岗位对话里自己发的消息画真头像（不传退回 🙋） */
  user?: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null
}

export function CollabProgressBoard({
  nodes, edges, steps, tickets, live, streamed, onClose, onOpenTicket, escapeDisabled = false, user,
}: Props) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  useEffect(() => {
    if (escapeDisabled) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (selectedKey) setSelectedKey(null)
      else onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, selectedKey, escapeDisabled])

  if (nodes.length === 0) return null

  const selectedNode = nodes.find((node) => node.key === selectedKey) ?? null

  return (
    <section className="collab-progress-board" role="region" aria-label="协作进度">
      <div className={`collab-progress-board-body${selectedNode ? ' has-conversation' : ''}`}>
        <div className="collab-progress-board-main">
          <CollabFlowMap nodes={nodes} edges={edges} onOpenNode={setSelectedKey} />
        </div>

        {selectedNode && (
          <aside className="collab-progress-conversation">
            <AgentConversationPanel
              node={selectedNode}
              roster={nodes}
              steps={steps}
              tickets={tickets}
              live={liveOfNode(live, selectedNode.key)}
              streamed={streamed}
              onOpenTicket={onOpenTicket}
              user={user}
              onClose={() => setSelectedKey(null)}
              closeLabel="← 返回概览"
              variant="embedded"
            />
          </aside>
        )}
      </div>
    </section>
  )
}
