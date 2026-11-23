/**
 * 工单看板（全屏三栏）：左边工单列表、中间流转时间线、右边静态规则。
 *
 * 工单是智能体之间**唯一**的交接物，所以这张看板要在一屏里回答三件事：
 * 有哪些工单（左上→下）、这一张上谁把什么交给了谁（中）、派发时定下的规则是什么（右）。
 * 时间线上只有动作徽章是彩色；token、耗时、工作目录指纹都压成卡片底部的小字，
 * 唯一例外是「只读节点却动了工作目录」——它翻红，并列出被点名的文件。
 *
 * 视图模型在 `lib/ticketBoard.ts`（与移动端共用），这里只管布局与交互。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import type { CollabGraph, CollabStep, CollabTicket, AgentProfile } from '../lib/types'
import type { BoardFilter, BoardTicket } from '../lib/ticketBoard'
import { buildBoardModel, filterTickets, formatTokens } from '../lib/ticketBoard'
import {
  BoardAvatar,
  BoardBriefSections,
  BoardEntryCard,
  BoardLegend,
  BoardStatePill,
  BoardTicketCard,
} from './TicketBoardKit'

interface Props {
  /** 本次运行的工单与步骤（`CollabRunDetail`） */
  tickets: CollabTicket[]
  steps: CollabStep[]
  /** 本次运行的协作图，用来取节点的岗位与工作目录作用域 */
  graph?: CollabGraph | null
  agents: AgentProfile[]
  /** 把节点 key 翻成人看的智能体名 */
  nodeLabel: (key: string) => string
  /** 运行标题，放在顶栏说明这是哪次协作 */
  runTitle?: string
  /** 从对话里的工单卡点进来时，直接选中这张工单 */
  initialTicketId?: string | null
  onClose: () => void
}

const FILTERS: { key: BoardFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '进行中' },
  { key: 'done', label: '已完成' },
]

