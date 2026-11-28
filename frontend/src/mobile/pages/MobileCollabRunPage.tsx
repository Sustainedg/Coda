import { useCallback, useEffect, useMemo, useRef, useState, type Ref } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { ClipboardIcon } from '../../components/Icons'
import { useAuth } from '../../components/AuthGate'
import { api, collabRunFileUrl, isNetworkError, streamGet, streamPost } from '../../lib/api'
import { useComposerDraft } from '../../lib/composerDraft'
import { collabChoiceAnswer, findPendingCollabAsks } from '../../lib/collabAsk'
import {
  applyChatEvent, advanceOvertakenByTicketSteps, chatAdvanceFrom, dismissChatAdvance,
  dismissChatNotice, emptyChatTurn, hideChatEcho, type ChatTurnState,
} from '../../lib/collabChat'
import {
  ADVANCE_MARKER, forgetConsumedAdvances, loadConsumedAdvances, rememberConsumedAdvance,
} from '../../lib/collabAdvance'
import { MediaUrlProvider, composeWithAttachments } from '../../lib/media'
import {
  applyCollabEvent, dismissCollabNotice, liveOfNode, syncTurnWithRun,
  type CollabNotice, type CollabTurnState,
} from '../../lib/collabStream'
import {
  forgetDismissedNotices, loadDismissedNotices, rememberDismissedNotice,
} from '../../lib/collabNotices'
import { buildCollabProgress } from '../../lib/collabProgress'
import type {
  AttachmentRef, CollabChoice, CollabEvent, CollabRunDetail, CollabTeam, AgentProfile, StreamClosedEvent,
} from '../../lib/types'
import { useAutoRefresh } from '../../lib/useAutoRefresh'
import { rawUsageTotal } from '../../lib/usage'
import { MobileCollabProgress } from '../components/MobileCollabProgress'
import { Composer } from '../../components/Composer'
import { MobileAgentConversationSheet } from '../components/MobileAgentConversationSheet'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const STATUS_LABEL: Record<string, string> = {
  running: '进行中',
  waiting_user: '等待你决定',
  done: '已完成',
  error: '出错',
  cancelled: '已取消',
  interrupted: '已中断',
}

const STATUS_PILL: Record<string, string> = {
  running: 'ok',
  waiting_user: 'warn',
  done: 'brand',
  error: 'err',
  cancelled: '',
  interrupted: 'warn',
}

interface TimelineEntry {
  id: string
  message: RenderMessage
  avatar?: string
  label: string
  /** CEO 消息带过「需求同步进工单」时，气泡下方挂一张工单更新小卡片（点开进工单看板） */
  ticketUpdate?: { ticketIds: string[]; count: number; fields: string[] }
}

/** 协作运行详情：步骤时间线 + 预算进度，可叫停、继续、回复入口岗位。 */
/** 一次订阅可能收到协作信封，也可能收到传输层的 ``stream.closed`` 收尾标记。 */
type RunEvent = CollabEvent | StreamClosedEvent

/** 重连前的等待：每次退避一点，避免断线时疯狂重试。 */
const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

/** CEO 消息的 `meta.applied`（需求同步进工单）→ 小卡片数据；没同步过返回 `undefined`。 */
function ticketUpdateOf(meta: Record<string, unknown> | undefined): TimelineEntry['ticketUpdate'] {
  const applied = meta?.applied as
    | { ticket_ids?: string[]; ticket_count?: number; fields?: string[] }
    | undefined
  if (!applied?.ticket_count) return undefined
  return {
    ticketIds: applied.ticket_ids ?? [],
    count: applied.ticket_count,
    fields: applied.fields ?? [],
  }
}

