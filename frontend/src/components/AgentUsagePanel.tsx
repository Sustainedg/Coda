import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { dayInputValue, usageRangeQuery, type UsageRangeKind } from '../lib/usage'
import type { AgentProfile, UsageOverview } from '../lib/types'
import { UsageReport } from './UsageReport'

const RANGES: { kind: UsageRangeKind; label: string }[] = [
  { kind: 'today', label: '今日' },
  { kind: 'week', label: '一周' },
  { kind: 'month', label: '一月' },
  { kind: 'custom', label: '自定义' },
  { kind: 'all', label: '全部' },
]

/**
 * 智能体设定页的「统计」标签：**这一个智能体**的 token 用量与技能调用。
 *
 * 数据来自用量总览接口按 `agent_id` 过滤的口径（与「用量」页同一套归一化规则）；
 * 仪表盘本体是 `components/UsageReport.tsx`（与用量页共用唯一一份），这里只负责
 * 时间范围、加载与错误状态。
 */
export function AgentUsagePanel({ agent }: { agent: AgentProfile }) {
  const [data, setData] = useState<UsageOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [range, setRange] = useState<UsageRangeKind>('week')
  const [customStart, setCustomStart] = useState(() =>
    dayInputValue(new Date(Date.now() - 6 * 86_400_000)),
  )
  const [customEnd, setCustomEnd] = useState(() => dayInputValue(new Date()))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = usageRangeQuery(range, customStart, customEnd)
      setData(await api.usageOverview({ ...params, agent_id: agent.id }))
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [range, customStart, customEnd, agent.id])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <section className="stack">
      <div className="row" style={{ gap: 10 }}>
        <h2 className="card-section-title" style={{ margin: 0 }}>
          统计
        </h2>
        <div className="spacer" style={{ flex: 1 }} />
        <div className="tabs">
          {RANGES.map((item) => (
            <button
              key={item.kind}
              className={`tab${range === item.kind ? ' active' : ''}`}
              onClick={() => setRange(item.kind)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <button className="btn sm" onClick={() => void load()} disabled={loading}>
          {loading ? <span className="spin">◌</span> : '↻'} 刷新
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}

      {range === 'custom' && (
        <div className="usage-range">
          <span className="usage-range-label">自定义区间</span>
          <input
            className="input"
            type="date"
            style={{ maxWidth: 150 }}
            value={customStart}
            max={customEnd}
            onChange={(e) => setCustomStart(e.target.value)}
          />
          <span className="dim">→</span>
          <input
            className="input"
            type="date"
            style={{ maxWidth: 150 }}
            value={customEnd}
            min={customStart}
            onChange={(e) => setCustomEnd(e.target.value)}
          />
          <span className="usage-range-hint">含起止当天</span>
        </div>
      )}

      {loading && !data && <div className="banner info">正在加载「{agent.name}」的用量…</div>}

      {data && <UsageReport data={data} range={range} agent={agent} />}
    </section>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
