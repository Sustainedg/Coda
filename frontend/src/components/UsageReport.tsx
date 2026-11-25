import { useMemo, type CSSProperties } from 'react'
import { AgentAvatar } from './AgentAvatar'
import { dayInputValue, type UsageRangeKind } from '../lib/usage'
import type { AgentProfile, UsageOverview, UsageTokens } from '../lib/types'

const RANGE_HINT: Record<UsageRangeKind, string> = {
  today: '今天 00:00 起',
  week: '近 7 天',
  month: '近 30 天',
  custom: '自定义区间',
  all: '全部记录',
}

/** 一份 token 构成的展示数据：用于环形图的扇区与右侧图例。 */
interface CompositionRow {
  key: string
  label: string
  value: number
  color: string
}

/**
 * 用量仪表盘（桌面「用量」页与智能体设定页「统计」标签共用的唯一一份）。
 *
 * 不传 `agent` = 平台口径：额外展示「按智能体」排行、每日峰值 / 日均，技能行可点。
 * 传 `agent` = 单智能体口径：标题与空状态带上名字，不展示平台级排行。
 * 时间范围与刷新按钮留在各自页面（用量页在页头、设定页在标签行），这里只管数据展示。
 */
export function UsageReport({
  data,
  range,
  agent,
  onSkillClick,
}: {
  data: UsageOverview
  range: UsageRangeKind
  /** 有值 = 智能体口径；不传 = 平台口径（「用量」页）。 */
  agent?: AgentProfile | null
  /** 技能行点击（平台用量页跳到技能页）；不传则纯展示。 */
  onSkillClick?: () => void
}) {
  const platform = !agent
  const agentName = agent?.name ?? ''
  const tokens = data.tokens
  const skills = data.skills
  const hasData = tokens.messages > 0 || skills.calls > 0
  const todayKey = dayInputValue(new Date())

  const composition = useMemo<CompositionRow[]>(
    () => [
      { key: 'input', label: '输入', value: tokens.input, color: 'var(--brand)' },
      { key: 'output', label: '输出', value: tokens.output, color: 'var(--accent)' },
      { key: 'cache_read', label: '命中缓存', value: tokens.cache_read, color: 'var(--warn)' },
      { key: 'cache_creation', label: '写入缓存', value: tokens.cache_creation, color: 'var(--text-dim)' },
    ],
    [tokens],
  )

  const ringBackground = useMemo(() => {
    const total = composition.reduce((sum, row) => sum + row.value, 0)
    if (total <= 0) return 'var(--bg-hover)'
    let cursor = 0
    const stops = composition
      .filter((row) => row.value > 0)
      .map((row) => {
        const from = (cursor / total) * 100
        cursor += row.value
        const to = (cursor / total) * 100
        return `${row.color} ${from}% ${to}%`
      })
    return stops.length > 0 ? `conic-gradient(${stops.join(', ')})` : 'var(--bg-hover)'
  }, [composition])

  const dailyMax = Math.max(1, ...data.daily.map((day) => day.total), 1)
  const agentMax = Math.max(1, ...data.by_agent.map((row) => row.tokens.total), 1)
  const skillMax = Math.max(1, ...skills.top.map((skill) => skill.calls), 1)
  const dailyTotal = data.daily.reduce((sum, day) => sum + day.total, 0)
  const dailyPeak = data.daily.length
    ? data.daily.reduce((best, day) => (day.total > best.total ? day : best))
    : null
  const dailyAvg = data.daily.length ? dailyTotal / data.daily.length : 0

  if (!hasData) {
    return (
      <div className={platform ? 'empty usage-empty' : 'empty'}>
        <div className="empty-icon">📊</div>
        <p>
          {platform
            ? '所选时间范围内没有用量记录。'
            : `所选时间范围内，「${agentName}」没有用量记录。`}
        </p>
        <p className="dim small">
          {platform ? '换个范围，或者聊几句再回来看看。' : '换个范围，或者跟它聊几句再回来看看。'}
        </p>
      </div>
    )
  }

  return (
    <>
      <section className="usage-hero">
        <div className="usage-hero-main">
          <span className="usage-hero-kicker">
            {platform ? '总 token 消耗' : `「${agentName}」的 token 消耗`}
          </span>
          <div className="usage-hero-value">
            {fmt(tokens.total)}
            <span className="usage-hero-unit">tok</span>
          </div>
          <div className="usage-hero-chips">
            <span className="usage-chip">统计消息 {fmt(tokens.messages)} 条</span>
            <span className="usage-chip">
              {platform ? `${fmt(data.by_agent.length)} 个智能体` : `${fmt(data.conversations)} 个对话`}
            </span>
            <span className="usage-chip">{fmt(skills.calls)} 次技能调用</span>
          </div>
          <p className="usage-hero-note">
            {`${RANGE_HINT[range]} · 数据来自助手回复上报的用量${platform ? '' : ' · 平台总览在「用量」页'}`}
          </p>
        </div>

        <div className="usage-hero-side">
          <div
            className="usage-ring"
            style={{ background: ringBackground }}
            role="img"
            aria-label="token 构成环形图"
          >
            <div className="usage-ring-inner">
              <strong>{cacheRate(tokens)}</strong>
              <span>缓存命中率</span>
            </div>
          </div>
          <ul className="usage-legend">
            {composition.map((row) => (
              <li key={row.key}>
                <span className="usage-dot" style={{ background: row.color }} />
                <span className="usage-legend-label">{row.label}</span>
                <span className="usage-legend-value">{compact(row.value)}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {data.daily.length > 0 && (
        <section className="usage-panel">
          <div className="usage-panel-head">
            <div>
              <h2 className="usage-panel-title">每日消耗</h2>
              <p className="usage-panel-sub">按本地自然日汇总，柱高代表当天总 token</p>
            </div>
            {platform && (
              <ul className="usage-legend inline">
                <li>
                  <span className="usage-dot" style={{ background: 'var(--brand)' }} />
                  <span className="usage-legend-label">输入</span>
                </li>
                <li>
                  <span className="usage-dot" style={{ background: 'var(--accent)' }} />
                  <span className="usage-legend-label">输出</span>
                </li>
                <li>
                  <span
                    className="usage-dot"
                    style={{ background: 'linear-gradient(90deg, var(--brand), var(--accent))', opacity: 0.45 }}
                  />
                  <span className="usage-legend-label">缓存</span>
                </li>
              </ul>
            )}
          </div>

          <div className="usage-chart-wrap">
            <div className="usage-chart" role="img" aria-label="每日 token 消耗柱状图">
              <div className={`usage-chart-cols${data.daily.length <= 7 ? ' sparse' : ''}`}>
                {data.daily.map((day) => {
                  const cache = Math.max(0, day.total - day.input - day.output)
                  return (
                    <div
                      className={`usage-chart-col${day.date === todayKey ? ' today' : ''}`}
                      key={day.date}
                      title={`${day.date} · 输入 ${fmt(day.input)} · 输出 ${fmt(day.output)} · 缓存 ${fmt(cache)} · 共 ${fmt(day.total)}`}
                    >
                      <div className="usage-chart-track">
                        <span
                          className="usage-chart-seg seg-input"
                          style={segShare(barHeight(day.input, dailyMax))}
                        />
                        <span
                          className="usage-chart-seg seg-output"
                          style={segShare(barHeight(day.output, dailyMax))}
                        />
                        <span
                          className="usage-chart-seg seg-cache"
                          style={segShare(barHeight(cache, dailyMax))}
                        />
                      </div>
                      <span className="usage-chart-date">{day.date.slice(5)}</span>
                    </div>
                  )
                })}
              </div>
            </div>

            {platform && dailyPeak && (
              <aside className="usage-chart-aside">
                <div className="usage-chart-stat">
                  <span>峰值</span>
                  <strong>{compact(dailyPeak.total)}</strong>
                  <em>{dailyPeak.date.slice(5)}</em>
                </div>
                <div className="usage-chart-stat">
                  <span>日均</span>
                  <strong>{compact(dailyAvg)}</strong>
                  <em>{data.daily.length} 个活跃日</em>
                </div>
                <div className="usage-chart-stat">
                  <span>区间合计</span>
                  <strong>{compact(dailyTotal)}</strong>
                  <em>tok</em>
                </div>
              </aside>
            )}
          </div>
        </section>
      )}

      <div className={platform ? 'usage-columns' : undefined}>
        {platform && data.by_agent.length > 0 && (
          <section className="usage-panel">
            <div className="usage-panel-head">
              <div>
                <h2 className="usage-panel-title">按智能体</h2>
                <p className="usage-panel-sub">哪个智能体在替你消耗 token</p>
              </div>
            </div>

            <div className="usage-agent-list">
              {data.by_agent.map((row, index) => (
                <div className="usage-agent" key={row.id}>
                  <span className="usage-rank">{rank(index)}</span>
                  <AgentAvatar
                    avatar={row.avatar}
                    avatarPreset={row.avatar_preset}
                    size={26}
                    className="usage-agent-avatar"
                  />
                  <div className="usage-agent-main">
                    <div className="usage-agent-line">
                      <span className="usage-agent-name">{row.name}</span>
                      <span className="usage-agent-total">
                        {fmt(row.tokens.total)}
                        <em>tok</em>
                      </span>
                    </div>
                    <div className="usage-agent-meta">
                      {row.type_name ?? row.type_id ?? '未知内核'} · 输入 {compact(row.tokens.input)} ·
                      输出 {compact(row.tokens.output)}
                    </div>
                    <div className="usage-agent-track">
                      <span style={{ width: `${Math.max(2, (row.tokens.total / agentMax) * 100)}%` }} />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        <section className="usage-panel">
          <div className="usage-panel-head">
            <div>
              <h2 className="usage-panel-title">技能用量</h2>
              <p className="usage-panel-sub">
                {platform ? '按调用次数排行' : `「${agentName}」调用各技能的次数`}
              </p>
            </div>
          </div>

          <div className="usage-skill-summary">
            <div>
              <strong>{fmt(skills.calls)}</strong>
              <span>调用总次数</span>
            </div>
            <div>
              <strong>{fmt(skills.skills_used)}</strong>
              <span>涉及技能</span>
            </div>
          </div>

          {skills.top.length > 0 ? (
            <div className="usage-skill-list">
              {skills.top.map((skill, index) => {
                const body = (
                  <>
                    <span className="usage-rank">{rank(index)}</span>
                    <span className="usage-skill-main">
                      <span className="usage-skill-line">
                        <span className="usage-skill-name">{skill.name}</span>
                        <span className="usage-skill-calls">
                          {fmt(skill.calls)}
                          <em>次</em>
                        </span>
                      </span>
                      <span className="usage-skill-meta">
                        {skill.type_name ?? skill.type_id}
                        {skill.source_label ? ` · ${skill.source_label}` : ''}
                      </span>
                      <span className="usage-skill-track">
                        <span style={{ width: `${Math.max(2, (skill.calls / skillMax) * 100)}%` }} />
                      </span>
                    </span>
                  </>
                )
                return onSkillClick ? (
                  <button
                    className="usage-skill"
                    key={skill.id}
                    onClick={onSkillClick}
                    title="到技能页查看"
                  >
                    {body}
                  </button>
                ) : (
                  <div className="usage-skill" key={skill.id}>
                    {body}
                  </div>
                )
              })}
            </div>
          ) : (
            <p className="usage-empty-line">所选时间范围内没有技能调用。</p>
          )}

          {platform && (
            <p className="usage-note">
              技能调用按消息里的工具调用识别（如 Claude Code 的 Skill 工具），只统计所选时间范围内发生的调用；
              未声明调用形式的内核不会被计入。
            </p>
          )}
        </section>
      </div>
    </>
  )
}

function fmt(value: number): string {
  return value.toLocaleString('en-US')
}

/** 图例 / 图表轴上的紧凑数字：1234567 → 1.2M。 */
function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

function cacheRate(tokens: UsageTokens): string {
  return tokens.cache_ratio === null ? '—' : `${Math.round(tokens.cache_ratio * 100)}%`
}

function barHeight(value: number, max: number): string {
  if (value <= 0) return '0'
  return `${Math.max(1.2, (value / max) * 100)}%`
}

/**
 * 一段柱子的占比交给 CSS 变量：桌面端当高度用（竖柱），移动端当宽度用（横条）。
 * 两端样式表各自消费 ``--usage-share``，组件只负责给出同一个数。
 */
function segShare(share: string): CSSProperties {
  return { '--usage-share': share } as CSSProperties
}

function rank(index: number): string {
  return String(index + 1).padStart(2, '0')
}
