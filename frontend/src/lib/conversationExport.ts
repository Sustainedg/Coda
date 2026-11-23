/**
 * 对话导出：把一个会话整理成 Markdown 或长图能直接用的纯文本。
 *
 * 两档内容（与导出弹窗里的选项一一对应）：
 * - `content`：只保留你和智能体说出来的正文（附件清单这类给智能体看的提示词会剥掉）；
 * - `full`：按真实时间线补上思考、工具调用入参与工具结果 —— 也就是界面上「过程」
 *   折叠块里能点开看到的那份数据。
 *
 * 本文件只做纯数据整理，不碰 DOM、不发请求；落盘下载见 `lib/download.ts`。
 */
import { composeWithAttachments, splitAttachmentBlock } from './media'
import type { MessagePart } from './types'
import { summarizeUsage } from './usage'

/** 导出格式：PNG 长图 / Markdown 文本。 */
export type ExportFormat = 'image' | 'markdown'

/** 导出范围：对话正文 / 完整数据（含思考与工具调用）。 */
export type ExportScope = 'content' | 'full'

export interface ExportChoice {
  format: ExportFormat
  scope: ExportScope
}

/** 一条待导出的消息（界面消息与实时消息都能直接映射过来）。 */
export interface ExportMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  parts?: MessagePart[]
  createdAt?: string | null
  /** 回答者名字：@ 召唤轮与主场智能体可能不是同一个人 */
  agentLabel?: string | null
  status?: string
  error?: string | null
  usage?: Record<string, number>
}

export interface ExportMeta {
  title: string
  agentName?: string | null
  agentType?: string | null
  conversationId?: string
}

export interface ExportInput {
  meta: ExportMeta
  messages: ExportMessage[]
  scope: ExportScope
}

/** 页面渲染消息里导出需要的字段（`RenderMessage` 的子集）。 */
export interface ExportSourceMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  parts?: MessagePart[]
  created_at?: string | null
  status?: string
  error?: string | null
  usage?: Record<string, number>
  /** 这条回复由哪个智能体产出；为空 = 对话归属的智能体，非空 = @ 召唤的 */
  agent_profile_id?: string | null
}

/** 正在跑的这一轮（页面手里的实时状态）。 */
export interface ExportLiveTurn {
  userText: string
  /** 这一轮用户消息带上的附件（相对路径） */
  userAttachments: string[]
}

export interface BuildExportMessagesInput {
  /** 已落库的消息（页面按渲染口径过滤过流式占位） */
  messages: ExportSourceMessage[]
  /** 每条落库消息的作答者名字（只对 assistant 生效） */
  agentLabelOf: (message: ExportSourceMessage) => string | null | undefined
  /** 正在跑的实时轮 */
  live?: ExportLiveTurn | null
  /** 实时轮回答者名字（@ 召唤轮可能与主场不同） */
  liveAgentLabel?: string | null
  /** 实时回答（还在流式输出） */
  liveMessage?: ExportSourceMessage | null
  /** 实时消息的时间戳（默认当前时间；只用于导出文件里的标注） */
  now?: string
}

/**
 * 把页面消息 + 实时轮拼成 `ExportMessage[]`（两端导出共用）。
 *
 * 附件清单是写给智能体读的提示词，导出时只保留用户真正说的话；正在跑的这一轮
 * 用户消息可能还没进 `messages`（要等运行结束才刷新详情），这里补上，
 * 免得导出的文件里只有 AI 的回答、没有刚问的问题。
 */
export function buildExportMessages({
  messages,
  agentLabelOf,
  live,
  liveAgentLabel,
  liveMessage,
  now,
}: BuildExportMessagesInput): ExportMessage[] {
  const stamp = now ?? new Date().toISOString()
  return [
    ...messages.map((message) => ({
      role: message.role,
      content: splitAttachmentBlock(message.content).text,
      parts: message.parts,
      createdAt: message.created_at,
      agentLabel: message.role === 'assistant' ? agentLabelOf(message) : undefined,
      status: message.status,
      error: message.error,
      usage: message.usage,
    })),
    ...(live && (live.userText || live.userAttachments.length > 0)
      ? [{
          role: 'user' as const,
          content: splitAttachmentBlock(
            composeWithAttachments(live.userText, live.userAttachments),
          ).text,
          parts: [],
          createdAt: stamp,
          status: 'complete',
        }]
      : []),
    ...(liveMessage
      ? [{
          role: 'assistant' as const,
          content: liveMessage.content,
          parts: liveMessage.parts,
          createdAt: stamp,
          agentLabel: liveAgentLabel ?? undefined,
          status: 'streaming',
          error: liveMessage.error,
          usage: liveMessage.usage,
        }]
      : []),
  ]
}

