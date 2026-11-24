import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../../lib/api'
import { AgentAvatar } from '../../components/AgentAvatar'
import {
  DUTIES,
  EDGE_KINDS,
  addNodeToDraft,
  collabTeamPayload,
  isWarningIssue,
  issuesFromValidation,
  newEdgeId,
  removeNodeFromDraft,
  renameNodeInDraft,
  toTeamDraft,
  type TeamDraft,
} from '../../lib/collabDraft'
import type {
  CollabDuty,
  CollabEdge,
  CollabEdgeKind,
  CollabIssue,
  CollabNode,
  AgentProfile,
  AgentType,
  Skill,
  McpServer,
} from '../../lib/types'
import { CollabEdgeForm } from '../components/CollabEdgeForm'
import { CollabNodeForm } from '../components/CollabNodeForm'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/**
 * 协作组编排（移动端）：**表单式**设置协作组 —— 岗位用卡片列表、连线用「从谁到谁」的下拉选择，
 * 不做桌面端的画布拖拽（手机上精确连线体验很差，见 `components/CollabCanvas`）。
 *
 * 与桌面端共用同一份字段语义与默认值（`lib/collabDraft`），校验一律交给服务端
 * （`POST /teams/{id}/validate`，与运行时同一套规则），前端不重复实现。
 */
