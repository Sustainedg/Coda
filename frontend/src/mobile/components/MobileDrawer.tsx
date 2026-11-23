import { useCallback, useEffect, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { useAuth } from '../../components/AuthGate'
import {
  findUnusedConversation,
  runningConversationIds,
  sortConversationsByRecent,
} from '../../lib/conversations'
import { formatListTime } from '../../lib/datetime'
import type { Conversation, AgentProfile } from '../../lib/types'
import { sameConversationList, useAutoRefresh } from '../../lib/useAutoRefresh'
import { describeError } from '../util'
import { BrandMark } from '../../components/BrandMark'
import { UserAvatar } from '../../components/UserAvatar'
import { AgentAvatar } from '../../components/AgentAvatar'
import { ConversationAvatar } from '../../components/ConversationAvatar'
import { NavIcon, LogoutIcon, SettingsIcon, type NavIconName } from '../../components/Icons'

/**
 * 抽屉上半部分的主导航：对应曾经固定在底部的 Tab（「对话」是回聊天页的入口）。
 * 图标存 `NavIconName` key，图形取 `components/Icons.tsx` 的共享线条 SVG（与桌面侧栏同一张表）。
 */
const NAV: { to: string; icon: NavIconName; label: string }[] = [
  { to: '/chat', icon: 'chat', label: '对话' },
  { to: '/agents', icon: 'agents', label: '智能体' },
  { to: '/collab', icon: 'collab', label: '协作组' },
  { to: '/skills', icon: 'skills', label: '技能' },
  { to: '/mcp', icon: 'mcp', label: 'MCP' },
  { to: '/usage', icon: 'usage', label: '用量' },
]

interface Props {
  open: boolean
  onClose: () => void
  version?: string
}

/**
 * 左侧抽屉：上半部分是主导航，下半部分是对话列表（点左上角的 logo 打开）。
 *
 * 移动端只有这一处主导航 —— 底部的 Tab 栏因此取消，把屏幕高度让给内容；
 * 对话列表也从聊天页里挪到这里，聊天页打开就是聊天。
 *
 * 每个对话行右侧的「⋯」是会话级操作（重新生成 / 重置记忆 / 复制工作目录 / 删除）的入口：
 * 它把信号（路由 state）交给聊天页去弹面板 —— 那几件事要用到那边的消息与会话状态。
 *
 * 对话行首是这条对话的头像（`ConversationAvatar`）：单聊一枚，用过 @ 召唤的对话按群聊
 * 样式叠成一摞。其余只留文字（标题 + 智能体/条数 + 时间）与那个「⋯」，字号比其它列表
 * 小一档（见 mobile.css 的 `.m-conv-row`），列表才不会太长。
 */
export function MobileDrawer({ open, onClose, version }: Props) {
  const { user, authEnabled, logout } = useAuth()
  const navigate = useNavigate()
  const { pathname } = useLocation()

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  /** 正在跑着的对话 id：这些行显示「正在回复」的旋转小环 */
  const [runningIds, setRunningIds] = useState<Set<string>>(() => new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  /**
   * true 时下半部分换成「选智能体新建对话」。
   *
   * 这只是**兜底**：正常情况下「＋ 新建」直接用默认智能体开一条对话，
   * 只有还没设过默认（且可选智能体不止一个）时才退回来让用户挑一个。
   */
  const [pickerOpen, setPickerOpen] = useState(false)
  /** 左下角账号菜单：平时只有头像 + 用户名，点开才有「设置 / 退出登录」 */
  const [accountOpen, setAccountOpen] = useState(false)

  const activeId = /^\/chat\/([^/]+)/.exec(pathname)?.[1] ?? null

  const load = useCallback(async () => {
    try {
      const [snapshot, agentList] = await Promise.all([api.listConversations(), api.listAgents()])
      // 「正在回复」的对话整组置顶，组内再按最近活动倒序（后端已按同一口径排过，这里兜底）。
      const running = runningConversationIds(snapshot.active_runs)
      const items = sortConversationsByRecent(snapshot.items, running)
      setConversations((prev) => (sameConversationList(prev, items) ? prev : items))
      setRunningIds((prev) => (sameIdSet(prev, running) ? prev : running))
      setAgents(agentList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  // 抽屉开着时保持最新：另一台设备新发的消息会让标题、条数与时间自己变。
  useAutoRefresh(load, { intervalMs: 8000, enabled: open })

  // 收起后回到对话列表，下次打开不会停在上次的「选智能体」那一步；账号菜单也一并收起。
  useEffect(() => {
    if (!open) {
      setPickerOpen(false)
      setAccountOpen(false)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  /**
   * 新建对话。
   *
   * 不带智能体 = 用「默认智能体」（智能体卡片右上角那颗五角星），工作目录也先留空
   * （进去后在输入框上方那两个小图标里随时改）—— 所以点一下「＋ 新建」就能直接说话。
   *
   * 真开新对话之前先找一条「从未被使用」的空对话（没发过消息、标题没动过、输入框没
   * 草稿）：有就直接跳过去，连点也不会堆出一排「新对话」。用户在挑选器里明确点了某个
   * 智能体时跳过这步 —— 人家点名要「跟这个人」开一条新的。
   */
  const start = async (agentId?: string) => {
    setCreating(true)
    setError(null)
    try {
      if (!agentId) {
        const unused = findUnusedConversation(conversations)
        if (unused) {
          setPickerOpen(false)
          onClose()
          navigate(`/chat/${unused.id}`)
          return
        }
      }
      const conversation = await api.createConversation({ agent_profile_id: agentId })
      setPickerOpen(false)
      onClose()
      navigate(`/chat/${conversation.id}`)
    } catch (e) {
      // 还没设过默认智能体（或者一个智能体都没有）：退回「挑一个」的老路径，并把原因说清楚。
      setError(describeError(e))
      setPickerOpen(true)
    } finally {
      setCreating(false)
    }
  }

  if (!open) return null

  const canLogout = authEnabled && user && user.id !== '__system__'
  const displayName = user?.display_name || user?.username || '系统'

  return (
    <div className="m-drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="m-drawer" role="dialog" aria-modal="true" aria-label="导航与对话列表">
        <div className="m-drawer-head">
          <span className="m-brand-mark">
            <BrandMark />
          </span>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="m-drawer-title">
              Coda
              {version && <span className="m-badge">{version}</span>}
            </div>
            <div className="m-drawer-sub">多智能体控制台</div>
          </div>
          <button className="m-icon-btn" onClick={onClose} aria-label="关闭菜单">
            ✕
          </button>
        </div>

        <nav className="m-drawer-nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `m-drawer-link${isActive ? ' active' : ''}`}
              onClick={onClose}
            >
              <span className="m-drawer-link-icon"><NavIcon name={item.icon} size={20} /></span>
              <span>{item.label}</span>
              <span className="m-drawer-chevron">›</span>
            </NavLink>
          ))}
        </nav>

        <div className="m-drawer-section">
          {pickerOpen ? (
            <>
              <div className="m-drawer-section-head">
                <span>选择智能体</span>
                <button className="m-icon-btn" onClick={() => setPickerOpen(false)} aria-label="返回对话列表">
                  ‹
                </button>
              </div>
              {error && <div className="banner err">{error}</div>}
              {agents.length === 0 ? (
                <div className="banner info" style={{ margin: '0 12px' }}>
                  还没有智能体。智能体 = 内核 + 网关 + 模型 + 人格设定，请先到「智能体」页创建。
                </div>
              ) : (
                <div className="m-drawer-scroll">
                  <div className="m-list">
                    {agents.map((agent) => (
                      <button
                        key={agent.id}
                        className="m-item"
                        disabled={creating}
                        onClick={() => void start(agent.id)}
                      >
                        <AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} avatarFile={agent.avatar_file} agentId={agent.id} size={34} />
                        <span className="m-item-main">
                          <span className="m-item-title">
                            {agent.name}
                            {agent.is_default && <span className="pill brand">默认</span>}
                          </span>
                          <span className="m-item-sub">
                            {agent.type_name ?? agent.type_id} · {agent.tagline ?? '无备注'}
                          </span>
                        </span>
                        <span className="pill brand">开始</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="m-drawer-section-head">
                <span>对话</span>
                <button
                  className="btn sm primary"
                  disabled={creating}
                  onClick={() => void start()}
                  title="用默认智能体开一条新对话（智能体与工作目录进去后还能改）"
                >
                  ＋ 新建
                </button>
              </div>
              {error && <div className="banner err">{error}</div>}
              <div className="m-drawer-scroll">
                {loading ? (
                  <p className="dim small">正在加载对话…</p>
                ) : conversations.length === 0 ? (
                  <p className="dim small">
                    还没有对话：点「＋ 新建」直接用默认智能体开始，进去后还能在输入框上方换智能体与工作目录。
                  </p>
                ) : (
                  <div className="m-list">
                    {conversations.map((conversation) => {
                      const running = runningIds.has(conversation.id)
                      // 当前打开的那条正在看，不亮未读；正在回复时以动画优先。
                      const unread =
                        Boolean(conversation.unread) && !running && conversation.id !== activeId
                      return (
                        // 一行里两个按钮（打开对话 / 会话操作），button 不能嵌套，所以拆成两段。
                        <div
                          key={conversation.id}
                          className={`m-item m-conv-row${conversation.id === activeId ? ' active' : ''}`}
                        >
                          <button
                            className="m-conv-main"
                            onClick={() => {
                              onClose()
                              navigate(`/chat/${conversation.id}`)
                            }}
                          >
                            <ConversationAvatar conversation={conversation} />
                            <span className="m-item-main">
                              <span className="m-item-head">
                                {running ? (
                                  <span
                                    className="m-item-state running"
                                    role="img"
                                    aria-label="智能体正在回复"
                                    title="智能体正在回复…"
                                  />
                                ) : unread ? (
                                  <span
                                    className="m-item-state unread"
                                    role="img"
                                    aria-label="有未读回复"
                                    title="有未读回复"
                                  />
                                ) : null}
                                <span className="m-item-title">{conversation.title}</span>
                              </span>
                              <span className="m-item-sub m-item-sub-row">
                                <span className="m-item-meta">
                                  {conversation.agent_name} · {conversation.message_count} 条
                                </span>
                                <span className="m-item-time">{formatListTime(conversation.updated_at)}</span>
                              </span>
                            </span>
                          </button>
                          <button
                            className="m-conv-more"
                            aria-label={`对话「${conversation.title}」的操作`}
                            title="重新生成 / 重置记忆 / 复制工作目录 / 删除"
                            onClick={() => {
                              onClose()
                              navigate(`/chat/${conversation.id}`, { state: { openActions: true } })
                            }}
                          >
                            ⋯
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        <div className="m-drawer-account">
          <div className="m-drawer-account-wrap">
            {accountOpen && (
              <div className="m-drawer-account-menu" role="menu" aria-label="账号菜单">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setAccountOpen(false)
                    onClose()
                    navigate('/settings')
                  }}
                >
                  <span className="m-drawer-account-menu-icon"><SettingsIcon size={14} /></span>
                  设置
                </button>
                {canLogout && (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setAccountOpen(false)
                      logout()
                    }}
                  >
                    <span className="m-drawer-account-menu-icon"><LogoutIcon size={14} /></span>
                    退出登录
                  </button>
                )}
              </div>
            )}
            <button
              type="button"
              className="m-drawer-account-btn"
              onClick={() => setAccountOpen((openMenu) => !openMenu)}
              aria-haspopup="menu"
              aria-expanded={accountOpen}
            >
              <UserAvatar user={user} size={30} className="m-drawer-account-avatar" />
              <span className="m-drawer-account-name">{displayName}</span>
            </button>
          </div>
        </div>
      </aside>
    </div>
  )
}

/** 两个 id 集合是否一样（轮询回来没变化就不触发重渲染）。 */
function sameIdSet(left: Set<string>, right: Set<string>): boolean {
  if (left.size !== right.size) return false
  for (const id of left) if (!right.has(id)) return false
  return true
}
