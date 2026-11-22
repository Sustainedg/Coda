import { useEffect, useRef, useState } from 'react'
import { ClipboardIcon } from './Icons'
import {
  copyTraceText,
  formatDuration,
  formatTraceTime,
  traceSource,
  traceTypeLabel,
} from '../lib/trace'
import type { TraceEvent } from '../lib/types'

/** 展开后 `<pre>` 默认只渲染前 200 行：大会话的超长 payload 不该把浏览器拖死（方案 §6.5）。 */
const JSON_PREVIEW_LINES = 200

const BADGE_CLASS: Record<TraceEvent['type'], string> = {
  context_snapshot: 'trace-badge ctx',
  request: 'trace-badge req',
  response: 'trace-badge res',
  tool_call: 'trace-badge call',
  tool_result: 'trace-badge result',
}

/**
 * 时间线上的一条轨迹事件：默认折叠，展开看 pretty JSON，可单条复制。
 *
 * 只读：这里没有任何写请求 —— 复制走剪贴板，JSON 只是原样渲染（payload 入库前
 * 已在后端统一脱敏，前端不再改写它的内容）。
 */
export function TraceEntry({ event }: { event: TraceEvent }) {
  const [copied, setCopied] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const timerRef = useRef<number | null>(null)
  useEffect(() => () => {
    if (timerRef.current != null) window.clearTimeout(timerRef.current)
  }, [])

  const json = JSON.stringify(event.payload, null, 2)
  const lines = json.length > 0 ? json.split('\n') : ['(空)']
  const clipped = !showAll && lines.length > JSON_PREVIEW_LINES
  const shown = clipped ? [...lines.slice(0, JSON_PREVIEW_LINES), '…'] : lines

  const copy = async () => {
    const ok = await copyTraceText(JSON.stringify(event, null, 2))
    setCopied(ok)
    if (timerRef.current != null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => setCopied(false), 1600)
  }

  return (
    <details className="trace-entry">
      <summary>
        <span className={BADGE_CLASS[event.type] ?? 'trace-badge'}>{traceTypeLabel(event.type)}</span>
        <span className="trace-entry-time mono-sm">{formatTraceTime(event.timestamp)}</span>
        {event.duration_ms != null && (
          <span className="trace-entry-dur mono-sm">{formatDuration(event.duration_ms)}</span>
        )}
        {event.error && <span className="pill err">出错</span>}
        {event.truncated && <span className="pill warn">已截断</span>}
        <span className="trace-entry-meta dim small">
          {traceSource(event)}
          {event.payload?.['call_id'] != null && ` · call_id=${String(event.payload['call_id'])}`}
          {` · seq=${event.seq}`}
        </span>
        <button
          type="button"
          className="btn ghost sm trace-copy"
          onClick={(cause) => {
            // 别让点击复制把折叠条也翻开 / 收上（summary 的默认行为）。
            cause.preventDefault()
            cause.stopPropagation()
            void copy()
          }}
          aria-label={`复制这条${traceTypeLabel(event.type)}的原始 JSON`}
        >
          <ClipboardIcon size={13} />
          {copied ? '已复制' : '复制'}
        </button>
      </summary>
      <div className="trace-entry-body">
        {event.error && <div className="banner err">采集说明：{event.error}</div>}
        <pre className="trace-json">{shown.join('\n')}</pre>
        {clipped && (
          <button type="button" className="btn ghost sm" onClick={() => setShowAll(true)}>
            显示全部（共 {lines.length} 行）
          </button>
        )}
      </div>
    </details>
  )
}
