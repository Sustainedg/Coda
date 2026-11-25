import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshIcon } from '../components/Icons'
import { UsageReport } from '../components/UsageReport'
import { api, ApiError } from '../lib/api'
import { dayInputValue, usageRangeQuery, type UsageRangeKind } from '../lib/usage'
import type { UsageOverview } from '../lib/types'

const RANGES: { kind: UsageRangeKind; label: string }[] = [
  { kind: 'today', label: '今日' },
  { kind: 'week', label: '一周' },
  { kind: 'month', label: '一月' },
  { kind: 'custom', label: '自定义' },
  { kind: 'all', label: '全部' },
]

/**
 * 用量页：把平台记录下来的 token 消耗与技能调用汇总到一处。
 *
 * token 数据来自每条助手回复里保存的用量计数（各适配器上报的字段名在后端已归一化）；
 * 技能用量是**调用次数**——从消息里的工具调用识别（Claude Code 的 ``Skill`` 工具），
 * 按所选时间范围统计。仪表盘本体是 `components/UsageReport.tsx`（与智能体设定页
 * 「统计」标签共用唯一一份），这里只管时间范围、加载与刷新。
 */
export function UsagePage() {
  const navigate = useNavigate()
  const [data, setData] = useState<UsageOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [range, setRange] = useState<UsageRangeKind>('week')
  const [customStart, setCustomStart] = useState(() => dayInputValue(new Date(Date.now() - 6 * 86_400_000)))
  const [customEnd, setCustomEnd] = useState(() => dayInputValue(new Date()))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.usageOverview(usageRangeQuery(range, customStart, customEnd)))
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [range, customStart, customEnd])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <>
      <header className="page-header usage-page-head">
        <div>
          <h1 className="page-title">用量</h1>
          <p className="page-desc">
            token 消耗与技能调用的总览，按所选时间范围汇总助手回复里记录的用量。
          </p>
        </div>
        <div className="spacer" />
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
        <button
          className={`icon-btn usage-refresh${loading ? ' spinning' : ''}`}
          onClick={() => void load()}
          disabled={loading}
          aria-label="刷新用量"
          title="刷新用量"
        >
          <RefreshIcon size={17} />
        </button>
      </header>

      <div className="page-body stack usage-page">
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

        {error && <div className="banner err">{error}</div>}
        {loading && !data && <UsageSkeleton />}

        {data && (
          <UsageReport data={data} range={range} onSkillClick={() => navigate('/skills')} />
        )}
      </div>
    </>
  )
}

/** 首屏骨架：只在还没有数据时出现，刷新时保留旧数据，避免页面闪一下。 */
function UsageSkeleton() {
  return (
    <div className="usage-skeleton" aria-hidden="true">
      <div className="usage-skeleton-hero" />
      <div className="usage-skeleton-panel" />
      <div className="usage-skeleton-grid">
        <div className="usage-skeleton-panel" />
        <div className="usage-skeleton-panel" />
      </div>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
