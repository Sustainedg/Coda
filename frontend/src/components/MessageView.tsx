import { useMemo, useState } from 'react'
import type { AuthUser, MessagePart } from '../lib/types'
import { extractImagePaths, isImagePath, splitAttachmentBlock, useMediaUrl } from '../lib/media'
import { stripTaskCardFence } from '../lib/taskCardFence'
import { MarkdownView } from './MarkdownView'
import { UserAvatar } from './UserAvatar'
import { AgentAvatar } from './AgentAvatar'
import { ZoomableImage } from './ZoomableImage'

export interface RenderMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  parts: MessagePart[]
  status: string
  error?: string | null
  usage?: Record<string, unknown>
  /** 消息创建时间（导出对话时写进文件；界面上不显示） */
  created_at?: string | null
  streaming?: boolean
  logs?: string[]
  /**
   * 历史消息的「过程」折叠块懒加载器：不传表示 parts 已在手上（流式消息、单聊）。
   * 传了而 parts 为空时，点开折叠块才拉取这一步的完整工具流水——
   * 协作详情接口不再携带 parts 本体（长跑一次几十 MB），按需取。
   */
  loadParts?: () => Promise<MessagePart[]>
  /** 懒加载模式下这一步有多少过程片段（折叠行上显示「工具调用 N」）。 */
  partsCount?: number
  /** 这条回复由哪个智能体产出；为空 = 对话归属的智能体，非空 = @ 召唤的 */
  agent_profile_id?: string | null
}

