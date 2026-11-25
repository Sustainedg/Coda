import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AccountPanel } from '../components/AccountPanel'
import { UserAdminPanel } from '../components/UserAdminPanel'
import { CredentialVault } from '../components/CredentialVault'
import { GatewayManager } from '../components/GatewayManager'
import { KernelManager } from '../components/KernelManager'
import { ProxySettings } from '../components/ProxySettings'
import { TitleSettingsSection } from '../components/TitleSettings'
import { TraceSettingsSection } from '../components/TraceSettingsSection'
import { UpdatePanel } from '../components/UpdatePanel'
import { api, ApiError } from '../lib/api'
import { desktopUpdates } from '../lib/desktop'
import { useAuth } from '../components/AuthGate'
import { THEME_OPTIONS, useThemeMode } from '../lib/theme'
import type { Overview } from '../lib/types'

/**
 * 设置页的标签页：**按设置类型分组**，一页只放一类设置。
 *
 * 「关于」收的是**只读信息**（系统信息 · 版本记录）；
 * 「内核管理」管各 CLI 内核本身（装没装 / 什么版本 / 安装 · 更新 · 卸载），
 * 以及「正在运行」的智能体（对话域 + 协作域，每 8 秒刷新，见 `KernelManager`）。
 * IM 不在这里：它是按智能体绑定的通道，收在每个智能体设定页的「IM」标签里。
 * 顺序按「日常会动的在前」排：外观 / 对话最常改，内核管理与关于基本只看。
 */
const ALL_TABS = [
  { id: 'appearance', label: '外观' },
  { id: 'account', label: '账号' },
  { id: 'users', label: '用户' },
  { id: 'chat', label: '对话' },
  { id: 'gateways', label: '网关' },
  { id: 'credentials', label: '凭据' },
  { id: 'network', label: '网络' },
  { id: 'kernels', label: '内核管理' },
  { id: 'about', label: '关于' },
] as const

//: 标签页定义的旧名字（守卫测试按这个名字找）；实际显示时会按角色过滤掉「用户」。
const TABS = ALL_TABS

type SettingsTab = (typeof ALL_TABS)[number]['id']

function isSettingsTab(value: string | null): value is SettingsTab {
  return ALL_TABS.some((item) => item.id === value)
}

/**
 * 设置（桌面端）：按设置类型分标签页。当前页写进 `?tab=` —— 刷新、分享链接，
 * 以及老地址 `/runtimes → /settings?tab=kernels` 的重定向都能落到正确的页。
 *
 * 标签页只做「分组」，各项设置的组件与文档语义都没变 —— 每个组件仍自带标题与说明，
 * 换页只是把它们放到不同页里。移动端的设置页**不跟这套**：它刻意只留「现在要动的开关」。
 */
