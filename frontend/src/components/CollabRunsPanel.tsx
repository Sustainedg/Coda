/**
 * 「协作运行」页签：左栏是运行列表，主区是时间线（复用 `MessageView`）与工单状态摘要，
 * 底部输入框直接和入口智能体说话（走协作 SSE）。
 *
 * 工单的细节（列表、流转时间线、规则与越权审计）在点击后打开的全屏 `TicketBoard`（三栏看板）里。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { CollabProgressBoard } from './CollabProgressBoard'
import { Composer } from './Composer'
import { ConversationThread, type ConversationThreadItem } from './ConversationThread'
import { useAuth } from './AuthGate'
import { ClipboardIcon } from './Icons'
import type { RenderMessage } from './MessageView'
import { TicketBoard } from './TicketBoard'
import { Modal } from './Modal'
import { api, ApiError, collabRunFileUrl, streamGet, streamPost } from '../lib/api'
import { collabChoiceAnswer, findPendingCollabAsks } from '../lib/collabAsk'
import {
  applyChatEvent, advanceOvertakenByTicketSteps, chatAdvanceFrom, dismissChatAdvance,
  dismissChatNotice, emptyChatTurn, type ChatTurnState,
} from '../lib/collabChat'
import {
  ADVANCE_MARKER, forgetConsumedAdvances, loadConsumedAdvances, rememberConsumedAdvance,
} from '../lib/collabAdvance'
import { MediaUrlProvider, composeWithAttachments } from '../lib/media'
import { useComposerDraft } from '../lib/composerDraft'
import {
  applyCollabEvent, dismissCollabNotice, syncTurnWithRun, type CollabNotice, type CollabTurnState,
} from '../lib/collabStream'
import {
  forgetDismissedNotices, loadDismissedNotices, rememberDismissedNotice,
} from '../lib/collabNotices'
import { buildCollabProgress } from '../lib/collabProgress'
import { useAutoRefresh } from '../lib/useAutoRefresh'
import { rawUsageTotal, summarizeUsage } from '../lib/usage'
import type {
  AttachmentRef,
  CollabChoice, CollabEvent, CollabRun, CollabRunDetail, CollabTeam, AgentProfile, StreamClosedEvent,
} from '../lib/types'

/** 一次订阅可能收到协作信封，也可能收到传输层的 ``stream.closed`` 收尾标记。 */
type RunEvent = CollabEvent | StreamClosedEvent

/** 重连前的等待：每次退避一点，避免断线时疯狂重试。 */
const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

const RUN_STATUS: Record<string, { label: string; pill: string }> = {
  running: { label: '进行中', pill: 'brand' },
  waiting_user: { label: '等待你', pill: 'warn' },
  done: { label: '已完成', pill: 'ok' },
  error: { label: '出错', pill: 'err' },
  cancelled: { label: '已停止', pill: '' },
  interrupted: { label: '被中断', pill: 'warn' },
}

interface Props {
  runId?: string
  teams: CollabTeam[]
  agents: AgentProfile[]
  onOpenRun: (runId: string | null) => void
}