export function MessageView({ message, avatar, avatarPreset, avatarFile, agentId, label, finalOnly = false, user }: {
  message: RenderMessage
  /** 老的 emoji 头像字段（已下线，仅作兜底挑头像包的种子） */
  avatar?: string
  /** 智能体的内置头像包编号 / 自定义头像文件：AI 侧气泡画真头像用（见 AgentAvatar） */
  avatarPreset?: string | null
  avatarFile?: string | null
  agentId?: string | null
  label?: string
  /** 当前登录用户（自己发的消息画真头像，不再用 🙋 emoji）；不传退回 🙋 */
  user?: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null
  /**
   * 「只看最终回复」：藏起推理 / 工具调用 / 中间解说词 —— 运行中只有思考气泡，
   * 跑完只亮时间线上**最后一段**正文（多轮「思考→调工具→解说」后的收尾总结）。
   * 出错 / 中断的报错永远保留（错误必须显式暴露，不能藏成「转完就没了」的黑盒）。
   */
  finalOnly?: boolean
}) {
  const isUser = message.role === 'user'
  // 发消息时附件被后端写进正文末尾的「📎 附件」清单；这里把它拆出来，图片画缩略图，
  // 正文照常渲染 —— 否则自己发出去的图，在气泡里只剩一行看不到的路径。
  const split = isUser
    ? splitAttachmentBlock(message.content)
    : { text: message.content, paths: [] as string[] }
  // 任务进度卡协议块永远不进气泡：实时流里未闭合的围栏也一并剥到末尾。
  const bodyText = isUser ? split.text : stripTaskCardFence(split.text)
  const attachmentPaths = split.paths
  // 智能体经常只把「文件名 / 工作目录路径」写成普通文字（而不是 Markdown 图片），
  // 这些文件其实已经生成好了。把它们从正文里摘出来单独画成图，正文照旧保留，
  // 用户就不用再对着 `/app/data/workspaces/...` 自己猜图在哪。
  const bodyImagePaths = isUser
    ? []
    : extractImagePaths(
        [bodyText, ...message.parts.filter((part) => part.kind === 'text').map((part) => part.text ?? '')].join('\n'),
      )

  // 一律按真实时间线渲染：思考 / 正文 / 工具调用与结果保持它们实际发生的先后顺序。
  // 旧消息只有 content 与工具、思考片段（没有内联正文段），这里把 content 合成一段兜底，
  // 并保持「思考在前、正文其次、工具在后」。
  const hasInlineText = message.parts.some((p) => p.kind === 'text')
  const timeline: MessagePart[] = (
    hasInlineText
      ? message.parts
      : [
          ...message.parts.filter((p) => p.kind === 'thinking'),
          ...(bodyText ? [{ kind: 'text', text: bodyText } as MessagePart] : []),
          ...message.parts.filter((p) => p.kind !== 'thinking' && p.kind !== 'text'),
        ]
  ).map((part) =>
    // 只剥智能体的输出：用户自己发的正文里若有同名的围栏代码块，不该被吞掉。
    part.kind === 'text' && !isUser
      ? { ...part, text: stripTaskCardFence(part.text ?? '') }
      : part,
  )

  const lastIndex = timeline.length - 1
  // 还在跑、但最后一段不是正文（正在思考 / 调工具）：气泡里没有可挂光标的文字，
  // 就在末尾补一个等待气泡。最后一段是正文时，光标由 `.bubble-body.streaming::after` 那条竖条负责。
  const showThinking =
    Boolean(message.streaming) && (timeline.length === 0 || timeline[lastIndex].kind !== 'text')

  // 连续的「思考 / 工具调用 / 工具结果」收成一个可展开的「过程」块：
  // 默认折叠成一行，点开才按原顺序逐条查看；正文原样内联，整条时间线顺序不变。
  // 历史消息（parts 懒加载）点开时才去拉完整工具流水。
  const blocks = groupTimeline(timeline, message)

  // 纯净模式下 AI 消息的最终正文：时间线上最后一段 text（多轮思考后的收尾总结）。
  // 旧消息没有内联正文段时上面的兜底已经把 content 合成了一段，行为一致。
  const finalTextPart = finalOnly && !isUser ? findFinalText(timeline) : null
  // 最终正文里提到的生成图片：只从这一段提取（图片属于回复内容，不算「过程」）。
  const finalImagePaths = finalTextPart ? extractImagePaths(finalTextPart.text ?? '') : []

  return (
    <div className={`bubble-row${isUser ? ' user' : ''}`}>
      <div className={`avatar${isUser ? ' user' : ''}`}>
        {/* 自己的消息画真头像（有图显示图，没图回退显示名首字母）；
            AI 侧统一走 AgentAvatar：头像包 / 自定义上传，没有就按 id 兜底头像包（emoji 已下线） */}
        {isUser
          ? (user ? <UserAvatar user={user} className="avatar-user" /> : '🙋')
          : <AgentAvatar avatar={avatar} avatarPreset={avatarPreset} avatarFile={avatarFile} agentId={agentId} />}
      </div>
      <div className="bubble">
        <div className="bubble-head">
          <span>{isUser ? '你' : (label ?? '智能体')}</span>
          {message.status === 'error' && <span className="pill err">出错</span>}
          {message.status === 'interrupted' && <span className="pill warn">已中断</span>}
        </div>

        {finalOnly && !isUser ? (
          <>
            {/* 运行中：无论正在推理 / 调工具 / 吐字，一律只有思考气泡 ——
                中途亮字再消失会像出了 bug，索性等跑完一次性亮最终回复。 */}
            {message.streaming && <ThinkingBubble />}
            {!message.streaming && finalTextPart && (
              <>
                {/* 被藏起来的段落统计：让用户知道有东西被藏了（答案偶尔落在中间段），一键可切回。
                    只有最后一段时整条时间线就是「纯正文」，没必要报数。 */}
                {timeline.length > 1 && (
                  <div className="final-only-note dim small">
                    已隐藏 {timeline.length - 1} 段过程与说明
                  </div>
                )}
                <div className="bubble-body">
                  <MarkdownView text={finalTextPart.text ?? ''} />
                </div>
                {/* 最终正文里提到的生成图片照常画出：图片属于回复内容，不算「过程」 */}
                {finalImagePaths.length > 0 && <AttachmentGallery paths={finalImagePaths} />}
              </>
            )}
            {/* 跑完了却没有任何正文（纯调工具的一轮）：占位一行，别让气泡变成空气 */}
            {!message.streaming && !finalTextPart && !message.error && (
              <div className="bubble-body dim">（这一轮没有正文输出）</div>
            )}
            {/* 出错 / 中断的报错永远保留：藏了用户会以为 AI 在摸鱼 */}
            {message.error && <div className="banner err" style={{ marginTop: 8 }}>{message.error}</div>}
          </>
        ) : (
          <>
            {blocks.map((block) =>
              block.kind === 'text' ? (
                <div
                  key={`text-${block.index}`}
                  className={`bubble-body${
                    Boolean(message.streaming) && block.index === lastIndex ? ' streaming' : ''
                  }`}
                >
                  <MarkdownView text={block.part.text ?? ''} />
                </div>
              ) : (
                <ProcessFold
                  key={`process-${block.items[0]?.index ?? 'lazy'}`}
                  items={block.items}
                  lazy={block.lazy}
                  loadParts={message.loadParts}
                  partsCount={message.partsCount}
                />
              ),
            )}

            {showThinking && <ThinkingBubble />}

            {attachmentPaths.length > 0 && <AttachmentGallery paths={attachmentPaths} />}
            {bodyImagePaths.length > 0 && <AttachmentGallery paths={bodyImagePaths} />}

            {message.logs && message.logs.length > 0 && (
              <details className="tool-card">
                <summary>
                  <DisclosureCaret />
                  <span className="tool-chip">日志</span>
                  <span className="dim">{message.logs.length} 条运行日志</span>
                </summary>
                <pre>{message.logs.join('\n')}</pre>
              </details>
            )}

            {message.error && <div className="banner err" style={{ marginTop: 8 }}>{message.error}</div>}
          </>
        )}
      </div>
    </div>
  )
}

