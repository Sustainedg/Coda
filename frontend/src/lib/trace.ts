/**
 * 轨迹调试视图的取数与导出工具（只读旁路，不碰对话状态）。
 *
 * 与方案 `docs/trace-debug-view-plan.md` §6.5/§6.6 对齐：
 * - 分页拉取：`GET /trace` 按 `seq` 升序，`next_after_seq` 为空即到底；
 * - 导出：前端拼 JSON Blob 下载（阶段 4 的后端流式接口尚未拍板，先做前端版）；
 * - 脱敏：入库前后端已统一脱敏，这里在**展示 / 复制 / 导出**前再做一道
 *   「请求头、上下文快照正文」的可选擦除（导出默认开启）。
 *
 * 本文件不产生任何写请求：调用方拿到的全是 GET 与浏览器本地的 Blob 下载。
 */
import { api } from './api'
import type { Conversation, TraceEvent, TraceEventType } from './types'

/** 与后端 `MAX_PAGE_LIMIT` 无关的单页默认条数：首屏一页，展开大会话再往后翻。 */
export const TRACE_PAGE_SIZE = 100

/** 类型徽标文案：与后端 `TraceEvent.type` 一一对应。 */
const TYPE_LABELS: Record<TraceEventType, string> = {
  context_snapshot: '上下文快照',
  request: '模型请求',
  response: '模型响应',
  tool_call: '工具调用',
  tool_result: '工具结果',
}

export function traceTypeLabel(type: string): string {
  return TYPE_LABELS[type as TraceEventType] ?? type
}

/**
 * 一条事件的「来源标注」：说清这条数据是从哪个环节采的。
 *
 * 这是方案 §10 偏差 1（CLI 内核拿不到真实 HTTP 报文）在界面上的落地一半：
 * 来源必须写明，不能让 CLI 的进程入参冒充「模型请求体」。
 */
export function traceSource(event: TraceEvent): string {
  const transport = event.payload?.['transport']
  if (transport === 'http') return '来源：API 内核 HTTP 调用（真实请求 / 响应）'
  if (transport === 'gateway') return '来源：记录网关转发（CLI 内核真实请求 / 响应，已脱敏）'
  if (transport === 'cli') return '来源：CLI 内核子进程（进程入参 / 原始输出，非模型报文）'
  if (event.type === 'tool_call' || event.type === 'tool_result') return '来源：运行层规范化工具事件'
  if (event.type === 'context_snapshot') return '来源：每轮组装完成后的上下文快照'
  return `来源：trace_events（type=${event.type}）`
}

/**
 * 这条事件之后是否要渲染「不可见环节」占位。
 *
 * 方案 §11 裁决 1：CLI 的进程入参事件只记录启动描述与原始输出，不含模型报文；
 * 真实请求 / 响应由记录网关（transport=gateway）单独成条。这里必须明说「缺口在
 * 这条」并指路，不许静默留白，更不许伪造。
 */
export function hasInvisibleSegment(event: TraceEvent): boolean {
  const transport = event.payload?.['transport']
  return (event.type === 'request' || event.type === 'response') && transport === 'cli'
}

/** CLI 不可见环节的占位文案（含来源标注）。 */
export const INVISIBLE_SEGMENT_NOTE =
  '不可见环节：本条只记录 CLI 进程入参（invocation）与原始输出（raw_lines），不含模型报文。' +
  '若本轮经记录网关转发且已开启调试，真实请求 / 响应见同轮的「记录网关转发」事件。'

/** 单条时间的展示文案：今天只显示时分秒，其余显示日期 + 时分（与列表同口径，多了秒）。 */
export function formatTraceTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  if (
    date.getFullYear() === new Date().getFullYear() &&
    date.getMonth() === new Date().getMonth() &&
    date.getDate() === new Date().getDate()
  ) {
    return time
  }
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  return `${day} ${time}`
}