export function SettingsPage() {
  const { isAdmin } = useAuth()
  const { mode, setMode } = useThemeMode()
  const [params, setParams] = useSearchParams()
  const current = params.get('tab')
  const requested: SettingsTab = isSettingsTab(current) ? current : 'appearance'
  // 「用户」只有管理员能看；非管理员直接落到外观，避免链接能打开一个空页。
  const tab: SettingsTab = requested === 'users' && !isAdmin ? 'appearance' : requested
  const tabs = TABS.filter((item) => item.id !== 'users' || isAdmin)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [error, setError] = useState<string | null>(null)

  const setTab = (next: SettingsTab) => {
    // replace：切标签不该往浏览器历史里塞一串记录 —— 退一次就离开设置页更符合直觉。
    setParams(next === 'appearance' ? {} : { tab: next }, { replace: true })
  }

  const reload = async () => {
    try {
      setOverview(await api.overview())
      setError(null)
    } catch (e) {
      setError(describe(e))
    }
  }

  // 「关于」需要概览（版本号 + 版本记录）：进这一页拉一次就够 ——
  // 「正在运行的智能体」已挪去内核管理页，这里不再每 8 秒轮询。
  useEffect(() => {
    if (tab !== 'about') return
    void reload()
    // reload 每次渲染都是新的，这里只盯标签页
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  const versionHistory = overview?.version_history ?? []

  return (
    <>
      <header className="page-header settings-head">
        <div>
          <h1 className="page-title">设置</h1>
          <p className="page-desc">
            按类型分页：外观 · 账号{isAdmin ? ' · 用户' : ''} · 对话 · 网关 · 凭据 · 网络 · 内核管理 · 关于；
            IM 收在每个智能体设定页里。
          </p>
        </div>
      </header>

      {/* 标签栏独占一行：7 个标签，塞在页头右侧会被挤成一团（标签样式见 .tabs / .tab）。 */}
      <div className="settings-tabbar">
        <div className="tabs">
          {tabs.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`tab${tab === item.id ? ' active' : ''}`}
              aria-pressed={tab === item.id}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}

        {tab === 'appearance' && (
          <section className="stack">
            <h2 className="card-section-title">主题</h2>
            <div className="theme-picker">
              {THEME_OPTIONS.map((option) => {
                const active = mode === option.value
                return (
                  <button
                    key={option.value}
                    className={`theme-option${active ? ' active' : ''}`}
                    aria-pressed={active}
                    onClick={() => setMode(option.value)}
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
            <p className="dim small">外观是这台设备的个人偏好，只存在本机，不影响别人。</p>
          </section>
        )}

        {tab === 'account' && <AccountPanel />}

        {tab === 'users' && isAdmin && <UserAdminPanel />}

        {/* 「对话」标签：「调试模式」是**用户级**开关（所有人都能改自己的），
            与平台级的「自动起标题」（管理员）并列；后者仍只给管理员编辑入口。 */}
        {tab === 'chat' && (
          <>
            <TraceSettingsSection />
            {isAdmin ? (
              <TitleSettingsSection />
            ) : (
              <section className="card">
                <div className="card-title">自动起标题由管理员配置</div>
                <p className="dim small">这一项影响全平台，只有管理员能修改。</p>
              </section>
            )}
          </>
        )}

        {tab === 'gateways' && <GatewayManager />}

        {tab === 'credentials' && <CredentialVault />}

        {tab === 'network' &&
          (isAdmin ? (
            <ProxySettings />
          ) : (
            <section className="card">
              <div className="card-title">网络代理由管理员配置</div>
              <p className="dim small">这一项影响全平台的安装 / 更新，只有管理员能修改。</p>
            </section>
          ))}

        {tab === 'kernels' && <KernelManager />}

        {tab === 'about' && (
          <>
            {/* 桌面端的更新是本地壳操作（下载完整安装包），不要求管理员；
                网页 / Docker 的一键更新是平台级动作，仍只给管理员。 */}
            {(isAdmin || desktopUpdates()) && (
              <UpdatePanel currentVersion={overview?.version ?? '…'} />
            )}

            {/* 系统信息：只读概况（版本 / 适配器 / 数据目录 / 工作目录根）。
                任务运行状态已挪到「内核管理」页（那里每 8 秒刷新），
                这一页只在进入时按需拉一次概览。 */}
            <section className="stack">
              <div className="row" style={{ gap: 8 }}>
                <h2 className="card-section-title" style={{ flex: 1 }}>
                  系统信息
                </h2>
                <button className="btn sm" onClick={() => void reload()}>
                  ↻ 刷新
                </button>
              </div>
              {overview ? (
                <div className="grid">
                  <Stat label="版本" value={overview.version} />
                  <Stat label="适配器" value={`${overview.installed_types} / ${overview.agent_types} 可用`} />
                  <Stat label="数据目录" value={overview.data_dir} mono />
                  <Stat label="工作目录根" value={overview.workspaces_root} mono />
                </div>
              ) : (
                <Loading />
              )}
            </section>

            {versionHistory.length > 0 && (
              <section className="stack">
                <h2 className="card-section-title">版本记录</h2>
                {/* 最新一版做成 hero：当前运行的就是它，值得占最大的位置。
                    下面的历史版本走轻量时间线 —— 看一眼走过哪些里程碑就够了。 */}
                <article className="version-hero">
                  <div className="version-hero-head">
                    <div className="version-hero-version">
                      <span className="version-hero-badge">当前版本</span>
                      <span className="version-hero-number">{versionHistory[0].version}</span>
                    </div>
                    <span className="version-hero-date">{versionHistory[0].date}</span>
                  </div>
                  <h3 className="version-hero-title">{versionHistory[0].title}</h3>
                  <ul className="version-hero-highlights">
                    {versionHistory[0].highlights.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </article>
                {versionHistory.length > 1 && (
                  <div className="version-timeline">
                    {versionHistory.slice(1).map((release) => (
                      <article className="version-item" key={release.version}>
                        <span className="version-marker" />
                        <div className="version-content">
                          <div className="version-head">
                            <span className="version-tag">{release.version}</span>
                            <span className="version-title">{release.title}</span>
                            <span className="dim small version-date">{release.date}</span>
                          </div>
                          <ul className="version-highlights">
                            {release.highlights.map((item) => (
                              <li key={item}>{item}</li>
                            ))}
                          </ul>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            )}
          </>
        )}
      </div>
    </>
  )
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="card" style={{ gap: 6 }}>
      <span className="card-section-title">{label}</span>
      <span className={mono ? 'mono-sm' : ''} style={{ wordBreak: 'break-all' }}>
        {value}
      </span>
    </div>
  )
}

/** 概览还没回来时的占位：这些标签页只在需要时才拉数据，别让页面看起来是空的。 */
function Loading() {
  return (
    <div className="row">
      <span className="spin">◌</span> 正在加载…
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
