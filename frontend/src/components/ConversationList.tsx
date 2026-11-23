import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import {
  findUnusedConversation,
  notifyConversationsChanged,
  runningConversationIds,
  sortConversationsByRecent,
  useConversationsChanged,
} from '../lib/conversations'
import { formatListTime } from '../lib/datetime'
import type { Conversation, AgentProfile } from '../lib/types'
import { sameConversationList, useAutoRefresh } from '../lib/useAutoRefresh'
import { PencilIcon, TrashIcon } from './Icons'
import { AgentAvatar } from './AgentAvatar'
import { ConversationAvatar } from './ConversationAvatar'

/**
 * 左侧栏下半部分的对话列表（桌面端）：与移动端抽屉里那份同构 —— 导航在上、列表在下。
 *
 * 每行行首是这条对话的头像（`ConversationAvatar`）：单聊一枚，用过 @ 召唤的对话按群聊
 * 样式把多枚头像叠起来。列表按「最近活动」倒序 —— 谁刚有消息，谁就在最上面。
 *
 * 每行在鼠标移上去时浮出两个小图标：
 * - 「✎」就地重命名（回车提交，Esc 取消）；
 * - 「🗑」直接删除（删掉的正是当前打开的那条时，自动落到剩下的最近一条）。
 *
 * 会话级操作（重新生成 / 重置记忆 / 复制工作目录）只在移动端抽屉的「⋯」里提供 ——
 * 桌面端列表刻意不再放这一枚，避免悬停时按钮过密。
 *
 * 标题左侧还有两种状态点（不占悬停图标的位置）：
 * - 正在回复：旋转的小环，数据来自列表接口的 `active_runs`；
 * - 有未读回复：实心点，数据来自列表接口的 `unread`（最后一条已完成的助手回复晚于已读时间）。
 * 两者同时成立时只显示「正在回复」。
 *
 * 列表自己拉数据（4 秒一轮 + 聊天页改动后的广播），不跟聊天页共用一个状态 ——
 * 与移动端抽屉一样的做法，两边谁都不必等对方。
 */