/** 耗时文案：`duration_ms` 只在 request / response / tool 上存在。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  const minutes = Math.floor(ms / 60_000)
  return `${minutes} min ${Math.round((ms - minutes * 60_000) / 1000)} s`
}

/**
 * 从第一页开始把一条对话的轨迹全部拉完（导出前必须拉全：前端此刻可能只翻了一页）。
 *
 * 上限硬编码 30 页（3000 条）：比后端单会话 2000 条的裁剪线更宽，超出说明数据
 * 异常，宁可导出不完整也不无休止请求。
 */
export async function fetchAllTrace(
  conversationId: string,
  onPage?: (items: TraceEvent[], total: number) => void,
): Promise<TraceEvent[]> {
  const all: TraceEvent[] = []
  let afterSeq = 0
  for (let page = 0; page < 30; page += 1) {
    const next = await api.fetchTrace(conversationId, afterSeq, TRACE_PAGE_SIZE)
    all.push(...next.items)
    onPage?.(next.items, all.length)
    if (next.next_after_seq == null) return all
    afterSeq = next.next_after_seq
  }
  return all
}

/**
 * 脱敏一道事件：隐藏 `request.headers`、上下文快照里的正文（后端本就只存
 * 长度 + 哈希，这里兜底防的是未来字段放宽）。
 */
export function redactEvent(event: TraceEvent): TraceEvent {
  const payload: Record<string, unknown> = { ...event.payload }
  if (event.type === 'request') {
    if ('headers' in payload) payload['headers'] = { redacted: true }
  }
  if (event.type === 'context_snapshot') {
    const systemPrompt = payload['system_prompt']
    if (typeof systemPrompt === 'string' && systemPrompt.length > 0) {
      // 裁决 3（默认脱敏）：正文不入导出，只留长度。
      payload['system_prompt'] = { redacted: true, chars: systemPrompt.length }
    }
    const history = payload['history']
    if (Array.isArray(history)) {
      payload['history'] = history.map((entry) =>
        entry && typeof entry === 'object' && 'content' in entry
          ? { ...entry, content: '（脱敏导出：历史正文已隐藏）' }
          : entry,
      )
    }
  }
  return { ...event, payload }
}

/** 导出文件的结构（见方案 §6.5）：元信息 + 全量事件，`redacted` 标记有没有做脱敏。 */
export interface TraceExportFile {
  version: 1
  exported_at: string
  conversation: { id: string; title: string; agent_profile_id: string }
  redacted: boolean
  events: TraceEvent[]
}

/** 生成导出文件名：`trace-<对话id>-<yyyyMMdd-HHmmss>.json`。 */
export function traceExportFileName(conversationId: string, at: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  const stamp = `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}` +
    `-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}`
  return `trace-${conversationId}-${stamp}.json`
}

/** 下载一份轨迹导出：元信息 + 全量事件，可选脱敏（默认开）。 */
export function exportTraceFile(
  conversation: Pick<Conversation, 'id' | 'title' | 'agent_profile_id'>,
  events: TraceEvent[],
  redacted: boolean,
): { fileName: string; size: number } {
  const file: TraceExportFile = {
    version: 1,
    exported_at: new Date().toISOString(),
    conversation: {
      id: conversation.id,
      title: conversation.title,
      agent_profile_id: conversation.agent_profile_id,
    },
    redacted,
    events: redacted ? events.map(redactEvent) : events,
  }
  const text = JSON.stringify(file, null, 2)
  const blob = new Blob([text], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    const fileName = traceExportFileName(conversation.id)
    link.href = url
    link.download = fileName
    document.body.appendChild(link)
    link.click()
    link.remove()
    return { fileName, size: blob.size }
  } finally {
    URL.revokeObjectURL(url)
  }
}

/**
 * 复制一段文本：优先剪贴板 API，失败退回临时 textarea + execCommand
 * （Webkit 全屏 / 非安全上下文里 clipboard 会拿不到权限）。
 */
export async function copyTraceText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 落到下面的兜底
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    area.remove()
    return ok
  } catch {
    return false
  }
}
