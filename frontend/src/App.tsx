import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { BrandMark } from './components/BrandMark'
import { UserAvatar } from './components/UserAvatar'
import {
  NavIcon,
  LogoutIcon,
  SettingsIcon,
  type NavIconName,
} from './components/Icons'
import { ConversationList } from './components/ConversationList'
import { api } from './lib/api'
import {
  isSidebarForcedCollapsed,
  readSidebarCollapsed,
  subscribeSidebarCollapsed,
  subscribeSidebarViewport,
  toggleSidebarCollapsed,
} from './lib/sidebarCollapsed'
import { useAuth } from './components/AuthGate'
import { ChatPage } from './pages/ChatPage'
import { CollabPage } from './pages/CollabPage'
import { AgentEditPage } from './pages/AgentEditPage'
import { AgentsPage } from './pages/AgentsPage'
import { McpPage } from './pages/McpPage'
import { SkillsPage } from './pages/SkillsPage'
import { SettingsPage } from './pages/SettingsPage'
import { UsagePage } from './pages/UsagePage'

//: 与移动端抽屉的主导航**同一份菜单**（顺序、命名、图标都一样）：
//: 「内核管理」「自定义网关」不在菜单里，收在「设置」页的标签页里（移动端是配置入口）。
//: 图标存的是 `NavIconName` key，真实图形在 components/Icons.tsx 的 `NavIcon` 里取
//: （两端共用的线条 SVG，颜色走 `--nav-icon`，hover / 选中换成品牌色，不用 emoji）。
const NAV: { to: string; icon: NavIconName; label: string }[] = [
  { to: '/chat', icon: 'chat', label: '对话' },
  { to: '/agents', icon: 'agents', label: '智能体' },
  { to: '/collab', icon: 'collab', label: '协作组' },
  { to: '/skills', icon: 'skills', label: '技能' },
  { to: '/mcp', icon: 'mcp', label: 'MCP' },
  { to: '/usage', icon: 'usage', label: '用量' },
]

/** 老的「协作设置」深链 → 智能体页的协作组标签，并尽量保留原来选中的那条协作组。 */
function CollabSettingsRedirect() {
  const { teamId } = useParams<{ teamId?: string }>()
  return (
    <Navigate
      to={teamId ? `/agents?tab=collab&team=${encodeURIComponent(teamId)}` : '/agents?tab=collab'}
      replace
    />
  )
}

/**
 * 桌面端外壳：左侧栏（品牌 + 主导航 + 对话列表）+ 右侧内容区。
 *
 * 对话列表与移动端抽屉里那份同构 —— 导航和对话都在同一块，不再让聊天页自带第二条列表栏。
 * 导航的顺序、命名与行的样式（图标 · 文字 · 右侧箭头）也和移动端抽屉对齐；
 * 「内核管理」「自定义网关」不在主导航里，收在「设置」页的标签页里（与移动端一致）。
 */