/** 时间线片段：正文 / 思考 / 工具调用 / 工具结果。 */
export type ExportSegment =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool_call'; name: string; text: string }
  | { kind: 'tool_result'; name: string; text: string; isError: boolean }

/** 只要对话正文：优先取时间线里的 text 片段，旧消息没有 parts 时退回 content。 */
export function visibleText(message: ExportMessage): string {
  const texts = (message.parts ?? [])
    .filter((part) => part.kind === 'text')
    .map((part) => part.text ?? '')
    .filter((text) => text.trim().length > 0)
  return texts.length > 0 ? texts.join('\n\n') : message.content
}

/**
 * 展开一条消息的完整时间线。
 *
 * 与 `MessageView` 的归约规则保持一致：有新式内联正文时严格按 parts 顺序；
 * 旧消息没有内联正文段时，按「思考 → 正文 → 工具」把 content 补在中间。
 */
export function messageSegments(message: ExportMessage): ExportSegment[] {
  const parts = message.parts ?? []
  const hasInlineText = parts.some((part) => part.kind === 'text')
  const timeline: MessagePart[] = hasInlineText
    ? parts
    : [
        ...parts.filter((part) => part.kind === 'thinking'),
        ...(message.content ? [{ kind: 'text', text: message.content } as MessagePart] : []),
        ...parts.filter((part) => part.kind !== 'thinking' && part.kind !== 'text'),
      ]

  return timeline
    .map((part) => {
      if (part.kind === 'thinking') return { kind: 'thinking' as const, text: part.text ?? '' }
      if (part.kind === 'tool_call') {
        return {
          kind: 'tool_call' as const,
          name: part.name ?? '工具',
          text: stringifyToolInput(part.input),
        }
      }
      if (part.kind === 'tool_result') {
        return {
          kind: 'tool_result' as const,
          name: part.name ?? '工具',
          text: part.output ?? '',
          isError: Boolean(part.is_error),
        }
      }
      return { kind: 'text' as const, text: part.text ?? '' }
    })
    .filter((segment) => {
      // 空片段没有导出价值；工具调用即使入参为空也保留（名字本身说明发生过什么）。
      if (segment.kind === 'tool_call' || segment.kind === 'tool_result') return true
      return segment.text.trim().length > 0
    })
}

