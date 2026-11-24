import { useEffect, useRef, useState, type RefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshIcon } from './Icons'
import { KernelLogo } from './KernelLogo'
import { Modal } from './Modal'
import { useAuth } from './AuthGate'
import { api, ApiError } from '../lib/api'
import { formatListTime } from '../lib/datetime'
import { trackKernelLogoReady } from '../lib/kernelIcons'
import { updateTargetOf, versionCore } from '../lib/runtimeVersion'
import type {
  ActiveCollabRun,
  AgentRuntime,
  Overview,
  ProvisionEvent,
  ProvisionJobInfo,
  RuntimeAction,
  RuntimeAuth,
  RuntimeLatest,
} from '../lib/types'

const ACTION_LABELS: Record<RuntimeAction, string> = {
  install: '安装',
  update: '更新',
  uninstall: '卸载',
  login: '登录',
}

/**
 * 这两个「内核」没有可管理的实体，不该出现在内核管理列表里：
 * - `custom-cli`：用户自备可执行文件，装没装、在哪平台无从探测；
 * - `openai-harness`：进程内 API 内核，不存在本机安装这一说。
 * 它们仍可在智能体配置里被选用（只是「内核管理」不展示卡片）。
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
 * 内核管理：各 CLI 内核在本机的那份可执行文件（装没装、什么版本、要不要升级）。
 *
 * 与「智能体」页分工不同：那里配置的是「怎么调用某个内核」，这里管的是**内核本身**。
 * 安装 / 更新 / 卸载都是改本机环境的长任务，执行过程以日志流实时回显、随时可中止。
 *
 * 交互：列表只给**一行摘要**（Logo · 名称 · 公司 / 版本 · 状态），点卡片进**详情**看完整信息
 * 与动作 —— 动作按钮与安装日志都收在详情里，卡片本身保持干净（一处实现，桌面端设置页用）。
 */
