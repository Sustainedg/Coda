import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Composer } from '../components/Composer'
import { ContextMeter } from '../components/ContextMeter'
import { ConversationThread, type ConversationThreadItem } from '../components/ConversationThread'
import { AgentIcon, FolderIcon, MenuIcon, ModelIcon, PlusIcon, ShareIcon, SpeakerIcon, TraceIcon } from '../components/Icons'
import { TraceDrawer } from '../components/TraceDrawer'
import { AgentAvatar } from '../components/AgentAvatar'
import { MascotStage } from '../components/MascotStage'
import { TaskProgressCard } from '../components/TaskProgressCard'
import type { RenderMessage } from '../components/MessageView'
import { Modal } from '../components/Modal'
import { ConversationExportForm } from '../components/ConversationExportForm'
import { useAuth } from '../components/AuthGate'
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from '../lib/agentStream'
import { api, ApiError, conversationFileUrl, isNetworkError, streamGet, streamPost } from '../lib/api'
import { useComposerDraft } from '../lib/composerDraft'
import { contextRefreshKey, useContextMeter } from '../lib/contextUsage'
import { notifyConversationsChanged, findUnusedConversation } from '../lib/conversations'
import { formatListTime } from '../lib/datetime'
import { MediaUrlProvider, composeWithAttachments } from '../lib/media'
import { shortPath } from '../lib/paths'
import { useMarkConversationRead } from '../lib/readReceipts'
import { downloadConversationImage } from '../lib/shareImage'
import { downloadText } from '../lib/download'
import {
  buildConversationMarkdown,
  buildExportMessages,
  buildShareContent,
  markdownFileName,
  type ExportChoice,
} from '../lib/conversationExport'
import {
  isSidebarForcedCollapsed,
  readSidebarCollapsed,
  subscribeSidebarCollapsed,
  subscribeSidebarViewport,
  toggleSidebarCollapsed,
} from '../lib/sidebarCollapsed'
import { useThemeMode } from '../lib/theme'
import { useSpeechOutput } from '../lib/tts'
import { useShowTools } from '../lib/finalOnly'
import type {
  ActiveRunInfo,
  AgentEvent,
  AgentProfile,
  AttachmentRef,
  Conversation,
  ConversationDetail,
  ConversationModelOptions,
  Message,
  TaskCard,
} from '../lib/types'
import { sameMessageList, sameTaskCard, useAutoRefresh } from '../lib/useAutoRefresh'
import { summarizeUsage } from '../lib/usage'

interface LiveTurn extends AgentStreamState {
  userText: string
  /** 这一轮用户消息带上的附件（rel_path）：落库前先把缩略图画进「你」的气泡里 */
  userAttachments: string[]
  agentName: string
  avatar: string
  /** 本轮作答者的内置头像包编号：与 avatar 一并传递，气泡才能画出真头像 */
  avatarPreset?: string | null
  /** 本轮作答者的自定义头像文件标识（与 agent_profile_id 对应） */
  avatarFile?: string | null
  /** 本轮作答者的智能体 id：取自定义头像图要用 */
  agentProfileId?: string | null
  /** 这一轮是 @ 召唤的：气泡上方亮「某某正在回答」 */
  summoned?: boolean
}

/** 待发送消息：AI 回复期间先攒着，本轮结束后按顺序自动发出。 */
interface QueuedMessage {
  content: string
  attachments: AttachmentRef[]
  conversationId: string
}

/**
 * 对话页（桌面端）：左侧栏（导航 + 对话列表，见 `App.tsx`）选中一条对话，这里显示它。
 *
 * 与移动端同一套设计与分工：
 * - 顶栏右上角只有「导出 + 朗读」两个图标按钮（导出可选图片 / Markdown、
 *   对话正文 / 含工具调用的完整数据；朗读默认关闭，开着时每条新回复念出来）；
 * - 输入区是两端共享的 `components/Composer`（附件 / 语音 / 图标发送都在框里）；
 * - 清除智能体记忆用输入框里的 `/clear` 命令（在提交时拦下，不作为消息发出）；
 *   重命名与删除收在左侧栏对话行的悬停图标里，会话级弹窗不再保留。
 */