export function MobileCollabRunPage() {
  const { runId } = useParams<{ runId: string }>()
  // 当前登录用户：自己发的消息画真头像（换头像后这里跟着 `AuthGate` 的状态刷新）。
  const { user } = useAuth()
  const navigate = useNavigate()

  const [detail, setDetail] = useState<CollabRunDetail | null>(null)
  const [teams, setTeams] = useState<CollabTeam[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [turn, setTurn] = useState<CollabTurnState | null>(null)
  //: 用户点过「×」关掉的提醒（按运行记，刷新后不再重建）
  const dismissedNotices = useMemo(() => loadDismissedNotices(runId ?? ''), [runId])
  const [chatTurn, setChatTurn] = useState<ChatTurnState>(emptyChatTurn)
  //: 已经点选/关掉的「推进任务」卡（按 CEO 聊天消息 ID 记，刷新后不再恢复）
  const [consumedAdvances, setConsumedAdvances] = useState<Set<string>>(
    () => loadConsumedAdvances(runId ?? ''),
  )
  /**
   * 草稿（文字 + 待发附件）按「运行」各记各的：切到别的运行再切回来，没发出去的内容还在原处。
   */
  const { text: draft, setText: setDraft, attachments, setAttachments } = useComposerDraft(runId)
  const [busy, setBusy] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  //: 「提高预算」面板：步数/工单/token 超限挂起后，调高上限再继续
  const [budgetOpen, setBudgetOpen] = useState(false)
  const [budgetDraft, setBudgetDraft] = useState({ max_steps: 0, max_tickets: 0, max_tokens: 0, timeout_seconds: 0, step_timeout_seconds: 0 })
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null)
  /** 与入口智能体的对话界面：默认不显示，点流程图里的节点才弹出来 */
  const [chatOpen, setChatOpen] = useState(false)
  const [pendingUser, setPendingUser] = useState<{ id: string; text: string; attachments: string[] } | null>(null)
  const [reconnecting, setReconnecting] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const chatAbortRef = useRef<AbortController | null>(null)
  const busyRef = useRef(false)
  const chatBusyRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sheetComposerRef = useRef<HTMLTextAreaElement | null>(null)
  /** 当前横幅上的错误是不是「网络中断」（可自愈）：下一次拉取成功就把它撤掉，别让它一直挂着。 */
  const networkErrorRef = useRef(false)

  /** 统一上报异常：记下这次是不是网络中断，好让恢复后的第一次成功拉取把横幅撤掉。 */
  const reportError = useCallback((e: unknown) => {
    networkErrorRef.current = isNetworkError(e)
    setError(describeError(e))
  }, [])

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])
  /** 这次运行自己的编排图（第一次推进时冻结）：改协作组后，老运行的进度图与岗位名仍按原图渲染。 */
  const graph = detail?.graph ?? team?.graph
  const leaderKey = detail?.entry_key ?? team?.entry_key ?? ''
  const leaderLabel = graph?.nodes.find((node) => node.key === leaderKey)?.label || leaderKey || '领导者'
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

  const load = useCallback(async (id: string) => {
    try {
      const next = await api.getCollabRun(id)
      setDetail(next)
      setTurn((prev) => syncTurnWithRun(prev, next, next.tickets, next.steps, dismissedNotices))
      // 连接恢复（切回 PWA、重连成功）：把上一次「网络中断」的横幅撤掉，别让它长眠在界面上。
      if (networkErrorRef.current) {
        networkErrorRef.current = false
        setError(null)
      }
      return next
    } catch (e) {
      reportError(e)
      return null
    }
  }, [dismissedNotices, reportError])

  // 协作组与智能体只用于把步骤渲染成「谁的产出」：头像与岗位名。
  useEffect(() => {
    void (async () => {
      try {
        const [teamList, agentList] = await Promise.all([api.listCollabTeams(), api.listAgents()])
        setTeams(teamList)
        setAgents(agentList)
      } catch (e) {
        reportError(e)
      }
    })()
  }, [reportError])

  // 离开页面时退订；运行本身在服务端继续，回来再订阅即可。
  useEffect(() => () => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
  }, [])

  // 切到另一次运行：读取它自己的决策卡消费记忆（本地兜底，后端清 meta 为主）。
  useEffect(() => {
    setConsumedAdvances(loadConsumedAdvances(runId ?? ''))
  }, [runId])

  // SSE 只负责本设备发起的增量；运行详情以落库结果为准，并定时对齐其他设备。
  // 运行中 5s 对齐一次；挂起/完成后没有新东西，降到 20s——详情接口再瘦也经不起高频空转，
  // 移动端流量尤其金贵。本机 SSE 推进 / 聊天期间暂停轮询：那一轮由实时事件负责。
  const pollInterval = (turn?.status ?? detail?.status) === 'running' ? 5000 : 20000
  useAutoRefresh(() => (runId ? load(runId) : undefined), {
    intervalMs: pollInterval,
    enabled: Boolean(runId) && !busy && !chatBusy,
  })

  // 内容变化（新步骤、流式追加）或对话弹层刚打开时，把时间线滚到底部。
  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, chatMessages.length, leaderLive?.agent.text, chatTurn.live?.agent.text, chatOpen])

  /**
   * 订阅一次运行的实时事件并渲染。
   *
   * 推进跑在服务端独立任务里：连接被切断（手机锁屏、切后台、换网络）时这里会自动
   * 重连订阅续看，而不是把运行打断；`kickoff` 用于「发消息 / 继续」这类先启动再订阅的场景。
   */
  const subscribe = useCallback(
    async (
      target: string,
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
            reportError(e)
          }
          if (controller.signal.aborted) return
          const latest = await load(target)
          if (!latest || latest.status !== 'running') return
        }
        let attempt = 0
        while (!controller.signal.aborted && attempt < 30) {
          if (attempt > 0) {
            setReconnecting(true)
            await delay(Math.min(800 * attempt, 3000))
            if (controller.signal.aborted) break
          }
          attempt += 1
          try {
            await streamGet<RunEvent>(`/api/collab/runs/${target}/stream`, onEvent, controller.signal)
          } catch (e) {
            // 网络层断开不是运行结束：退避后重订阅，总线会补发本轮事件。
            if ((e as Error)?.name === 'AbortError') throw e
            reportError(e)
          }
          if (controller.signal.aborted) break
          // 以落库状态为准：运行真的收尾了才停，否则继续订阅。
          const latest = await load(target)
          if (!latest || latest.status !== 'running') break
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') reportError(e)
      } finally {
        busyRef.current = false
        abortRef.current = null
        setReconnecting(false)
        setBusy(false)
        await load(target)
      }
    },
    [load, reportError],
  )

  /** 入口智能体独立聊天通道：主流程在跑时也不占用、不打断这里的流。 */
  const subscribeChat = useCallback(
    async (
      target: string,
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
        if (!closed && !controller.signal.aborted && !kickoff) {
          await streamGet<RunEvent>(`/api/collab/runs/${target}/chat/stream`, onEvent, controller.signal)
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describeError(e))
      } finally {
        chatBusyRef.current = false
        chatAbortRef.current = null
        setChatBusy(false)
        await load(target)
      }
    },
    [load],
  )

  useEffect(() => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    if (!runId) return
    setError(null)
    setSelectedNodeKey(null)
    setChatOpen(false)
    setAttachments([])
    setChatTurn(emptyChatTurn())
    void (async () => {
      const next = await load(runId)
      // 这次运行在跑就订阅实时事件：本页发起的、或别的设备/上次会话发起的都一样。
      if (next && next.status === 'running' && !busyRef.current) void subscribe(runId)
      if (next?.chat_active && !chatBusyRef.current) void subscribeChat(runId)
    })()
  }, [runId, load, subscribe, subscribeChat])

  const stream = useCallback(
    async (path: string, body: unknown, userText?: string) => {
      if (!detail) return
      const pending = userText
        ? { id: `local-user-${Date.now()}`, text: userText, attachments: [] }
        : null
      if (pending) setPendingUser(pending)
      setError(null)
      try {
        await subscribe(detail.id, (onEvent, signal) => streamPost<RunEvent>(path, body, onEvent, signal))
      } finally {
        if (pending) setPendingUser(null)
      }
    },
    [detail, subscribe],
  )

  /**
   * 发一条消息：**全部走入口智能体的聊天通道**（可带附件）。
   * 是否推进任务由 CEO 判断 —— 它认为需要推进时会在回复末尾给出 chat_advance，
   * 界面渲染「推进任务」决策卡，用户点选后才把指令发进工单通道（`/messages`）。
   * 用户不再手动区分「聊天 / 推进」两条线。
   */
  const sendMessage = async (text: string, files: AttachmentRef[] = []) => {
    if (!detail) return
    if (!text.trim() && files.length === 0) return
    const paths = files.map((item) => item.rel_path)
    // 直接进对话界面：发出的内容与回复都要看得见。
    setChatOpen(true)
    const pending = {
      id: `local-user-${Date.now()}`,
      text: text.trim(),
      attachments: paths,
    }
    setPendingUser(pending)
    setError(null)
    /** 第一个事件到达即代表消息已落库：此刻把待发附件从输入区收走，而不是等整轮聊天跑完。 */
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
   * 用户在「推进任务」决策卡上点了确认：把 CEO 给出的推进指令发进工单通道。
   * 任务还没开始时这就是开场的任务描述。
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
   * 点「推进任务」决策卡上的选项：自由填写（或选项没带可发内容）只预填 CEO 建议并聚焦输入框，
   * 不发送。freeform 的 value 是空串，不能拿 label 兜底，否则「我再想想」会被当成指令发进工单通道。
   * 建议从 `advanceAsk` 取（含刷新后从落库 meta 恢复的卡片），不能只看 `chatTurn.advance`。
   * 选项 value 就是 CEO 的建议指令时走引用通道（`use_advance`），不把长指令重发一遍。
   */
  const answerAdvance = (choice: CollabChoice) => {
    if (!detail || busy || chatBusy) return
    if (choice.freeform || !choice.value.trim()) {
      const suggestion = advanceAsk?.options.find((item) => item.value.trim())?.value ?? ''
      if (suggestion) setDraft(suggestion)
      sheetComposerRef.current?.focus()
      return
    }
    advanceRun(choice.value.trim(), true)
  }

  /** 输入区（共享组件）把读好的 base64 交回来，这里只负责调接口。 */
  const uploadAttachment = async (name: string, contentBase64: string): Promise<AttachmentRef> => {
    if (!detail) throw new Error('这次协作还没加载完，稍后再试')
    return api.uploadCollabAttachment(detail.id, { name, content_base64: contentBase64 })
  }

  /** 输入区发出来的内容：交给聊天通道，附件由它决定何时清空。 */
  const submitDraft = () => {
    const text = draft
    setDraft('')
    void sendMessage(text, attachments)
  }

  /** 点流程图里的节点：入口智能体打开对话界面（它的对话就是与用户的这条线），其他岗位看只读会话。 */
  const openNode = (key: string) => {
    if (key === leaderKey) {
      setChatOpen(true)
      return
    }
    setSelectedNodeKey(key)
  }

  /** 关掉一条提醒：立即移除，并记住「用户已经看过」，刷新后不再冒出来。 */
  const dismissNotice = (notice: CollabNotice) => {
    setTurn((prev) => (prev ? dismissCollabNotice(prev, notice.key) : prev))
    rememberDismissedNotice(runId ?? detail?.id ?? '', notice.key)
  }

  const resumeWithRetry = async (stepId: string) => {
    try {
      await api.retryCollabStep(stepId)
    } catch (e) {
      reportError(e)
      return
    }
    await stream(`/api/collab/runs/${detail?.id}/resume`, {})
  }

  /** 打开「提高预算」面板：预填当前上限的 1.5 倍（至少 +20），保存后可直接继续推进。 */
  const openBudgetPatch = () => {
    if (!detail) return
    setMenuOpen(false)
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

  /** 保存新预算：成功后刷新详情；挂起原因清掉了，发消息或点继续就能把运行救回来。 */
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
      reportError(e)
      return
    }
    setBudgetOpen(false)
    await load(detail.id)
  }

  const answerAsk = (choice: CollabChoice) => {
    if (!detail || busy || chatBusy) return
    const text = collabChoiceAnswer(choice)
    if (!text) {
      sheetComposerRef.current?.focus()
      return
    }
    setDraft('')
    void stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
  }

  const stop = async () => {
    if (!detail) return
    setMenuOpen(false)
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    // 尽力而为：本地流已经断开，服务端叫停失败不影响用户（引擎会按预算自行收尾）。
    await api.cancelCollabRun(detail.id).catch(() => undefined)
    await load(detail.id)
  }

  const remove = async () => {
    if (!detail) return
    setMenuOpen(false)
    if (!confirm(`删除运行「${detail.title}」及其全部工单与步骤？`)) return
    try {
      await api.deleteCollabRun(detail.id)
    } catch (e) {
      reportError(e)
      return
    }
    forgetDismissedNotices(detail.id)
    forgetConsumedAdvances(detail.id)
    navigate('/collab', { replace: true })
  }

  const timeline = useMemo<TimelineEntry[]>(() => {
    if (!detail) return []
    // 聊天途中详情轮询会把「刚落库的用户消息」和「还没吐字的助手占位行」拉回来，
    // 与本地 pendingUser / 实时气泡重复；不藏掉就会「你 → 空气泡 → 你 → 思考中」。
    // 去重规则见 `hideChatEcho`。
    const chatRows = hideChatEcho(chatMessages, {
      liveMessageId: chatTurn.live?.messageId,
      pendingUserContent: pendingUser
        ? composeWithAttachments(pendingUser.text, pendingUser.attachments)
        : null,
      // 本机正在聊，或服务端说还有一轮在生成（另一台设备 / 刚打开页面尚未订阅）：
      // 都算「进行中」，空占位行不能漏成空气泡。
      chatActive: chatBusy || Boolean(detail.chat_active),
    })
    const pairs = [
      ...visibleSteps.map((step) => ({
        at: step.started_at,
        entry: (() => {
          if (step.role_key === 'user') {
            return {
              id: step.id,
              label: '你',
              message: { id: step.id, role: 'user' as const, content: step.content, parts: [], status: 'complete' },
            }
          }
          const node = graph?.nodes.find((item) => item.key === step.role_key)
          const agent = agents.find((item) => item.id === (step.agent_profile_id ?? node?.agent_profile_id))
          return {
            id: step.id,
            avatar: agent?.avatar ?? '',
            label: node?.label ?? step.profile_name ?? step.role_key,
            message: {
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
          }
        })(),
      })),
      ...chatRows.map((row) => ({
        at: row.created_at,
        entry: row.sender === 'user'
          ? {
              id: row.id,
              label: '你',
              message: { id: row.id, role: 'user' as const, content: row.content, parts: [], status: 'complete' },
            }
          : {
              id: row.id,
              avatar: agents.find((agent) => agent.id === row.agent_profile_id)?.avatar ?? '',
              label: leaderLabel,
              message: {
                id: row.id,
                role: 'assistant' as const,
                content: row.content,
                parts: row.parts,
                status: row.status,
                error: row.error,
                usage: row.usage,
              },
              // 这条 CEO 消息把新要求同步进了工单：气泡下方挂小卡片，点开看完整工单。
              ticketUpdate: ticketUpdateOf(row.meta),
            },
      })),
    ]
    const entries: TimelineEntry[] = pairs
      .sort((left, right) => String(left.at).localeCompare(String(right.at)))
      .map((pair) => pair.entry)
    if (pendingUser) {
      entries.push({
        id: pendingUser.id,
        label: '你',
        message: {
          id: pendingUser.id,
          role: 'user',
          // 附件清单拼回正文：图片缩略图当场就画进「你」的气泡，不用等落库后重新拉取。
          content: composeWithAttachments(pendingUser.text, pendingUser.attachments),
          parts: [],
          status: 'complete',
        },
      })
    }
    const persisted = new Set(visibleSteps.map((step) => step.id))
    for (const step of turn?.completed ?? []) {
      if (step.nodeKey !== leaderKey || persisted.has(step.stepId)) continue
      const node = graph?.nodes.find((item) => item.key === step.nodeKey)
      const agent = agents.find((item) => item.id === node?.agent_profile_id)
      entries.push({
        id: step.stepId,
        avatar: agent?.avatar ?? '',
        label: node?.label ?? step.agentName,
        message: {
          id: step.stepId,
          role: 'assistant',
          content: step.agent.text,
          parts: step.agent.parts,
          status: step.error ? 'error' : step.status,
          error: step.error ?? step.agent.error,
          usage: step.agent.usage,
          logs: step.agent.logs,
        },
      })
    }
    return entries
  }, [detail, team, agents, visibleSteps, chatMessages, pendingUser, chatTurn.live?.messageId, chatBusy, turn?.completed, leaderKey, leaderLabel])

  const liveEntry: TimelineEntry | null = leaderLive
    ? {
        id: 'live',
        avatar: agents.find(
          (agent) => agent.id === graph?.nodes.find((node) => node.key === leaderLive.nodeKey)?.agent_profile_id,
        )?.avatar ?? '',
        label: leaderLive.agentName,
        message: {
          id: 'live',
          role: 'assistant',
          content: leaderLive.agent.text,
          parts: leaderLive.agent.parts,
          status: leaderLive.agent.error ? 'error' : 'streaming',
          error: leaderLive.agent.error,
          usage: leaderLive.agent.usage,
          streaming: leaderLive.agent.status === 'running',
          logs: leaderLive.agent.logs,
        },
      }
    : null

  const chatLiveEntry: TimelineEntry | null = chatTurn.live
    ? {
        id: `chat-live-${chatTurn.live.messageId}`,
        avatar: agents.find((agent) => agent.id === graph?.nodes.find(
          (node) => node.key === (chatTurn.live?.roleKey || leaderKey),
        )?.agent_profile_id)?.avatar ?? '',
        label: graph?.nodes.find(
          (node) => node.key === (chatTurn.live?.roleKey || leaderKey),
        )?.label ?? chatTurn.live.agentName,
        message: {
          id: `chat-live-${chatTurn.live.messageId}`,
          role: 'assistant',
          content: chatTurn.live.agent.text,
          parts: chatTurn.live.agent.parts,
          status: chatTurn.live.agent.error ? 'error' : 'streaming',
          error: chatTurn.live.agent.error,
          usage: chatTurn.live.agent.usage,
          streaming: chatTurn.live.agent.status === 'running',
          logs: chatTurn.live.agent.logs,
        },
      }
    : null

  const status = turn?.status ?? detail?.status ?? 'running'
  // 详情轮询发现运行又推进起来了（另一台设备点了继续 / 本页此前掉线），而这里已经退订：
  // 自动补一次订阅，流程图里各岗位的实时对话才能继续拿到逐字输出。
  useEffect(() => {
    if (!runId || status !== 'running' || busyRef.current) return
    const timer = window.setTimeout(() => {
      if (!busyRef.current) void subscribe(runId)
    }, 800)
    return () => window.clearTimeout(timer)
  }, [runId, status, subscribe])
  const remoteRunning = status === 'running' && !busy
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
  const selectedNode = progress.nodes.find((node) => node.key === selectedNodeKey) ?? null
  const besideLeader =
    turn?.live?.find((step) => step.nodeKey !== leaderKey) ?? null
  const pendingAsks = useMemo(
    () =>
      status === 'waiting_user'
        ? findPendingCollabAsks(
            Object.values(turn?.tickets ?? {}).filter((ticket) => !detail || ticket.run_id === detail.id),
          )
        : [],
    [detail, status, turn?.tickets],
  )
  /**
   * 与入口智能体只有一条线：所有消息都进聊天通道（`/chat`，可带附件）。
   * 是否推进任务由 CEO 判断 —— 需要
   * 推进时它给出 chat_advance，界面渲染「推进任务」决策卡，用户点选后才把指令
   * 发进工单通道（`/messages`）。想从挂起点直接续跑，仍用「运行操作」里的继续 / 重试。
   */
  const usedTokens = rawUsageTotal(detail?.usage)
  const advancing = status === 'running' || busy
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
  const showComposerHeader = pendingAsks.length > 0 || advancing

  /**
   * 输入框上方的那一条：需要拍板 / 正在推进这类状态挂在上面；平时不硬塞东西。
   */
  const modeHint = advancing
    ? '正在推进：发消息只和入口智能体聊，不打断工单'
    : '消息先给入口智能体；要不要推进由它判断并给出卡片'
  const composerHeader = showComposerHeader ? (
    // 外层容器由共享的 Composer 渲染（`.composer-head`），这里只放内容，别再套一层。
    <>
      <span className="dim small">{modeHint}</span>
      {pendingAsks.length > 0 && (
        <button className="m-run-ask" onClick={() => setChatOpen(true)}>
          <span className="pill warn">需要你决定</span>
          <span className="m-run-ask-text">{pendingAsks[0].question}</span>
          <span className="m-run-ask-go">去回答 ›</span>
        </button>
      )}
      {(busy || remoteRunning) && (
        <div className="m-collab-running-bar">
          <span className="dim small">
            领导者团队正在推进{besideLeader ? `「${besideLeader.agentName}」` : ''}；点流程图里的领导者可以问进度
          </span>
          <button className="btn sm danger" onClick={() => void stop()}>
            {busy ? '■ 停止' : '叫停'}
          </button>
        </div>
      )}
    </>
  ) : undefined

  /** 输入区（共享组件）：主页面与对话弹层共用这一个渲染函数，ref 分别绑定当前可见的输入框。 */
  const renderComposer = (inputRef?: Ref<HTMLTextAreaElement>) => (
    <Composer
      value={draft}
      onChange={setDraft}
      onSend={submitDraft}
      attachments={attachments}
      onAttachmentsChange={setAttachments}
      onUpload={uploadAttachment}
      onStop={() => void stop()}
      busy={busy || chatBusy}
      disabled={!detail}
      submitOnEnter
      onNotice={(text) => {
        // 输入区的自述性提示（语音识别失败之类）：不是可自愈的网络中断，别被下一次刷新顺手撤掉。
        networkErrorRef.current = false
        setError(text)
      }}
      inputLabel={`对「${leaderLabel}」说点什么`}
      inputRef={inputRef}
      header={composerHeader}
    />
  )

  /** 对话时间线：入口智能体的产出、你与它的对话、以及需要你拍板的问题。 */
  const thread = (
    <>
      {timeline.length === 0 && !liveEntry && !chatLiveEntry && pendingAsks.length === 0 && !busy && (
        <div className="m-empty">
          <div className="m-empty-icon">🤝</div>
          <p>和领导者开始对话</p>
          <p className="dim small">先说你想做什么，要不要推进由领导者判断并给你一张卡片。</p>
        </div>
      )}
      {timeline.map((entry) => (
        <div key={entry.id}>
          <MessageView message={entry.message} avatar={entry.avatar} label={entry.label} user={user} />
          {entry.ticketUpdate && (
            <button
              type="button"
              className="collab-ticket-update-card"
              onClick={() => navigate(`/collab/runs/${detail?.id}/tickets?ticket=${entry.ticketUpdate!.ticketIds[0] ?? ''}`)}
            >
              <ClipboardIcon size={15} className="collab-ticket-update-icon" />
              <span className="collab-ticket-update-text">
                已同步到 {entry.ticketUpdate.count} 张工单
                {entry.ticketUpdate.fields.length > 0 ? `（${entry.ticketUpdate.fields.join('、')}）` : ''}
              </span>
              <span className="collab-ticket-update-more">查看工单 →</span>
            </button>
          )}
        </div>
      ))}
      {liveEntry && <MessageView message={liveEntry.message} avatar={liveEntry.avatar} label={liveEntry.label} user={user} />}
      {chatLiveEntry && (
        <MessageView message={chatLiveEntry.message} avatar={chatLiveEntry.avatar} label={chatLiveEntry.label} user={user} />
      )}
      {advanceAsk && !busy && !chatBusy && (
        <div className="m-ask-card m-advance-card">
          <div className="row" style={{ gap: 7, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="pill brand">推进任务</span>
            <span className="dim small">CEO 判断这条消息需要推进，点选后进入工单通道</span>
          </div>
          <div className="m-ask-question">{advanceAsk.question}</div>
          <div className="m-ask-options">
            {advanceAsk.options.map((choice, index) => (
              <button
                key={`advance-${index}-${choice.label}`}
                className={`m-ask-option${choice.freeform ? ' freeform' : ''}`}
                onClick={() => answerAdvance(choice)}
              >
                <span>{choice.label}</span>
                {choice.description && <span className="dim small">{choice.description}</span>}
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
              onClick={() => consumeAdvance(advanceAsk)}
            >
              ✕
            </button>
          </div>
        </div>
      )}
      {pendingAsks.map((ask) => (
        <div key={ask.ticketId} className="m-ask-card">
          <div className="row" style={{ gap: 7, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="pill warn">需要你决定</span>
            <span className="dim small">点一项作为答复，或直接在下方输入</span>
          </div>
          <div className="m-ask-question">{ask.question}</div>
          <div className="m-ask-options">
            {ask.options.map((choice, index) => (
              <button
                key={`${ask.ticketId}-${index}-${choice.label}`}
                className={`m-ask-option${choice.freeform ? ' freeform' : ''}`}
                disabled={busy || chatBusy}
                onClick={() => answerAsk(choice)}
              >
                <span>{choice.label}</span>
                {choice.description && <span className="dim small">{choice.description}</span>}
              </button>
            ))}
          </div>
        </div>
      ))}
    </>
  )

  return (
    <MediaUrlProvider
      resolver={(relPath) => (runId ? collabRunFileUrl(runId, relPath) : null)}
    >
      <div className="m-page">
      <MobileTopBar
        onBack={() => navigate('/collab')}
        title={detail?.title ?? '协作运行'}
        subtitle={
          detail
            ? `${detail.team_name ?? team?.name ?? '协作组'} · ${STATUS_LABEL[status] ?? status}`
            : '加载中…'
        }
        actions={
          <button className="m-icon-btn" onClick={() => setMenuOpen(true)} aria-label="更多操作">
            ⋯
          </button>
        }
      />

      {detail && (
        <div className="row wrap" style={{ gap: 6, padding: '10px 12px 0' }}>
          <span className={`pill ${STATUS_PILL[status] ?? ''}`}>{STATUS_LABEL[status] ?? status}</span>
          <span className="pill">
            步骤 {detail.steps_used}/{detail.budget.max_steps}
          </span>
          <span className="pill">
            工单 {detail.tickets_used}/{detail.budget.max_tickets}
          </span>
          {detail.tickets_used > 0 && <span className="pill">未结 {detail.open_tickets}</span>}
          {detail.tickets.length > 0 && (
            <button
              type="button"
              className="pill m-board-link"
              onClick={() => navigate(`/collab/runs/${detail.id}/tickets`)}
            >
              📋 工单看板 {detail.tickets.length}
            </button>
          )}
        </div>
      )}

      {/* 主页面就是那张可拖动的协作流程图；聊天界面点节点才出来。 */}
      <div className="m-run-stage">
        <MobileCollabProgress nodes={progress.nodes} edges={progress.edges} onOpenNode={openNode} />
      </div>

      {reconnecting && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner info">连接断了，正在自动重连订阅；协作仍在服务端继续推进。</div>
        </div>
      )}

      {error && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner err">{error}</div>
        </div>
      )}

      {turn && turn.notices.filter((notice) => notice.level !== 'ask').length > 0 && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="stack" style={{ gap: 6 }}>
            {turn.notices.filter((notice) => notice.level !== 'ask').slice(-3).map((notice) => (
              <div className={`banner${notice.level === 'error' ? ' err' : ''}`} key={notice.key}>
                <div className="row" style={{ gap: 8 }}>
                  <span style={{ flex: 1 }}>{notice.text}</span>
                  {notice.level === 'error' && notice.stepId && (
                    <button
                      className="btn sm"
                      disabled={busy}
                      onClick={() => void resumeWithRetry(notice.stepId as string)}
                    >
                      重试
                    </button>
                  )}
                  <button
                    className="banner-close"
                    aria-label="关掉这条提醒"
                    onClick={() => dismissNotice(notice)}
                  >
                    ✕
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {besideLeader && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner info">
            协作正在由「{besideLeader.agentName}」处理，点上方进度可查看实时输出。
          </div>
        </div>
      )}

      {chatTurn.notice && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className={`banner${chatTurn.notice.level === 'error' ? ' err' : chatTurn.notice.level === 'warn' ? ' warn' : ' info'}`}>
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

      {/* 底部只有输入框：附件 / 语音 / 发送都长在框里（与对话页共用 Composer）。 */}
      {renderComposer()}

      {chatOpen && detail && (
        <MobileSheet
          full
          title={`与「${leaderLabel}」的对话`}
          onClose={() => setChatOpen(false)}
          bodyClassName="m-run-chat-body"
        >
          <div className="m-thread m-run-chat-thread" ref={scrollRef}>
            {thread}
          </div>
          {renderComposer(sheetComposerRef)}
        </MobileSheet>
      )}

      {selectedNode && detail && (
        <MobileAgentConversationSheet
          node={selectedNode}
          roster={progress.nodes}
          steps={detail.steps}
          tickets={detail.tickets}
          live={liveOfNode(turn?.live ?? [], selectedNode.key)}
          streamed={turn?.completed ?? []}
          onOpenTicket={(ticketId) => navigate(`/collab/runs/${detail.id}/tickets?ticket=${ticketId}`)}
          onClose={() => setSelectedNodeKey(null)}
          user={user}
        />
      )}

      {menuOpen && detail && (
        <MobileSheet title="运行操作" onClose={() => setMenuOpen(false)}>
          <div className="stack" style={{ gap: 8 }}>
            <button
              className="btn block"
              onClick={() => {
                setMenuOpen(false)
                navigate(`/collab/runs/${detail.id}/tickets`)
              }}
            >
              📋 工单看板（{detail.tickets.length} 张）
            </button>
            {status === 'running' && (
              <button className="btn block danger" onClick={() => void stop()}>
                ■ 叫停这次运行
              </button>
            )}
            {status !== 'running' && (
              <button className="btn block" onClick={openBudgetPatch}>
                ⤴ 提高预算（步数 / 工单 / 时限）
              </button>
            )}
            <button
              className="btn block"
              disabled={busy}
              onClick={() => {
                setMenuOpen(false)
                void load(detail.id)
              }}
            >
              ↻ 刷新
            </button>
            <button className="btn block danger" onClick={() => void remove()}>
              删除这次运行
            </button>
          </div>
        </MobileSheet>
      )}

      {budgetOpen && detail && (
        <MobileSheet title="提高预算" onClose={() => setBudgetOpen(false)}>
          <div className="stack" style={{ gap: 10 }}>
            <div className="banner info">
              当前已用 {detail.steps_used} 步 / {detail.tickets_used} 张工单 / {usedTokens.toLocaleString()} tok。
              新上限必须大于已用量（token 填 0 表示不限）；保存后发消息或点继续就能从中断处接着跑。
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
                单步限时·秒（当前 {detail.budget.step_timeout_seconds ?? 36000}；0 = 不限；从下一步起算）
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
            <button
              className="btn primary block"
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
          </div>
        </MobileSheet>
      )}
      </div>
    </MediaUrlProvider>
  )
}
