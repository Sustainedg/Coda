import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../components/AuthGate'
import { THEME_OPTIONS, useThemeMode } from '../../lib/theme'
import { MobileTopBar } from '../components/MobileTopBar'
import { ProxySettings } from '../../components/ProxySettings'
import { UserAvatar } from '../../components/UserAvatar'
import { TitleSettingsSection } from '../../components/TitleSettings'
import { isIos, isStandalone, refreshShell, useInstallController } from '../pwa'

/**
 * 设置：外观 + 配置入口 + 少数维护动作。
 *
 * 手机上只留「现在要动的开关」：运行概览、版本记录这些信息在桌面端看更合适，
 * 小屏上重复一遍只会把真正要用的入口挤下去。
 */
export function MobileSettingsPage({ version }: { version: string }) {
  const navigate = useNavigate()
  const { user, authEnabled, logout } = useAuth()
  const install = useInstallController()
  const { mode, setMode } = useThemeMode()
  /** 刚切过外观：iOS 独立应用的状态栏样式只在启动时读取，得提示重开才生效 */
  const [themeChanged, setThemeChanged] = useState(false)

  return (
    <div className="m-page">
      <MobileTopBar
        title="设置"
        badge={version ? <span className="m-badge">{version}</span> : undefined}
        subtitle="外观与配置入口"
      />

      <div className="m-page-body">
        <div className="stack">
          {authEnabled && user && user.id !== '__system__' && (
            <div className="stack" style={{ gap: 8 }}>
              <div className="card-section-title">账号</div>
              <button className="m-item" onClick={() => navigate('/account')}>
                <UserAvatar user={user} size={30} />
                <span className="m-item-main">
                  <span className="m-item-title">{user.display_name || user.username}</span>
                  <span className="m-item-sub">
                    {user.role === 'admin' ? '管理员' : '成员'} · 资料 / 口令 / 退出登录
                  </span>
                </span>
                <span className="m-item-chevron">›</span>
              </button>
              <div className="row wrap" style={{ gap: 8 }}>
                <button className="btn sm" onClick={logout}>
                  退出登录
                </button>
              </div>
            </div>
          )}

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">外观</div>
            <div className="theme-picker">
              {THEME_OPTIONS.map((option) => {
                const active = mode === option.value
                return (
                  <button
                    key={option.value}
                    className={`theme-option${active ? ' active' : ''}`}
                    aria-pressed={active}
                    onClick={() => {
                      setMode(option.value)
                      setThemeChanged(true)
                    }}
                  >
                    <span className="theme-option-head">
                      <span className="theme-option-label">{option.label}</span>
                      {active && <span className="theme-option-check">✓</span>}
                    </span>
                    <span className="theme-option-hint">{option.hint}</span>
                  </button>
                )
              })}
            </div>
            <p className="dim small">外观只存在本机，不影响别人。</p>
            {themeChanged && isIos() && isStandalone() && (
              // iOS 限制：apple-mobile-web-app-status-bar-style 只在应用启动时读取，
              // 运行中改 meta 不生效 —— 深色下不重开就会留着一条浅色的状态栏（看起来像「顶部空了」）。
              <p className="dim small">
                状态栏由 iOS 在启动时读取：请把应用从后台划掉再打开，状态栏才会跟着外观切换。
              </p>
            )}
          </div>

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">配置</div>
            <button className="m-item" onClick={() => navigate('/agents')}>
              <span style={{ fontSize: 18 }}>🤖</span>
              <span className="m-item-main">
                <span className="m-item-title">智能体</span>
                <span className="m-item-sub">
                  人格 · 内核 · 网关 · 技能 / MCP 分配 · IM 绑定 · 用量统计
                </span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/skills')}>
              <span style={{ fontSize: 18 }}>🧩</span>
              <span className="m-item-main">
                <span className="m-item-title">技能</span>
                <span className="m-item-sub">统一编目各内核的技能，通过智能体分配生效</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/mcp')}>
              <span style={{ fontSize: 18 }}>🔌</span>
              <span className="m-item-main">
                <span className="m-item-title">MCP</span>
                <span className="m-item-sub">统一编目 MCP 服务器，通过智能体分配生效</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/gateways')}>
              <span style={{ fontSize: 18 }}>🌐</span>
              <span className="m-item-main">
                <span className="m-item-title">自定义网关</span>
                <span className="m-item-sub">API 密钥的唯一来源；官方链路 + 自建网关</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/credentials')}>
              <span style={{ fontSize: 18 }}>🔑</span>
              <span className="m-item-main">
                <span className="m-item-title">凭据库</span>
                <span className="m-item-sub">工具 / 服务要用的密钥，本地加密保存，智能体按引用使用</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/runtimes')}>
              <span style={{ fontSize: 18 }}>🧬</span>
              <span className="m-item-main">
                <span className="m-item-title">内核管理</span>
                <span className="m-item-sub">内核的安装 / 更新 / 卸载，以及当前版本与检测状态</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
          </div>

          <TitleSettingsSection />

          <ProxySettings />

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">更多</div>
            <div className="row wrap" style={{ gap: 8 }}>
              {!install.standalone && install.nativeReady && (
                <button className="btn sm primary" onClick={() => void install.install()}>
                  添加到主屏
                </button>
              )}
              <button className="btn sm" onClick={() => void refreshShell()}>
                更新到最新版本
              </button>
              <a className="btn sm ghost" href="/">
                切换到桌面端
              </a>
            </div>
            {!install.standalone && install.ios && (
              <p className="dim small">iPhone / iPad：在 Safari 的「分享」里选「添加到主屏幕」。</p>
            )}
            <p className="dim small">
              「更新到最新版本」会清掉离线缓存并重新加载 —— 独立应用（添加到主屏）里没有刷新按钮，这是
              卡在旧外壳时的出口。
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