export function TicketBoard({ tickets, steps, graph, agents, nodeLabel, runTitle, initialTicketId, onClose }: Props) {
  const [keyword, setKeyword] = useState('')
  const [filter, setFilter] = useState<BoardFilter>('all')
  const [selectedId, setSelectedId] = useState<string | null>(initialTicketId ?? null)

  // 从对话里的另一张工单卡再次点进来时，看板仍挂载着：跟着切到那张工单。
  useEffect(() => {
    if (initialTicketId) setSelectedId(initialTicketId)
  }, [initialTicketId])

  const model = useMemo(
    () => buildBoardModel({ tickets, steps, graph, agents, nodeLabel }),
    [tickets, steps, graph, agents, nodeLabel],
  )
  const visible = useMemo(
    () => filterTickets(model.tickets, filter, keyword),
    [model.tickets, filter, keyword],
  )
  const active =
    model.tickets.find((ticket) => ticket.id === (selectedId ?? model.focusedId)) ?? model.tickets[0] ?? null

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="collab-drawer-backdrop collab-ticket-board-backdrop">
      <aside className="collab-drawer collab-ticket-board" role="dialog" aria-modal="true" aria-label="工单看板">
        <div className="collab-drawer-head">
          <div>
            <div className="collab-inspector-title">工单看板</div>
            <div className="dim small">
              {runTitle ? `${runTitle} · ` : ''}
              共 {model.totals.tickets} 张 · {model.totals.steps} 条流转 · 累计{' '}
              {model.totals.tokens.toLocaleString('en-US')} tok
              {model.totals.violations > 0 && (
                <span className="board-head-warn"> · ⚠ {model.totals.violations} 条越权审计</span>
              )}
            </div>
          </div>
          <button className="btn ghost sm" style={{ marginLeft: 'auto' }} onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>

        {model.tickets.length === 0 ? (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="empty-icon">📋</div>
            <p>这次协作还没有工单</p>
            <p className="dim small" style={{ marginTop: 6 }}>入口智能体把活派出去之后，工单会出现在这里。</p>
          </div>
        ) : (
          <div className="board-shell">
            {/* 左 · 工单列表 */}
            <section className="board-col board-col-list">
              <div className="board-col-head">
                <input
                  className="board-search"
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                  placeholder="搜索标题 / 处理人"
                  aria-label="搜索工单"
                />
                <div className="board-filters">
                  {FILTERS.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      className={`board-filter${filter === item.key ? ' active' : ''}`}
                      onClick={() => setFilter(item.key)}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="board-col-body board-ticket-list">
                {visible.map((ticket) => (
                  <BoardTicketCard
                    key={ticket.id}
                    ticket={ticket}
                    active={ticket.id === active?.id}
                    onSelect={() => setSelectedId(ticket.id)}
                  />
                ))}
                {visible.length === 0 && <p className="dim small">没有匹配的工单。</p>}
              </div>
            </section>

            {/* 中 · 这张工单的流转时间线 */}
            <section className="board-col board-col-flow">
              {active ? (
                <>
                  <div className="board-flow-head">
                    <div className="board-flow-title-row">
                      <h2 className="board-flow-title">{active.title}</h2>
                      <BoardStatePill ticket={active} />
                      <span className="board-flow-holder">
                        <BoardAvatar
                          avatar={active.holderFace.avatar}
                          avatarPreset={active.holderFace.avatarPreset}
                          avatarFile={active.holderFace.avatarFile}
                          agentId={active.holderFace.agentId}
                          label={active.holderLabel}
                          duty={active.holderDuty}
                          size="sm"
                        />
                        当前处理人：{active.holderLabel}
                      </span>
                    </div>
                    <div className="board-flow-meta">
                      <span>发起人 {active.ownerLabel}</span>
                      <span>返工 {active.round} / {active.maxRounds} 轮</span>
                      <span>{active.stats.steps} 条流转 · {active.stats.handoffs} 次交接</span>
                      {formatTokens(active.stats.tokens) && <span>累计 {formatTokens(active.stats.tokens)}</span>}
                      {active.stats.violations > 0 && (
                        <span className="board-flow-violation">⚠ {active.stats.violations} 条越权审计</span>
                      )}
                    </div>
                  </div>

                  <div className="board-col-body board-flow-body">
                    {active.reviewComment && (
                      <div className={`banner ${active.verdict === 'pass' ? 'info' : 'warn'}`}>
                        审查意见：{active.reviewComment}
                      </div>
                    )}
                    {active.escalation?.reason && (
                      <div className={`banner ${active.state === 'escalated' ? 'warn' : 'info'}`}>
                        {active.state === 'escalated' ? '上报中' : '曾上报'}：{active.escalation.from} 交回：
                        {active.escalation.reason}
                        {active.escalation.need ? ` · 需要：${active.escalation.need}` : ''}
                        {active.escalation.hops.length > 0 ? `（已经过 ${active.escalation.hops.join(' → ')}）` : ''}
                      </div>
                    )}
                    {(active.summary || active.artifacts.length > 0) && (
                      <div className="board-output">
                        <div className="board-output-label">产出</div>
                        {active.summary && <p className="board-output-summary">{active.summary}</p>}
                        {active.artifacts.length > 0 && (
                          <div className="board-output-files">
                            {active.artifacts.map((artifact) => (
                              <span key={artifact.key} className="mono">{artifact.label}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                    {active.entries.length === 0 ? (
                      <p className="dim small">还没有智能体处理过它。</p>
                    ) : (
                      <div className="board-timeline">
                        <span className="board-timeline-rail" aria-hidden />
                        <ol className="board-timeline-list">
                          {active.entries.map((entry) => (
                            <BoardEntryCard key={entry.id} entry={entry} />
                          ))}
                        </ol>
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div className="board-col-body">
                  <p className="dim small">左侧选一张工单看它的流转。</p>
                </div>
              )}
            </section>

            {/* 右 · 静态规则 */}
            <section className="board-col board-col-rule">
              <div className="board-col-head">
                <div className="board-col-title">工单规则</div>
                <div className="dim small">派发时写死，运行中不改</div>
              </div>
              <div className="board-col-body board-rule-body">
                {active && (
                  <>
                    <dl className="board-summary">
                      <SummaryRow label="工单编号"><span className="mono">{active.id}</span></SummaryRow>
                      <SummaryRow label="当前处理人">
                        {active.holderLabel}
                        {active.holderDuty ? `（${active.holderDuty}）` : ''}
                      </SummaryRow>
                      <SummaryRow label="发起人">{active.ownerLabel}</SummaryRow>
                      <SummaryRow label="返工轮次">{active.round} / {active.maxRounds} 轮</SummaryRow>
                      <SummaryRow label="父工单">{active.parentTicketId ?? '—（根工单）'}</SummaryRow>
                    </dl>
                    <BoardBriefSections ticket={active} />
                  </>
                )}
                <div className="board-legend-box">
                  <div className="board-legend-title">动作徽章</div>
                  <BoardLegend />
                  <p className="dim small">
                    红色高亮 = 只读节点改动了工作目录（越权审计）。token、耗时、文件指纹属于次要信息，一律压在卡片底部。
                  </p>
                </div>
              </div>
            </section>
          </div>
        )}
      </aside>
    </div>
  )
}

function SummaryRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="board-summary-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

export type { BoardTicket }
