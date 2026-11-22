import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api'
import { useAuth } from '../../components/AuthGate'
import type {
  AgentRuntime,
  ProvisionEvent,
  ProvisionJobInfo,
  RuntimeAction,
  RuntimeAuth,
  RuntimeLatest,
} from '../../lib/types'
import { RefreshIcon } from '../../components/Icons'
import { KernelLogo } from '../../components/KernelLogo'
import { updateTargetOf, versionCore } from '../../lib/runtimeVersion'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError, relativeTime } from '../util'

const ACTION_LABELS: Record<RuntimeAction, string> = {
  install: '安装',
  update: '更新',
  uninstall: '卸载',
  login: '登录',
}

/**
 * 这两个「内核」没有可管理的实体，不该出现在内核管理列表里（与桌面端 KernelManager 同一规则）：
 * - `custom-cli`：用户自备可执行文件，装没装、在哪平台无从探测；
 * - `openai-harness`：进程内 API 内核，不存在本机安装这一说。
 */
const HIDDEN_RUNTIME_IDS = new Set(['custom-cli', 'openai-harness'])

interface LogLine {
  text: string
  level?: string
}

interface Job {
  runtimeId: string
  name: string
  action: RuntimeAction
  lines: LogLine[]
  /** 登录动作产出的二维码（data URL）；其它动作恒为 null */
  qr?: string | null
  status: 'running' | 'ok' | 'error'
  error?: string | null
}

/**
 * 内核管理（移动端）：与桌面端同一套接口，交互改成卡片 + 底部弹层。
 *
 * 这里只管「装 / 升 / 卸」这一件事；用某个内核的智能体（网关 · 模型 · 人格）在「智能体」页维护。
 * 日志在弹层里滚动，可随时中止。
 */