function stringifyToolInput(input: Record<string, unknown> | undefined): string {
  if (input === undefined || input === null) return ''
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

/** 代码围栏长度：正文里出现反引号时自动加长，避免把内容截断。 */
function fenceFor(text: string): string {
  let longest = 0
  let current = 0
  for (const char of text) {
    if (char === '`') {
      current += 1
      longest = Math.max(longest, current)
    } else {
      current = 0
    }
  }
  return '`'.repeat(Math.max(3, longest + 1))
}

function codeBlock(text: string, language = ''): string {
  const fence = fenceFor(text)
  return `${fence}${language}\n${text}\n${fence}`
}

/** 超过上限时截断：只用在长图上，Markdown 导出保持原样。 */
function limitForImage(text: string, limit?: number): string {
  if (!limit || text.length <= limit) return text
  return `${text.slice(0, limit)}\n…（内容过长，长图中已截断 ${text.length - limit} 个字符；完整内容请导出 Markdown）`
}

/**
 * 把时间线片段转成一段 Markdown。
 *
 * `toolLimit` 是给长图用的单段字符上限（工具结果动辄几万行，整段画进 canvas 会糊成一片）；
 * Markdown 导出不传，保留完整内容。
 */
export function segmentsToMarkdown(
  segments: ExportSegment[],
  options: { toolLimit?: number } = {},
): string {
  return segments
    .map((segment) => {
      if (segment.kind === 'text') return segment.text.trim()
      if (segment.kind === 'thinking') {
        return `**思考**\n\n${limitForImage(segment.text.trim(), options.toolLimit)}`
      }
      if (segment.kind === 'tool_call') {
        const body = limitForImage(segment.text, options.toolLimit)
        return `**工具调用 · ${segment.name}**\n\n${body ? codeBlock(body, 'json') : '（无入参）'}`
      }
      const label = segment.isError ? '工具结果（出错）' : '工具结果'
      const body = limitForImage(segment.text, options.toolLimit)
      return `**${label} · ${segment.name}**\n\n${body ? codeBlock(body) : '（空）'}`
    })
    .filter((text) => text.length > 0)
    .join('\n\n')
}

/**
 * 一条消息在长图上要画的正文。
 *
 * `content` 档只画正文；`full` 档把思考与工具流水按顺序拼进同一段 Markdown
 * （长图按代码块排版，不另造一套气泡样式）。
 */
export function buildShareContent(message: ExportMessage, scope: ExportScope): string {
  if (scope === 'content') return visibleText(message).trim()
  return segmentsToMarkdown(messageSegments(message), { toolLimit: 6000 })
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** 导出里用的完整时间：`YYYY-MM-DD HH:mm:ss`（本机时区）。 */
export function formatExportTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

function messageHeading(message: ExportMessage): string {
  if (message.role === 'user') return '你'
  if (message.role === 'system') return '系统'
  return message.agentLabel?.trim() || '智能体'
}

function usageLine(message: ExportMessage): string {
  if (!message.usage || Object.keys(message.usage).length === 0) return ''
  const usage = summarizeUsage([message.usage])
  return `> 用量：读取 ${usage.input.toLocaleString()} tok · 输出 ${usage.output.toLocaleString()} tok`
}

/**
 * 生成整段对话的 Markdown 文件。
 *
 * 结构刻意保持朴素：标题 → 元信息 → 每条消息一个二级标题。用户拿到的是一份
 * 能直接丢进任何编辑器、也能被智能体重新读懂的文本，不做花哨装饰。
 */
export function buildConversationMarkdown(
  input: ExportInput,
  generatedAt: Date = new Date(),
): string {
  const lines: string[] = []
  lines.push(`# ${input.meta.title.trim() || '对话'}`)
  lines.push('')

  const agent = [input.meta.agentName, input.meta.agentType]
    .filter((value): value is string => Boolean(value && value.trim()))
    .join(' · ')
  if (agent) lines.push(`- 智能体：${agent}`)
  lines.push(`- 导出时间：${formatExportTime(generatedAt.toISOString())}`)
  lines.push(
    `- 导出范围：${input.scope === 'full' ? '完整数据（含思考与工具调用）' : '对话内容'}`,
  )
  lines.push(`- 消息数：${input.messages.length}`)
  if (input.meta.conversationId) lines.push(`- 对话 id：${input.meta.conversationId}`)
  lines.push('')
  lines.push('---')

  for (const message of input.messages) {
    lines.push('')
    const stamp = formatExportTime(message.createdAt)
    lines.push(`## ${messageHeading(message)}${stamp ? ` · ${stamp}` : ''}`)

    if (message.status === 'error') lines.push('> 状态：出错')
    else if (message.status === 'interrupted') lines.push('> 状态：已中断')
    else if (message.status === 'streaming') lines.push('> 状态：正在生成（导出时尚未完成）')
    if (message.error) lines.push(`> 错误：${message.error}`)
    if (input.scope === 'full') {
      const usage = usageLine(message)
      if (usage) lines.push(usage)
    }
    lines.push('')

    const body =
      input.scope === 'content'
        ? visibleText(message).trim()
        : segmentsToMarkdown(messageSegments(message))
    lines.push(body || '（无内容）')
  }

  return `${lines.join('\n')}\n`
}

/** 导出文件名：`coda-<标题片段>-<yyyyMMdd-HHmmss>.md`。 */
export function markdownFileName(title: string, at: Date = new Date()): string {
  const slug = title.trim().replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 40) || 'conversation'
  const stamp =
    `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}` +
    `-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}`
  return `coda-${slug}-${stamp}.md`
}
