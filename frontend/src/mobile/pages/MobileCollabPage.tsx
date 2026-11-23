import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import type { AgentProfile, CollabRun, CollabTeam } from '../../lib/types'
import { useAutoRefresh } from '../../lib/useAutoRefresh'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { TeamAvatarStack } from '../../components/TeamAvatarStack'
import { describeError, relativeTime } from '../util'

const RUN_STATUS_LABEL: Record<string, { label: string; pill: string }> = {
  running: { label: '进行中', pill: 'ok' },
  waiting_user: { label: '等待你决定', pill: 'warn' },
  done: { label: '已完成', pill: 'brand' },
  error: { label: '出错', pill: 'err' },
  cancelled: { label: '已取消', pill: '' },
  interrupted: { label: '已中断', pill: 'warn' },
}

/**
 * 协作（移动端）：协作组的入口页 —— 新建协作组、进编排页、发起运行、查看运行。
 *
 * 编排本身在「协作组」详情页（`MobileCollabTeamPage`）里用**表单**完成：
 * 手机上不画线（画布见 `components/CollabCanvas`，只在桌面端使用）。
 */
export function MobileCollabPage() {
  const navigate = useNavigate()
  const [teams, setTeams] = useState<CollabTeam[]>([])
  /** 协作组卡片上的成员头像：协作组只存智能体 id，头像要从智能体列表里对（与协作组一起拉） */
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [runs, setRuns] = useState<CollabRun[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [startOpen, setStartOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [teamId, setTeamId] = useState('')
  const [title, setTitle] = useState('')
  const [workspace, setWorkspace] = useState('')
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    try {
      const [teamList, runList, agentList] = await Promise.all([
        api.listCollabTeams(),
        api.listCollabRuns(),
        api.listAgents(),
      ])
      setTeams(teamList)
      setRuns(runList)
      setAgents(agentList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useAutoRefresh(reload, { intervalMs: 8000 })

  const createTeam = async () => {
    setBusy(true)
    setError(null)
    try {
      const team = await api.createCollabTeam({ name: newName.trim() || '新建协作组' })
      setCreateOpen(false)
      setNewName('')
      navigate(`/collab/teams/${team.id}`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  const start = async () => {
    if (!teamId) return setError('请选择协作组')
    setBusy(true)
    setError(null)
    try {
      const run = await api.createCollabRun({
        team_id: teamId,
        title: title.trim() || undefined,
        workspace_dir: workspace.trim() || null,
      })
      setStartOpen(false)
      setTitle('')
      setWorkspace('')
      navigate(`/collab/runs/${run.id}`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="m-page">
      <MobileTopBar
        title="协作"
        subtitle="多智能体按工单串行推进"
        actions={
          <>
            <button
              className="m-icon-btn"
              onClick={() => setStartOpen(true)}
              disabled={teams.length === 0}
              aria-label="发起协作"
            >
              ▶
            </button>
            <button className="m-icon-btn" onClick={() => setCreateOpen(true)} aria-label="新建协作组">
              ＋
            </button>
          </>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          {loading ? (
            <p className="dim small">正在加载…</p>
          ) : (
            <>
              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">协作组（{teams.length}）</div>
                {teams.length === 0 ? (
                  <div className="banner info">
                    还没有协作组。新建一个，然后添加岗位、用「从谁到谁」把工单流转连起来。
                  </div>
                ) : (
                  <div className="m-list">
                    {teams.map((team) => (
                      <button
                        key={team.id}
                        className="m-item"
                        onClick={() => navigate(`/collab/teams/${team.id}`)}
                      >
                        <TeamAvatarStack team={team} agents={agents} size={34} max={3} />
                        <span className="m-item-main">
                          <span className="m-item-title">{team.name}</span>
                          <span className="m-item-sub">
                            {team.node_count} 个岗位
                            {team.graph.edges.length > 0 ? ` · ${team.graph.edges.length} 条连线` : ''}
                            {team.entry_key ? ` · 入口 ${team.entry_key}` : ''}
                          </span>
                        </span>
                        <span className={`pill ${team.validation.ok ? 'ok' : 'err'}`}>
                          {team.validation.ok ? '可运行' : `${team.validation.errors.length} 处待修`}
                        </span>
                        <span className="m-item-chevron">›</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">最近运行（{runs.length}）</div>
                {runs.length === 0 ? (
                  <p className="dim small">还没有协作运行。</p>
                ) : (
                  <div className="m-list">
                    {runs.map((run) => {
                      const status = RUN_STATUS_LABEL[run.status] ?? { label: run.status, pill: '' }
                      return (
                        <button key={run.id} className="m-item" onClick={() => navigate(`/collab/runs/${run.id}`)}>
                          <span className="m-item-main">
                            <span className="m-item-title">{run.title}</span>
                            <span className="m-item-sub">
                              {run.team_name ?? '协作组'} · {run.steps_used} 步 · {run.ticket_count} 张工单 ·{' '}
                              {relativeTime(run.updated_at)}
                            </span>
                          </span>
                          <span className={`pill ${status.pill}`}>{status.label}</span>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {createOpen && (
        <MobileSheet title="新建协作组" onClose={() => setCreateOpen(false)}>
          <div className="field">
            <label className="field-label">名称</label>
            <input
              className="input"
              autoFocus
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              placeholder="例如：客户端研发组"
            />
          </div>
          <span className="field-help">创建后进入编排页：添加岗位、设置职责，再用下拉选择把连线接起来。</span>
          <button className="btn primary block" onClick={() => void createTeam()} disabled={busy}>
            {busy ? '创建中…' : '创建并开始编排'}
          </button>
        </MobileSheet>
      )}

      {startOpen && (
        <MobileSheet title="发起协作运行" onClose={() => setStartOpen(false)}>
          <div className="field">
            <label className="field-label">协作组</label>
            <select className="select" value={teamId} onChange={(event) => setTeamId(event.target.value)}>
              <option value="">请选择…</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.name}（{team.node_count} 岗位）
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label">目标（可选）</label>
            <input
              className="input"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="这次协作要达成什么"
            />
          </div>
          <div className="field">
            <label className="field-label">工作路径</label>
            <input
              className="input mono"
              value={workspace}
              onChange={(event) => setWorkspace(event.target.value)}
              placeholder="例如 /vol1/projects/my-app；留空用默认路径"
            />
            <span className="field-help">不填写就使用协作组默认路径；填写后所有智能体只能在这个路径内读写。</span>
          </div>
          <button className="btn primary block" onClick={() => void start()} disabled={busy || !teamId}>
            {busy ? '创建中…' : '创建并进入运行'}
          </button>
        </MobileSheet>
      )}
    </div>
  )
}