export function ConversationList() {
  const navigate = useNavigate()
  const { pathname } = useLocation()

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  /** 正在跑着的对话 id：这些行显示「正在回复」的旋转小环 */
  const [runningIds, setRunningIds] = useState<Set<string>>(() => new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  /**
   * true 时下半部分换成「选智能体新建对话」。
   *
   * 这只是**兜底**：正常情况下「＋ 新建」直接用默认智能体开一条对话，
   * 只有还没设过默认（且可选智能体不止一个）时才退回来让用户挑一个。
   */
  const [pickerOpen, setPickerOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  /** 正在就地重命名的对话 id（下面那支输入框） */
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  /** Esc 取消重命名后，紧接着的 blur 不该把草稿提交上去 */
  const cancelRenameRef = useRef(false)

  const activeId = /^\/chat\/([^/]+)/.exec(pathname)?.[1] ?? null

  const load = useCallback(async () => {
    try {
      const snapshot = await api.listConversations()
      // 「正在回复」的对话整组置顶，组内再按最近活动倒序；后端已按同一口径排过，
      // 这里再排一次作为兜底（见 lib/conversations）。
      const running = runningConversationIds(snapshot.active_runs)
      const items = sortConversationsByRecent(snapshot.items, running)
      setConversations((prev) => (sameConversationList(prev, items) ? prev : items))
      setRunningIds((prev) => (sameIdSet(prev, running) ? prev : running))
      setError(null)
      return items
    } catch (e) {
      setError(describe(e))
      return []
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // 跨设备同步：另一台设备发的消息会让标题、条数与时间自己变。
  // 4 秒一轮（聊天页本身是 3 秒）：正在回复的动画要在几秒内出现，8 秒太迟了。
  useAutoRefresh(load, { intervalMs: 4000 })
  // 本端在聊天页里发消息 / 删对话之后，列表立刻对齐，不用等下一次轮询。
  useConversationsChanged(() => void load())

  const openPicker = async () => {
    setPickerOpen(true)
    setError(null)
    try {
      setAgents(await api.listAgents())
    } catch (e) {
      setError(describe(e))
    }
  }

  /**
   * 新建对话。
   *
   * 不带智能体 = 用「默认智能体」（智能体卡片右上角那颗五角星），工作目录也先留空
   * （进去后在输入框上方那两个小图标里随时改）—— 所以点一下「＋ 新建」就能直接说话。
   *
   * 真开新对话之前先找一条「从未被使用」的空对话（没发过消息、标题没动过、输入框没草稿）：
   * 有就直接跳过去，连点也不会堆出一排「新对话」。用户在挑选器里明确点了某个智能体时
   * 跳过这步 —— 人家点名要「跟这个人」开一条新的。
   */
  const create = async (agentId?: string) => {
    setCreating(true)
    setError(null)
    try {
      if (!agentId) {
        const unused = findUnusedConversation(await load())
        if (unused) {
          setPickerOpen(false)
          navigate(`/chat/${unused.id}`)
          return
        }
      }
      const conversation = await api.createConversation({ agent_profile_id: agentId })
      setPickerOpen(false)
      await load()
      notifyConversationsChanged()
      navigate(`/chat/${conversation.id}`)
    } catch (e) {
      // 还没设过默认智能体（或者一个智能体都没有）：退回「挑一个」的老路径，并把原因说清楚。
      setError(describe(e))
      await openPicker()
    } finally {
      setCreating(false)
    }
  }

  const startRename = (conversation: Conversation) => {
    cancelRenameRef.current = false
    setRenamingId(conversation.id)
    setRenameDraft(conversation.title)
  }

  const commitRename = async (conversation: Conversation) => {
    if (cancelRenameRef.current) {
      cancelRenameRef.current = false
      setRenamingId(null)
      return
    }
    const title = renameDraft.trim()
    setRenamingId(null)
    if (!title || title === conversation.title) return
    try {
      await api.updateConversation(conversation.id, { title })
      await load()
      notifyConversationsChanged()
    } catch (e) {
      setError(describe(e))
    }
  }

  const remove = async (conversation: Conversation) => {
    if (!confirm(`删除对话「${conversation.title}」？`)) return
    try {
      await api.deleteConversation(conversation.id)
    } catch (e) {
      setError(describe(e))
      return
    }
    setError(null)
    const remaining = await load()
    notifyConversationsChanged()
    // 删掉的正是当前打开的那条：落到剩下的最近一条（一条都没有时给空状态）。
    if (conversation.id !== activeId) return
    if (remaining.length > 0) navigate(`/chat/${remaining[0].id}`, { replace: true })
    else navigate('/chat', { replace: true })
  }

  return (
    <div className="sidebar-conversations">
      <div className="sidebar-section-head">
        <span className="card-section-title">对话</span>
        <button
          className="btn sm ghost"
          disabled={creating}
          onClick={() => (pickerOpen ? setPickerOpen(false) : void create())}
          aria-expanded={pickerOpen}
          title="用默认智能体开一条新对话（智能体与工作目录进去后还能改）"
        >
          {pickerOpen ? '取消' : '＋ 新建'}
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}

      <div className="sidebar-scroll">
        {pickerOpen ? (
          agents.length === 0 ? (
            <p className="dim small" style={{ padding: '8px 8px 4px', lineHeight: 1.6 }}>
              还没有智能体。先到「智能体」页创建一个：内核 + 网关 + 模型 + 人格设定。
            </p>
          ) : (
            <div className="sidebar-picker">
              {agents.map((agent) => (
                <button
                  key={agent.id}
                  className="chat-item"
                  disabled={creating}
                  onClick={() => void create(agent.id)}
                  title={`与「${agent.name}」开始新对话`}
                >
                  <span className="chat-item-title">
                    <AgentAvatar
                      avatar={agent.avatar}
                      avatarPreset={agent.avatar_preset}
                      avatarFile={agent.avatar_file}
                      agentId={agent.id}
                      size={18}
                      className="agent-avatar-inline"
                    />{' '}
                    {agent.name}
                    {agent.is_default && <span className="pill brand">默认</span>}
                  </span>
                  <span className="chat-item-meta">
                    <span>{agent.type_name ?? agent.type_id}</span>
                  </span>
                </button>
              ))}
            </div>
          )
        ) : loading && conversations.length === 0 ? (
          <p className="dim small" style={{ padding: '8px' }}>
            正在加载对话…
          </p>
        ) : conversations.length === 0 ? (
          <p className="dim small" style={{ padding: '8px 8px 4px', lineHeight: 1.6 }}>
            还没有对话：点上面的「＋ 新建」直接用默认智能体开始，进去后还能在输入框上方换智能体与工作目录。
          </p>
        ) : (
          conversations.map((conversation) => {
            const running = runningIds.has(conversation.id)
            // 当前打开的那条正在看，不亮未读；正在回复时以动画优先。
            const unread = Boolean(conversation.unread) && !running && conversation.id !== activeId
            return (
              <div
                key={conversation.id}
                className={`chat-item${conversation.id === activeId ? ' active' : ''}`}
              >
                {renamingId === conversation.id ? (
                  <input
                    className="chat-item-rename"
                    autoFocus
                    value={renameDraft}
                    onChange={(event) => setRenameDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') void commitRename(conversation)
                      else if (event.key === 'Escape') {
                        cancelRenameRef.current = true
                        setRenamingId(null)
                      }
                    }}
                    onBlur={() => void commitRename(conversation)}
                    aria-label={`重命名对话「${conversation.title}」`}
                  />
                ) : (
                  <button className="chat-item-main" onClick={() => navigate(`/chat/${conversation.id}`)}>
                    <ConversationAvatar conversation={conversation} />
                    <span className="chat-item-body">
                      <span className="chat-item-head">
                        {running ? (
                          <span
                            className="chat-item-state running"
                            role="img"
                            aria-label="智能体正在回复"
                            title="智能体正在回复…"
                          />
                        ) : unread ? (
                          <span
                            className="chat-item-state unread"
                            role="img"
                            aria-label="有未读回复"
                            title="有未读回复"
                          />
                        ) : null}
                        <span className="chat-item-title">{conversation.title}</span>
                      </span>
                      <span className="chat-item-meta">
                        <span>{conversation.agent_name}</span>
                        {/* 群聊行（用过 @ 召唤）留给「谁 + 时间」：条数在窄侧栏里会把智能体名
                            挤成「C…」，而头像叠片本身已经说明了这是多人对话。 */}
                        {(conversation.participants?.length ?? 0) <= 1 && (
                          <>
                            <span>·</span>
                            <span>{conversation.message_count} 条</span>
                          </>
                        )}
                        <span className="chat-item-time">{formatListTime(conversation.updated_at)}</span>
                      </span>
                    </span>
                  </button>
                )}

                <div className="chat-item-actions">
                  <button
                    className="chat-item-act"
                    onClick={() => startRename(conversation)}
                    aria-label={`重命名对话「${conversation.title}」`}
                    title="重命名"
                  >
                    <PencilIcon size={14} />
                  </button>
                  <button
                    className="chat-item-act danger"
                    onClick={() => void remove(conversation)}
                    aria-label={`删除对话「${conversation.title}」`}
                    title="删除"
                  >
                    <TrashIcon size={14} />
                  </button>
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

function sameIdSet(left: Set<string>, right: Set<string>): boolean {
  if (left.size !== right.size) return false
  for (const id of left) if (!right.has(id)) return false
  return true
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