export function CollabRunsPanel({ runId, teams, agents, onOpenRun }: Props) {
  // 当前登录用户：自己发的消息画真头像（换头像后这里跟着 `AuthGate` 的状态刷新）。
  const { user } = useAuth()
  const [runs, setRuns] = useState<CollabRun[]>([])
  const [detail, setDetail] = useState<CollabRunDetail | null>(null)
  const [turn, setTurn] = useState<CollabTurnState | null>(null)
  //: 用户点过「×」关掉的提醒（按运行记，刷新后不再重建）
  const dismissedNotices = useMemo(() => loadDismissedNotices(runId ?? ''), [runId])
  const [chatTurn, setChatTurn] = useState<ChatTurnState>(emptyChatTurn)
  //: 已经点选/关掉的「推进任务」卡（按 CEO 聊天消息 ID 记，刷新后不再恢复）
  const [consumedAdvances, setConsumedAdvances] = useState<Set<string>>(
    () => loadConsumedAdvances(runId ?? ''),
  )
  /** 草稿与待发附件按运行各记各的：切走再回来还在原处（与普通对话共用同一份实现）。 */
  const { text: draft, setText: setDraft, attachments, setAttachments } = useComposerDraft(runId)
  const [busy, setBusy] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [picking, setPicking] = useState(false)
  const [startTeamId, setStartTeamId] = useState('')
  const [startTitle, setStartTitle] = useState('')
  const [startWorkspace, setStartWorkspace] = useState('')
  const [boardOpen, setBoardOpen] = useState(false)
  //: 从岗位对话里的工单卡点进来时，看板直接定位到这张工单
  const [boardTicketId, setBoardTicketId] = useState<string | null>(null)
  const [progressOpen, setProgressOpen] = useState(false)
  const [pendingUser, setPendingUser] = useState<{ id: string; text: string; attachments: string[] } | null>(null)
  const [reconnecting, setReconnecting] = useState(false)
  //: 「提高预算」弹窗：步数/工单/token 超限挂起后，调高上限再继续
  const [budgetOpen, setBudgetOpen] = useState(false)
  const [budgetDraft, setBudgetDraft] = useState({ max_steps: 0, max_tickets: 0, max_tokens: 0, timeout_seconds: 0, step_timeout_seconds: 0 })

  const abortRef = useRef<AbortController | null>(null)
  const chatAbortRef = useRef<AbortController | null>(null)
  const busyRef = useRef(false)
  const chatBusyRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])
  /** 这次运行自己的编排图（第一次推进时冻结）：改协作组后，老运行的进度图与岗位名仍按原图渲染。 */
  const graph = detail?.graph ?? team?.graph
  const leaderKey = detail?.entry_key ?? team?.entry_key ?? ''
  // 同一步骤既有落库行（可能还停在 streaming、内容为空）又有流式结果时，以流式结果为准。
  const streamedStepIds = useMemo(
    () => new Set([
      ...(turn?.completed ?? []).map((step) => step.stepId),
      ...(turn?.live ?? []).map((step) => step.stepId),
    ]),
    [turn?.completed, turn?.live],
  )
  const visibleSteps = useMemo(
    () => (detail?.steps ?? []).filter(
      (step) => (step.role_key === 'user' || step.role_key === leaderKey) && !streamedStepIds.has(step.id),
    ),
    [detail?.steps, leaderKey, streamedStepIds],
  )
  const chatMessages = detail?.chat_messages ?? []
  const leaderLive = turn?.live?.find((step) => step.nodeKey === leaderKey) ?? null
  const status = turn?.status ?? detail?.status ?? 'waiting_user'

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await api.listCollabRuns())
    } catch (e) {
      setError(describe(e))
    }
  }, [])

  const loadDetail = useCallback(async (id: string) => {
    try {
      const next = await api.getCollabRun(id)
      setDetail(next)
      setTurn((prev) => syncTurnWithRun(prev, next, next.tickets, next.steps, dismissedNotices))
      return next
    } catch (e) {
      setError(describe(e))
      // 定时刷新遇到网络抖动时保留当前详情；只有运行确实被删除才清空。
      if (e instanceof ApiError && e.status === 404) {
        setDetail(null)
        setTurn(null)
      }
      return null
    }
  }, [dismissedNotices])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  // 切到另一次运行：读取它自己的决策卡消费记忆（本地兜底，后端清 meta 为主）。
  useEffect(() => {
    setConsumedAdvances(loadConsumedAdvances(runId ?? ''))
  }, [runId])

  // 离开页面时退订；运行本身在服务端继续，回来再订阅即可。
  useEffect(() => () => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
  }, [])

  // 两台设备看同一次协作时，以落库详情为准定时对齐；本机 SSE 推进期间暂停轮询。
  // 运行中 5s 对齐一次（另一台设备发起的动作要能尽快出现）；挂起/完成后没有新东西，
  // 降到 20s——详情接口再瘦也经不起 3s 一次的空转，尤其远程访问时。
  const pollInterval = status === 'running' ? 5000 : 20000
  useAutoRefresh(() => (runId ? loadDetail(runId) : undefined), {
    intervalMs: pollInterval,
    enabled: Boolean(runId) && !busy,
  })
  useAutoRefresh(loadRuns, { intervalMs: 15000 })

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, chatMessages.length, leaderLive?.agent.text, chatTurn.live?.agent.text, turn?.notices.length])

  /**
   * 订阅一次运行的实时事件并渲染。
   *
   * 推进跑在服务端独立任务里，所以连接被切断（手机锁屏、切后台、隧道抖动）时这里
   * 会自动重连订阅续看，而不是把运行打断；`kickoff` 用于「发消息 / 继续」这类先启动再订阅的场景。
   */
  const subscribe = useCallback(
    async (
      runId: string,
      kickoff?: (onEvent: (event: RunEvent) => void, signal: AbortSignal) => Promise<void>,
    ) => {
      if (busyRef.current) return
      busyRef.current = true
      setBusy(true)
      const controller = new AbortController()
      abortRef.current = controller
      const onEvent = (event: RunEvent) => {
        if (event.type === 'stream.closed') return
        setTurn((prev) => (prev ? applyCollabEvent(prev, event) : prev))
      }
      try {
        if (kickoff) {
          try {
            await kickoff(onEvent, controller.signal)
          } catch (e) {
            // 发起这一轮的响应断了不代表运行停了：先看落库状态，还在跑就继续订阅围观。
            if ((e as Error)?.name === 'AbortError') throw e
            setError(describe(e))
          }
          if (controller.signal.aborted) return
          const latest = await loadDetail(runId)
          if (!latest || latest.status !== 'running') return
        }
        let attempt = 0
        while (!controller.signal.aborted && attempt < 30) {
          // 第一次订阅（刚打开页面就围观在跑的运行）不算「断线」；只有重连才提示并退避。
          if (attempt > 0) {
            setReconnecting(true)
            await delay(Math.min(800 * attempt, 3000))
            if (controller.signal.aborted) break
          }
          attempt += 1
          try {
            await streamGet<RunEvent>(`/api/collab/runs/${runId}/stream`, onEvent, controller.signal)
          } catch (e) {
            // 网络层断开（锁屏、隧道抖动）不是运行结束：退避后重订阅，总线会补发本轮事件。
            if ((e as Error)?.name === 'AbortError') throw e
            setError(describe(e))
          }
          if (controller.signal.aborted) break
          // 以落库状态为准：运行真的收尾了才停，否则继续订阅。
          const latest = await loadDetail(runId)
          if (!latest || latest.status !== 'running') break
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describe(e))
      } finally {
        busyRef.current = false
        abortRef.current = null
        setReconnecting(false)
        setBusy(false)
        await loadDetail(runId)
        await loadRuns()
      }
    },
    [loadDetail, loadRuns],
  )

  /** 订阅/发起一次 CEO 聊天；状态与主流程完全分开，主流程在跑也不占用这里。 */
  const subscribeChat = useCallback(
    async (
      runId: string,
      kickoff?: (onEvent: (event: RunEvent) => void, signal: AbortSignal) => Promise<void>,
    ) => {
      if (chatBusyRef.current) return
      chatBusyRef.current = true
      setChatBusy(true)
      const controller = new AbortController()
      chatAbortRef.current = controller
      let closed = false
      const onEvent = (event: RunEvent) => {
        if (event.type === 'stream.closed') {
          closed = true
          return
        }
        if (
          event.type === 'collab.chat.started'
          || event.type === 'collab.chat.event'
          || event.type === 'collab.chat.finished'
        ) {
          setChatTurn((prev) => applyChatEvent(prev, event))
        }
      }
      try {
        if (kickoff) await kickoff(onEvent, controller.signal)
        let attempt = 0
        while (!closed && !controller.signal.aborted && attempt < 5) {
          attempt += 1
          // 主动发消息后的第一次读取已经由 kickoff 完成；只有断线才需要补订阅。
          if (attempt === 1 && kickoff) break
          await delay(Math.min(800 * attempt, 3000))
          if (controller.signal.aborted) break
          await streamGet<RunEvent>(`/api/collab/runs/${runId}/chat/stream`, onEvent, controller.signal)
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describe(e))
      } finally {
        chatBusyRef.current = false
        chatAbortRef.current = null
        setChatBusy(false)
        await loadDetail(runId)
        await loadRuns()
      }
    },
    [loadDetail, loadRuns],
  )

  useEffect(() => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    if (!runId) {
      setDetail(null)
      setTurn(null)
      setChatTurn(emptyChatTurn())
      return
    }
    setDetail(null)
    setTurn(null)
    setChatTurn(emptyChatTurn())
    setError(null)
    void (async () => {
      const next = await loadDetail(runId)
      // 这次运行在跑就订阅实时事件：不管它是本页发起，还是另一台设备/上次会话发起的。
      if (next && next.status === 'running' && !busyRef.current) void subscribe(runId)
      // 刷新/换设备时，如果 CEO 正好在回复，重新接上聊天流。
      if (next?.chat_active && !chatBusyRef.current) void subscribeChat(runId)
    })()
  }, [runId, loadDetail, subscribe, subscribeChat])

  /** 发消息（或空内容触发重跑）并订阅这一轮的推进。 */
  const stream = async (path: string, body: unknown, userText?: string) => {
    if (!detail) return
    const pending = userText ? { id: `local-user-${Date.now()}`, text: userText, attachments: [] } : null
    if (pending) setPendingUser(pending)
    setError(null)
    try {
      await subscribe(detail.id, (onEvent, signal) => streamPost<RunEvent>(path, body, onEvent, signal))
    } finally {
      if (pending) setPendingUser(null)
    }
  }

  /**
   * 把一句话发给入口智能体：**全部走独立聊天通道**（可带附件）。
   * 是否推进任务由 CEO 判断：它认为需要推进时会在回复末尾给出 chat_advance，
   * 界面渲染「推进任务」决策卡，用户点选后才把指令发进工单通道（`/messages`）。
   * 用户不再手动区分「聊天 / 推进」两条线。
   */
  const sendMessage = async (text: string, files: AttachmentRef[] = []) => {
    if (!detail || (!text.trim() && files.length === 0)) return
    const paths = files.map((item) => item.rel_path)
    const pending = { id: `local-user-${Date.now()}`, text: text.trim(), attachments: paths }
    setPendingUser(pending)
    setError(null)
    // 第一个事件到达即代表消息已落库：此刻把附件从输入区收走，而不是等整轮聊天跑完。
    let cleared = paths.length === 0
    try {
      await subscribeChat(detail.id, (onEvent, signal) =>
        streamPost<RunEvent>(
          `/api/collab/runs/${detail.id}/chat`,
          { content: text, attachments: paths },
          (event) => {
            if (!cleared) {
              cleared = true
              setAttachments([])
            }
            onEvent(event)
          },
          signal,
        ),
      )
    } finally {
      setPendingUser(null)
    }
  }

  /** 点选/关掉一张「推进任务」卡：本地记住它已消费，避免刷新后又从 meta 里恢复。 */
  const consumeAdvance = (ask: { messageId?: string } | null) => {
    const messageId = ask?.messageId ?? ''
    if (detail && messageId) {
      rememberConsumedAdvance(detail.id, messageId)
      setConsumedAdvances((prev) => new Set(prev).add(messageId))
    }
    setChatTurn((prev) => dismissChatAdvance(prev))
  }

  /**
   * 用户在「推进任务」决策卡上点了确认（或直接发自由指令）：把 CEO 给出的推进
   * 指令发进工单通道。任务还没开始时这就是开场的任务描述。
   *
   * 点「按建议推进」时带 `use_advance`：指令原文照发（保证执行的就是 CEO 给的），
   * 但时间线上不把长指令再显示一遍，只落一句短标记 —— 指令在 CEO 的消息里已经
   * 写过，重发一遍既占屏幕又多花一遍 tokens。
   */
  const advanceRun = (text: string, useAdvance = false) => {
    if (!detail || busy || chatBusy || !text.trim()) return
    consumeAdvance(advanceAsk)
    const marker = ADVANCE_MARKER
    const body = useAdvance ? { content: text, use_advance: true } : { content: text }
    void stream(`/api/collab/runs/${detail.id}/messages`, body, useAdvance ? marker : text)
  }

  /**
   * 点决策卡上的选项：自由填写（或选项没带可发内容）只预填 CEO 建议并聚焦输入框，不发送。
   * freeform 的 value 是空串，不能拿 label 兜底 —— 否则「我再想想」这四个字会被当成指令发进工单通道。
   * 选项 value 就是 CEO 的建议指令时走引用通道（`use_advance`），不把长指令重发一遍。
   */
  const answerAdvance = (choice: CollabChoice) => {
    if (!detail || busy || chatBusy) return
    if (choice.freeform || !choice.value.trim()) {
      const suggestion = advanceAsk?.options.find((item) => item.value.trim())?.value ?? ''
      if (suggestion) setDraft(suggestion)
      composerRef.current?.focus()
      return
    }
    advanceRun(choice.value.trim(), true)
  }

  /** 上传待发附件：走协作运行自己的附件目录，与移动端共用同一个接口。 */
  const uploadAttachment = async (name: string, contentBase64: string) => {
    if (!detail) throw new Error('还没有打开协作运行，暂时不能上传附件')
    return api.uploadCollabAttachment(detail.id, { name, content_base64: contentBase64 })
  }

  const start = async () => {
    if (!startTeamId) {
      setError('请选择对应的协作团队')
      return
    }
    try {
      const run = await api.createCollabRun({
        team_id: startTeamId,
        title: startTitle.trim() || undefined,
        workspace_dir: startWorkspace.trim() || null,
      })
      setPicking(false)
      setStartTitle('')
      setStartWorkspace('')
      await loadRuns()
      onOpenRun(run.id)
    } catch (e) {
      setError(describe(e))
    }
  }

  const stop = async () => {
    if (!detail) return
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    await api.cancelCollabRun(detail.id).catch(() => undefined)
    await loadDetail(detail.id)
  }

  /** 关掉一条提醒：立即从待处理区移除，并记住「用户已经看过、不用再提」。 */
  const dismissNotice = async (notice: CollabNotice) => {
    const current = runId ?? detail?.id ?? ''
    setTurn((prev) => (prev ? dismissCollabNotice(prev, notice.key) : prev))
    rememberDismissedNotice(current, notice.key)
  }

  const retryAndResume = async (stepId: string) => {
    if (!detail) return
    try {
      await api.retryCollabStep(stepId)
    } catch (e) {
      setError(describe(e))
      return
    }
    await stream(`/api/collab/runs/${detail.id}/resume`, {})
  }

  /** 打开「提高预算」弹窗：预填当前上限的 1.5 倍（至少 +20），改完保存后可直接点「继续」。 */
  const openBudgetPatch = () => {
    if (!detail) return
    const bump = (current: number) => Math.max(current + 20, Math.ceil(current * 1.5))
    setBudgetDraft({
      max_steps: bump(detail.budget.max_steps),
      max_tickets: bump(detail.budget.max_tickets),
      max_tokens: detail.budget.max_tokens ? bump(detail.budget.max_tokens) : 0,
      timeout_seconds: detail.budget.timeout_seconds,
      step_timeout_seconds: detail.budget.step_timeout_seconds ?? 36000,
    })
    setBudgetOpen(true)
  }

  /** 保存新预算：成功后刷新详情；挂起原因清掉了，「继续」按钮就能把运行救回来。 */
  const saveBudgetPatch = async () => {
    if (!detail) return
    try {
      await api.patchCollabRunBudget(detail.id, {
        max_steps: budgetDraft.max_steps,
        max_tickets: budgetDraft.max_tickets,
        max_tokens: budgetDraft.max_tokens,
        timeout_seconds: budgetDraft.timeout_seconds,
        step_timeout_seconds: budgetDraft.step_timeout_seconds,
      })
    } catch (e) {
      setError(describe(e))
      return
    }
    setBudgetOpen(false)
    await loadDetail(detail.id)
    await loadRuns()
  }

  const answerAsk = (choice: CollabChoice) => {
    if (!detail || busy || chatBusy) return
    const text = collabChoiceAnswer(choice)
    if (!text) {
      composerRef.current?.focus()
      return
    }
    setDraft('')
    void stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
  }

  // 头像三字段一起查：气泡画真头像（自定义图 > 头像包 > emoji 回退）要用，
  // 换了头像时间线同步显示。
  const agentFace = (agentId?: string | null, nodeKey?: string) => {
    const node = graph?.nodes.find((item) => item.key === nodeKey)
    const agent = agents.find((item) => item.id === (agentId ?? node?.agent_profile_id))
    return {
      avatar: agent?.avatar ?? '',
      avatarPreset: agent?.avatar_preset ?? null,
      avatarFile: agent?.avatar_file ?? null,
      agentId: agent?.id ?? null,
    }
  }
  const labelOf = (nodeKey: string, fallback?: string | null) =>
    graph?.nodes.find((item) => item.key === nodeKey)?.label || fallback || nodeKey

  const persistedIds = useMemo(() => new Set(visibleSteps.map((step) => step.id)), [visibleSteps])
  const streamedLeader = useMemo(
    () => (turn?.completed ?? []).filter((step) => step.nodeKey === leaderKey && !persistedIds.has(step.stepId)),
    [leaderKey, persistedIds, turn?.completed],
  )
  const timeline: RenderMessage[] = [
    ...[
      ...visibleSteps.map((step) => ({
        at: step.started_at,
        message: step.role_key === 'user'
          ? { id: step.id, role: 'user' as const, content: step.content, parts: [], status: 'complete' }
          : {
              id: step.id,
              role: 'assistant' as const,
              content: step.content,
              parts: step.parts,
              status: step.status,
              error: step.error,
              usage: step.usage,
              // 详情不再携带 parts 本体：点开「过程」折叠块时按需拉这一步的完整流水
              loadParts: () => api.getCollabStepParts(step.run_id, step.id).then((r) => r.parts),
              partsCount: step.parts_count ?? 0,
            },
      })),
      ...chatMessages.map((row) => ({
        at: row.created_at,
        message: row.sender === 'user'
          ? { id: row.id, role: 'user' as const, content: row.content, parts: [], status: 'complete' }
          : {
              id: row.id,
              role: 'assistant' as const,
              content: row.content,
              parts: row.parts,
              status: row.status,
              error: row.error,
              usage: row.usage,
            },
      })),
    ]
      .sort((left, right) => String(left.at).localeCompare(String(right.at)))
      .map((item) => item.message),
    ...(pendingUser
      ? [{
          id: pendingUser.id,
          role: 'user' as const,
          // 附件清单拼回正文：图片缩略图当场画进「你」的气泡，不用等落库后重新拉取。
          content: composeWithAttachments(pendingUser.text, pendingUser.attachments),
          parts: [],
          status: 'complete',
        }]
      : []),
    ...streamedLeader.map((step) => ({
      id: step.stepId,
      role: 'assistant' as const,
      content: step.agent.text,
      parts: step.agent.parts,
      status: step.error ? 'error' : step.status,
      error: step.error ?? step.agent.error,
      usage: step.agent.usage,
    })),
  ]

  const liveMessage: RenderMessage | null = leaderLive
    ? {
        id: `live-${leaderLive.stepId}`,
        role: 'assistant',
        content: leaderLive.agent.text,
        parts: leaderLive.agent.parts,
        status: leaderLive.agent.error ? 'error' : 'streaming',
        error: leaderLive.agent.error,
        usage: leaderLive.agent.usage,
        streaming: true,
      }
    : null

  const chatLiveMessage: RenderMessage | null = chatTurn.live
    ? {
        id: `chat-live-${chatTurn.live.messageId}`,
        role: 'assistant',
        content: chatTurn.live.agent.text,
        parts: chatTurn.live.agent.parts,
        status: chatTurn.live.agent.error ? 'error' : 'streaming',
        error: chatTurn.live.agent.error,
        usage: chatTurn.live.agent.usage,
        streaming: true,
      }
    : null

  /**
   * CEO 消息的「工单更新」小卡片：这条消息把新要求同步进了几张工单、动了哪些字段。
   * 点卡片打开工单看板（完整内容在那里渲染成结构化卡片，不再是一大段文本）。
   * 没有 `meta.applied`（没同步过工单）就返回 `undefined`，消息上方什么都不挂。
   */
  const ticketUpdateCardOf = (messageId: string): ReactNode | undefined => {
    const row = chatMessages.find((item) => item.id === messageId)
    const applied = row?.meta?.applied as
      | { ticket_ids?: string[]; ticket_count?: number; fields?: string[] }
      | undefined
    if (!applied?.ticket_count) return undefined
    const fields = (applied.fields ?? []).join('、')
    return (
      <button
        type="button"
        className="collab-ticket-update-card"
        onClick={() => {
          setBoardTicketId(applied.ticket_ids?.[0] ?? null)
          setBoardOpen(true)
        }}
      >
        <ClipboardIcon size={15} className="collab-ticket-update-icon" />
        <span className="collab-ticket-update-text">
          已同步到 {applied.ticket_count} 张工单{fields ? `（${fields}）` : ''}
        </span>
        <span className="collab-ticket-update-more">查看工单 →</span>
      </button>
    )
  }

  const threadItems: ConversationThreadItem[] = [
    ...timeline.map((message) => ({
      id: message.id,
      message,
      ...(message.role === 'user' ? {} : agentFace(undefined, leaderKey)),
      label: message.role === 'user' ? undefined : labelOf(leaderKey, detail?.entry_key),
      // CEO 消息带过「需求同步进工单」时，消息上方挂一张工单更新小卡片：
      // 摘要一行（同步了几张、哪些字段），点开看渲染好的完整工单内容。
      meta: message.role === 'assistant' ? ticketUpdateCardOf(message.id) : undefined,
    })),
    ...(liveMessage && leaderLive
      ? [{
          id: liveMessage.id,
          message: liveMessage,
          ...agentFace(undefined, leaderLive.nodeKey),
          label: labelOf(leaderLive.nodeKey, leaderLive.agentName),
        }]
      : []),
    ...(chatLiveMessage && chatTurn.live
      ? [{
          id: chatLiveMessage.id,
          message: chatLiveMessage,
          ...agentFace(undefined, chatTurn.live.roleKey || leaderKey),
          label: labelOf(chatTurn.live.roleKey || leaderKey, chatTurn.live.agentName),
        }]
      : []),
  ]

  // 详情轮询发现运行又推进起来了（例如另一台设备点了继续），而本页已经退订：
  // 自动补一次订阅，让「协作进度 / 岗位实时对话」重新有逐字输出可看。
  useEffect(() => {
    if (!runId || status !== 'running' || busyRef.current) return
    const timer = window.setTimeout(() => {
      if (!busyRef.current) void subscribe(runId)
    }, 800)
    return () => window.clearTimeout(timer)
  }, [runId, status, subscribe])
  const progress = useMemo(
    () =>
      buildCollabProgress({
        graph,
        entryKey: leaderKey,
        steps: detail?.steps ?? [],
        tickets: detail?.tickets ?? [],
        agents,
        live: turn?.live ?? [],
        streamed: turn?.completed ?? [],
        runStatus: status,
      }),
    [graph, leaderKey, detail?.steps, detail?.tickets, agents, turn?.live, turn?.completed, status],
  )
  const progressDone = progress.nodes.filter((node) => node.state === 'done').length
  const progressLive = progress.nodes.some((node) => node.isLive)
  const progressAttention = progress.nodes.some(
    (node) => node.state === 'waiting' || node.state === 'error' || node.state === 'active',
  )
  const remoteRunning = status === 'running' && !busy
  const pendingAsks = useMemo(
    () =>
      status === 'waiting_user'
        ? findPendingCollabAsks(
            Object.values(turn?.tickets ?? {}).filter((ticket) => !detail || ticket.run_id === detail.id),
          )
        : [],
    [detail, status, turn?.tickets],
  )
  //: 刷新后从最近一条 CEO 聊天的 meta 里恢复「推进任务」决策卡（live 流里已由事件带出）。
  //: 已点选/关掉的卡、以及之后已有用户指令进了工单通道的旧卡，都不再恢复。
  const lastChat = chatMessages.at(-1)
  const persistedAdvance = useMemo(() => {
    if (!lastChat || lastChat.sender !== 'leader') return null
    if (consumedAdvances.has(lastChat.id)) return null
    if (advanceOvertakenByTicketSteps(lastChat, detail?.steps ?? [])) return null
    return chatAdvanceFrom(lastChat.meta?.advance, lastChat.id)
  }, [lastChat, consumedAdvances, detail?.steps])
  const advanceAsk = chatTurn.live ? null : (chatTurn.advance ?? persistedAdvance)
  const runUsage = summarizeUsage([detail?.usage])
  const usedTokens = rawUsageTotal(detail?.usage)

  return (
    <MediaUrlProvider
      resolver={(relPath) => (runId ? collabRunFileUrl(runId, relPath) : null)}
    >
    <div className="collab-shell">
      <aside className="collab-list">
        <button
          className="btn primary collab-new-run"
          onClick={() => setPicking(true)}
          title="新建协作任务"
          aria-label="新建协作任务"
        >
          ＋
        </button>
        {runs.map((run) => (
          <button
            key={run.id}
            className={`collab-item${run.id === runId ? ' active' : ''}`}
            onClick={() => onOpenRun(run.id)}
          >
            <div className="chat-item-title">
              <span className={`pill ${RUN_STATUS[run.status]?.pill ?? ''}`}>{RUN_STATUS[run.status]?.label ?? run.status}</span>
              {run.title}
            </div>
            <div className="chat-item-meta">
              <span>{run.team_name}</span>
              <span>·</span>
              <span>{run.steps_used} 步 / {run.ticket_count} 张工单</span>
            </div>
          </button>
        ))}
        {runs.length === 0 && (
          <p className="dim small" style={{ padding: '14px 8px', lineHeight: 1.6 }}>
            还没有协作运行。先在「智能体 → 协作组」里画一张编排图，再回来发起。
          </p>
        )}
      </aside>

      <section className="collab-main">
        {detail ? (
          <>
            {/* 顶栏只放操作按钮：运行标题、状态、预算与工单摘要都在左栏列表与工单看板里看。 */}
            <div className="collab-topbar">
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                <button
                  className={`btn sm collab-progress-trigger${progressAttention ? ' attention' : ''}${progressOpen ? ' open' : ''}`}
                  onClick={() => setProgressOpen((prev) => !prev)}
                  title="查看流程图实况、岗位状态与内部对话入口"
                >
                  <span>🧭 协作进度</span>
                  <span className="collab-progress-trigger-count">{progressDone}/{progress.nodes.length}</span>
                  {progressLive && <span className="collab-progress-trigger-dot" aria-label="有岗位正在实时输出" />}
                </button>
                <button
                  className="btn sm"
                  onClick={() => {
                    setBoardTicketId(null)
                    setBoardOpen(true)
                  }}
                >
                  📋 工单看板 {detail.tickets.length}
                </button>
                {busy || remoteRunning ? (
                  <button className="btn sm danger" onClick={() => void stop()}>
                    {busy ? '■ 停止' : '■ 叫停'}
                  </button>
                ) : (
                  (status === 'waiting_user' || status === 'interrupted' || status === 'error') && (
                    <>
                      <button className="btn sm" onClick={openBudgetPatch} title="步数/工单/token 超限挂起后，调高上限再继续">
                        ⤴ 提高预算
                      </button>
                      <button className="btn sm" disabled={chatBusy} onClick={() => void stream(`/api/collab/runs/${detail.id}/resume`, {})}>
                        ▶ 继续
                      </button>
                    </>
                  )
                )}
                <button
                  className="btn sm danger"
                  onClick={async () => {
                    if (!confirm(`删除这次协作「${detail.title}」？工单与时间线都会一起删除。`)) return
                    try {
                      await api.deleteCollabRun(detail.id)
                    } catch (e) {
                      setError(describe(e))
                      return
                    }
                    forgetDismissedNotices(detail.id)
                    forgetConsumedAdvances(detail.id)
                    onOpenRun(null)
                    await loadRuns()
                  }}
                >
                  删除
                </button>
              </div>
            </div>

            <div className="collab-stack">
            {turn && turn.notices.filter((notice) => notice.level !== 'ask').length > 0 && (
              <div className="stack" style={{ gap: 6, padding: '0 22px 6px' }}>
                {turn.notices.filter((notice) => notice.level !== 'ask').slice(-4).map((notice) => (
                  <div key={notice.key} className={`banner ${notice.level === 'error' ? 'err' : 'info'}`}>
                    <div className="row" style={{ gap: 8 }}>
                      <span style={{ flex: 1 }}>{notice.text}</span>
                      {notice.level === 'error' && notice.stepId && !busy && !chatBusy && (
                        <button className="btn sm" onClick={() => void retryAndResume(notice.stepId!)}>
                          重试这一步
                        </button>
                      )}
                      <button
                        className="banner-close"
                        aria-label="关掉这条提醒"
                        title="关掉这条提醒（状态没变化时，刷新后不会再出现）"
                        onClick={() => void dismissNotice(notice)}
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {reconnecting && (
              <div style={{ padding: '8px 22px 0' }}>
                <div className="banner info">
                  本页的连接断了，正在自动重连订阅；协作仍在服务端继续推进，不会中断。
                </div>
              </div>
            )}

            <div className="collab-scroll" ref={scrollRef}>
              <ConversationThread
                centered
                items={threadItems}
                user={user}
                empty={timeline.length === 0 && !liveMessage ? (
                  <div className="empty">
                    <div className="empty-icon">🤝</div>
                    <p>这次协作还没有开始</p>
                    <p className="dim small" style={{ marginTop: 6 }}>
                      在下面告诉入口智能体「{detail.entry_key}」你想做什么
                    </p>
                  </div>
                ) : undefined}
              />
            </div>

            {advanceAsk && !busy && !chatBusy && (
              <div className="collab-ask-stack">
                <div className="collab-ask-card collab-advance-card">
                  <div className="collab-ask-head">
                    <span className="pill brand">推进任务</span>
                    <span className="dim small">CEO 判断这条消息需要推进，点选后进入工单通道</span>
                  </div>
                  <div className="collab-ask-question">{advanceAsk.question}</div>
                  <div className="collab-ask-options">
                    {advanceAsk.options.map((choice, index) => (
                      <button
                        key={`advance-${index}-${choice.label}`}
                        className={`collab-ask-option${choice.freeform ? ' freeform' : ''}`}
                        onClick={() => answerAdvance(choice)}
                      >
                        <span className="collab-ask-option-label">{choice.label}</span>
                        {choice.description && (
                          <span className="collab-ask-option-desc">{choice.description}</span>
                        )}
                      </button>
                    ))}
                  </div>
                  <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                    <span className="dim small" style={{ flex: 1 }}>
                      也可以直接在下方输入调整后的指令。
                    </span>
                    <button
                      className="banner-close"
                      aria-label="关掉这张决策卡"
                      title="不推进：只把它当聊天"
                      onClick={() => consumeAdvance(advanceAsk)}
                    >
                      ✕
                    </button>
                  </div>
                </div>
              </div>
            )}

            {pendingAsks.length > 0 && (
              <div className="collab-ask-stack">
                {pendingAsks.map((ask) => (
                  <div key={ask.ticketId} className="collab-ask-card">
                    <div className="collab-ask-head">
                      <span className="pill warn">需要你决定</span>
                      <span className="dim small">入口智能体在等你的答复</span>
                    </div>
                    <div className="collab-ask-question">{ask.question}</div>
                    <div className="collab-ask-options">
                      {ask.options.map((choice, index) => (
                        <button
                          key={`${ask.ticketId}-${index}-${choice.label}`}
                          className={`collab-ask-option${choice.freeform ? ' freeform' : ''}`}
                          disabled={busy || chatBusy}
                          onClick={() => answerAsk(choice)}
                        >
                          <span className="collab-ask-option-label">{choice.label}</span>
                          {choice.description && (
                            <span className="collab-ask-option-desc">{choice.description}</span>
                          )}
                        </button>
                      ))}
                    </div>
                    <div className="dim small">也可以直接在下方输入其他答复。</div>
                  </div>
                ))}
              </div>
            )}

            {error && (
              <div style={{ padding: '0 22px 6px' }}>
                <div className="banner err">{error}</div>
              </div>
            )}

            {chatTurn.notice && (
              <div style={{ padding: '0 22px 6px' }}>
                <div className={`banner ${chatTurn.notice.level === 'error' ? 'err' : chatTurn.notice.level === 'warn' ? 'warn' : 'info'}`}>
                  <div className="row" style={{ gap: 8 }}>
                    <span style={{ flex: 1 }}>{chatTurn.notice.text}</span>
                    <button
                      className="banner-close"
                      aria-label="关掉这条提示"
                      onClick={() => setChatTurn((prev) => dismissChatNotice(prev))}
                    >
                      ✕
                    </button>
                  </div>
                </div>
              </div>
            )}

            {progressOpen && (
              <CollabProgressBoard
                nodes={progress.nodes}
                edges={progress.edges}
                steps={detail.steps}
                tickets={detail.tickets}
                live={turn?.live ?? []}
                streamed={turn?.completed ?? []}
                onClose={() => setProgressOpen(false)}
                onOpenTicket={(ticketId) => {
                  setBoardTicketId(ticketId)
                  setBoardOpen(true)
                }}
                escapeDisabled={boardOpen}
                user={user}
              />
            )}
            </div>

            {/* 与普通对话共用同一个输入区组件与版式：圆角输入框、附件 / 语音 / 发送都在框里。 */}
            <div className="chat-composer">
              <div className="chat-composer-inner">
                <Composer
                  value={draft}
                  onChange={setDraft}
                  onSend={() => {
                    const text = draft
                    const files = attachments
                    setDraft('')
                    setAttachments([])
                    void sendMessage(text, files)
                  }}
                  attachments={attachments}
                  onAttachmentsChange={setAttachments}
                  onUpload={detail ? uploadAttachment : undefined}
                  disabled={chatBusy}
                  submitOnEnter
                  onNotice={setError}
                  inputRef={composerRef}
                  inputLabel={
                    detail.goal
                      ? '对入口智能体说点什么'
                      : '告诉入口智能体你想做什么'
                  }
                  footer={
                    runUsage.messages > 0 ? (
                      <div className="usage-bar" title="本次协作累计消耗">
                        <span className="usage-tag">本次协作消耗</span>
                        <span className="usage-item">
                          <span className="usage-label">读取</span>
                          <span className="usage-value">{runUsage.input.toLocaleString()}</span>
                          <span className="usage-unit">tok</span>
                        </span>
                        <span className="usage-item">
                          <span className="usage-label">输出</span>
                          <span className="usage-value">{runUsage.output.toLocaleString()}</span>
                          <span className="usage-unit">tok</span>
                        </span>
                        <span className="usage-item">
                          <span className="usage-label">总计</span>
                          <span className="usage-value">{runUsage.total.toLocaleString()}</span>
                          <span className="usage-unit">tok</span>
                        </span>
                        <span className="usage-item">
                          <span className="usage-label">缓存命中</span>
                          <span className="usage-value">
                            {runUsage.cacheRatio === null ? '—' : `${Math.round(runUsage.cacheRatio * 100)}%`}
                          </span>
                        </span>
                        {runUsage.cacheRead > 0 && (
                          <span className="usage-item usage-muted">
                            <span className="usage-label">命中缓存</span>
                            <span className="usage-value">{runUsage.cacheRead.toLocaleString()}</span>
                            <span className="usage-unit">tok</span>
                          </span>
                        )}
                      </div>
                    ) : undefined
                  }
                />
              </div>
            </div>
          </>
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="empty-icon">🤝</div>
            <p>选择一次协作，或新建一次</p>
              <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setPicking(true)}>
              + 新建协作任务
            </button>
          </div>
        )}
      </section>

      {boardOpen && detail && (
        <TicketBoard
          tickets={detail.tickets}
          steps={detail.steps}
          graph={graph ?? null}
          agents={agents}
          nodeLabel={(key) => labelOf(key)}
          runTitle={detail.title}
          initialTicketId={boardTicketId}
          onClose={() => {
            setBoardOpen(false)
            setBoardTicketId(null)
          }}
        />
      )}

      {picking && (
        <Modal
          title="新建协作任务"
          onClose={() => setPicking(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setPicking(false)}>
                取消
              </button>
              <button className="btn primary" onClick={() => void start()} disabled={!startTeamId}>
                创建并开始对话
              </button>
            </>
          }
        >
          {teams.length === 0 ? (
            <div className="banner info">还没有协作组。请先到「智能体 → 协作组」画一张编排图。</div>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              <label className="field">
                <span className="field-label">协作团队</span>
                <select
                  className="select"
                  value={startTeamId}
                  onChange={(event) => setStartTeamId(event.target.value)}
                >
                  <option value="">请选择…</option>
                  {teams.map((item) => (
                    <option key={item.id} value={item.id} disabled={!item.validation.ok}>
                      {item.name}（{item.node_count} 个岗位）
                      {item.validation.ok ? '' : ' · 暂不可运行'}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">任务标题</span>
                <input
                  className="input"
                  value={startTitle}
                  onChange={(event) => setStartTitle(event.target.value)}
                  placeholder="例如：把登录页改成暗色主题"
                />
              </label>
              <label className="field">
                <span className="field-label">工作路径</span>
                <input
                  className="input mono"
                  value={startWorkspace}
                  onChange={(event) => setStartWorkspace(event.target.value)}
                  placeholder="例如 /vol1/projects/my-app；留空用默认路径"
                />
                <span className="field-help">
                  {teams.find((item) => item.id === startTeamId)?.workspace_dir
                    ? `留空则使用团队默认路径：${teams.find((item) => item.id === startTeamId)?.workspace_dir}`
                    : '留空则使用每次运行自动分配的默认路径。填写后所有智能体只能在该路径内读写。'}
                </span>
              </label>
            </div>
          )}
        </Modal>
      )}

      {budgetOpen && detail && (
        <Modal
          title="提高预算"
          onClose={() => setBudgetOpen(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setBudgetOpen(false)}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={() => void saveBudgetPatch()}
                disabled={
                  budgetDraft.max_steps <= detail.steps_used ||
                  budgetDraft.max_tickets <= detail.tickets_used ||
                  (budgetDraft.max_tokens !== 0 && budgetDraft.max_tokens <= usedTokens) ||
                  budgetDraft.timeout_seconds < 30
                }
              >
                保存
              </button>
            </>
          }
        >
          <div className="stack" style={{ gap: 12 }}>
            <div className="banner info">
              当前已用 {detail.steps_used} 步 / {detail.tickets_used} 张工单 / {usedTokens.toLocaleString()} tok。
              新上限必须大于已用量（token 填 0 表示不限）；保存后点「继续」就能从中断处接着跑。
            </div>
            <label className="field">
              <span className="field-label">步数上限（当前 {detail.budget.max_steps}）</span>
              <input
                className="input"
                type="number"
                min={detail.steps_used + 1}
                value={budgetDraft.max_steps}
                onChange={(event) =>
                  setBudgetDraft((prev) => ({ ...prev, max_steps: Number(event.target.value) || 0 }))
                }
              />
            </label>
            <label className="field">
              <span className="field-label">工单上限（当前 {detail.budget.max_tickets}）</span>
              <input
                className="input"
                type="number"
                min={detail.tickets_used + 1}
                value={budgetDraft.max_tickets}
                onChange={(event) =>
                  setBudgetDraft((prev) => ({ ...prev, max_tickets: Number(event.target.value) || 0 }))
                }
              />
            </label>
            <label className="field">
              <span className="field-label">
                token 上限（当前 {detail.budget.max_tokens ? detail.budget.max_tokens.toLocaleString() : '不限'}；0 = 不限）
              </span>
              <input
                className="input"
                type="number"
                min={0}
                value={budgetDraft.max_tokens}
                onChange={(event) =>
                  setBudgetDraft((prev) => ({ ...prev, max_tokens: Number(event.target.value) || 0 }))
                }
              />
            </label>
            <label className="field">
              <span className="field-label">运行时限·秒（当前 {detail.budget.timeout_seconds}）</span>
              <input
                className="input"
                type="number"
                min={30}
                value={budgetDraft.timeout_seconds}
                onChange={(event) =>
                  setBudgetDraft((prev) => ({ ...prev, timeout_seconds: Number(event.target.value) || 0 }))
                }
              />
            </label>
            <label className="field">
              <span className="field-label">
                单步限时·秒（当前 {detail.budget.step_timeout_seconds ?? 36000}；0 = 不限；对正在跑的那步不生效，从下一步起算）
              </span>
              <input
                className="input"
                type="number"
                min={0}
                value={budgetDraft.step_timeout_seconds}
                onChange={(event) =>
                  setBudgetDraft((prev) => ({ ...prev, step_timeout_seconds: Number(event.target.value) || 0 }))
                }
              />
            </label>
          </div>
        </Modal>
      )}
    </div>
    </MediaUrlProvider>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
