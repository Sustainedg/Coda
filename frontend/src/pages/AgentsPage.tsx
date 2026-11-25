import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { CollapsibleGroup } from '../components/CollapsibleGroup'
import { CollabSettingsPanel } from '../components/CollabSettingsPanel'
import { AgentAvatar } from '../components/AgentAvatar'
import { TeamAvatarStack } from '../components/TeamAvatarStack'
import { Modal } from '../components/Modal'
import { StarIcon } from '../components/Icons'
import { api, ApiError } from '../lib/api'
import { groupAgents } from '../lib/agentGroups'
import { useExpandMap } from '../lib/collapse'
import { formatListTime } from '../lib/datetime'
import type {
  AgentProfile,
  AgentType,
  CollabTeam,
  McpServer,
  Skill,
  TestResult,
} from '../lib/types'

/**
 * 智能体：人格 + 内核 + 网关 + 模型。
 *
 * 一个智能体就是「用哪个内核、走哪个网关、选哪个模型、带什么人格」的一份完整配置，
 * 也是对话与协作唯一的最小单位（原「角色」与「智能体实例」合并而来）。
 */
export function AgentsPage() {
  const navigate = useNavigate()
  const [types, setTypes] = useState<AgentType[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcpServers, setMcpServers] = useState<McpServer[]>([])
  const [teams, setTeams] = useState<CollabTeam[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  /** 协作组是旁路数据：单独加载、单独报错，一个接口挂了不能把智能体列表一起带黑。 */
  const [teamsLoading, setTeamsLoading] = useState(true)
  const [teamError, setTeamError] = useState<string | null>(null)

  /** 页内标签：智能体 / 协作组。写进 `?tab=`，刷新和分享链接都能落回同一处。 */
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'collab' ? 'collab' : 'agents'
  const editingTeamId = tab === 'collab' ? params.get('team') ?? undefined : undefined

  const [teamCreating, setTeamCreating] = useState(false)
  const [newTeamName, setNewTeamName] = useState('')
  const [testing, setTesting] = useState<{ agent: AgentProfile; result: TestResult | null; busy: boolean } | null>(null)

  const loadTeams = async () => {
    setTeamsLoading(true)
    try {
      setTeams(await api.listCollabTeams())
      setTeamError(null)
    } catch (e) {
      setTeamError(describe(e))
    } finally {
      setTeamsLoading(false)
    }
  }

  const reload = async () => {
    setLoading(true)
    try {
      const [typeList, agentList, skillList, mcpList] = await Promise.all([
        api.agentTypes(),
        api.listAgents(),
        api.listSkills(),
        api.listMcpServers(),
      ])
      setTypes(typeList)
      setAgents(agentList)
      setSkills(skillList)
      setMcpServers(mcpList)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
    // 协作组单独拉：它的失败只落在 teamError，不影响上面的智能体数据。
    await loadTeams()
  }

  useEffect(() => {
    void reload()
  }, [])

  const typeMap = useMemo(() => Object.fromEntries(types.map((t) => [t.id, t])), [types])
  /** 按分组折叠展示（默认展开：一进页面就能看到卡片，想收起来再点标题）。 */
  const { isExpanded, toggle } = useExpandMap(true)
  const groups = useMemo(() => groupAgents(agents), [agents])

  /** 切换页内标签；离开智能体列表时把手上的弹窗收掉，避免下次回来还挂着。 */
  const openTab = (next: 'agents' | 'collab') => {
    setTeamCreating(false)
    setTesting(null)
    setParams(next === 'collab' ? { tab: 'collab' } : {})
  }

  /** 打开某张协作组的画布；传 null 回到协作组卡片列表。 */
  const openTeam = (teamId: string | null) => {
    setParams(teamId ? { tab: 'collab', team: teamId } : { tab: 'collab' })
  }

  const createTeam = async () => {
    try {
      const created = await api.createCollabTeam({ name: newTeamName.trim() || '新建协作组' })
      setTeamCreating(false)
      setNewTeamName('')
      await loadTeams()
      openTeam(created.id)
    } catch (e) {
      setTeamError(describe(e))
    }
  }

  /** 点卡片右上角的五角星：设为默认智能体（再点一下取消），全局只有一颗。 */
  const toggleDefault = async (agent: AgentProfile) => {
    try {
      await api.updateAgent(agent.id, { is_default: !agent.is_default })
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const remove = async (agent: AgentProfile) => {
    if (!confirm(`删除智能体「${agent.name}」？已有对话会保留在历史里。`)) return
    try {
      await api.deleteAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  /** 启停：只改 enabled，不动其它配置（停用后仍保留配置、历史对话照旧可读）。 */
  const toggleEnabled = async (agent: AgentProfile) => {
    try {
      await api.updateAgent(agent.id, { enabled: !agent.enabled })
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const duplicate = async (agent: AgentProfile) => {
    try {
      await api.duplicateAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const runTest = async (agent: AgentProfile) => {
    setTesting({ agent, result: null, busy: true })
    try {
      const result = await api.testAgent(agent.id)
      setTesting({ agent, result, busy: false })
    } catch (e) {
      setTesting({ agent, result: { ok: false, message: describe(e), checks: [] }, busy: false })
    }
  }

  /**
   * 一张智能体卡片。分组之后它嵌在 `CollapsibleGroup` → `.grid` 里，抽成函数免得缩进成一大坨。
   */
  const renderAgentCard = (agent: AgentProfile) => {
    const type = typeMap[agent.type_id]
    const availability = agent.availability ?? type?.availability
    // 可用性那一颗已经写着「2.1.316 (Claude Code)」这类版本串时，别再重复一遍内核名
    // （只写「0.68.0」这种没有内核名的版本串，仍然要保留名字那颗）
    const showTypeName = !versionCarriesName(
      availability?.installed ? availability.version : null,
      agent.type_name ?? agent.type_id,
    )
    return (
      <article className="card" key={agent.id}>
        <div className="card-head">
          <AgentAvatar
            avatar={agent.avatar}
            avatarPreset={agent.avatar_preset}
            avatarFile={agent.avatar_file}
            agentId={agent.id}
            size={40}
          />
          <div style={{ minWidth: 0 }}>
            <div className="card-title">{agent.name}</div>
            <div className="card-sub">{agent.tagline ?? '无备注'}</div>
          </div>
          <button
            type="button"
            className={`star-btn${agent.is_default ? ' on' : ''}`}
            onClick={() => void toggleDefault(agent)}
            aria-pressed={agent.is_default}
            aria-label={
              agent.is_default
                ? `取消「${agent.name}」的默认智能体`
                : `把「${agent.name}」设为默认智能体`
            }
            title={
              agent.is_default
                ? '默认智能体：新建对话直接用它（点一下取消）'
                : '设为默认智能体：新建对话直接用它'
            }
          >
            <StarIcon size={18} on={agent.is_default} />
          </button>
        </div>

        <div className="chips">
          {showTypeName && <span className="pill brand">{agent.type_name ?? agent.type_id}</span>}
          <span className={`pill ${agent.runtime === 'api' ? 'brand' : ''}`}>
            {agent.runtime === 'api' ? '进程内 API' : '本地 CLI'}
          </span>
          {availability &&
            (availability.installed ? (
              <span className="pill ok">
                <span className="dot" />
                {availability.version ?? '就绪'}
              </span>
            ) : (
              <span className="pill err">
                <span className="dot off" />
                未安装
              </span>
            ))}
          {agent.enabled ? <span className="pill ok">已启用</span> : <span className="pill warn">已停用</span>}
          {agent.skills.length > 0 && <span className="pill ok">🧩 {agent.skills.length} 个技能</span>}
          {agent.mcp_servers.length > 0 && (
            <span className="pill ok">🔌 {agent.mcp_servers.length} 个 MCP</span>
          )}
          {agent.credentials.length > 0 && <span className="pill">🔑 {agent.credentials.length} 条凭据</span>}
        </div>

        <dl className="kv tight">
          <dt>网关</dt>
          <dd title={agent.gateway_base_url ?? ''}>
            {agent.gateway_kind === 'official' || !agent.gateway_name
              ? '官方默认'
              : agent.gateway_name}
          </dd>
          <dt>模型</dt>
          <dd>{agent.model ?? '默认'}</dd>
        </dl>

        <div className="card-actions" style={{ marginLeft: 0 }}>
          <button className="btn sm primary" onClick={() => void runTest(agent)}>
            连通性测试
          </button>
          <button className="btn sm" onClick={() => void toggleEnabled(agent)}>
            {agent.enabled ? '停用' : '启用'}
          </button>
          <button className="btn sm" onClick={() => navigate(`/agents/${agent.id}`)}>
            设定
          </button>
          <button className="btn sm" onClick={() => void duplicate(agent)}>
            复制
          </button>
          <button className="btn sm danger" onClick={() => void remove(agent)}>
            删除
          </button>
        </div>
      </article>
    )
  }

  /** 一张协作组卡片：点整张卡进入画布编排（与智能体卡片同一套外观）。 */
  const renderTeamCard = (team: CollabTeam) => (
    <button
      type="button"
      className="card card-clickable"
      key={team.id}
      onClick={() => openTeam(team.id)}
      title={`进入「${team.name}」的协作组设置`}
    >
      <div className="card-head">
        <TeamAvatarStack team={team} agents={agents} size={40} />
        <div style={{ minWidth: 0 }}>
          <div className="card-title">{team.name}</div>
          <div className="card-sub">{team.description?.trim() || '未填写说明'}</div>
        </div>
        <span className={`pill ${team.validation.ok ? 'ok' : 'err'}`} style={{ marginLeft: 'auto' }}>
          {team.validation.ok ? '可运行' : `${team.validation.errors.length} 个问题`}
        </span>
      </div>

      <div className="chips">
        <span className="pill">{team.node_count} 个岗位</span>
        {team.entry_key && <span className="pill brand">入口 {team.entry_key}</span>}
        {team.validation.errors.length > 0 && <span className="pill err">需要处理</span>}
      </div>

      <dl className="kv tight">
        <dt>工作目录</dt>
        <dd title={team.workspace_dir ?? ''}>{team.workspace_dir ?? '默认'}</dd>
        <dt>更新</dt>
        <dd>{formatListTime(team.updated_at)}</dd>
      </dl>

      <div className="card-actions" style={{ marginLeft: 0 }}>
        <span className="dim small">点击进入协作组设置 →</span>
      </div>
    </button>
  )

  return (
    <>
      {/* 顶栏：切换按钮（智能体 / 协作组）在最左侧，操作按钮贴右 —— 中间不放标题与说明。 */}
      <header className="page-header">
        <nav className="tabs">
          <button
            type="button"
            className={`tab${tab === 'agents' ? ' active' : ''}`}
            aria-current={tab === 'agents' ? 'page' : undefined}
            onClick={() => openTab('agents')}
          >
            智能体
          </button>
          <button
            type="button"
            className={`tab${tab === 'collab' ? ' active' : ''}`}
            aria-current={tab === 'collab' ? 'page' : undefined}
            onClick={() => openTab('collab')}
          >
            协作组
          </button>
        </nav>
        <div className="spacer" />
        {tab === 'agents' ? (
          <>
            <button className="btn" onClick={() => void reload()} disabled={loading}>
              {loading ? <span className="spin">◌</span> : '↻'} 刷新
            </button>
            <button className="btn primary" onClick={() => navigate('/agents/new')}>
              + 新建智能体
            </button>
          </>
        ) : editingTeamId ? (
          <button className="btn" onClick={() => openTeam(null)}>
            ← 返回协作组列表
          </button>
        ) : (
          <>
            <button className="btn" onClick={() => void loadTeams()} disabled={teamsLoading}>
              {teamsLoading ? <span className="spin">◌</span> : '↻'} 刷新
            </button>
            <button className="btn primary" onClick={() => setTeamCreating(true)}>
              + 新建协作组
            </button>
          </>
        )}
      </header>

      {tab === 'collab' && editingTeamId ? (
        <CollabSettingsPanel
          teamId={editingTeamId}
          teams={teams}
          agents={agents}
          skills={skills}
          mcpServers={mcpServers}
          types={types}
          onOpenTeam={openTeam}
          onTeamsChanged={loadTeams}
        />
      ) : (
        <div className="page-body stack">
          {tab === 'agents' ? (
            <>
              {error && <div className="banner err">{error}</div>}

              {agents.length === 0 && !loading ? (
                <div className="empty">
                  <div className="empty-icon">🤖</div>
                  <p>还没有配置任何智能体。</p>
                  <p className="dim small" style={{ marginTop: 6 }}>
                    点击「新建智能体」，选择内核与网关，写下它的人格设定。
                  </p>
                </div>
              ) : (
                <div className="stack">
                  {groups.map((group) => (
                    <CollapsibleGroup
                      key={group.key}
                      title={group.label}
                      count={group.items.length}
                      expanded={isExpanded(group.key)}
                      onToggle={() => toggle(group.key)}
                    >
                      <div className="grid">{group.items.map(renderAgentCard)}</div>
                    </CollapsibleGroup>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              {teamError && (
                <div className="banner err">
                  <div className="row" style={{ gap: 8 }}>
                    <span style={{ flex: 1 }}>协作组加载失败：{teamError}</span>
                    <button className="btn sm" onClick={() => void loadTeams()}>
                      重试
                    </button>
                  </div>
                </div>
              )}
              {teamsLoading && teams.length === 0 ? (
                <div className="banner info">正在加载协作组…</div>
              ) : teams.length === 0 ? (
                <div className="empty">
                  <div className="empty-icon">🤝</div>
                  <p>还没有协作组。</p>
                  <p className="dim small" style={{ marginTop: 6 }}>
                    新建一个，然后把智能体拖到画布上连起来。
                  </p>
                  <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setTeamCreating(true)}>
                    + 新建协作组
                  </button>
                </div>
              ) : (
                <div className="grid">{teams.map(renderTeamCard)}</div>
              )}
            </>
          )}
        </div>
      )}

      {teamCreating && (
        <Modal
          title="新建协作组"
          onClose={() => setTeamCreating(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setTeamCreating(false)}>
                取消
              </button>
              <button className="btn primary" onClick={() => void createTeam()}>
                创建
              </button>
            </>
          }
        >
          <label className="field">
            <span className="field-label">名称</span>
            <input
              className="input"
              autoFocus
              value={newTeamName}
              onChange={(e) => setNewTeamName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void createTeam()}
              placeholder="例如：客户端研发组"
            />
          </label>
          <span className="field-help">
            创建后直接进入画布：从上方智能体抽屉拖入（或点选）智能体，再把节点连起来。
          </span>
        </Modal>
      )}

      {testing && (
        <Modal
          title={`连通性测试 · ${testing.agent.name}`}
          onClose={() => setTesting(null)}
          footer={
            <button className="btn" onClick={() => setTesting(null)}>
              关闭
            </button>
          }
        >
          {testing.busy ? (
            <div className="row">
              <span className="spin">◌</span> 正在探测…
            </div>
          ) : (
            <div className="stack" style={{ gap: 10 }}>
              <div className={`banner ${testing.result?.ok ? 'ok' : 'err'}`}>
                {testing.result?.ok ? '✅ 配置可用' : '❌ 存在问题'}：{testing.result?.message}
              </div>
              {testing.result?.checks.map((check) => (
                <div className="row" key={check.name}>
                  <span className={`pill ${check.ok ? 'ok' : 'err'}`}>{check.ok ? '通过' : '失败'}</span>
                  <span className="muted small">{check.name}</span>
                  <span className="dim mono-sm" style={{ marginLeft: 'auto' }}>
                    {check.detail}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </>
  )
}

/** 版本串里是否已经带上了内核名（忽略大小写与连字符之类的分隔符）。 */
function versionCarriesName(version: string | null | undefined, typeName: string): boolean {
  if (!version) return false
  const normalize = (text: string) => text.toLowerCase().replace(/[^a-z0-9]/g, '')
  return normalize(version).includes(normalize(typeName))
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
