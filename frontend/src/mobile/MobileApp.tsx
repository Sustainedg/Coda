import { useEffect, useState } from 'react'
import { Navigate, Outlet, Route, Routes } from 'react-router-dom'
import { api } from '../lib/api'
import { MobileDrawer } from './components/MobileDrawer'
import { MobileMenuProvider } from './components/MobileMenuButton'
import { MobileChatPage } from './pages/MobileChatPage'
import { MobileCollabPage } from './pages/MobileCollabPage'
import { MobileCollabRunPage } from './pages/MobileCollabRunPage'
import { MobileCollabTeamPage } from './pages/MobileCollabTeamPage'
import { MobileTicketBoardPage } from './pages/MobileTicketBoardPage'
import { MobileAccountPage } from './pages/MobileAccountPage'
import { MobileAgentEditPage } from './pages/MobileAgentEditPage'
import { MobileAgentsPage } from './pages/MobileAgentsPage'
import { MobileCredentialsPage } from './pages/MobileCredentialsPage'
import { MobileGatewaysPage } from './pages/MobileGatewaysPage'
import { MobileRuntimesPage } from './pages/MobileRuntimesPage'
import { MobileSettingsPage } from './pages/MobileSettingsPage'
import { MobileMcpPage } from './pages/MobileMcpPage'
import { MobileSkillsPage } from './pages/MobileSkillsPage'
import { MobileUsagePage } from './pages/MobileUsagePage'

/**
 * 移动端应用：`/m/` 这条链接的独立入口。
 *
 * 与桌面端的 `App.tsx` 物理分离（各自的 HTML、入口、页面与样式表），
 * 只共享与界面无关的基础设施：`lib/api`、`lib/types`、`lib/agentStream` 等。
 */
export function MobileApp() {
  const [version, setVersion] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    // 与桌面端同理：只取版本号，用最轻的探活接口。
    api
      .health()
      .then((info) => setVersion(info.version))
      .catch(() => undefined)
  }, [])

  return (
    <Routes>
      <Route
        element={
          <MobileLayout
            menuOpen={menuOpen}
            onMenuOpen={() => setMenuOpen(true)}
            onMenuClose={() => setMenuOpen(false)}
            version={version}
          />
        }
      >
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="/chat" element={<MobileChatPage />} />
        <Route path="/chat/:conversationId" element={<MobileChatPage />} />
        <Route path="/agents" element={<MobileAgentsPage />} />
        {/* 智能体设定：整页 + 页内标签（基础信息 / 技能 / MCP / IM / 统计），与桌面端同一组。
            `/agents/new` 是静态路由、不匹配 `:agentId`，必须显式传 `creating`，
            否则页面里 `useParams()` 拿不到 id，新建页会永远停在「正在加载…」。 */}
        <Route path="/agents/new" element={<MobileAgentEditPage creating />} />
        <Route path="/agents/:agentId" element={<MobileAgentEditPage />} />
        <Route path="/gateways" element={<MobileGatewaysPage />} />
        <Route path="/credentials" element={<MobileCredentialsPage />} />
        <Route path="/im" element={<Navigate to="/agents" replace />} />
        <Route path="/skills" element={<MobileSkillsPage />} />
        <Route path="/mcp" element={<MobileMcpPage />} />
        <Route path="/usage" element={<MobileUsagePage />} />
        <Route path="/runtimes" element={<MobileRuntimesPage />} />
        <Route path="/collab" element={<MobileCollabPage />} />
        <Route path="/collab/teams/:teamId" element={<MobileCollabTeamPage />} />
        <Route path="/collab/runs/:runId" element={<MobileCollabRunPage />} />
        <Route path="/collab/runs/:runId/tickets" element={<MobileTicketBoardPage />} />
        <Route path="/settings" element={<MobileSettingsPage version={version} />} />
        <Route path="/account" element={<MobileAccountPage />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Route>
    </Routes>
  )
}

/**
 * 外壳：内容区 + 左侧抽屉。
 *
 * 移动端只留这一处主导航：底部的 Tab 栏已取消，导航与对话列表都收进左上角 logo 打开的
 * 抽屉里，内容区整屏归页面（独立应用里没有浏览器的后退键，抽屉是唯一的出口）。
 */
function MobileLayout({
  menuOpen,
  onMenuOpen,
  onMenuClose,
  version,
}: {
  menuOpen: boolean
  onMenuOpen: () => void
  onMenuClose: () => void
  version: string
}) {
  return (
    <MobileMenuProvider onOpen={onMenuOpen}>
      <div className="m-app">
        <div className="m-main">
          <Outlet />
        </div>
        <MobileDrawer open={menuOpen} onClose={onMenuClose} version={version} />
      </div>
    </MobileMenuProvider>
  )
}
