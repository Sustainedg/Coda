import { useCallback, useEffect, useRef, useState } from 'react'
import { TraceTimeline } from './TraceTimeline'
import { ApiError, api } from '../lib/api'
import {
  TRACE_PAGE_SIZE,
  exportTraceFile,
  fetchAllTrace,
  formatTraceTime,
} from '../lib/trace'
import type { Conversation, TraceEvent } from '../lib/types'

/**
 * 轨迹调试抽屉：挂在当前对话页右侧的只读旁路（方案 §6.3）。
 *
 * 只读约束（方案 §8.4）：
 * - 全程只有 `GET /trace`（首屏一页、加载更多一页、导出前拉全量）；
 * - 不碰 `ChatPage` 的消息 / 运行状态，打开它不会重放、不会写消息表；
 * - 刷新页面或重启服务后重新打开，读到的仍是 `trace_events` 里同一份数据。
 */
export function TraceDrawer({
  conversation,
  onClose,
}: {
  conversation: Conversation
  onClose: () => void
}) {
  const conversationId = conversation.id
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nextAfterSeq, setNextAfterSeq] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [redactedExport, setRedactedExport] = useState(true)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  /** 首屏一页（或刷新）：重置列表，从头读。 */
  const loadFirstPage = useCallback(async () => {
    setLoading(true)
    setError(null)
    setNotice(null)
    try {
      const page = await api.fetchTrace(conversationId, 0, TRACE_PAGE_SIZE)
      setEvents(page.items)
      setNextAfterSeq(page.next_after_seq)
    } catch (cause) {
      setEvents([])
      setNextAfterSeq(null)
      setError(describe(cause))
    } finally {
      setLoading(false)
    }
  }, [conversationId])

  useEffect(() => {
    void loadFirstPage()
  }, [loadFirstPage])

  // Esc 关闭（与工单看板同一套收法）。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // 首屏数据落地后滚到最新（时间线最新在底部，与对话页「跟随下沿」一致）。
  useEffect(() => {
    if (loading) return
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [loading, events.length])

  const loadMore = async () => {
    if (nextAfterSeq == null || loadingMore) return
    setLoadingMore(true)
    setError(null)
    try {
      const page = await api.fetchTrace(conversationId, nextAfterSeq, TRACE_PAGE_SIZE)
      setEvents((prev) => [...prev, ...page.items])
      setNextAfterSeq(page.next_after_seq)
    } catch (cause) {
      setError(describe(cause))
    } finally {
      setLoadingMore(false)
    }
  }

  const jumpToLatest = () => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }

  /**
   * 导出：前端此刻可能只翻了一页，所以先按 `after_seq` 把剩余页拉完再拼文件。
   * 脱敏开关默认开（隐藏请求头 / 上下文正文），与方案 §8.2 一致。
   */
  const doExport = async () => {
    if (exporting) return
    setExporting(true)
    setError(null)
    try {
      const all = nextAfterSeq == null ? events : await fetchAllTrace(conversationId)
      setEvents(all)
      setNextAfterSeq(null)
      const { fileName, size } = exportTraceFile(conversation, all, redactedExport)
      setNotice(
        `已导出 ${all.length} 条到 ${fileName}（${formatBytes(size)}，${redactedExport ? '脱敏' : '原样'}）`,
      )
    } catch (cause) {
      setError(describe(cause))
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="trace-drawer-backdrop" onClick={onClose}>
      <aside
        className="trace-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="轨迹调试视图（只读）"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="trace-drawer-head">
          <div className="trace-drawer-title">
            <span className="trace-drawer-name">轨迹调试</span>
            <span className="dim small">
              {conversation.title || '当前对话'} · 只读 · {events.length}
              {nextAfterSeq != null ? '+' : ''} 条事件
            </span>
          </div>
          <div className="trace-drawer-actions">
            <label className="switch trace-export-redact" title="隐藏请求头与上下文正文（默认开启）">
              <input
                type="checkbox"
                checked={redactedExport}
                onChange={(event) => setRedactedExport(event.target.checked)}
              />
              <span>脱敏导出</span>
            </label>
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => void loadFirstPage()}
              disabled={loading || exporting}
              title="重新读取当前对话的轨迹（只有 GET）"
            >
              刷新
            </button>
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => void doExport()}
              disabled={loading || exporting || events.length === 0}
              title="把这条对话的轨迹导出成 JSON 文件"
            >
              {exporting ? '导出中…' : '导出'}
            </button>
            <button type="button" className="btn ghost sm" onClick={onClose} aria-label="关闭">
              ✕
            </button>
          </div>
        </div>

        <div className="trace-drawer-note">
          轨迹含系统提示词摘要与用户内容，请勿外传。调试模式关闭时不会产生新轨迹；
          历史轨迹只读可查。
        </div>

        {error && <div className="banner err trace-drawer-banner">{error}</div>}
        {notice && <div className="banner ok trace-drawer-banner">{notice}</div>}

        <div className="trace-drawer-body" ref={scrollRef}>
          {loading ? (
            <div className="row trace-drawer-loading">
              <span className="spin">◌</span> 正在读取轨迹…
            </div>
          ) : events.length === 0 ? (
            <div className="empty">
              <div className="empty-icon">🧭</div>
              <p>这条对话还没有轨迹事件</p>
              <p className="dim small" style={{ marginTop: 6 }}>
                调试模式默认关闭：开启后从下一轮开始采集。历史轮次不会补采，所以这里可能是空的。
              </p>
            </div>
          ) : (
            <>
              <TraceTimeline events={events} />
              {nextAfterSeq != null && (
                <button
                  type="button"
                  className="btn ghost sm trace-load-more"
                  onClick={() => void loadMore()}
                  disabled={loadingMore}
                >
                  {loadingMore ? '加载中…' : '加载后续事件'}
                </button>
              )}
            </>
          )}
        </div>

        <div className="trace-drawer-foot">
          <span className="dim small">
            {events.length > 0
              ? `最早 ${formatTraceTime(events[0].timestamp)} · 最新 ${formatTraceTime(events[events.length - 1].timestamp)}`
              : '只读：本面板不会发起任何写请求'}
          </span>
          {events.length > 0 && (
            <button type="button" className="btn ghost sm" onClick={jumpToLatest}>
              跳到最新
            </button>
          )}
        </div>
      </aside>
    </div>
  )
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return `后端拒绝了轨迹读取（403）：${error.message}`
    return `${error.status} · ${error.message}`
  }
  return error instanceof Error ? error.message : String(error)
}