export function KernelManager() {
  const { isAdmin } = useAuth()
  const navigate = useNavigate()
  const [runtimes, setRuntimes] = useState<AgentRuntime[]>([])
  const [latest, setLatest] = useState<Record<string, RuntimeLatest>>({})
  const [checking, setChecking] = useState<Record<string, boolean>>({})
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [provisionJobs, setProvisionJobs] = useState<Record<string, ProvisionJobInfo>>({})
  /** 账号型内核的登录状态（按内核 id 缓存；打开详情时才探测） */
  const [auth, setAuth] = useState<Record<string, RuntimeAuth | null>>({})
  const [authLoading, setAuthLoading] = useState<Record<string, boolean>>({})
  /** 正在看详情的内核 id（点卡片打开） */
  const [openId, setOpenId] = useState<string | null>(null)
  /** 「正在运行」：对话域的运行表（overview.active_runs）+ 协作域的运行表（另一张表）一起列出 */
  const [activeRuns, setActiveRuns] = useState<Overview['active_runs']>([])
  const [collabRuns, setCollabRuns] = useState<ActiveCollabRun[]>([])
  const abortRef = useRef<AbortController | null>(null)
  const logRef = useRef<HTMLPreElement>(null)
  /**
   * 「页面可见」的时刻：组件挂载即代表这一页已经出现在屏幕上。
   * 图标耗时的度量以它为起点（页面可见 → 图标全部显示完成）。
   */
  const visibleAtRef = useRef(performance.now())
  /** 是否已经记过一次图标耗时（手动刷新不再重复记）。 */
  const measuredRef = useRef(false)

  const openRuntime = runtimes.find((item) => item.id === openId) ?? null
  // 只认「属于当前这条详情」的任务日志：别的内核的安装进度不该在这里串台。
  const openJob = job && job.runtimeId === openId ? job : null

  const checkLatest = async (runtime: AgentRuntime) => {
    if (!runtime.provisioning.commands?.latest?.length) return
    setChecking((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const result = await api.checkRuntimeLatest(runtime.id)
      setLatest((prev) => ({ ...prev, [runtime.id]: result }))
    } catch (e) {
      setLatest((prev) => ({ ...prev, [runtime.id]: { supported: false, latest: null, error: describe(e) } }))
    } finally {
      setChecking((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  /** 强制重测一个内核（后端会把结果写回缓存，界面直接用新值覆盖）。 */
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
      // 账号型内核：重新检测也顺手重探一次登录态 —— 用户在终端里 /login 之后，
      // 回到页面点一下「重新检测」就能把「未登录」翻成「已登录」。
      if (runtime.provisioning.installed && (runtime.provisioning.commands?.login?.length || runtime.auth_supported)) {
        void loadAuth(runtime)
      }
    } catch (e) {
      setError(describe(e))
    } finally {
      setRefreshing((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  /** 读取账号型内核的登录状态（打开详情弹层、登录结束后调用）。 */
  const loadAuth = async (runtime: AgentRuntime) => {
    setAuthLoading((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const status = await api.getRuntimeAuth(runtime.id)
      setAuth((prev) => ({ ...prev, [runtime.id]: status }))
    } catch (e) {
      setAuth((prev) => ({
        ...prev,
        [runtime.id]: {
          supported: false,
          required: false,
          logged_in: null,
          detail: describe(e),
        },
      }))
    } finally {
      setAuthLoading((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  /** 多账号内核：切换 / 删除一个已保存的账号，成功后就地刷新登录状态。 */
  const switchAccount = async (runtime: AgentRuntime, accountId: string, remove = false) => {
    try {
      const status = remove
        ? await api.removeRuntimeAccount(runtime.id, accountId)
        : await api.activateRuntimeAccount(runtime.id, accountId)
      setAuth((prev) => ({ ...prev, [runtime.id]: status }))
    } catch (e) {
      setError(describe(e))
      setAuth((prev) => {
        const current = prev[runtime.id]
        return {
          ...prev,
          [runtime.id]: {
            supported: true,
            required: current?.required ?? true,
            logged_in: current?.logged_in ?? null,
            account: current?.account ?? null,
            accounts: current?.accounts ?? null,
            detail: describe(e),
          },
        }
      })
    }
  }

  const reload = async () => {
    const apiStartedAt = performance.now()
    try {
      // 无实体的内核（自定义 CLI / OpenAI 兼容）不进列表 —— 见 HIDDEN_RUNTIME_IDS 的说明。
      const items = (await api.listRuntimes()).filter((item) => !HIDDEN_RUNTIME_IDS.has(item.id))
      const apiMs = performance.now() - apiStartedAt
      setRuntimes(items)
      setError(null)
      // 账号型内核：列表里顺手探一次登录状态（与移动端同一策略），卡片才能直接
      // 显示「已登录 / 未登录」，而不是非得点开详情才知道（探测是本地配置读取）。
      for (const item of items) {
        if (
          item.provisioning.installed &&
          (item.provisioning.commands?.login?.length || item.auth_supported)
        ) {
          void loadAuth(item)
        }
      }
      // 「检查更新」是每个内核一次远端查询（npm view 之类），不能挡住首屏：
      // 卡片先出来，查到结果再逐个补上。该接口仅管理员可用，成员不要自动触发。
      setLoading(false)
      // 图标秒显的度量：页面可见 → 全部图标显示完成（含内核列表接口的耗时）。
      // 只记首次进入这一笔：手动「刷新」不是「进入页面」，混进来会污染统计。
      if (!measuredRef.current) {
        measuredRef.current = true
        void trackKernelLogoReady(
          items.map((item) => item.logo ?? ''),
          visibleAtRef.current,
          apiMs,
        )
      }
      if (isAdmin) {
        await Promise.all(items.map((item) => checkLatest(item)))
      }
      // 任务日志可能带登录二维码等短期凭据，接口只对管理员开放：成员不要自动调用。
      if (isAdmin) {
        try {
          const jobs = await api.listProvisionJobs()
          setProvisionJobs(Object.fromEntries(jobs.map((j) => [j.runtime_id, j])))
        } catch {
          /* 忽略 */
        }
      }
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
    return () => abortRef.current?.abort()
  }, [])

  /** 「正在运行」每 8 秒刷新：状态与时长自己在走，开着这页就能盯着任务跑完。 */
  useEffect(() => {
    const loadRuns = async () => {
      try {
        const [info, collab] = await Promise.all([api.overview(), api.listActiveCollabRuns()])
        setActiveRuns(info.active_runs)
        setCollabRuns(collab)
      } catch {
        /* 运行表拉不到不影响内核列表：下一轮再试 */
      }
    }
    void loadRuns()
    const timer = setInterval(() => void loadRuns(), 8000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [openJob?.lines])

  const start = async (runtime: AgentRuntime, action: RuntimeAction) => {
    if (action === 'uninstall') {
      const command = (runtime.provisioning.commands?.uninstall ?? []).join(' ')
      if (!confirm(`确认卸载内核「${runtime.name}」？\n将执行：${command}`)) return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setJob({ runtimeId: runtime.id, name: runtime.name, action, lines: [], qr: null, status: 'running' })
    setProvisionJobs((prev) => ({
      ...prev,
      [runtime.id]: {
        runtime_id: runtime.id,
        name: runtime.name,
        action,
        status: 'running',
        started_at: Date.now() / 1000,
      },
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
            ? {
                ...prev,
                [runtime.id]: {
                  ...prev[runtime.id],
                  status: event.ok ? 'ok' : 'error',
                  error: event.error,
                },
              }
            : prev,
        )
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
        prev ? { ...prev, status: 'error', error: controller.signal.aborted ? '已中止' : describe(e) } : prev,
      )
    } finally {
      abortRef.current = null
      void reload()
      if (action === 'login') void loadAuth(runtime)
    }
  }

  const cancel = () => {
    abortRef.current?.abort()
    setJob((prev) => (prev ? { ...prev, status: 'error', error: '已中止' } : prev))
  }

  /** 从后端重连到一个进行中 / 已完成的任务，把全部日志取回来。 */
  const viewProgress = async (runtimeId: string) => {
    const info = provisionJobs[runtimeId]
    if (!info) return
    // 本次会话已经拿过它的日志了，直接展示
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

  const openDetail = (runtime: AgentRuntime) => {
    setOpenId(runtime.id)
    // 账号型内核：打开详情时探一次登录状态（用户主动触发，不拖慢列表）。
    // 有登录命令或实现了登录态探测的都算（后者只能展示状态，没有登录入口）。
    if (
      runtime.provisioning.installed &&
      (runtime.provisioning.commands?.login?.length || runtime.auth_supported)
    ) {
      void loadAuth(runtime)
    }
    // 这个内核还有没看完的任务：一进来就把日志接上（进度页与安装动作在同一个窗口里）。
    if (provisionJobs[runtime.id] && job?.runtimeId !== runtime.id) void viewProgress(runtime.id)
  }

  const closeDetail = () => {
    const closing = openId
    setOpenId(null)
    // 任务还在跑：留着记录（再点开还能看进度），只是把窗口关掉。
    if (!job || job.runtimeId !== closing || job.status === 'running') return
    void api.clearProvisionJob(job.runtimeId).catch(() => {})
    setProvisionJobs((prev) => {
      const next = { ...prev }
      delete next[job.runtimeId]
      return next
    })
    setJob(null)
  }

  return (
    <>
      <div className="stack">
        {error && <div className="banner err">{error}</div>}

        {/* 正在运行：跑着的智能体一眼可见（对话域 + 协作域两张运行表合在一起）；
            有活儿在干时才显示这一段，空闲时整个板块不出现。 */}
        {activeRuns.length + collabRuns.length > 0 && (
          <section className="stack">
            <h2 className="card-section-title">正在运行</h2>
            <div className="grid">
              {activeRuns.map((run) => (
                <div className="card" key={run.id}>
                  <div className="card-head">
                    <div>
                      <div className="card-title">{run.agent_name}</div>
                      <div className="card-sub mono-sm">{run.id}</div>
                    </div>
                    <span className="pill ok" style={{ marginLeft: 'auto' }}>
                      <span className="dot" />
                      {run.status} · {run.elapsed_seconds}s
                    </span>
                  </div>
                </div>
              ))}
              {collabRuns.map((run) => (
                <button
                  type="button"
                  className="card card-clickable"
                  key={`${run.kind}-${run.run_id}-${run.agent_name}`}
                  onClick={() => navigate(`/collab/runs/${run.run_id}`)}
                  title="打开这条协作运行"
                >
                  <div className="card-head">
                    <div style={{ minWidth: 0 }}>
                      <div className="card-title">{run.agent_name}</div>
                      <div className="card-sub">
                        {run.kind === 'chat' ? '入口智能体的对话回复' : '协作工单步骤'}
                        {run.title ? ` · ${run.title}` : ''}
                      </div>
                    </div>
                    <span className="pill brand" style={{ marginLeft: 'auto' }}>
                      <span className="dot" />
                      协作 · {run.elapsed_seconds}s
                    </span>
                  </div>
                </button>
              ))}
            </div>
          </section>
        )}

        <div className="row" style={{ gap: 8 }}>
          <span className="dim small" style={{ flex: 1 }}>
            共 {runtimes.length} 个内核 · 点卡片看详情（安装 / 更新 / 卸载也都在详情里）
          </span>
          <button className="btn" onClick={() => void reload()} disabled={loading}>
            ↻ 刷新
          </button>
        </div>

        {loading && runtimes.length === 0 && (
          <div className="empty">
            <p>正在读取内核状态…</p>
          </div>
        )}
        {!loading && runtimes.length === 0 && !error && (
          <div className="empty">
            <div className="empty-icon">🧬</div>
            <p>没有已注册的内核。</p>
          </div>
        )}

        <div className="runtime-grid">
          {runtimes.map((runtime) => (
            <KernelCard
              key={runtime.id}
              runtime={runtime}
              latest={latest[runtime.id]}
              checking={Boolean(checking[runtime.id])}
              auth={auth[runtime.id] ?? null}
              authLoading={Boolean(authLoading[runtime.id])}
              busy={provisionJobs[runtime.id]?.status === 'running'}
              runningAction={provisionJobs[runtime.id]?.status === 'running' ? provisionJobs[runtime.id].action : null}
              onOpen={() => openDetail(runtime)}
            />
          ))}
        </div>
      </div>

      {openRuntime && (
        <Modal
          wide
          title={`内核 · ${openRuntime.name}`}
          onClose={closeDetail}
          footer={
            <button className="btn" onClick={closeDetail}>
              关闭
            </button>
          }
        >
          <KernelDetail
            runtime={openRuntime}
            latest={latest[openRuntime.id]}
            checking={Boolean(checking[openRuntime.id])}
            refreshing={Boolean(refreshing[openRuntime.id])}
            job={openJob}
            auth={auth[openRuntime.id] ?? null}
            authLoading={Boolean(authLoading[openRuntime.id])}
            logRef={logRef}
            onRefresh={() => void refreshStatus(openRuntime)}
            onCheck={() => void checkLatest(openRuntime)}
            onRun={(action) => void start(openRuntime, action)}
            onCancel={cancel}
            onSwitchAccount={(accountId) => void switchAccount(openRuntime, accountId)}
            onRemoveAccount={(accountId) => {
              if (!confirm('确认删除这份已保存的账号？当前正在使用的登录态不受影响。')) return
              void switchAccount(openRuntime, accountId, true)
            }}
          />
        </Modal>
      )}
    </>
  )
}

/** 列表卡片：**一行摘要**，整张卡是一个按钮 —— 点它进详情。 */
function KernelCard({
  runtime,
  latest,
  checking,
  auth,
  authLoading,
  busy,
  runningAction,
  onOpen,
}: {
  runtime: AgentRuntime
  latest?: RuntimeLatest
  checking: boolean
  auth: RuntimeAuth | null
  authLoading: boolean
  busy: boolean
  runningAction: RuntimeAction | null
  onOpen: () => void
}) {
  const provisioning = runtime.provisioning
  const current = provisioning.version ? versionCore(provisioning.version) : null
  // 查到比本机**更高**的远端版本才算「有更新」；没查到更新（还没查完 / 查询失败 /
  // 远端不比本机新）就默认按「已是最新」显示 —— 远端查询是 npm view，很慢，不能空等。
  // 连「查询最新版本」命令都没有的内核不参与这个默认（无从判断，别给假结论）。
  const canCheckVersion = Boolean(provisioning.commands?.latest?.length)
  const updateTarget = updateTargetOf(provisioning.version, latest)
  const versionPillTitle = checking
    ? '正在查询远端最新版本…'
    : latest?.error
      ? `远端版本查询失败（${latest.error}），暂按已是最新显示`
      : undefined
  // 账号型内核：实现了登录态探测就在卡片上亮出「已登录 / 未登录」（与移动端一致）。
  const authSupported = Boolean(provisioning.commands?.login?.length) || Boolean(runtime.auth_supported)
  const loggedIn = auth?.logged_in === true
  const authLabel = authLoading
    ? '检查登录…'
    : loggedIn
      ? '已登录'
      : auth?.logged_in === false
        ? '未登录'
        : '登录未知'
  return (
    <button
      type="button"
      className="card card-clickable"
      onClick={onOpen}
      aria-label={`查看内核「${runtime.name}」的详情`}
    >
      <span className="runtime-head">
        <KernelLogo logo={runtime.logo} style={runtime.logo_style} />
        <span className="runtime-card-text">
          <span className="card-title">{runtime.name}</span>
          <span className="card-sub runtime-sub">
            <span>{kernelSummary(runtime, current)}</span>
            {busy && runningAction ? (
              <span className="pill brand">正在{ACTION_LABELS[runningAction]}…</span>
            ) : provisioning.installed && current && canCheckVersion ? (
              updateTarget ? (
                <span className="pill warn" title={`远端最新版本 ${updateTarget}，点卡片进详情更新`}>
                  可更新 {updateTarget}
                </span>
              ) : (
                <span className="pill ok" title={versionPillTitle}>
                  已是最新
                </span>
              )
            ) : null}
            {authSupported && provisioning.installed && (
              <span className={`pill ${loggedIn ? 'ok' : 'warn'}`}>{authLabel}</span>
            )}
          </span>
          <span className="dim small">{detectedLabel(provisioning.checked_at)}</span>
        </span>
        <span className="runtime-card-chevron" aria-hidden>
          ›
        </span>
      </span>
    </button>
  )
}

/** 详情：完整状态 + 动作 + 这次的安装日志（有任务时才出现）。 */
function KernelDetail({
  runtime,
  latest,
  checking,
  refreshing,
  job,
  auth,
  authLoading,
  logRef,
  onRefresh,
  onCheck,
  onRun,
  onCancel,
  onSwitchAccount,
  onRemoveAccount,
}: {
  runtime: AgentRuntime
  latest?: RuntimeLatest
  checking: boolean
  refreshing: boolean
  job: Job | null
  auth: RuntimeAuth | null
  authLoading: boolean
  logRef: RefObject<HTMLPreElement>
  onRefresh: () => void
  onCheck: () => void
  onRun: (action: RuntimeAction) => void
  onCancel: () => void
  onSwitchAccount: (accountId: string) => void
  onRemoveAccount: (accountId: string) => void
}) {
  const { isAdmin } = useAuth()
  const provisioning = runtime.provisioning
  const managed = provisioning.supported
  const remote = latest?.latest ?? null
  const canCheck = isAdmin && Boolean(provisioning.commands?.latest?.length)
  const current = provisioning.version ? versionCore(provisioning.version) : null
  const busy = job?.status === 'running'
  // 账号型内核：只有声明了登录命令才显示「登录」按钮；登录状态则只要适配器
  // 实现了探测（`auth_supported`）就展示 —— 有些内核只能交互式登录，平台代跑不了，
  // 但「已登录 / 未登录 / 无法判断」对管理员仍然有用。
  const loginSupported = Boolean(provisioning.commands?.login?.length)
  const authSupported = loginSupported || Boolean(runtime.auth_supported)
  const loggedIn = auth?.logged_in === true
  const authLabel = authLoading
    ? '检查中…'
    : loggedIn
      ? `已登录${auth?.account ? ` · ${auth.account}` : ''}`
      : auth?.logged_in === false
        ? '未登录'
        : auth?.detail ?? '未知'
  // 查到比本机更高的版本才提示「更新到 X」；已经是最新版就不给更新入口。
  // 查不到（不支持 / 失败 / 还没查）时保留普通「更新」，免得失去升级手段。
  const updateTarget = updateTargetOf(provisioning.version, latest)
  const upToDate = Boolean(remote && current && !updateTarget)
  const showUpdate =
    isAdmin &&
    managed &&
    provisioning.installed &&
    Boolean(provisioning.commands?.update?.length) &&
    (updateTarget !== null || !canCheck || !remote)

  return (
    <div className="stack">
      <div className="runtime-head">
        <KernelLogo logo={runtime.logo} style={runtime.logo_style} />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="card-title">{runtime.name}</div>
          <div className="card-sub">
            {runtime.vendor} · {runtime.runtime === 'api' ? '进程内运行' : '本地 CLI'}
          </div>
        </div>
        <span className={`pill ${provisioning.installed ? 'ok' : managed ? 'warn' : ''}`}>
          {provisioning.installed ? '已安装' : managed ? '未安装' : '不适用'}
        </span>
        {updateTarget && <span className="pill warn">可更新到 {updateTarget}</span>}
      </div>

      {managed && provisioning.note && <div className="banner warn">{provisioning.note}</div>}

      <dl className="kv">
        <dt>安装方式</dt>
        <dd>{managed ? '平台代管（可安装 / 更新 / 卸载）' : runtime.runtime === 'api' ? '进程内运行' : '自备可执行文件'}</dd>
        <dt>当前版本</dt>
        <dd>{current ?? '—'}</dd>
        {authSupported && provisioning.installed && (
          <>
            <dt>登录状态</dt>
            <dd className={loggedIn ? undefined : 'dim'}>{authLabel}</dd>
          </>
        )}
        {canCheck && (
          <>
            <dt>远端最新</dt>
            <dd>
              {checking ? '查询中…' : remote ?? (latest?.error ? '查询失败' : '—')}
              {!checking && upToDate && remote ? '（已是最新）' : ''}
            </dd>
          </>
        )}
        <dt>检测时间</dt>
        <dd>{detectedLabel(provisioning.checked_at)}</dd>
      </dl>

      {/* 未登录时把适配器给出的登录方式亮出来（多行指引，保留换行）。
          有些内核（如 CodeBuddy）没有可代跑的登录命令，这块就是唯一的「登录方式」。 */}
      {authSupported && provisioning.installed && auth?.logged_in === false && auth.detail && (
        <div className="banner warn" style={{ whiteSpace: 'pre-line' }}>
          {auth.detail}
        </div>
      )}

      {/* 多账号：内核只保留「当前这一个」登录态，平台把每次登录存成槽位，点切换即可。
          切换只影响之后启动的对话；正在跑的那一轮仍然用原来的账号。
          `accounts` 为数组（哪怕是空的）就说明内核支持多账号 —— 首次登录前也给一句说明，
          否则用户会以为这个功能没生效。 */}
      {isAdmin && authSupported && provisioning.installed && Array.isArray(auth?.accounts) && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="dim small">已保存的账号（切换后新对话生效，正在运行的任务不受影响）：</div>
          {auth!.accounts!.length === 0 ? (
            <div className="dim small">
              还没有保存的账号 —— 点「登录」完成授权后会自动保存；再点「登录其他账号」即可加入第二个账号。
            </div>
          ) : (
            auth!.accounts!.map((account) => (
              <div key={account.id} className="row wrap" style={{ gap: 8, alignItems: 'center' }}>
                <span className={account.active ? undefined : 'dim'}>
                  {account.active ? '● ' : '○ '}
                  {account.label}
                </span>
                {account.active ? (
                  <span className="pill ok">当前</span>
                ) : (
                  <>
                    <button className="btn sm" onClick={() => onSwitchAccount(account.id)} disabled={busy}>
                      切换
                    </button>
                    <button className="btn sm ghost" onClick={() => onRemoveAccount(account.id)} disabled={busy}>
                      删除
                    </button>
                  </>
                )}
              </div>
            ))
          )}
        </div>
      )}

      <div className="row wrap" style={{ gap: 8 }}>
        {isAdmin && managed && !provisioning.installed && provisioning.commands?.install?.length ? (
          <button className="btn sm primary" onClick={() => onRun('install')} disabled={busy}>
            安装
          </button>
        ) : null}
        {showUpdate ? (
          <button className="btn sm primary" onClick={() => onRun('update')} disabled={busy}>
            {updateTarget ? `更新到 ${updateTarget}` : '更新'}
          </button>
        ) : null}
        {isAdmin && loginSupported && provisioning.installed && !loggedIn ? (
          <button className="btn sm primary" onClick={() => onRun('login')} disabled={busy}>
            登录
          </button>
        ) : null}
        {isAdmin && loginSupported && provisioning.installed && loggedIn ? (
          <button className="btn sm" onClick={() => onRun('login')} disabled={busy}>
            {auth?.accounts?.length ? '登录其他账号' : '重新登录'}
          </button>
        ) : null}
        {isAdmin && managed && provisioning.installed && provisioning.commands?.uninstall?.length ? (
          <button className="btn sm danger" onClick={() => onRun('uninstall')} disabled={busy}>
            卸载
          </button>
        ) : null}
        {canCheck && (
          <button className="btn sm" onClick={onCheck} disabled={checking || busy}>
            {checking ? '查询中…' : '检查更新'}
          </button>
        )}
        {isAdmin && (
          <button className="btn sm" onClick={onRefresh} disabled={refreshing || busy}>
            <RefreshIcon size={14} className={refreshing ? 'spin' : undefined} />
            重新检测
          </button>
        )}
        {runtime.docs_url && (
          <a className="btn sm ghost" href={runtime.docs_url} target="_blank" rel="noreferrer">
            文档 ↗
          </a>
        )}
      </div>

      {job && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="row wrap" style={{ gap: 8 }}>
            <span className={`pill ${job.status === 'running' ? 'brand' : job.status === 'ok' ? 'ok' : 'err'}`}>
              {job.status === 'running' ? `正在${ACTION_LABELS[job.action]}…` : job.status === 'ok' ? '完成' : '失败'}
            </span>
            <span className="dim small">{ACTION_LABELS[job.action]}任务在服务器本地执行，关掉窗口不会中断。</span>
            {job.status === 'running' && (
              <button className="btn sm danger" onClick={onCancel}>
                中止
              </button>
            )}
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
      )}
    </div>
  )
}

/** 卡片副标题：所属公司 + 当前版本（卡片上只留这三样，其余信息在详情里）。 */
function kernelSummary(runtime: AgentRuntime, current: string | null): string {
  const provisioning = runtime.provisioning
  if (!provisioning.supported) {
    return `${runtime.vendor} · ${runtime.runtime === 'api' ? '进程内运行' : '自备可执行文件'}`
  }
  return provisioning.installed && current ? `${runtime.vendor} · 当前版本 ${current}` : `${runtime.vendor} · 未安装`
}

/** 「检测于 …」：安装状态是后端缓存 + 定时检测的，不是每次打开页面实时探的。 */
function detectedLabel(checkedAt: string | null | undefined): string {
  return checkedAt ? `检测于 ${formatListTime(checkedAt)}` : '尚未检测'
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