export function ChatPage() {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const speech = useSpeechOutput()
  const { theme } = useThemeMode()
  /** 当前用户：自己发的消息画真头像（见 MessageView 的 user prop） */
  const { user } = useAuth()
  /** 侧栏折叠：与外壳（App）共用同一份真值（顶栏的折叠按钮也能折，见 lib/sidebarCollapsed） */
  const manualCollapsed = useSyncExternalStore(subscribeSidebarCollapsed, readSidebarCollapsed)
  const sidebarNarrow = useSyncExternalStore(subscribeSidebarViewport, isSidebarForcedCollapsed)
  const sidebarCollapsed = sidebarNarrow || manualCollapsed
  /** 顶栏「新建对话」正在创建（期间按钮置灰，避免连点开出两条） */
  const [creating, setCreating] = useState(false)
  /**
   * 轨迹调试抽屉是否打开。按钮显隐只由后端权威开关 `user.trace_debug` 决定
   * （设置页改它），这里只是「当前这条对话的抽屉开没开」。
   */
  const [traceOpen, setTraceOpen] = useState(false)
  /** 「显示工具调用」（输入区齿轮菜单）：关掉后对话只亮最终回复（本机偏好，默认显示） */
  const { showTools, setShowTools } = useShowTools()
  /** 打开 / 轮询到已完成的回复时，把这条对话标为已读（左侧栏的未读点据此消失） */
  const markRead = useMarkConversationRead()

  const [messages, setMessages] = useState<Message[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  /** 任务进度卡片（对话级对象，只读）：随详情返回，按锚点插进消息流 */
  const [taskCard, setTaskCard] = useState<TaskCard | null>(null)
  const [live, setLive] = useState<LiveTurn | null>(null)
  /**
   * 草稿（文字 + 待发附件）按对话各记各的：切到别的对话再切回来，没发出去的内容还在原处，
   * 不会跟着跳。发送成功即清空，那一段记忆也跟着抹掉。
   */
  const { text: draft, setText: setDraft, attachments, setAttachments } = useComposerDraft(conversationId)
  /** AI 回复期间按下的消息：本轮结束后自动发出（只针对当前这条对话） */
  const [queue, setQueue] = useState<QueuedMessage[]>([])
  const [error, setError] = useState<string | null>(null)
  /** 后台同步（轮询）产生的提示：下一次同步成功就撤掉，不长期占着界面 */
  const [syncError, setSyncError] = useState<string | null>(null)
  /** 一次性的提示（导出文件已下载之类），与错误分开显示 */
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [remoteRunning, setRemoteRunning] = useState(false)
  /** 这条对话当前正在跑的运行（另一台设备 / 刷新后重连时据此订阅实时输出） */
  const [activeRun, setActiveRun] = useState<ActiveRunInfo | null>(null)
  /** 强制重新订阅的计数器：同一轮运行被 openConversation 清掉实时卡片后要能重连 */
  const [attachNonce, setAttachNonce] = useState(0)
  /** 导出弹窗开没开：右上角按钮只负责打开它，真正的格式 / 范围在弹窗里选 */
  const [exportOpen, setExportOpen] = useState(false)
  /** 正在生成导出文件（长图要几百毫秒，期间按钮置灰避免连点出两份） */
  const [exporting, setExporting] = useState(false)
  /** 导出失败时留在弹窗里的提示（成功会直接关窗，不占这条状态） */
  const [exportError, setExportError] = useState<string | null>(null)
  /** 输入框上方那几个小图标打开的面板：工作目录 / 智能体 / 模型 */
  const [scopePanel, setScopePanel] = useState<'workspace' | 'agent' | 'model' | null>(null)
  const [scopeAgents, setScopeAgents] = useState<AgentProfile[]>([])
  /** 面板里最近用过的几个工作目录（从对话列表里挑，省得手打长路径） */
  const [scopeRecent, setScopeRecent] = useState<string[]>([])
  const [workspaceDraft, setWorkspaceDraft] = useState('')
  const [scopeSaving, setScopeSaving] = useState(false)
  /** 换模型面板：可选清单 / 当前填写的值 */
  const [modelOptions, setModelOptions] = useState<ConversationModelOptions | null>(null)
  const [modelDraft, setModelDraft] = useState('')
  /** 模型面板里的搜索词：白名单一长串时按名字过滤，省得从头翻到尾 */
  const [modelQuery, setModelQuery] = useState('')
  /** @ 召唤：输入 @ 后弹出的智能体选择浮层（null = 关闭），值为 @ 后的过滤词 */
  const [mentionQuery, setMentionQuery] = useState<string | null>(null)
  /** 本轮召唤的智能体 id：随下一条消息发出，发送即清空（一次性语义） */
  const [summonId, setSummonId] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  /** 已经念过的回复 id（切换对话时把历史一次性记为「已念过」，不重念） */
  const spokenRef = useRef<Set<string>>(new Set())
  const primedConversationRef = useRef<string | null>(null)
  /** 正在订阅的另一台设备 / 后台运行 */
  const attachRef = useRef<AbortController | null>(null)
  const attachedRunIdRef = useRef<string | null>(null)
  const attachedLiveRef = useRef(false)
  const activeRef = useRef<Conversation | null>(null)
  activeRef.current = active
  /**
   * 本端正在读的那条流属于哪条对话。
   *
   * `busy` 是它的派生状态：切换对话时如果旧流还挂在另一条对话上，要中止**读取**
   * （后端那一轮运行与连接解耦，不受影响）并把 busy 交还，订阅 effect 才能接管
   * 当前对话的 `active_run`，从缓冲重放后继续实时输出。
   */
  const localStreamConversationRef = useRef<string | null>(null)
  /** 当前路由指向的对话 id：`send()` 收尾时据此判断该不该把详情写回画面 */
  const openedConversationRef = useRef<string | null>(conversationId ?? null)
  openedConversationRef.current = conversationId ?? null
  /** 消息列表是否贴着底部：用户往上翻看历史时，流式输出不能把人拽回底部 */
  const stickRef = useRef(true)
  const stickConversationRef = useRef<string | null>(null)

  /**
   * 统一上报异常：网络中断（切后台、休眠唤醒、断网）是可自愈的，交给 `syncError` ——
   * 下一次拉取成功就会撤掉；其余错误留在 `error` 上，等用户下一次操作。
   *
   * 不这样分流的话，切后台掐断的在途 SSE 会把一句原生「Load failed」写进 `error`，
   * 而 `error` 只有用户主动做点什么才会被清 —— 这就是「切回来提示一直消不掉」的根因。
   */
  const reportError = useCallback((e: unknown) => {
    if (isNetworkError(e)) {
      setSyncError(describe(e))
      return
    }
    setError(describe(e))
  }, [])

  const applyDetail = useCallback((detail: ConversationDetail, resetLive: boolean) => {
    setMessages((prev) => (sameMessageList(prev, detail.messages) ? prev : detail.messages))
    // 卡片与消息分开比较：版本没变就不重渲染整张卡（3 秒轮询不会闪）。
    setTaskCard((prev) => {
      const next = detail.task_card ?? null
      return sameTaskCard(prev, next) ? prev : next
    })
    setActive(detail.conversation)
    if (resetLive) {
      // 主动重开 / 刷新这条对话：旧的实时卡片先撤掉，若仍在运行则让订阅 effect 重新连一次。
      // 本端正在读的那条流（切换对话时可能还挂着上一条）也停掉：停的只是这里的读取，
      // 后端那一轮运行会继续跑完；若它属于当前这条对话，订阅 effect 会从缓冲接着放。
      if (localStreamConversationRef.current !== null) {
        localStreamConversationRef.current = null
        abortRef.current?.abort()
        abortRef.current = null
        setBusy(false)
      }
      setLive(null)
      attachRef.current?.abort()
      attachedRunIdRef.current = null
      attachedLiveRef.current = false
      setAttachNonce((value) => value + 1)
    }
    setActiveRun(detail.active_run ?? null)
    setRemoteRunning(Boolean(detail.active_run))
    // 最后一条是已完成的助手回复 = 用户正看着它，顺手标已读。
    markRead(detail)
  }, [markRead])

  const openConversation = useCallback(
    async (id: string) => {
      // 明确要打开某条对话（点击 / 路由 / 重置记忆）：先撤掉上一次的提示，再按本次结果决定报不报错。
      setError(null)
      setSyncError(null)
      try {
        const detail = await api.getConversation(id)
        // 连续切换时旧请求可能后到：只把结果写给当前路由指向的那条对话。
        if (openedConversationRef.current !== id) return
        applyDetail(detail, true)
        // 消息里可能有 @ 召唤的回复（agent_profile_id 非空）：拉一份智能体名单用于
        // 显示「✦ 某某 的回答」。只拉一次，之后 @ 浮层也复用这份数据。
        if (scopeAgents.length === 0) {
          void api
            .listAgents()
            .then(setScopeAgents)
            .catch(() => undefined)
        }
      } catch (e) {
        // 过期请求的失败也不该在当前这条对话上报错。
        if (openedConversationRef.current === id) reportError(e)
      }
    },
    [applyDetail, reportError, scopeAgents.length],
  )

  /** 正在看的这条对话已经不在了（多半是在别的设备上被删）：落到剩下的对话，别留一条消不掉的 404。 */
  const leaveMissingConversation = useCallback(async () => {
    setError(null)
    setSyncError(null)
    try {
      const list = (await api.listConversations()).items
      if (list.length > 0) navigate(`/chat/${list[0].id}`, { replace: true })
      else {
        setActive(null)
        setMessages([])
        navigate('/chat', { replace: true })
      }
    } catch (e) {
      setSyncError(describe(e))
    }
  }, [navigate])

  useEffect(() => {
    void (async () => {
      if (conversationId) {
        await openConversation(conversationId)
        return
      }
      // `/chat`：进入最近一次对话（一条都没有时给空状态）。
      setError(null)
      try {
        const list = (await api.listConversations()).items
        if (list.length > 0) navigate(`/chat/${list[0].id}`, { replace: true })
        else {
          setActive(null)
          setMessages([])
        }
      } catch (e) {
        setError(describe(e))
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  // 切到别的对话时收起轨迹抽屉：抽屉里画的是「打开时那条对话」的轨迹，
  // 留着会让人把上一条的轨迹误认成当前这条的。
  useEffect(() => {
    setTraceOpen(false)
  }, [conversationId])

  // 流式输出时只在下沿跟随；用户把滚轮往上翻看历史，就停在原处，不再被拽回底部。
  // 切换对话时强制回到底部（新对话从最新一条看起）。
  useEffect(() => {
    const node = scrollRef.current
    if (!node) return
    if (stickConversationRef.current !== (conversationId ?? null)) {
      stickConversationRef.current = conversationId ?? null
      stickRef.current = true
      node.scrollTop = node.scrollHeight
      return
    }
    if (stickRef.current) node.scrollTop = node.scrollHeight
  }, [messages, live?.text, live?.parts.length, conversationId])

  const onThreadScroll = () => {
    const node = scrollRef.current
    if (!node) return
    // 离底部 80px 以内算「还在跟随」，给触屏和鼠标滚轮都留一点余量。
    stickRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80
  }

  const refreshActive = useCallback(async () => {
    if (!conversationId) return
    try {
      applyDetail(await api.getConversation(conversationId), false)
      setSyncError(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        // 删除入口在左侧栏（删除流程自己会跳走）：这里照旧提示「对话不见了」即可。
        await leaveMissingConversation()
        return
      }
      setSyncError(describe(e))
    }
  }, [conversationId, applyDetail, leaveMissingConversation])

  // 跨设备同步：另一台设备发出的消息与最后状态会在这里自动出现。
  useAutoRefresh(refreshActive, { intervalMs: 3000, enabled: Boolean(conversationId) && !busy })

  /**
   * 另一台设备 / 刷新后的实时重连：详情里带 `active_run` 就订阅那条事件流。
   *
   * 后端把运行与 HTTP 连接解耦了，所以这里只是「订阅者」：页面刷新不会终止运行，
   * 重新进来会先重放缓冲里的事件，再接着收实时增量，和本地发消息的体验一致。
   */
  useEffect(() => {
    if (!conversationId || busy || !activeRun) return
    if (attachedRunIdRef.current === activeRun.id) return

    const runId = activeRun.id
    const conversation = activeRef.current
    const controller = new AbortController()
    attachRef.current = controller
    attachedRunIdRef.current = runId
    attachedLiveRef.current = true
    setLive({
      ...emptyAgentStream(),
      // 用户消息已经在数据库里（详情会拉到），这里只渲染正在生成的助手回复。
      userText: '',
      userAttachments: [],
      // 运行快照里带的是**本轮作答者**（@ 召唤轮＝被召唤者），不能用对话归属者顶替；
      // 另一台设备 / 刷新后重连时，头像和名字也要跟当前这一轮保持一致。
      agentName: activeRun.agent_name ?? conversation?.agent_name ?? '智能体',
      avatar: activeRun.agent_avatar ?? conversation?.agent_avatar ?? '🤖',
      avatarPreset: activeRun.agent_avatar_preset ?? conversation?.agent_avatar_preset ?? null,
      avatarFile: conversation?.agent_avatar_file ?? null,
      agentProfileId: activeRun.agent_profile_id ?? conversation?.agent_profile_id ?? null,
      summoned: Boolean(
        activeRun.agent_profile_id &&
          conversation &&
          activeRun.agent_profile_id !== conversation.agent_profile_id,
      ),
    })

    let cancelled = false
    void streamGet<AgentEvent>(
      `/api/conversations/${conversationId}/stream`,
      (event) => setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev)),
      controller.signal,
    )
      .catch((e) => {
        if (cancelled || (e as Error)?.name === 'AbortError') return
        // 运行刚好结束（订阅接口 404）：退回普通同步，下一轮轮询给出最终消息。
        if (e instanceof ApiError && e.status === 404) return
        setSyncError(describe(e))
      })
      .finally(() => {
        if (cancelled) return
        attachedLiveRef.current = false
        attachRef.current = null
        attachedRunIdRef.current = null
        setLive(null)
        setActiveRun(null)
        setRemoteRunning(false)
        void refreshActive()
        notifyConversationsChanged()
      })

    return () => {
      cancelled = true
      controller.abort()
      if (attachRef.current === controller) attachRef.current = null
      if (attachedRunIdRef.current === runId) attachedRunIdRef.current = null
      // 切换对话 / 运行结束：实时卡片让位给数据库里的最终消息。
      if (attachedLiveRef.current) {
        attachedLiveRef.current = false
        setLive(null)
      }
    }
  }, [conversationId, activeRun?.id, busy, refreshActive, attachNonce])

  /**
   * 朗读：开关开着 + 这条回复还没念过才出声，一次只念**最新**那条（新的会打断旧的）。
   *
   * 统一在这里判「新回复」，而不是在 `send()` 里 —— 这样另一台设备写入、由
   * `useAutoRefresh` 拉回来的回复也会念。切换对话时先把已有消息记成「已念过」，
   * 打开历史对话不会从头念一遍。
   */
  useEffect(() => {
    const replies = messages.filter((message) => message.role === 'assistant' && message.status !== 'error')
    if (primedConversationRef.current !== (active?.id ?? null)) {
      primedConversationRef.current = active?.id ?? null
      spokenRef.current = new Set(replies.map((message) => message.id))
      return
    }
    const fresh = replies.filter((message) => !spokenRef.current.has(message.id))
    fresh.forEach((message) => spokenRef.current.add(message.id))
    if (fresh.length > 0) speech.speakIfEnabled(fresh[fresh.length - 1].content)
  }, [messages, active?.id, speech.speakIfEnabled])

  // 一次性提示看完就撤，不长期占着输入框上方。
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  // 离开页面（切换对话 / 返回）时终止本端正在读的流，避免后台空转。
  // 注意：后端已经和连接解耦，终止的只是「读取」，不是那一轮运行本身。
  // （语音识别由输入区共享组件在自己卸载时收起。）
  useEffect(() => () => {
    abortRef.current?.abort()
    attachRef.current?.abort()
  }, [])

  const send = async (
    content: string,
    /** 待发送队列里的附件：不传就用输入区当前挂着的附件 */
    queuedAttachments?: AttachmentRef[],
  ): Promise<boolean> => {
    const conversation = active
    const pending = queuedAttachments ?? attachments

    /**
     * 消息没能发出去时，把正文与附件原样还给输入区，用户可以直接重发，不会凭空丢掉。
     *
     * 判据是**有没有收到事件**（`announced`）而不是有没有报错：服务端把用户消息落库之后才
     * 开始推事件，所以「收到过事件」＝消息已经发出去了，之后哪怕连接断开、用户按了停止，
     * 也只能算「没读到回复」，不能再把内容挂回输入区（那会变成重复发送）。
     */
    const restoreUnsent = () => {
      if (content.trim()) setDraft((prev) => (prev ? prev : content))
      if (pending.length > 0) {
        setAttachments((prev) => {
          const seen = new Set(prev.map((item) => item.rel_path))
          return [...prev, ...pending.filter((item) => !seen.has(item.rel_path))]
        })
      }
    }

    if (!conversation) {
      restoreUnsent()
      return false
    }
    if (remoteRunning) {
      restoreUnsent()
      return false
    }
    if (!content.trim() && pending.length === 0) return false

    setError(null)
    setBusy(true)
    setRemoteRunning(false)
    setActiveRun(null)
    attachRef.current?.abort()
    attachedRunIdRef.current = null
    attachedLiveRef.current = false
    // 主动发言：无论之前翻到哪里，都回到下沿看这次输出。
    stickRef.current = true
    // @ 召唤的一轮：只在「这次发送」带上，发出去就清掉（一次性语义，下一轮回到主场智能体）。
    const summon = summonId
    setSummonId(null)
    const summonAgent = summon ? scopeAgents.find((a) => a.id === summon) : undefined
    setLive({
      ...emptyAgentStream(),
      userText: content,
      userAttachments: pending.map((item) => item.rel_path),
      agentName: summonAgent?.name ?? conversation.agent_name ?? '智能体',
      avatar: summonAgent?.avatar ?? conversation.agent_avatar ?? '🤖',
      avatarPreset: summonAgent?.avatar_preset ?? conversation.agent_avatar_preset ?? null,
      avatarFile: summonAgent?.avatar_file ?? conversation.agent_avatar_file ?? null,
      agentProfileId: summonAgent?.id ?? conversation.agent_profile_id,
      summoned: Boolean(summon),
    })

    const controller = new AbortController()
    abortRef.current = controller
    localStreamConversationRef.current = conversation.id
    /** 第一个事件到达时通知列表：左侧栏立刻出现「正在回复」的动画，不用等下一轮 8 秒轮询 */
    let announced = false

    try {
      await streamPost(
        `/api/conversations/${conversation.id}/messages`,
        {
          content,
          attachments: pending.map((item) => item.rel_path),
          ...(summon ? { summon_profile_id: summon } : {}),
        },
        (event) => {
          if (!announced) {
            announced = true
            notifyConversationsChanged()
            // 服务端把用户消息落库之后才开始推事件（`run.started` 最先到达），
            // 所以「收到第一个事件」＝「这条消息已经发出去了」—— 此刻把待发附件从输入区收走。
            // 不早于这个时刻清：POST 被拒 / 还没连上时附件原样留着，用户可以直接重发，不会凭空丢掉。
            // 只清这一批（按 rel_path 命中），等待期间新加的附件保留。
            if (pending.length > 0) {
              const sent = new Set(pending.map((item) => item.rel_path))
              setAttachments((prev) => prev.filter((item) => !sent.has(item.rel_path)))
            }
          }
          setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev))
        },
        controller.signal,
      )
      return true
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') reportError(e)
      // 一个事件都没收到＝这一轮根本没跑起来（POST 被拒 / 还没连上）：内容还给输入区。
      if (!announced) restoreUnsent()
      return false
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      // 只收尾「本端当前这条」流：切走时 applyDetail 已经中止它并交还了 busy，
      // 用户也可能已经在别的对话里开了新的一轮 —— 旧流不能把 busy / 详情盖回去。
      if (localStreamConversationRef.current === conversation.id) {
        localStreamConversationRef.current = null
        // 先刷新详情、再交还 busy：待发送队列是盯着 busy 的，先置 false 会让下一条消息
        // 在详情还没写完时抢先开跑，随后 applyDetail 又把它当成旧流中止掉。
        if (openedConversationRef.current === conversation.id) {
          try {
            applyDetail(await api.getConversation(conversation.id), true)
          } catch {
            /* 刷新失败时保留流式渲染出来的内容 */
          }
        }
        setBusy(false)
      }
      // 这一轮会让条数与时间变化，左侧栏列表立刻对齐（不用等下一次轮询）。
      notifyConversationsChanged()
    }
  }

  /** 这条对话是否正在出一轮回复（本端读流 / 另一台设备在跑 / 服务端还有 active_run）。 */
  const runActive = busy || remoteRunning || Boolean(activeRun)

  // 输入框上方的上下文用量：消息增减 / 本轮结束就重新取；运行中轻量轮询（压缩状态、已用量）。
  // 取数口径只在后端（见 lib/contextUsage），这里只负责把变化时机告诉它。
  const contextMeter = useContextMeter(
    active?.id ?? null,
    // 条数 + 正文总长 + 本轮状态：另一台设备补完正文时也会触发刷新（见 lib/contextUsage）。
    contextRefreshKey(messages, live?.status),
    runActive,
  )

  /**
   * 输入区「发送」：
   * - `/clear` 命令：清除智能体的会话记忆，不作为消息发出；
   * - 空闲：直接发，草稿立刻清空（附件在消息落库、收到第一个事件时由 `send` 收走）；
   * - AI 正在回复：消息进入待发送队列，输入区马上可以继续写；本轮结束后按顺序自动发出
   *   （@ 召唤不支持排队：正在回复时按发送会取消召唤并提示，不悄悄丢掉用户的选择）。
   */
  const submitDraft = () => {
    if (!active) return
    if (!draft.trim() && attachments.length === 0) return
    if (draft.trim() === '/clear') {
      if (runActive) {
        setError('正在回复时不能清除记忆：等这一轮结束再发')
        return
      }
      setDraft('')
      void clearMemory()
      return
    }
    if (runActive) {
      if (summonId) {
        setSummonId(null)
        setError('正在回复时不能 @ 召唤：等这一轮结束再发')
        return
      }
      setQueue((prev) => [...prev, { content: draft, attachments, conversationId: active.id }])
      setDraft('')
      setAttachments([])
      return
    }
    const content = draft
    setDraft('')
    void send(content)
  }

  // 换到别的对话时清空队列：上一段对话的消息不能发进新对话。
  useEffect(() => {
    setQueue([])
    // 召唤目标也跟着作废：那是针对上一段对话的一次性选择。
    setSummonId(null)
    setMentionQuery(null)
  }, [conversationId])

  /**
   * @ 提及过滤词变化时按需加载智能体清单（只在第一次弹浮层时拉一次，
   * 之后复用 `scopeAgents`；与「换智能体」面板同一份数据）。
   */
  useEffect(() => {
    if (mentionQuery === null || scopeAgents.length > 0) return
    void api
      .listAgents()
      .then(setScopeAgents)
      .catch(() => undefined)
  }, [mentionQuery, scopeAgents.length])

  /** @ 浮层里可见的智能体：按过滤词匹配名字，主场智能体与停用的不参与召唤。 */
  const mentionCandidates = useMemo(() => {
    if (mentionQuery === null || !active) return []
    const query = mentionQuery.toLowerCase()
    return scopeAgents.filter(
      (agent) =>
        agent.enabled &&
        agent.id !== active.agent_profile_id &&
        (!query || agent.name.toLowerCase().includes(query)),
    )
  }, [mentionQuery, scopeAgents, active])

  /**
   * 在 @ 浮层里选中一个智能体：把草稿里的 `@过滤词` 替换成 `@名字 `，
   * 记下召唤目标并收起浮层。召唤只在下一条消息生效（发送时清空）。
   */
  const pickMention = (agent: AgentProfile) => {
    if (!draft) return
    const caret = draft.length
    const at = draft.slice(0, caret).lastIndexOf('@')
    if (at < 0) {
      setMentionQuery(null)
      return
    }
    setDraft(`${draft.slice(0, at)}@${agent.name} ${draft.slice(caret)}`)
    setSummonId(agent.id)
    setMentionQuery(null)
  }

  // 待发送队列：本轮回复结束后按顺序自动发出。
  useEffect(() => {
    if (queue.length === 0 || !active || runActive) return
    const next = queue[0]
    if (next.conversationId !== active.id) return
    setQueue((prev) => prev.slice(1))
    // 发送失败时由 `send` 把正文与附件还回输入区（只还「一个事件都没收到」的那种）；
    // 这里不再兜一次，免得消息其实已经落库、却把内容重复挂回输入框。
    void send(next.content, next.attachments)
    // send / runActive 每次渲染都是新的，这里只盯队列与运行状态
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue, runActive, active?.id])

  const stop = async () => {
    if (!active) return
    abortRef.current?.abort()
    await api.cancelRun(active.id).catch(() => undefined)
  }

  /**
   * 顶栏的「新建对话」：与侧栏「＋ 新建」同一条路 —— 用默认智能体开一条新对话，
   * 工作目录留空（进去后随时改）；还没设过默认智能体时把错误亮出来，让用户去侧栏挑一个。
   *
   * 先找「从未被使用」的空对话：有就直接跳过去（标题没动过、没发过消息、输入框没草稿），
   * 连点这个按钮也不会堆出一排「新对话」；确实没有才真开一条新的。
   */
  const startConversation = async () => {
    if (creating) return
    setCreating(true)
    setError(null)
    try {
      const unused = findUnusedConversation((await api.listConversations()).items)
      if (unused) {
        navigate(`/chat/${unused.id}`)
        return
      }
      const conversation = await api.createConversation({})
      navigate(`/chat/${conversation.id}`)
    } catch (e) {
      setError(describe(e))
    } finally {
      setCreating(false)
    }
  }

  /** 输入区共享组件把读好的 base64 交回来，这里只负责调接口。 */
  const uploadAttachment = async (name: string, contentBase64: string): Promise<AttachmentRef> => {
    if (!active) throw new Error('先选一段对话，再添加附件')
    return api.uploadAttachment(active.id, { name, content_base64: contentBase64 })
  }

  /**
   * 把当前对话导出成文件。
   *
   * 弹窗里选的两维组合最终落到这里：`format` 决定 PNG 长图还是 `.md`，
   * `scope` 决定只带正文还是连思考 / 工具调用一起带（后者直接读消息里
   * 已经落库的 `parts`，与界面上展开「过程」看到的是同一份数据）。
   */
  const exportChat = async (choice: ExportChoice) => {
    if (!active || (messages.length === 0 && !liveMessage)) return
    setExporting(true)
    setExportError(null)
    setError(null)
    setNotice(null)
    try {
      const exportMessages = buildExportMessages({
        messages: chatMessages,
        agentLabelOf: (message) => assistantIdentity(message).label,
        live,
        liveAgentLabel: live?.agentName ?? active.agent_name,
        liveMessage,
      })

      if (choice.format === 'markdown') {
        const text = buildConversationMarkdown({
          meta: {
            title: active.title,
            agentName: active.agent_name,
            agentType: active.agent_type_id,
            conversationId: active.id,
          },
          messages: exportMessages,
          scope: choice.scope,
        })
        downloadText(text, markdownFileName(active.title))
      } else {
        await downloadConversationImage({
          title: active.title,
          subtitle: [active.agent_name, active.agent_type_id].filter(Boolean).join(' · '),
          theme,
          messages: exportMessages.map((message) => ({
            role: message.role,
            content: buildShareContent(message, choice.scope),
            createdAt: formatListTime(message.createdAt),
          })),
        })
      }

      setExportOpen(false)
      setNotice(choice.scope === 'full' ? '已导出完整数据（含思考与工具调用）' : '已导出对话内容')
    } catch (e) {
      setExportError(describe(e))
    } finally {
      setExporting(false)
    }
  }

  /**
   * `/clear` 命令：清掉智能体的会话记忆（下一轮从零开始，不带内核侧会话）。
   * 不发消息 —— 命令在输入链路里就被拦下，不会作为正文进入对话。
   */
  const clearMemory = async () => {
    if (!active) return
    setError(null)
    try {
      await api.resetSession(active.id)
      await openConversation(active.id)
      // 摘要与折叠进度也清了：输入框上方的用量重新取一次。
      void contextMeter.reload()
      setNotice('已清除智能体记忆，下一轮重新开始')
    } catch (e) {
      setError(describe(e))
    }
  }

  /** 打开「工作目录」面板：顺手把别人用过的目录捞出来当快捷选项。 */
  const openWorkspacePanel = () => {
    if (!active) return
    setWorkspaceDraft(active.workspace_dir ?? '')
    setScopePanel('workspace')
    void api
      .listConversations()
      .then(({ items }) =>
        setScopeRecent(
          [
            ...new Set(
              items
                .map((item) => item.workspace_dir)
                .filter((item): item is string => Boolean(item)),
            ),
          ].slice(0, 5),
        ),
      )
      .catch(() => setScopeRecent([]))
  }

  const openAgentPanel = () => {
    setScopePanel('agent')
    void api
      .listAgents()
      .then(setScopeAgents)
      .catch((e) => setError(describe(e)))
  }

  /**
   * 改这条对话的「工作目录」或「智能体」。
   *
   * 两件事都在后端会清空会话记忆（内核的会话是绑在智能体 + 工作目录上的），所以：
   * 点中的就是当前值 → 什么都不做，免得白丢一次记忆。
   */
  const applyScope = async (patch: { workspace_dir?: string; agent_profile_id?: string }) => {
    if (!active) return
    const sameAgent = patch.agent_profile_id === undefined || patch.agent_profile_id === active.agent_profile_id
    const sameWorkspace =
      patch.workspace_dir === undefined || patch.workspace_dir === (active.workspace_dir ?? '')
    if (sameAgent && sameWorkspace) {
      setScopePanel(null)
      return
    }
    setScopeSaving(true)
    setError(null)
    try {
      await api.updateConversation(active.id, patch)
      setScopePanel(null)
      await openConversation(active.id)
      notifyConversationsChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setScopeSaving(false)
    }
  }

  /**
   * 改这条对话使用的模型。
   *
   * 只影响这条对话的下一轮运行，不重置会话记忆（与换智能体 / 换工作目录不同）。
   * 模型清单来自后端：自定义网关只给白名单，官方默认链路允许直接填写模型名。
   */
  const openModelPanel = () => {
    if (!active) return
    setModelDraft(active.model ?? '')
    setModelQuery('')
    setModelOptions(null)
    setScopePanel('model')
    void api
      .getConversationModels(active.id)
      .then((options) => {
        setModelOptions(options)
        setModelDraft(options.current ?? active.model ?? '')
      })
      .catch((e) => setError(describe(e)))
  }

  const applyModel = async (value?: string) => {
    if (!active) return
    const next = (value ?? modelDraft).trim()
    if (next === (active.model ?? '')) {
      setScopePanel(null)
      return
    }
    setScopeSaving(true)
    setError(null)
    try {
      await api.updateConversation(active.id, { model: next || null })
      setScopePanel(null)
      await openConversation(active.id)
      notifyConversationsChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setScopeSaving(false)
    }
  }

  // 重连时数据库里那条「正在生成」的空占位要藏起来：同一段内容由实时卡片渲染，
  // 否则会先出现一个空气泡、跑完又换成正式消息。
  const livePlaceholderId = live ? activeRun?.assistant_message_id ?? null : null
  const chatMessages: RenderMessage[] = messages
    .filter(
      (message) =>
        !(
          live &&
          ((livePlaceholderId !== null && message.id === livePlaceholderId) ||
            (livePlaceholderId === null && message.role === 'assistant' && message.status === 'streaming'))
        ),
    )
    .map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      parts: message.parts,
      status: message.status,
      error: message.error,
      usage: message.usage,
      created_at: message.created_at,
      // 不能在这里丢掉：@ 召唤轮的回复靠它区分作答者，头像 / 名字都从这里查
      agent_profile_id: message.agent_profile_id,
    }))

  const liveMessage: RenderMessage | null = live
    ? {
        id: 'live',
        role: 'assistant',
        content: live.text,
        parts: live.parts,
        status: live.error ? 'error' : 'streaming',
        error: live.error,
        usage: live.usage,
        streaming: live.status === 'running',
        logs: live.logs,
      }
    : null

  /**
   * 一条助手消息该用谁的头像 / 名字：@ 召唤轮的回复归被召唤者，其余归对话归属的智能体。
   *
   * 不能一律用 `active.agent_avatar` —— 那会让被召唤者顶着主场智能体的脸说话；
   * 被召唤者已经从名单里删掉（软删除后不再出现在 /api/agents）时，宁可显示占位，
   * 也不要冒用主场智能体的身份。
   */
  const assistantIdentity = (message: { role: string; agent_profile_id?: string | null }) => {
    if (message.role !== 'assistant' || !message.agent_profile_id) {
      return {
        avatar: active?.agent_avatar ?? undefined,
        avatarPreset: active?.agent_avatar_preset ?? undefined,
        avatarFile: active?.agent_avatar_file ?? undefined,
        agentId: active?.agent_profile_id ?? undefined,
        label: active?.agent_name ?? undefined,
        meta: undefined,
      }
    }
    const summoner = scopeAgents.find((agent) => agent.id === message.agent_profile_id)
    return {
      avatar: summoner?.avatar ?? '🤖',
      avatarPreset: summoner?.avatar_preset ?? undefined,
      avatarFile: summoner?.avatar_file ?? undefined,
      agentId: summoner?.id ?? undefined,
      label: summoner?.name ?? '被召唤智能体',
      meta: summoner ? `✦ ${summoner.name} 的回答（@ 召唤）` : '✦ 被召唤智能体的回答（@ 召唤）',
    }
  }

  // 任务进度卡片：插在「建卡那一轮助手消息」之后；锚点找不到（历史数据异常）时
  // 放到最后一条已落库消息之后、实时流条目之前，保证始终可见且不重复。
  const cardItem: ConversationThreadItem | null = taskCard
    ? {
        id: `task-card-${taskCard.id}`,
        className: 'task-card-turn',
        custom: <TaskProgressCard card={taskCard} />,
      }
    : null

  const threadItems: ConversationThreadItem[] = []
  let cardPlaced = false
  for (const message of chatMessages) {
    const identity = assistantIdentity(message)
    threadItems.push({
      id: message.id,
      message,
      avatar: identity.avatar,
      avatarPreset: identity.avatarPreset,
      avatarFile: identity.avatarFile,
      agentId: identity.agentId,
      label: identity.label,
      // @ 召唤轮的回复：气泡上方再亮一条「✦ 某某 的回答」，一眼分清不是主场智能体说的
      meta: identity.meta,
    })
    if (cardItem && taskCard && message.id === taskCard.anchor_message_id) {
      threadItems.push(cardItem)
      cardPlaced = true
    }
  }
  if (cardItem && !cardPlaced) threadItems.push(cardItem)

  if (live && (live.userText || live.userAttachments.length > 0)) {
    threadItems.push({
      id: 'live-user',
      message: {
        id: 'live-user',
        role: 'user' as const,
        // 附件清单拼回正文：图片缩略图当场就画进「你」的气泡，不用等整轮回复跑完、重新拉库。
        content: composeWithAttachments(live.userText, live.userAttachments),
        parts: [],
        status: 'complete',
      },
    })
  }
  if (liveMessage) {
    threadItems.push({
      id: liveMessage.id,
      message: liveMessage,
      avatar: live?.avatar,
      avatarPreset: live?.avatarPreset,
      avatarFile: live?.avatarFile,
      agentId: live?.agentProfileId,
      // 正在作答的是谁就显示谁：@ 召唤轮是 live.agentName（send 时已取被召唤者）
      label: live?.agentName,
      // @ 召唤轮正在输出：同样亮出「某某正在回答」，与历史消息的标识一致
      meta: live?.summoned ? `✦ ${live.agentName} 正在回答（@ 召唤）` : undefined,
    })
  }

  // 整段对话（含正在流式输出的这一轮）累计的 token 消耗。
  const usageSummary = useMemo(
    () =>
      summarizeUsage([
        ...messages.filter((m) => m.role === 'assistant').map((m) => m.usage),
        live?.usage,
      ]),
    [messages, live?.usage],
  )

  // 气泡与输入区里的图片（附件缩略图、智能体产出的图）都从这条出口取：rel_path → 只读文件 URL。
  const resolveMediaUrl = useCallback(
    (relPath: string) => (active ? conversationFileUrl(active.id, relPath) : null),
    [active?.id],
  )

  return (
    <MediaUrlProvider resolver={resolveMediaUrl}>
      <div className="chat-shell">
      <section className="chat-pane">
        {active ? (
          <>
            {/* 顶栏左侧：折叠侧栏 + 新建对话（与移动端顶栏同一套布局）；中间不放标题 ——
                对话名左侧栏一直看得见，顶栏不重复它。右侧是导出 / 朗读。 */}
            <div className="chat-topbar">
              <div className="chat-topbar-lead">
                {!sidebarNarrow && (
                  <button
                    className="icon-btn"
                    onClick={toggleSidebarCollapsed}
                    aria-label={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
                    title={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
                  >
                    <MenuIcon size={18} />
                  </button>
                )}
                <button
                  className="icon-btn"
                  onClick={() => void startConversation()}
                  disabled={creating}
                  aria-label="新建对话"
                  title="用默认智能体开一条新对话（智能体与工作目录进去后还能改）"
                >
                  <PlusIcon size={18} />
                </button>
                {/* 调试按钮紧挨「新建对话」：显隐只认后端权威开关 user.trace_debug，
                    关闭时整颗按钮不渲染（不是 display:none，避免被误点）。 */}
                {user?.trace_debug === true && (
                  <button
                    className={`icon-btn${traceOpen ? ' on' : ''}`}
                    onClick={() => setTraceOpen((open) => !open)}
                    aria-label="轨迹调试"
                    aria-pressed={traceOpen}
                    title="查看这条对话的调试轨迹（只读，不发写请求）"
                  >
                    <TraceIcon size={18} />
                  </button>
                )}
              </div>
              <div className="chat-topbar-actions">
                <button
                  className="icon-btn"
                  onClick={() => {
                    setExportError(null)
                    setExportOpen(true)
                  }}
                  disabled={messages.length === 0 && !liveMessage}
                  aria-label="导出对话"
                  title="导出这段对话（图片或 Markdown，可选是否包含工具调用）"
                >
                  <ShareIcon size={18} />
                </button>
                <button
                  className={`icon-btn${speech.enabled ? ' on' : ''}`}
                  onClick={() => {
                    const next = !speech.enabled
                    speech.setEnabled(next)
                    setNotice(next ? '朗读已开启：新回复会自动念出来' : '朗读已关闭')
                  }}
                  disabled={!speech.supported}
                  aria-pressed={speech.enabled}
                  aria-label={speech.enabled ? '关闭朗读' : '开启朗读'}
                  title={
                    !speech.supported
                      ? '当前浏览器不支持朗读，可用系统自带的读屏'
                      : speech.enabled
                        ? '朗读已开启：新回复会自动念出来，点一下关闭'
                        : '朗读已关闭：点一下开启，新回复会自动念出来'
                  }
                >
                  <SpeakerIcon on={speech.enabled} size={18} />
                </button>
              </div>
            </div>

            <div className="chat-scroll" ref={scrollRef} onScroll={onThreadScroll}>
              <ConversationThread
                centered
                finalOnly={!showTools}
                user={user}
                items={threadItems}
                empty={chatMessages.length === 0 && !liveMessage ? (
                  <div className="empty">
                    <MascotStage
                      avatar={active.agent_avatar}
                      avatarPreset={active.agent_avatar_preset}
                      avatarFile={active.agent_avatar_file}
                      agentId={active.agent_profile_id}
                      agentName={active.agent_name}
                    />
                    <p style={{ marginTop: 14 }}>与「{active.agent_name}」的对话已就绪</p>
                    <p className="dim small" style={{ marginTop: 6 }}>
                      内核：{active.agent_type_id ?? '智能体'} · 工作目录：{active.workspace_dir ?? '默认'}
                    </p>
                  </div>
                ) : undefined}
              />
            </div>

            {(error ?? syncError) && (
              <div style={{ padding: '0 26px' }}>
                <div className="banner err">{error ?? syncError}</div>
              </div>
            )}

            {notice && (
              <div style={{ padding: '0 26px' }}>
                <div className="banner info">{notice}</div>
              </div>
            )}

            {remoteRunning && !busy && !live && (
              <div style={{ padding: '8px 26px 0' }}>
                <div className="banner info">正在连接另一台设备的实时输出…</div>
              </div>
            )}

            <div className="chat-composer">
              <div className="chat-composer-inner">
                {/* 输入框上方是「工作目录 + 智能体」两个小图标（新建对话时不用先选，这里随时改）；
                    用量统计挪到输入框下面 —— 贴底的是输入框。 */}
                <Composer
                  value={draft}
                  onChange={setDraft}
                  onSend={submitDraft}
                  attachments={attachments}
                  onAttachmentsChange={setAttachments}
                  onUpload={uploadAttachment}
                  onStop={() => void stop()}
                  // 本端读流 / 另一台设备在跑都算「正在回复」：发送会排队，右侧给停止。
                  busy={runActive}
                  queueWhenBusy
                  submitOnEnter
                  // 齿轮菜单里的「显示工具调用」：关掉 = 对话只亮最终回复（本机偏好）。
                  showTools={showTools}
                  onShowToolsChange={setShowTools}
                  inputRef={inputRef}
                  onNotice={setError}
                  // 输入 @ 时弹智能体选择浮层（@ 召唤）
                  onMentionChange={setMentionQuery}
                  inputLabel={`发消息给 ${active.agent_name}`}
                  header={
                    <>
                      <div className="composer-scope">
                        <ContextMeter
                          usage={contextMeter.usage}
                          settings={contextMeter.settings}
                          loading={contextMeter.loading}
                          error={contextMeter.error}
                          saving={contextMeter.saving}
                          saveError={contextMeter.saveError}
                          notice={contextMeter.notice}
                          onSave={contextMeter.save}
                        />
                        <button
                          type="button"
                          className="composer-scope-btn"
                          onClick={openWorkspacePanel}
                          title={`工作目录：${active.workspace_dir ?? '按对话自动分配'}（点一下改）`}
                        >
                          <FolderIcon size={15} />
                          <span>{shortPath(active.workspace_dir)}</span>
                        </button>
                        <button
                          type="button"
                          className="composer-scope-btn"
                          onClick={openAgentPanel}
                          title={`智能体：${active.agent_name ?? '未知'}（点一下换）`}
                        >
                          <AgentIcon size={15} />
                          <span>{active.agent_name}</span>
                        </button>
                        <button
                          type="button"
                          className="composer-scope-btn"
                          onClick={openModelPanel}
                          title={`模型：${active.model ?? active.agent_model ?? '由内核决定'}（点一下换）`}
                        >
                          <ModelIcon size={15} />
                          <span>{active.model ?? active.agent_model ?? '默认模型'}</span>
                          {/* 没单独指定模型时用的是智能体默认：旁边点个标签，免得以为是专门选的 */}
                          {!active.model && active.agent_model && <span className="composer-scope-tag">默认</span>}
                        </button>
                      </div>
                      {queue.length > 0 && (
                        <div className="chat-queued-row" title="本轮回复结束后自动发出">
                          <span className="chat-queued-tag">待发送</span>
                          <span className="chat-queued-text">
                            {queue[0].content.trim() || `附件 ${queue[0].attachments.length} 个`}
                          </span>
                          {queue.length > 1 && <span className="chat-queued-count">+{queue.length - 1}</span>}
                          <button
                            type="button"
                            className="chat-queued-cancel"
                            onClick={() => setQueue([])}
                            aria-label="取消全部待发送消息"
                            title="取消全部待发送消息"
                          >
                            ✕
                          </button>
                        </div>
                      )}
                      {/* @ 召唤：正在输入 @ 时弹出的智能体选择（一次性，不影响对话归属） */}
                      {mentionQuery !== null && mentionCandidates.length > 0 && (
                        <div className="chat-mention-list" role="listbox" aria-label="选择要召唤的智能体">
                          {mentionCandidates.slice(0, 6).map((agent) => (
                            <button
                              key={agent.id}
                              type="button"
                              role="option"
                              aria-selected={false}
                              className="agent-pick"
                              onClick={() => pickMention(agent)}
                              title={`让「${agent.name}」回答这一轮（共享对话记忆，不影响本对话的智能体）`}
                            >
                              <AgentAvatar
                                avatar={agent.avatar}
                                avatarPreset={agent.avatar_preset}
                                avatarFile={agent.avatar_file}
                                agentId={agent.id}
                                size={26}
                              />
                              <span className="agent-pick-main">
                                <span className="agent-pick-name">{agent.name}</span>
                                <span className="card-sub">{agent.type_name ?? agent.type_id}</span>
                              </span>
                              {summonId === agent.id && <span className="pill ok">已选</span>}
                            </button>
                          ))}
                        </div>
                      )}
                      {/* 已选了召唤目标：贴一条提示，发送即生效、即失效 */}
                      {summonId && mentionQuery === null && (
                        <div className="chat-queued-row" title="发送这条消息后由被召唤的智能体回答，之后回到本对话的智能体">
                          <span className="chat-queued-tag">✦ 召唤</span>
                          <span className="chat-queued-text">
                            {scopeAgents.find((a) => a.id === summonId)?.name ?? '已选择的智能体'}
                            {' '}将回答这一轮
                          </span>
                          <button
                            type="button"
                            className="chat-queued-cancel"
                            onClick={() => setSummonId(null)}
                            aria-label="取消召唤"
                            title="取消召唤"
                          >
                            ✕
                          </button>
                        </div>
                      )}
                    </>
                  }
                  footer={
                    usageSummary.messages > 0 ? (
                      <div className="usage-bar" title="整段对话累计消耗">
                        <span className="usage-tag">本对话消耗</span>
                        <span className="usage-item">
                          <span className="usage-label">读取</span>
                          <span className="usage-value">{usageSummary.input.toLocaleString()}</span>
                          <span className="usage-unit">tok</span>
                        </span>
                        <span className="usage-item">
                          <span className="usage-label">输出</span>
                          <span className="usage-value">{usageSummary.output.toLocaleString()}</span>
                          <span className="usage-unit">tok</span>
                        </span>
                        <span className="usage-item">
                          <span className="usage-label">总计</span>
                          <span className="usage-value">{usageSummary.total.toLocaleString()}</span>
                          <span className="usage-unit">tok</span>
                        </span>
                        <span className="usage-item">
                          <span className="usage-label">缓存命中</span>
                          <span className="usage-value">
                            {usageSummary.cacheRatio === null
                              ? '—'
                              : `${Math.round(usageSummary.cacheRatio * 100)}%`}
                          </span>
                        </span>
                        {usageSummary.cacheRead > 0 && (
                          <span className="usage-item usage-muted">
                            <span className="usage-label">命中缓存</span>
                            <span className="usage-value">{usageSummary.cacheRead.toLocaleString()}</span>
                            <span className="usage-unit">tok</span>
                          </span>
                        )}
                      </div>
                    ) : null
                  }
                />
              </div>
            </div>
          </>
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="empty-icon">💬</div>
            <p>在左侧选一条对话，或点「＋ 新建对话」开始</p>
          </div>
        )}
      </section>

      {exportOpen && active && (
        <Modal title="导出对话" onClose={() => !exporting && setExportOpen(false)}>
          <ConversationExportForm
            busy={exporting}
            error={exportError}
            onClose={() => setExportOpen(false)}
            onExport={(choice) => void exportChat(choice)}
          />
        </Modal>
      )}

      {/* 轨迹抽屉是覆盖层的只读旁路：不卸载 ChatPage，正在跑的 SSE 与滚动位置都不丢。 */}
      {traceOpen && active && user?.trace_debug === true && (
        <TraceDrawer conversation={active} onClose={() => setTraceOpen(false)} />
      )}

      {scopePanel === 'workspace' && active && (
        <Modal
          title="工作目录"
          onClose={() => setScopePanel(null)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setScopePanel(null)}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={scopeSaving}
                onClick={() => void applyScope({ workspace_dir: workspaceDraft.trim() })}
              >
                {scopeSaving ? '保存中…' : '保存'}
              </button>
            </>
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <label className="field">
              <span className="field-label">路径</span>
              <input
                className="input mono"
                autoFocus
                value={workspaceDraft}
                onChange={(event) => setWorkspaceDraft(event.target.value)}
                placeholder="例如 /vol1/projects/my-app"
              />
            </label>
            <p className="field-help">
              留空 = 按这条对话自动分配一个独立沙箱目录。智能体就在这个目录里读写文件，
              换目录会重置这条对话的记忆（会话重新开始）。
            </p>
            {scopeRecent.length > 0 && (
              <div className="stack" style={{ gap: 6 }}>
                <span className="field-help">最近用过</span>
                <div className="chips">
                  {scopeRecent.map((path) => (
                    <button
                      key={path}
                      type="button"
                      className="chip-btn mono-sm"
                      onClick={() => setWorkspaceDraft(path)}
                      title={path}
                    >
                      {shortPath(path)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <p className="field-help">当前：{active.workspace_dir ?? '按对话自动分配'}</p>
          </div>
        </Modal>
      )}

      {scopePanel === 'agent' && active && (
        <Modal
          title="换智能体"
          onClose={() => setScopePanel(null)}
          footer={
            <button className="btn ghost" onClick={() => setScopePanel(null)}>
              关闭
            </button>
          }
        >
          <div className="stack" style={{ gap: 6 }}>
            <p className="field-help">
              换智能体会重置这条对话的记忆：不同内核的会话不通用，换了就从头开始。
            </p>
            {scopeAgents.length === 0 && (
              <p className="dim small">正在加载智能体…（一个都没有的话，先到「智能体」页创建）</p>
            )}
            {scopeAgents.map((agent) => {
              const current = agent.id === active.agent_profile_id
              return (
                <button
                  key={agent.id}
                  type="button"
                  className={`agent-pick${current ? ' active' : ''}`}
                  disabled={scopeSaving || !agent.enabled}
                  onClick={() => void applyScope({ agent_profile_id: agent.id })}
                  title={agent.enabled ? undefined : '这个智能体已停用'}
                >
                  <AgentAvatar
                    avatar={agent.avatar}
                    avatarPreset={agent.avatar_preset}
                    avatarFile={agent.avatar_file}
                    agentId={agent.id}
                    size={30}
                  />
                  <span className="agent-pick-main">
                    <span className="agent-pick-name">
                      {agent.name}
                      {agent.is_default && <span className="pill brand">默认</span>}
                    </span>
                    <span className="card-sub">
                      {agent.type_name ?? agent.type_id} ·{' '}
                      {agent.gateway_kind === 'official' || !agent.gateway_name
                        ? '官方默认'
                        : agent.gateway_name}{' '}
                      · {agent.model ?? '默认模型'}
                    </span>
                  </span>
                  {current && <span className="pill ok">当前</span>}
                </button>
              )
            })}
          </div>
        </Modal>
      )}

      {scopePanel === 'model' && active && (
        <Modal
          title="模型"
          onClose={() => setScopePanel(null)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setScopePanel(null)}>
                取消
              </button>
              <button className="btn primary" disabled={scopeSaving} onClick={() => void applyModel()}>
                {scopeSaving ? '保存中…' : '保存'}
              </button>
            </>
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <p className="field-help">
              只影响这条对话的下一轮运行；换模型不会重置会话记忆。
            </p>
            {modelOptions === null ? (
              <p className="dim small">正在加载可选模型…</p>
            ) : modelOptions.models.length > 0 ? (
              <div className="stack" style={{ gap: 6 }}>
                {/* 搜索框：白名单一长串时按名字过滤（大小写不敏感，子串匹配） */}
                <input
                  className="input"
                  type="search"
                  value={modelQuery}
                  onChange={(event) => setModelQuery(event.target.value)}
                  placeholder="搜索模型…"
                  aria-label="搜索模型"
                />
                {/* 默认模型排第一（带「默认」标签）：选中它 = 跟随智能体默认。
                    搜索时按词过滤：默认模型也得搜得到（它就是个模型名）。
                    白名单里本来就会含默认模型，下面列表要把它滤掉，否则同一个名字出现两次。 */}
                {(() => {
                  const query = modelQuery.trim().toLowerCase()
                  const matches = (name: string) => !query || name.toLowerCase().includes(query)
                  return (
                    <>
                      {modelOptions.default && matches(modelOptions.default) && (
                        <button
                          type="button"
                          className={`agent-pick${!modelDraft || modelDraft === modelOptions.default ? ' active' : ''}`}
                          onClick={() => setModelDraft('')}
                        >
                          <span className="agent-pick-main">
                            <span className="agent-pick-name mono-sm">
                              {modelOptions.default}
                              <span className="composer-scope-tag">默认</span>
                            </span>
                          </span>
                          {(!modelDraft || modelDraft === modelOptions.default) && <span className="pill ok">当前</span>}
                        </button>
                      )}
                      {modelOptions.models
                        .filter((item) => item !== modelOptions.default)
                        .filter(matches)
                        .map((item) => (
                          <button
                            key={item}
                            type="button"
                            className={`agent-pick${modelDraft === item ? ' active' : ''}`}
                            onClick={() => setModelDraft(item)}
                          >
                            <span className="agent-pick-main">
                              <span className="agent-pick-name mono-sm">{item}</span>
                            </span>
                            {modelDraft === item && <span className="pill ok">当前</span>}
                          </button>
                        ))}
                      {/* 正在用的名字不在白名单里（且被搜到）也照样显示，别让当前选择凭空消失。
                          等于默认模型时不重复列：上面那条「默认」已经代表它了。 */}
                      {modelDraft &&
                        modelDraft !== modelOptions.default &&
                        !modelOptions.models.includes(modelDraft) &&
                        matches(modelDraft) && (
                          <button type="button" className="agent-pick active" onClick={() => setModelDraft(modelDraft)}>
                            <span className="agent-pick-main">
                              <span className="agent-pick-name mono-sm">{modelDraft}</span>
                              <span className="card-sub">不在网关白名单里，保存后仍会按这个名字运行</span>
                            </span>
                            <span className="pill ok">当前</span>
                          </button>
                        )}
                      {/* 一个都没搜到：明说，别给一张空列表 */}
                      {!modelOptions.models.some(matches) &&
                        !(modelOptions.default && matches(modelOptions.default)) &&
                        !(modelDraft && !modelOptions.models.includes(modelDraft) && matches(modelDraft)) && (
                          <p className="dim small">没有匹配「{modelQuery.trim()}」的模型</p>
                        )}
                    </>
                  )
                })()}
              </div>
            ) : (
              <div className="stack" style={{ gap: 8 }}>
                <label className="field">
                  <span className="field-label">模型名</span>
                  <input
                    className="input mono"
                    autoFocus
                    value={modelDraft}
                    onChange={(event) => setModelDraft(event.target.value)}
                    placeholder={modelOptions.default ?? '例如 sonnet / gpt-5-codex'}
                  />
                </label>
                <p className="field-help">
                  {modelOptions.gateway
                    ? `当前网关「${modelOptions.gateway}」没有勾选模型白名单，可直接填写模型名。`
                    : '官方默认链路：可直接填写模型名，留空则使用内核默认模型。'}
                </p>
              </div>
            )}
          </div>
        </Modal>
      )}

      </div>
    </MediaUrlProvider>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  // 切后台 / 断网时浏览器抛的是「Load failed」这类原生英文，不该原样丢给用户。
  if (isNetworkError(error)) return '连接中断：请检查网络后重试'
  return error instanceof Error ? error.message : String(error)
}