export function MobileRuntimesPage() {
  const { isAdmin } = useAuth()
  const [runtimes, setRuntimes] = useState<AgentRuntime[]>([])
  const [latest, setLatest] = useState<Record<string, RuntimeLatest>>({})
  const [checking, setChecking] = useState<Record<string, boolean>>({})
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const showLogRef = useRef(false)
  const [showLog, setShowLog] = useState(false)
  const [provisionJobs, setProvisionJobs] = useState<Record<string, ProvisionJobInfo>>({})
  /** 账号型内核的登录状态（按内核 id 缓存） */
  const [auth, setAuth] = useState<Record<string, RuntimeAuth | null>>({})
  const [authLoading, setAuthLoading] = useState<Record<string, boolean>>({})

  const logRef = useRef<HTMLPreElement>(null)

  const checkLatest = async (runtime: AgentRuntime) => {
    if (!runtime.provisioning.commands?.latest?.length) return
    setChecking((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const result = await api.checkRuntimeLatest(runtime.id)
      setLatest((prev) => ({ ...prev, [runtime.id]: result }))
    } catch (e) {
      setLatest((prev) => ({ ...prev, [runtime.id]: { supported: false, latest: null, error: describeError(e) } }))
    } finally {
      setChecking((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  /** 强制重测一个内核（后端把结果写回缓存，界面直接用新值覆盖）。 */
  const refreshStatus = async (runtime: AgentRuntime) => {
    setRefreshing((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const status = await api.refreshRuntime(runtime.id)
      setRuntimes((prev) =>
        prev.map((item) =>
          item.id === runtime.id
            ? {
                ...item,
                provisioning: {
                  ...item.provisioning,
                  installed: status.installed,
                  version: status.version ?? null,
                  path: status.installed ? status.detail ?? null : null,
                  checked_at: status.checked_at ?? null,
                },
              }
            : item,
        ),
      )
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setRefreshing((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  /** 读取账号型内核的登录状态（刷新列表、登录结束后调用）。 */
  const loadAuth = async (runtime: AgentRuntime) => {
    setAuthLoading((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const status = await api.getRuntimeAuth(runtime.id)
      setAuth((prev) => ({ ...prev, [runtime.id]: status }))
    } catch (e) {
      setAuth((prev) => ({
        ...prev,
        [runtime.id]: { supported: false, required: false, logged_in: null, detail: describeError(e) },
      }))
    } finally {
      setAuthLoading((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  const reload = async () => {
    try {
      // 无实体的内核（自定义 CLI / OpenAI 兼容）不进列表 —— 见 HIDDEN_RUNTIME_IDS 的说明。
      const items = (await api.listRuntimes()).filter((item) => !HIDDEN_RUNTIME_IDS.has(item.id))
      setRuntimes(items)
      setError(null)
      // 账号型内核：列表里带登录状态的，顺手探一次（有登录命令或实现了登录态探测）。
      for (const item of items) {
        if (
          item.provisioning.installed &&
          (item.provisioning.commands?.login?.length || item.auth_supported)
        ) {
          void loadAuth(item)
        }
      }
      // 「检查更新」是每个内核一次远端查询，不能挡住首屏：卡片先出来，结果到了再补。
      // 该接口仅管理员可用，成员不要自动触发。
      setLoading(false)
      if (isAdmin) {
        await Promise.all(items.map((item) => checkLatest(item)))
      }
      try {
        const jobs = await api.listProvisionJobs()
        setProvisionJobs(Object.fromEntries(jobs.map((j) => [j.runtime_id, j])))
      } catch {
        /* 忽略 */
      }
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
    return () => abortRef.current?.abort()
  }, [])

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [job?.lines])

  const start = async (runtime: AgentRuntime, action: RuntimeAction) => {
    if (action === 'uninstall') {
      const command = (runtime.provisioning.commands?.uninstall ?? []).join(' ')
      if (!confirm(`确认卸载内核「${runtime.name}」？\n将执行：${command}`)) return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setJob({ runtimeId: runtime.id, name: runtime.name, action, lines: [], qr: null, status: 'running' })
    setShowLog(true)
    showLogRef.current = true
    setProvisionJobs((prev) => ({
      ...prev,
      [runtime.id]: { runtime_id: runtime.id, name: runtime.name, action, status: 'running', started_at: Date.now() / 1000 },
    }))

    const onEvent = (event: ProvisionEvent) => {
      if (event.type === 'provision.started' && event.command) {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: `执行命令：${event.command}` }] } : prev,
        )
      } else if (event.type === 'provision.output') {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: event.text ?? '', level: event.level }] } : prev,
        )
      } else if (event.type === 'provision.qr') {
        setJob((prev) => (prev ? { ...prev, qr: event.data_url ?? null } : prev))
      } else if (event.type === 'provision.finished') {
        setJob((prev) => (prev ? { ...prev, status: event.ok ? 'ok' : 'error', error: event.error } : prev))
        if (event.auth) {
          setAuth((prev) => ({ ...prev, [runtime.id]: event.auth ?? null }))
        }
        setProvisionJobs((prev) =>
          prev[runtime.id]
            ? { ...prev, [runtime.id]: { ...prev[runtime.id], status: event.ok ? 'ok' : 'error', error: event.error } }
            : prev,
        )
        if (!showLogRef.current) {
          setShowLog(true)
          showLogRef.current = true
        }
      }
    }

    const call =
      action === 'install'
        ? api.installRuntime
        : action === 'update'
          ? api.updateRuntime
          : action === 'login'
            ? api.loginRuntime
            : api.uninstallRuntime
    try {
      await call(runtime.id, onEvent, controller.signal)
    } catch (e) {
      setJob((prev) =>
        prev ? { ...prev, status: 'error', error: controller.signal.aborted ? '已中止' : describeError(e) } : prev,
      )
      if (!showLogRef.current) {
        setShowLog(true)
        showLogRef.current = true
      }
    } finally {
      abortRef.current = null
      void reload()
      if (action === 'login') void loadAuth(runtime)
    }
  }

  /** 从后端重连到一个进行中 / 已完成的任务，获取全部日志。 */
  const viewProgress = async (runtimeId: string) => {
    const info = provisionJobs[runtimeId]
    if (!info) return
    setShowLog(true)
    showLogRef.current = true
    if (job && job.runtimeId === runtimeId && job.lines.length > 0) return
    setJob({ runtimeId, name: info.name, action: info.action, lines: [], qr: null, status: info.status })
    const controller = new AbortController()
    abortRef.current = controller
    const onEvent = (event: ProvisionEvent) => {
      if (event.type === 'provision.started' && event.command) {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: `执行命令：${event.command}` }] } : prev,
        )
      } else if (event.type === 'provision.output') {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: event.text ?? '', level: event.level }] } : prev,
        )
      } else if (event.type === 'provision.qr') {
        setJob((prev) => (prev ? { ...prev, qr: event.data_url ?? null } : prev))
      } else if (event.type === 'provision.finished') {
        setJob((prev) => (prev ? { ...prev, status: event.ok ? 'ok' : 'error', error: event.error } : prev))
        if (event.auth) {
          setAuth((prev) => ({ ...prev, [runtimeId]: event.auth ?? null }))
        }
        setProvisionJobs((prev) =>
          prev[runtimeId]
            ? { ...prev, [runtimeId]: { ...prev[runtimeId], status: event.ok ? 'ok' : 'error', error: event.error } }
            : prev,
        )
      }
    }
    try {
      await api.streamProvisionJob(runtimeId, onEvent, controller.signal)
    } catch {
      /* 忽略 */
    } finally {
      abortRef.current = null
      void reload()
    }
  }

  return (
    <div className="m-page">
      <MobileTopBar
        title="内核管理"
        subtitle="内核安装状态与版本"
        actions={
          <button className="m-icon-btn" onClick={() => void reload()} aria-label="刷新">
            ↻
          </button>
        }
      />
      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}
          {loading && <div className="m-empty">正在读取内核状态…</div>}

          <div className="m-list">
            {runtimes.map((runtime) => {
              const provisioning = runtime.provisioning
              const managed = provisioning.supported
              const remote = latest[runtime.id]?.latest ?? null
              const canCheck = isAdmin && Boolean(provisioning.commands?.latest?.length)
              const current = provisioning.version ? versionCore(provisioning.version) : null
              // 查到比本机**更高**的远端版本才提示「更新到 X」；没查到更新（还没查完 /
              // 查询失败 / 已是最新）就默认按「已是最新」显示，不让状态一直空着。
              const updateTarget = updateTargetOf(provisioning.version, latest[runtime.id])
              const showUpdate =
                isAdmin &&
                managed &&
                provisioning.installed &&
                Boolean(provisioning.commands?.update?.length) &&
                (updateTarget !== null || !canCheck || !remote)
              const busy = provisionJobs[runtime.id]?.status === 'running'
              const loginSupported = Boolean(provisioning.commands?.login?.length)
              // 没有登录命令、但实现了登录态探测的内核：只展示状态，不给登录按钮。
              const authSupported = loginSupported || Boolean(runtime.auth_supported)
              const authInfo = auth[runtime.id]
              const loggedIn = authInfo?.logged_in === true
              return (
                <div className="card runtime-card" key={runtime.id}>
                  <div className="runtime-head">
                    <KernelLogo logo={runtime.logo} style={runtime.logo_style} />
                    <div className="m-item-main">
                      <div className="m-item-title">{runtime.name}</div>
                      <div className="m-item-sub runtime-sub">
                        <span>{kernelSummary(runtime, current)}</span>
                        {busy ? null : provisioning.installed && current && Boolean(provisioning.commands?.latest?.length) ? (
                          updateTarget ? (
                            <span className="pill warn">可更新 {updateTarget}</span>
                          ) : (
                            <span className="pill ok" title={latest[runtime.id]?.error ? `远端版本查询失败（${latest[runtime.id]?.error}），暂按已是最新显示` : undefined}>
                              已是最新
                            </span>
                          )
                        ) : null}
                        {authSupported && provisioning.installed && (
                          <span className={`pill ${loggedIn ? 'ok' : 'warn'}`}>
                            {authLoading[runtime.id]
                              ? '检查登录…'
                              : authInfo?.logged_in === true
                                ? '已登录'
                                : authInfo?.logged_in === false
                                  ? '未登录'
                                  : '登录未知'}
                          </span>
                        )}
                      </div>
                      <div className="dim small">{detectedLabel(provisioning.checked_at)}</div>
                    </div>
                    {/* 「重新检测」收成右上角一个小图标（状态本来就带缓存，偶尔点一次） */}
                    {isAdmin && (
                      <button
                        className="m-icon-btn"
                        onClick={() => void refreshStatus(runtime)}
                        disabled={Boolean(refreshing[runtime.id]) || busy}
                        aria-label="重新检测"
                        title="重新检测安装状态与版本"
                      >
                        <RefreshIcon size={17} className={refreshing[runtime.id] ? 'm-spin' : undefined} />
                      </button>
                    )}
                  </div>

                  {managed && provisioning.note && <div className="banner warn">{provisioning.note}</div>}

                  <div className="row wrap" style={{ gap: 6 }}>
                    {busy && (
                      <>
                        <span className="pill brand">正在{ACTION_LABELS[provisionJobs[runtime.id]!.action]}…</span>
                        <button
                          className="btn sm"
                          onClick={() => void viewProgress(runtime.id)}
                        >
                          查看进度
                        </button>
                      </>
                    )}
                    {canCheck && (
                      <button
                        className="btn sm"
                        onClick={() => void checkLatest(runtime)}
                        disabled={Boolean(checking[runtime.id]) || busy}
                      >
                        {checking[runtime.id] ? '查询中…' : '检查更新'}
                      </button>
                    )}
                    {isAdmin && managed && !provisioning.installed && provisioning.commands?.install?.length ? (
                      <button className="btn sm primary" onClick={() => void start(runtime, 'install')} disabled={busy}>
                        安装
                      </button>
                    ) : null}
                    {showUpdate ? (
                      <button className="btn sm primary" onClick={() => void start(runtime, 'update')} disabled={busy}>
                        {updateTarget ? `更新到 ${updateTarget}` : '更新'}
                      </button>
                    ) : null}
                    {isAdmin && loginSupported && provisioning.installed ? (
                      <button
                        className={`btn sm${loggedIn ? '' : ' primary'}`}
                        onClick={() => void start(runtime, 'login')}
                        disabled={busy}
                      >
                        {loggedIn ? '重新登录' : '登录'}
                      </button>
                    ) : null}
                    {isAdmin && managed && provisioning.installed && provisioning.commands?.uninstall?.length ? (
                      <button className="btn sm danger" onClick={() => void start(runtime, 'uninstall')} disabled={busy}>
                        卸载
                      </button>
                    ) : null}
                    {runtime.docs_url && (
                      <a className="btn sm ghost" href={runtime.docs_url} target="_blank" rel="noreferrer">
                        文档 ↗
                      </a>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>

      {job && showLog && (
        <MobileSheet
          title={`${ACTION_LABELS[job.action]}「${job.name}」`}
          onClose={() => {
            if (job.status === 'running') {
              setShowLog(false)
              showLogRef.current = false
            } else {
              if (job) void api.clearProvisionJob(job.runtimeId).catch(() => {})
              setProvisionJobs((prev) => {
                const next = { ...prev }
                if (job) delete next[job.runtimeId]
                return next
              })
              setJob(null)
              setShowLog(false)
              showLogRef.current = false
            }
          }}
          full
          footer={
            job.status === 'running' ? (
              <button className="btn danger block" onClick={() => abortRef.current?.abort()}>
                中止
              </button>
            ) : (
              <button
                className="btn block"
                onClick={() => {
                  if (job) void api.clearProvisionJob(job.runtimeId).catch(() => {})
                  setProvisionJobs((prev) => {
                    const next = { ...prev }
                    if (job) delete next[job.runtimeId]
                    return next
                  })
                  setJob(null)
                  setShowLog(false)
                  showLogRef.current = false
                }}
              >
                关闭
              </button>
            )
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <div className="row wrap" style={{ gap: 8 }}>
              <span className={`pill ${job.status === 'running' ? 'brand' : job.status === 'ok' ? 'ok' : 'err'}`}>
                {job.status === 'running' ? '进行中…' : job.status === 'ok' ? '完成' : '失败'}
              </span>
              <span className="dim small">任务在服务器本地执行。</span>
            </div>
            {job.error && <div className={`banner ${job.status === 'ok' ? 'info' : 'err'}`}>{job.error}</div>}
            {job.qr && (
              <div className="login-qr">
                <img src={job.qr} alt="登录二维码" />
                <span>用手机扫描二维码完成登录；扫码成功后状态会自动更新。</span>
              </div>
            )}
            <pre className="runtime-log" ref={logRef}>
              {job.lines.length === 0
                ? '等待输出…'
                : job.lines.map((line, index) => (
                    <span key={index} className={line.level === 'warn' ? 'runtime-log-warn' : undefined}>
                      {line.text}
                      {'\n'}
                    </span>
                  ))}
            </pre>
          </div>
        </MobileSheet>
      )}
    </div>
  )
}

/** 卡片副标题：所属公司 + 当前版本（卡片上只留这三样，其余信息收进按钮后面的动作里）。 */
function kernelSummary(runtime: AgentRuntime, current: string | null): string {
  const provisioning = runtime.provisioning
  if (!provisioning.supported) {
    return `${runtime.vendor} · ${runtime.runtime === 'api' ? '进程内运行' : '自备可执行文件'}`
  }
  return provisioning.installed && current ? `${runtime.vendor} · 当前版本 ${current}` : `${runtime.vendor} · 未安装`
}

/** 「检测于 …」：安装状态由后端缓存 + 定时检测，不是每次打开页面实时探的。 */
function detectedLabel(checkedAt: string | null | undefined): string {
  const text = relativeTime(checkedAt)
  return text ? `检测于 ${text}` : '尚未检测'
}
