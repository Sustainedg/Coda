/**
 * 移动端「工单看板」：和桌面端同一份数据（`lib/ticketBoard.ts`），换成手机上做得顺的交互。
 *
 * 桌面是三栏并排（列表 / 时间线 / 规则），手机上并排不起来，于是拆成两步：
 * 顶上一个下拉选工单，下面「流转 / 规则」两个分段 —— 一屏只回答一个问题。
 * 卡片刻度（动作徽章、越权红框、底部小字元数据）与桌面端完全一致，用的是同一批组件。
 *
 * 本页属于移动端（`/m/`）：不引用桌面端的页面与 `index.css`，只复用 `lib/*` 与纯展示件。
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../../lib/api'
import { buildBoardModel, formatTokens } from '../../lib/ticketBoard'
import type { CollabRunDetail, CollabTeam, AgentProfile } from '../../lib/types'
import { useAutoRefresh } from '../../lib/useAutoRefresh'
import {
  BoardAvatar,
  BoardBriefSections,
  BoardEntryCard,
  BoardLegend,
  BoardStatePill,
} from '../../components/TicketBoardKit'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

type Tab = 'flow' | 'rule'

export function MobileTicketBoardPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  // 从岗位实时对话里的工单卡点进来：?ticket=<id> 直接选中这张工单。
  const [searchParams] = useSearchParams()
  const requestedTicketId = searchParams.get('ticket')

  const [detail, setDetail] = useState<CollabRunDetail | null>(null)
  const [teams, setTeams] = useState<CollabTeam[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [tab, setTab] = useState<Tab>('flow')
  const [selectedId, setSelectedId] = useState<string | null>(requestedTicketId)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (requestedTicketId) setSelectedId(requestedTicketId)
  }, [requestedTicketId])

  const load = useCallback(async (id: string) => {
    try {
      setDetail(await api.getCollabRun(id))
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }, [])

  useEffect(() => {
    if (!runId) return
    void load(runId)
    // 智能体名与头像只是「好看一点」，拿不到也不能挡住看板，所以失败静默。
    void Promise.all([api.listCollabTeams(), api.listAgents()])
      .then(([teamList, agentList]) => {
        setTeams(teamList)
        setAgents(agentList)
      })
      .catch(() => undefined)
  }, [runId, load])

  // 手机常常停在一边看，协作还在跑时定时对齐一次；跑完就不再轮询。
  useAutoRefresh(() => (runId ? load(runId) : undefined), {
    enabled: Boolean(runId) && detail?.status === 'running',
  })

  const team = useMemo(
    () => teams.find((item) => item.id === detail?.team_id) ?? null,
    [teams, detail],
  )
  const nodeLabel = useCallback(
    (key: string) => team?.graph.nodes.find((node) => node.key === key)?.label || key,
    [team],
  )
  const model = useMemo(
    () =>
      detail
        ? buildBoardModel({
            tickets: detail.tickets,
            steps: detail.steps,
            graph: team?.graph ?? null,
            agents,
            nodeLabel,
          })
        : null,
    [detail, team, agents, nodeLabel],
  )
  const active = model
    ? model.tickets.find((ticket) => ticket.id === (selectedId ?? model.focusedId)) ?? model.tickets[0] ?? null
    : null

  return (
    <div className="m-page">
      <MobileTopBar
        onBack={() => navigate(runId ? `/collab/runs/${runId}` : '/collab')}
        title="工单看板"
        subtitle={
          model
            ? `${detail?.title ?? '本次协作'} · ${model.totals.tickets} 张工单`
            : '正在读取工单……'
        }
      />

      {error && <div className="banner err" style={{ margin: '10px 12px 0' }}>{error}</div>}

      {model && active ? (
        <>
          <div className="m-board-picker">
            <select
              className="m-board-select"
              value={active.id}
              aria-label="选择工单"
              onChange={(event) => setSelectedId(event.target.value)}
            >
              {model.tickets.map((ticket) => (
                <option key={ticket.id} value={ticket.id}>
                  {`${'　'.repeat(Math.min(ticket.depth, 3))}${ticket.title} · ${ticket.stateLabel}`}
                </option>
              ))}
            </select>
            <div className="m-board-picker-meta">
              <BoardStatePill ticket={active} />
              <BoardAvatar
                avatar={active.holderFace.avatar}
                avatarPreset={active.holderFace.avatarPreset}
                avatarFile={active.holderFace.avatarFile}
                agentId={active.holderFace.agentId}
                label={active.holderLabel}
                duty={active.holderDuty}
                size="sm"
              />
              <span>当前处理人：{active.holderLabel}</span>
            </div>
          </div>

          <div className="m-board-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={tab === 'flow'}
              className={`m-board-tab${tab === 'flow' ? ' active' : ''}`}
              onClick={() => setTab('flow')}
            >
              流转 {active.stats.steps}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === 'rule'}
              className={`m-board-tab${tab === 'rule' ? ' active' : ''}`}
              onClick={() => setTab('rule')}
            >
              工单规则
            </button>
          </div>

          <div className="m-page-body">
            {tab === 'flow' ? (
              <div className="m-board-flow">
                <div className="m-board-chips">
                  <span>发起人 {active.ownerLabel}</span>
                  <span>返工 {active.round} / {active.maxRounds} 轮</span>
                  <span>{active.stats.handoffs} 次交接</span>
                  {formatTokens(active.stats.tokens) && <span>累计 {formatTokens(active.stats.tokens)}</span>}
                  {active.stats.violations > 0 && (
                    <span className="danger">⚠ {active.stats.violations} 条越权审计</span>
                  )}
                </div>

                {active.reviewComment && (
                  <div className={`m-board-note${active.verdict === 'pass' ? ' ok' : ' warn'}`}>
                    <span className="m-board-note-label">审查意见</span>
                    <span>{active.reviewComment}</span>
                  </div>
                )}

                {active.escalation?.reason && (
                  <div className="m-board-note warn">
                    <span className="m-board-note-label">
                      {active.state === 'escalated' ? '上报中' : '曾上报'} · {active.escalation.from} 交回
                    </span>
                    <span>{active.escalation.reason}</span>
                    {active.escalation.need && <span>需要：{active.escalation.need}</span>}
                    {active.escalation.hops.length > 0 && (
                      <span className="dim small">已经过 {active.escalation.hops.join(' → ')}</span>
                    )}
                  </div>
                )}

                {(active.summary || active.artifacts.length > 0) && (
                  <div className="m-board-note">
                    <span className="m-board-note-label">产出</span>
                    {active.summary && <span>{active.summary}</span>}
                    {active.artifacts.length > 0 && (
                      <div className="m-board-files">
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
                  <ol className="m-board-list">
                    {active.entries.map((entry) => (
                      <BoardEntryCard key={entry.id} entry={entry} />
                    ))}
                  </ol>
                )}
              </div>
            ) : (
              <div className="m-board-rule">
                <dl className="m-board-rows">
                  <Row label="工单编号"><span className="mono">{active.id}</span></Row>
                  <Row label="当前处理人">
                    {active.holderLabel}
                    {active.holderDuty ? `（${active.holderDuty}）` : ''}
                  </Row>
                  <Row label="发起人">{active.ownerLabel}</Row>
                  <Row label="返工轮次">{active.round} / {active.maxRounds} 轮</Row>
                  <Row label="父工单">{active.parentTicketId ?? '—（根工单）'}</Row>
                </dl>
                <BoardBriefSections ticket={active} />
                <div className="m-board-note">
                  <span className="m-board-note-label">动作徽章</span>
                  <BoardLegend />
                  <span className="dim small">
                    红色高亮 = 只读节点改动了工作目录（越权审计）。token、耗时、文件指纹属于次要信息，
                    一律压在卡片底部。
                  </span>
                </div>
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="m-page-body">
          {detail && model && model.tickets.length === 0 ? (
            <p className="dim small">这次协作还没有工单。入口智能体把活派出去之后就会出现。</p>
          ) : (
            <p className="dim small">{error ? '' : '正在读取这次协作的工单……'}</p>
          )}
        </div>
      )}
    </div>
  )
}

/** 右栏（这里是「工单规则」分段）里的一行「字段 → 值」。 */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="m-board-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}