export default function App() {
  const { user, authEnabled, logout } = useAuth()
  const navigate = useNavigate()
  const [version, setVersion] = useState('')
  /** 用户手动折叠的本机偏好（宽窗时生效） */
  const manualCollapsed = useSyncExternalStore(subscribeSidebarCollapsed, readSidebarCollapsed)
  /** 窗口窄到放不下展开的侧栏：强制折叠，避免对话列表被挤变形 */
  const narrow = useSyncExternalStore(subscribeSidebarViewport, isSidebarForcedCollapsed)
  const collapsed = narrow || manualCollapsed
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const userMenuRef = useRef<HTMLDivElement | null>(null)

  // 账号菜单：点外面或按 Esc 收起（侧栏底部只剩头像 + 用户名，设置与退出都收进这个菜单）。
  useEffect(() => {
    if (!userMenuOpen) return
    const onPointerDown = (event: MouseEvent) => {
      if (!userMenuRef.current?.contains(event.target as Node)) setUserMenuOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setUserMenuOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [userMenuOpen])

  const displayName = user?.display_name || user?.username || '系统'

  // 折叠真值在 store 里，切换后由 useSyncExternalStore 触发重渲染
  const toggleSidebar = () => {
    toggleSidebarCollapsed()
  }

  useEffect(() => {
    // 侧边栏只要一个版本号：走最轻的 /api/health，别让每次打开页面都去汇总系统信息。
    api
      .health()
      .then((info) => setVersion(info.version))
      .catch(() => undefined)
  }, [])

  return (
    <div className={`app-shell${collapsed ? ' collapsed' : ''}`}>
      <aside className="sidebar" id="app-sidebar">
        <div className="brand">
          <div className="brand-mark">
            <BrandMark />
          </div>
          <div className="brand-text">
            <div className="brand-name">
              Coda
              {version && <span className="brand-version">{version}</span>}
            </div>
            <div className="brand-sub">多智能体控制台</div>
          </div>
          {/* 折叠入口有两处：这里的 ‹/› 与对话页顶栏的汉堡（同一份状态，见 lib/sidebarCollapsed）。
              非对话页面没有顶栏汉堡，这里的按钮是它们唯一的折叠出口。 */}
          {!narrow && (
            <button
              type="button"
              className="sidebar-toggle"
              onClick={toggleSidebar}
              aria-expanded={!collapsed}
              aria-controls="app-sidebar"
              aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
              title={collapsed ? '展开侧边栏' : '折叠侧边栏'}
            >
              {collapsed ? '›' : '‹'}
            </button>
          )}
        </div>
        <nav className="stack" style={{ gap: 2 }}>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            >
              <span className="nav-icon"><NavIcon name={item.icon} size={20} /></span>
              <span className="nav-label">{item.label}</span>
              {/* 折叠后只剩图标：hover 时从图标右侧弹出名字气泡（CSS 控制，只在折叠态显示） */}
              <span className="nav-tip">{item.label}</span>
              <span className="nav-chevron">›</span>
            </NavLink>
          ))}
        </nav>
        <ConversationList />
        <div className="sidebar-footer">
          <div className="sidebar-user-wrap" ref={userMenuRef}>
            {userMenuOpen && (
              <div className="sidebar-user-menu" role="menu" aria-label="账号菜单">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setUserMenuOpen(false)
                    navigate('/settings')
                  }}
                >
                  <span className="sidebar-user-menu-icon"><SettingsIcon size={14} /></span>
                  设置
                </button>
                {authEnabled && user && user.id !== '__system__' && (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setUserMenuOpen(false)
                      logout()
                    }}
                  >
                    <span className="sidebar-user-menu-icon"><LogoutIcon size={14} /></span>
                    退出登录
                  </button>
                )}
              </div>
            )}
            <button
              type="button"
              className="sidebar-user-btn"
              onClick={() => setUserMenuOpen((open) => !open)}
              aria-haspopup="menu"
              aria-expanded={userMenuOpen}
              title={user?.role === 'admin' ? '管理员' : undefined}
            >
              <UserAvatar user={user} size={28} className="sidebar-user-avatar" />
              <span className="sidebar-user-name">{displayName}</span>
            </button>
          </div>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/collab" element={<CollabPage />} />
          {/* 协作组设置已移到「智能体 → 协作组」；老地址保留重定向，书签与外部链接不会失效。 */}
          <Route path="/collab/settings" element={<Navigate to="/agents?tab=collab" replace />} />
          <Route path="/collab/settings/:teamId" element={<CollabSettingsRedirect />} />
          <Route path="/collab/runs/:runId" element={<CollabPage />} />
          <Route path="/roles" element={<Navigate to="/agents" replace />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/mcp" element={<McpPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          {/* 智能体设定：整页 + 页内标签（基础信息 / 技能 / MCP / IM / 统计）。
              「新建」的内核选择表单也走这一页（new 只是占位 id，页面内完成创建）。 */}
          <Route path="/agents/new" element={<AgentEditPage />} />
          <Route path="/agents/:agentId" element={<AgentEditPage />} />
          {/* 内核管理已经收进「设置 → 内核管理」（以前这里有独立一页，还从设置里再链一次，
              等于同一个东西有两个入口）。老地址保留重定向，书签不会失效。 */}
          <Route path="/runtimes" element={<Navigate to="/settings?tab=kernels" replace />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  )
}