/**
 * 时间线上最后一段正文：后端落库时相邻同类文本合并、中间夹了思考 / 工具就分段
 * （`_merge_part` 的规则），所以「思考→调工具→解说→……→收尾」的多轮回复里，
 * 最后一段 text 就是最终回复 —— 倒序找第一段 text 即可，不需要任何智能识别。
 */
function findFinalText(timeline: MessagePart[]): MessagePart | null {
  for (let i = timeline.length - 1; i >= 0; i--) {
    if (timeline[i].kind === 'text' && (timeline[i].text ?? '').trim()) return timeline[i]
  }
  return null
}

type TimelineItem = { part: MessagePart; index: number }
type TimelineBlock =
  | { kind: 'text'; part: MessagePart; index: number }
  | { kind: 'process'; items: TimelineItem[]; lazy?: boolean }

/** 把时间线切成「正文」与「连续的过程片段」两种块，只做归并、不改变先后顺序。 */
function groupTimeline(timeline: MessagePart[], message: RenderMessage): TimelineBlock[] {
  const blocks: TimelineBlock[] = []
  timeline.forEach((part, index) => {
    if (part.kind === 'text') {
      blocks.push({ kind: 'text', part, index })
      return
    }
    const last = blocks[blocks.length - 1]
    if (last && last.kind === 'process') last.items.push({ part, index })
    else blocks.push({ kind: 'process', items: [{ part, index }] })
  })
  // 懒加载的历史消息：parts 为空、且时间线里没有任何过程类部件，但后端说这一步有过程
  // （parts_count > 0）时，造一个占位过程块，让折叠行可见（「工具调用 N」），点开时才拉取真实内容。
  // 判据不能用 timeline.length === 0 —— 正文会合成一个 text 段，协作历史步骤的 content 通常非空，
  // 那样占位块永远不会出现，parts_count 指向的工具流水界面就不可达。
  if (message.loadParts && !timeline.some((part) => part.kind !== 'text') && (message.partsCount ?? 0) > 0) {
    blocks.push({ kind: 'process', items: [], lazy: true })
  }
  return blocks
}

/**
 * 一段连续的「思考 / 工具调用 / 工具结果」：默认收成一行，点标题栏展开后按原顺序列出每一步，
 * 每一步还能再单独点开看详情。这样一轮几十次工具调用也只是一行，不再「每次调用都占一条」。
 *
 * 历史消息（协作步骤）的 parts 是懒加载的：折叠行只显示数量，第一次点开才拉完整流水，
 * 拉过的缓存在组件里，收起再展开不重复请求。
 */