export function MobileCollabTeamPage() {
  const { teamId = '' } = useParams<{ teamId: string }>()
  const navigate = useNavigate()
  const [draft, setDraft] = useState<TeamDraft | null>(null)
  const [issues, setIssues] = useState<CollabIssue[]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcpServers, setMcpServers] = useState<McpServer[]>([])
  const [types, setTypes] = useState<AgentType[]>([])
  const [nodeKey, setNodeKey] = useState<string | null>(null)
  const [edgeId, setEdgeId] = useState<string | null>(null)
  const [agentPickerOpen, setAgentPickerOpen] = useState(false)
  const [runOpen, setRunOpen] = useState(false)
  const [runTitle, setRunTitle] = useState('')
  const versionRef = useRef(0)

  const load = useCallback(async () => {
    try {
      const team = await api.getCollabTeam(teamId)
      setDraft(toTeamDraft(team))
      setIssues(issuesFromValidation(team.validation))
      setDirty(false)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [teamId])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    void (async () => {
      try {
        // 技能 / MCP / 内核目录：岗位表单的「协作专属分配」选择器要用（与智能体页同一套数据源）
        const [agentList, skillList, mcpList, typeList] = await Promise.all([
          api.listAgents(),
          api.listSkills(),
          api.listMcpServers(),
          api.agentTypes(),
        ])
        setAgents(agentList)
        setSkills(skillList)
        setMcpServers(mcpList)
        setTypes(typeList)
      } catch (e) {
        setError(describeError(e))
      }
    })()
  }, [])

  // 实时校验：改动后防抖 500ms 调一次服务端校验（与桌面端一致）
  useEffect(() => {
    if (!draft || !dirty) return
    const version = ++versionRef.current
    const timer = window.setTimeout(async () => {
      try {
        const validation = await api.validateCollabTeam(teamId, {
          graph: { nodes: draft.nodes, edges: draft.edges },
          entry_key: draft.entry_key,
        })
        if (version !== versionRef.current) return
        setIssues(issuesFromValidation(validation))
      } catch (e) {
        if (version !== versionRef.current) return
        setError(describeError(e))
      }
    }, 500)
    return () => window.clearTimeout(timer)
  }, [draft, dirty, teamId])

  const patch = (next: Partial<TeamDraft>) => {
    setDraft((prev) => (prev ? { ...prev, ...next } : prev))
    setDirty(true)
  }

  const addAgent = (agent: AgentProfile) => {
    if (!draft) return
    const next = addNodeToDraft(draft, agent)
    setDraft(next)
    setDirty(true)
    setAgentPickerOpen(false)
    setNodeKey(next.nodes[next.nodes.length - 1].key)
  }

  /** 岗位表单里改的是本地副本，点「保存岗位」才写回草稿（含 key 改名与连线同步）。 */
  const saveNode = (oldKey: string, next: CollabNode) => {
    setDraft((prev) => {
      if (!prev) return prev
      const renamed = next.key !== oldKey ? renameNodeInDraft(prev, oldKey, next.key) : prev
      return { ...renamed, nodes: renamed.nodes.map((node) => (node.key === next.key ? next : node)) }
    })
    setNodeKey(null)
    setDirty(true)
  }

  const deleteNode = (key: string) => {
    const node = draft?.nodes.find((item) => item.key === key)
    if (node && !confirm(`删除岗位「${node.label || key}」及其全部连线？`)) return
    setDraft((prev) => (prev ? removeNodeFromDraft(prev, key) : prev))
    setNodeKey(null)
    setDirty(true)
  }

  const saveEdge = (id: string, next: Omit<CollabEdge, 'id'>) => {
    setDraft((prev) =>
      prev ? { ...prev, edges: prev.edges.map((edge) => (edge.id === id ? { ...next, id } : edge)) } : prev,
    )
    setEdgeId(null)
    setDirty(true)
  }

  const addEdge = (next: Omit<CollabEdge, 'id'>) => {
    if (!draft) return
    if (draft.edges.some((edge) => edge.from_key === next.from_key && edge.to_key === next.to_key && edge.kind === next.kind)) {
      setError('同方向、同类型的连线已经存在')
      return
    }
    setDraft({ ...draft, edges: [...draft.edges, { ...next, id: newEdgeId() }] })
    setEdgeId(null)
    setDirty(true)
  }

  const deleteEdge = (id: string) => {
    setDraft((prev) => (prev ? { ...prev, edges: prev.edges.filter((edge) => edge.id !== id) } : prev))
    setEdgeId(null)
    setDirty(true)
  }

  const save = async () => {
    if (!draft) return
    setSaving(true)
    setError(null)
    try {
      const saved = await api.saveCollabTeam(teamId, collabTeamPayload(draft))
      setDraft(toTeamDraft(saved))
      setIssues(issuesFromValidation(saved.validation))
      setDirty(false)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  const startRun = async () => {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      const run = await api.createCollabRun({ team_id: teamId, title: runTitle.trim() || undefined })
      setRunOpen(false)
      setRunTitle('')
      navigate(`/collab/runs/${run.id}`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  const duplicate = async () => {
    setBusy(true)
    setError(null)
    try {
      const clone = await api.duplicateCollabTeam(teamId)
      navigate(`/collab/teams/${clone.id}`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!draft || !confirm(`删除协作组「${draft.name}」？有运行记录的协作组不能删除。`)) return
    setBusy(true)
    setError(null)
    try {
      await api.deleteCollabTeam(teamId)
      navigate('/collab')
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  const errors = issues.filter((issue) => !isWarningIssue(issue))
  const warnings = issues.filter(isWarningIssue)
  const node = draft && nodeKey ? draft.nodes.find((item) => item.key === nodeKey) : undefined
  const edge = draft && edgeId ? draft.edges.find((item) => item.id === edgeId) : undefined
  const agentsById = new Map(agents.map((agent) => [agent.id, agent]))
  const nodeLabel = (key: string) => {
    const target = draft?.nodes.find((item) => item.key === key)
    return target ? `${target.label || target.key}（${target.key}）` : key
  }

  return (
    <div className="m-page">
      <MobileTopBar
        title={draft?.name || '协作组'}
        subtitle={
          draft
            ? `${draft.nodes.length} 个岗位 · ${draft.edges.length} 条连线 · ${errors.length === 0 ? '可运行' : `${errors.length} 处待修`}`
            : '正在加载…'
        }
        onBack={() => navigate('/collab')}
        actions={
          <button className="btn sm primary" onClick={() => void save()} disabled={!dirty || saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          {loading ? (
            <p className="dim small">正在加载…</p>
          ) : !draft ? (
            <div className="m-empty">
              <div className="m-empty-icon">🤝</div>
              <p>这个协作组打不开</p>
              <button className="btn" onClick={() => navigate('/collab')}>
                返回列表
              </button>
            </div>
          ) : (
            <>
              {agents.length === 0 && (
                <div className="banner info">
                  还没有智能体。协作组里的每个岗位都要绑一个智能体，请先创建智能体。
                  <div style={{ marginTop: 8 }}>
                    <button className="btn sm" onClick={() => navigate('/agents')}>
                      去创建智能体
                    </button>
                  </div>
                </div>
              )}

              {(errors.length > 0 || warnings.length > 0) && (
                <div className="stack" style={{ gap: 6 }}>
                  <div className="card-section-title">
                    校验 · {errors.length} 个错误 · {warnings.length} 条提醒
                  </div>
                  {[...errors, ...warnings].slice(0, 8).map((issue, index) => (
                    <div
                      key={`${issue.code}-${index}`}
                      className={`banner ${isWarningIssue(issue) ? 'info' : 'err'}`}
                    >
                      {issue.message}
                    </div>
                  ))}
                </div>
              )}

              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">基本信息</div>
                <div className="card">
                  <div className="field">
                    <label className="field-label">名称</label>
                    <input
                      className="input"
                      value={draft.name}
                      onChange={(event) => patch({ name: event.target.value })}
                      placeholder="例如 客户端研发组"
                    />
                  </div>
                  <div className="field">
                    <label className="field-label">说明</label>
                    <textarea
                      className="textarea"
                      style={{ minHeight: 66 }}
                      value={draft.description}
                      onChange={(event) => patch({ description: event.target.value })}
                      placeholder="这个协作组是干什么的"
                    />
                  </div>
                  <div className="field">
                    <label className="field-label">入口节点</label>
                    <select
                      className="select"
                      value={draft.entry_key ?? ''}
                      onChange={(event) => patch({ entry_key: event.target.value || null })}
                    >
                      <option value="">（未指定）</option>
                      {draft.nodes.map((item) => (
                        <option key={item.key} value={item.key}>
                          {item.label || item.key}（{item.key}）
                        </option>
                      ))}
                    </select>
                    <span className="field-help">唯一能与用户对话的节点，必须是协调者。</span>
                  </div>
                  <div className="field">
                    <label className="field-label">工作路径</label>
                    <input
                      className="input mono"
                      value={draft.workspace_dir}
                      onChange={(event) => patch({ workspace_dir: event.target.value })}
                      placeholder="例如 /vol1/projects/my-app；留空自动分配"
                    />
                    <span className="field-help">
                      作为这个团队的默认工作路径；新建协作任务时可以覆盖。留空则每次运行自动分配。
                    </span>
                  </div>
                </div>
              </div>

              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">预算与上限</div>
                <div className="card">
                  <div className="form-grid">
                    <NumberField
                      label="最大步数"
                      value={draft.budget.max_steps}
                      onChange={(value) => patch({ budget: { ...draft.budget, max_steps: value } })}
                    />
                    <NumberField
                      label="最大工单数"
                      value={draft.budget.max_tickets}
                      onChange={(value) => patch({ budget: { ...draft.budget, max_tickets: value } })}
                    />
                    <NumberField
                      label="单工单返工上限"
                      value={draft.budget.max_rounds_per_ticket}
                      onChange={(value) => patch({ budget: { ...draft.budget, max_rounds_per_ticket: value } })}
                    />
                    <NumberField
                      label="token 上限（0=不限）"
                      value={draft.budget.max_tokens}
                      onChange={(value) => patch({ budget: { ...draft.budget, max_tokens: value } })}
                    />
                    <NumberField
                      label="运行时限（秒）"
                      value={draft.budget.timeout_seconds}
                      onChange={(value) => patch({ budget: { ...draft.budget, timeout_seconds: value } })}
                    />
                    <NumberField
                      label="单步限时·秒（0=不限）"
                      value={draft.budget.step_timeout_seconds ?? 36000}
                      onChange={(value) => patch({ budget: { ...draft.budget, step_timeout_seconds: value } })}
                    />
                    <NumberField
                      label="并行上限（1=串行）"
                      value={draft.budget.max_parallel ?? 1}
                      onChange={(value) => patch({ budget: { ...draft.budget, max_parallel: value } })}
                    />
                  </div>
                  <span className="field-help">任何一项超限都会挂起并明确提示，不会静默烧钱。</span>
                </div>
              </div>

              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">岗位（{draft.nodes.length}）</div>
                {draft.nodes.length === 0 ? (
                  <p className="dim small">还没有岗位。添加岗位 = 把某个智能体请进这次协作，并给它一个职责。</p>
                ) : (
                  draft.nodes.map((item) => {
                    const agent = agentsById.get(item.agent_profile_id)
                    return (
                      <div className="m-node-card" key={item.key}>
                        <div className="m-node-head">
                          <span className="avatar">{agent?.avatar ?? '🤖'}</span>
                          <div className="m-item-main">
                            <div className="m-node-title">
                              {item.label || agent?.name || item.key}
                              {item.key === draft.entry_key && <span className="pill brand">入口</span>}
                            </div>
                            <div className="m-node-sub mono-sm">
                              {item.key} · {dutyLabel(item.duty)}
                              {agent ? ` · ${agent.name}` : ' · 智能体已删除，请重新选择'}
                            </div>
                          </div>
                        </div>
                        <div className="m-item-actions" style={{ marginTop: 0 }}>
                          <button className="btn sm" onClick={() => setNodeKey(item.key)}>
                            编辑
                          </button>
                          <button className="btn sm danger" onClick={() => deleteNode(item.key)}>
                            删除
                          </button>
                        </div>
                      </div>
                    )
                  })
                )}
                <button
                  className="btn block"
                  onClick={() => setAgentPickerOpen(true)}
                  disabled={agents.length === 0}
                >
                  ＋ 添加岗位（选一个智能体）
                </button>
              </div>

              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">连线（{draft.edges.length}）</div>
                {draft.edges.length === 0 ? (
                  <p className="dim small">还没有连线。连线决定工单能从哪个岗位交给哪个岗位。</p>
                ) : (
                  draft.edges.map((item) => (
                    <div className="m-edge-row" key={item.id}>
                      <span className="m-edge-text">
                        <b>{nodeLabel(item.from_key)}</b> → <b>{nodeLabel(item.to_key)}</b>
                        <span className="dim small">
                          {' '}
                          · {edgeKindLabel(item.kind)}
                          {item.label ? ` · ${item.label}` : ''}
                        </span>
                      </span>
                      <button className="btn sm" onClick={() => setEdgeId(item.id)}>
                        编辑
                      </button>
                    </div>
                  ))
                )}
                <button
                  className="btn block"
                  onClick={() => setEdgeId('')}
                  disabled={draft.nodes.length < 2}
                >
                  ＋ 添加连线
                </button>
              </div>

              <div className="stack" style={{ gap: 8 }}>
                <div className="card-section-title">运行与维护</div>
                <button className="btn primary block" onClick={() => setRunOpen(true)} disabled={busy}>
                  ▶ 用这个协作组发起运行
                </button>
                <div className="row" style={{ gap: 8 }}>
                  <button className="btn sm" onClick={() => void duplicate()} disabled={busy}>
                    复制一份
                  </button>
                  <button className="btn sm danger" onClick={() => void remove()} disabled={busy}>
                    删除协作组
                  </button>
                </div>
                <span className="field-help">
                  有运行记录的协作组不能删除；改动只影响还没开跑 / 之后新建的任务（已下工单的运行按当时编排继续）。
                </span>
              </div>
            </>
          )}
        </div>
      </div>

      {agentPickerOpen && (
        <MobileSheet title="添加岗位" onClose={() => setAgentPickerOpen(false)}>
          <div className="m-list">
            {agents.map((agent) => (
              <button key={agent.id} className="m-item" onClick={() => addAgent(agent)}>
                <AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} avatarFile={agent.avatar_file} agentId={agent.id} size={34} />
                <span className="m-item-main">
                  <span className="m-item-title">{agent.name}</span>
                  <span className="m-item-sub">{agent.type_name ?? agent.type_id} · {agent.tagline ?? '无备注'}</span>
                </span>
                <span className="m-item-chevron">›</span>
              </button>
            ))}
          </div>
        </MobileSheet>
      )}

      {node && (
        <MobileSheet full title={`岗位 · ${node.label || node.key}`} onClose={() => setNodeKey(null)}>
          <CollabNodeForm
            node={node}
            agents={agents}
            skills={skills}
            mcpServers={mcpServers}
            types={types}
            isEntry={node.key === draft?.entry_key}
            onSave={(next) => saveNode(node.key, next)}
            onDelete={() => deleteNode(node.key)}
            onSetEntry={() => {
              patch({ entry_key: node.key })
              setNodeKey(null)
            }}
          />
        </MobileSheet>
      )}

      {edgeId !== null && (
        <MobileSheet
          title={edge ? '编辑连线' : '添加连线'}
          onClose={() => setEdgeId(null)}
        >
          <CollabEdgeForm
            edge={edge}
            nodes={draft?.nodes ?? []}
            defaultFrom={draft?.nodes[0]?.key ?? ''}
            onSave={(next) => (edge ? saveEdge(edge.id, next) : addEdge(next))}
            onDelete={() => edge && deleteEdge(edge.id)}
          />
        </MobileSheet>
      )}

      {runOpen && (
        <MobileSheet title="发起协作运行" onClose={() => setRunOpen(false)}>
          <div className="field">
            <label className="field-label">协作组</label>
            <input className="input" value={draft?.name ?? ''} readOnly />
          </div>
          <div className="field">
            <label className="field-label">目标（可选）</label>
            <input
              className="input"
              value={runTitle}
              onChange={(event) => setRunTitle(event.target.value)}
              placeholder="这次协作要达成什么"
            />
          </div>
          <button className="btn primary block" onClick={() => void startRun()} disabled={busy}>
            {busy ? '创建中…' : '创建并进入运行'}
          </button>
          <span className="field-help">创建后进入运行页，再补充更详细的目标说明。</span>
        </MobileSheet>
      )}
    </div>
  )
}

function dutyLabel(duty: CollabDuty): string {
  return DUTIES.find((item) => item.value === duty)?.label ?? duty
}

function edgeKindLabel(kind: CollabEdgeKind): string {
  return EDGE_KINDS.find((item) => item.value === kind)?.label ?? kind
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (next: number) => void }) {
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <input
        className="input mono"
        type="number"
        min={0}
        value={value}
        onChange={(event) => onChange(Math.max(0, Number(event.target.value) || 0))}
      />
    </div>
  )
}