function ProcessFold({ items, lazy, loadParts, partsCount }: {
  items: TimelineItem[]
  lazy?: boolean
  loadParts?: () => Promise<MessagePart[]>
  partsCount?: number
}) {
  const [loaded, setLoaded] = useState<MessagePart[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const load = useMemo(
    () => async () => {
      if (!loadParts || loaded || loading) return
      setLoading(true)
      setFailed(false)
      try {
        setLoaded(await loadParts())
      } catch {
        setFailed(true)
      } finally {
        setLoading(false)
      }
    },
    [loadParts, loaded, loading],
  )
  const show = lazy ? (loaded ?? []) : items.map((item) => item.part)
  const thinking = show.filter((part) => part.kind === 'thinking').length
  const tools = show.length - thinking
  const summary = [
    thinking > 0 ? `思考 ${thinking}` : '',
    tools > 0 ? `工具调用 ${tools}` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <details
      className="tool-card process-fold"
      onToggle={(event) => {
        if ((event.target as HTMLDetailsElement).open) void load()
      }}
    >
      <summary>
        <DisclosureCaret />
        <span className="tool-chip">过程</span>
        <span className="dim">
          {lazy
            ? loading
              ? '加载中…'
              : failed
                ? '加载失败，点开重试'
                : loaded
                  ? summary || '（没有过程片段）'
                  : `工具调用 ${partsCount ?? '…'}`
            : summary}
        </span>
        <span className="dim small process-fold-hint">点击展开</span>
      </summary>
      <div className="process-fold-body">
        {show.map((part, index) => (
          <TimelinePart key={`${part.kind}-${index}`} part={part} cursor={false} />
        ))}
        {lazy && loading && <div className="dim" style={{ padding: '6px 2px' }}>正在拉取这一步的完整过程…</div>}
        {lazy && failed && <div className="banner err" style={{ margin: 0 }}>拉取过程失败，请重试。</div>}
      </div>
    </details>
  )
}

function TimelinePart({ part, cursor }: { part: MessagePart; cursor: boolean }) {
  if (part.kind === 'thinking') {
    return (
      <details className="tool-card thinking-card">
        <summary>
          <DisclosureCaret />
          <span className="tool-chip">思考</span>
          <span className="dim">推理过程</span>
        </summary>
        <pre className="thinking" style={{ borderTop: 'none' }}>{part.text}</pre>
      </details>
    )
  }
  if (part.kind === 'text') {
    return (
      <div className={`bubble-body${cursor ? ' streaming' : ''}`}>
        <MarkdownView text={part.text ?? ''} />
      </div>
    )
  }
  return <ToolCard part={part} />
}

function ToolCard({ part }: { part: MessagePart }) {
  const isCall = part.kind === 'tool_call'
  const label = isCall ? '调用' : part.is_error ? '错误' : '结果'
  const payload = isCall ? JSON.stringify(part.input ?? {}, null, 2) : (part.output ?? '')
  return (
    <details className="tool-card">
      <summary>
        <DisclosureCaret />
        <span className={`tool-chip${part.is_error ? ' err' : ''}`}>{part.name ?? label}</span>
        <span className="dim">{isCall ? '工具调用' : '工具结果'}</span>
        {!isCall && <span className="dim mono-sm" style={{ marginLeft: 'auto' }}>
          {String(payload).split('\n').length} 行
        </span>}
      </summary>
      <pre>{payload || '(空)'}</pre>
    </details>
  )
}

/**
 * 一条消息带过的附件：图片画成可点开放大的缩略图，文档等给一个文件名胶囊。
 *
 * 不在气泡里复述「📎 附件（已存入工作目录，可直接读取）：」这行提示，也不铺完整相对路径 ——
 * 那是写给智能体看的提示词，用户只需要一眼认出「带了什么」。完整路径留在 `title` 里备查；
 * 图片取不到（或页面没提供解析器）时退化成文件名胶囊，不至于留一块空白。
 * 取图走页面的 `MediaUrlResolver`（见 `lib/media.tsx`）。
 */
function AttachmentGallery({ paths }: { paths: string[] }) {
  const mediaUrl = useMediaUrl()
  const [failed, setFailed] = useState<string[]>([])
  return (
    <div className="bubble-attachments">
      {paths.map((relPath) => {
        const name = relPath.split('/').pop() ?? relPath
        const url = mediaUrl?.(relPath) ?? null
        const thumb = url && isImagePath(relPath) && !failed.includes(relPath)
        if (thumb) {
          return (
            <ZoomableImage
              key={relPath}
              className="bubble-attachment-thumb"
              src={url ?? undefined}
              alt={name}
              onError={() => setFailed((current) => (current.includes(relPath) ? current : [...current, relPath]))}
            />
          )
        }
        return (
          <span className="bubble-attachment-name" key={relPath} title={relPath}>
            {name}
          </span>
        )
      })}
    </div>
  )
}

/**
 * AI 还没吐出正文（在思考 / 调工具）时的等待气泡：三个品牌色小点依次升起再落下。
 * 以前这里是一个一闪一闪的「▍」输入光标，硬闪像坏的输入框；换成这个柔和得多。
 * 正文一旦开始流出，光标由 `.bubble-body.streaming::after` 那条渐变竖条接手。
 */
function ThinkingBubble() {
  return (
    <div className="bubble-body thinking-bubble" role="status" aria-label="正在生成回复">
      <span className="thinking-dot" />
      <span className="thinking-dot" />
      <span className="thinking-dot" />
    </div>
  )
}

/** 折叠箭头：收起时指右，展开时转 90°。纯装饰，点击由原生 details 处理。 */
function DisclosureCaret() {
  return <span className="disclosure-caret" aria-hidden>▶</span>
}
