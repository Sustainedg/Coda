import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { CollapsibleGroup } from '../components/CollapsibleGroup'
import { CollabSettingsPanel } from '../components/CollabSettingsPanel'
import {
  CredentialEditor,
  credentialsToDraft,
  draftsToPayload,
  validateCredentials,
} from '../components/CredentialEditor'
import { DynamicForm } from '../components/DynamicForm'
import { AgentAvatarPicker } from '../components/AgentAvatarPicker'
import { AgentAvatar } from '../components/AgentAvatar'
import { TeamAvatarStack } from '../components/TeamAvatarStack'
import { Modal } from '../components/Modal'
import { StarIcon } from '../components/Icons'
import { McpPicker } from '../components/McpPicker'
import { SkillPicker } from '../components/SkillPicker'
import { api, ApiError } from '../lib/api'
import { groupAgents } from '../lib/agentGroups'
import { useExpandMap } from '../lib/collapse'
import { formatListTime } from '../lib/datetime'
import type {
  AgentProfile,
  AgentType,
  CollabTeam,
  Credential,
  Gateway,
  McpServer,
  Skill,
  TestResult,
} from '../lib/types'

/* 头像选择走 AgentAvatarPicker（可爱 emoji 池 + 随机生成 + 自定义上传），旧的固定池删了 */
const PROMPT_TEMPLATES: { label: string; body: string }[] = [
  {
    label: '严格代码审查',
    body: '你是一名严格的资深工程师。先阅读仓库再动手，指出风险与权衡，只提交最小且可验证的改动，并说明如何验证。',
  },
  {
    label: '结对的编程搭子',
    body: '你是务实的结对编程伙伴。改动小步提交，每次修改后运行相关测试，用一两句话总结改动与后续建议。',
  },
  {
    label: '调研助手',
    body: '你是研究型助手。先收集证据，引用你读过的文件路径，明确区分「事实」与「推测」。',
  },
]

/**
 * 智能体：人格 + 内核 + 网关 + 模型。
 *
 * 一个智能体就是「用哪个内核、走哪个网关、选哪个模型、带什么人格」的一份完整配置，
 * 也是对话与协作唯一的最小单位（原「角色」与「智能体实例」合并而来）。
 */
export function AgentsPage() {
  const [types, setTypes] = useState<AgentType[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [protocolLabels, setProtocolLabels] = useState<Record<string, string>>({})
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

  const [editing, setEditing] = useState<AgentProfile | null>(null)
  const [creating, setCreating] = useState(false)
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
      const [typeList, agentList, gatewayList, skillList, credentialList, mcpList] =
        await Promise.all([
          api.agentTypes(),
          api.listAgents(),
          api.listGateways(),
          api.listSkills(),
          api.listCredentials(),
          api.listMcpServers(),
        ])
      setTypes(typeList)
      setAgents(agentList)
      setGateways(gatewayList.items)
      setCredentials(credentialList.items)
      setProtocolLabels(Object.fromEntries(gatewayList.protocols.map((item) => [item.value, item.label])))
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
    setEditing(null)
    setCreating(false)
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
          <button className="btn sm" onClick={() => setEditing(agent)}>
            编辑
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
            <button className="btn primary" onClick={() => setCreating(true)}>
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

      {(creating || editing) && (
        <AgentModal
          types={types}
          gateways={gateways}
          protocolLabels={protocolLabels}
          skills={skills}
          mcpServers={mcpServers}
          library={credentials}
          /** 已有分组名：分组输入框点开时的快捷选项 */
          groups={[...new Set(agents.map((a) => a.group_name).filter(Boolean))].sort()}
          agent={editing}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={async () => {
            setCreating(false)
            setEditing(null)
            await reload()
          }}
        />
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

/**
 * 可以选择的自定义网关。
 *
 * 官方链路不在这里选：选「官方默认」就等于平台不接管路由、沿用内核自带的网关与凭据。
 * 自定义网关全部列出（协议与内核不一致的排到后面并标注），由用户自己判断能不能用。
 */
function gatewaysFor(type: AgentType | null, gateways: Gateway[]): Gateway[] {
  const custom = gateways.filter((g) => g.kind === 'custom')
  const protocol = type?.protocol
  if (!protocol) return custom
  const matches = (g: Gateway) => g.protocol === protocol
  return [...custom.filter(matches), ...custom.filter((g) => !matches(g))]
}

function AgentModal({
  types,
  gateways,
  protocolLabels,
  skills,
  mcpServers,
  library,
  groups,
  agent,
  onClose,
  onSaved,
}: {
  types: AgentType[]
  gateways: Gateway[]
  protocolLabels: Record<string, string>
  skills: Skill[]
  mcpServers: McpServer[]
  library: Credential[]
  /** 已有分组名（去重排序）：「分组」输入框聚焦时弹出的快捷选项 */
  groups: string[]
  agent: AgentProfile | null
  onClose: () => void
  onSaved: () => void
}) {
  // 「新建智能体」只列可选的内核模板（逃生舱型模板太技术化，不放在推荐入口）；
  // 编辑存量智能体时，即使它的内核已不再作为新选项，也要把当前内核留在下拉里，
  // 否则一打开表单就会显示成另一个内核。
  const selectableTypes = useMemo(() => {
    const usable = types.filter((type) => type.selectable !== false)
    if (agent && !usable.some((type) => type.id === agent.type_id)) {
      const current = types.find((type) => type.id === agent.type_id)
      if (current) return [...usable, current]
    }
    return usable
  }, [types, agent])

  const [typeId, setTypeId] = useState<string>(
    agent?.type_id ?? selectableTypes[0]?.id ?? types[0]?.id ?? '',
  )
  const selected = types.find((t) => t.id === typeId) ?? null

  const [name, setName] = useState(agent?.name ?? '')
  const [avatar, setAvatar] = useState(agent?.avatar ?? '🛠️')
  /** 内置头像包编号：随机生成 / 画廊挑选写这里，随表单一并保存 */
  const [avatarPreset, setAvatarPreset] = useState<string | null>(agent?.avatar_preset ?? null)
  /** 自定义头像文件标识：上传接口直接落库，这里只是镜像（保存 payload 不带它） */
  const [avatarFileId, setAvatarFileId] = useState<string | null>(agent?.avatar_file ?? null)
  const [groupName, setGroupName] = useState(agent?.group_name ?? '')
  /** 分组快捷建议的开合：聚焦弹出、失焦/选中即收 */
  const [groupSuggestOpen, setGroupSuggestOpen] = useState(false)
  const [tagline, setTagline] = useState(agent?.tagline ?? '')
  const [description, setDescription] = useState(agent?.description ?? '')
  const [systemPrompt, setSystemPrompt] = useState(agent?.system_prompt ?? '')
  // 官方链路统一显示为「官方默认」（等于平台不接管路由），因此这里归一化成空值。
  const [gatewayId, setGatewayId] = useState(
    agent && agent.gateway_kind !== 'official' ? agent.gateway_id ?? '' : '',
  )
  const [model, setModel] = useState(agent?.model ?? '')
  const [options, setOptions] = useState<Record<string, unknown>>(agent?.options ?? {})
  const [credentials, setCredentials] = useState(() => credentialsToDraft(agent?.credentials))
  const [skillIds, setSkillIds] = useState<string[]>(agent?.skill_ids ?? [])
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [mcpIds, setMcpIds] = useState<string[]>(agent?.mcp_ids ?? [])
  const [mcpOpen, setMcpOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 新建时按当前内核补齐适配器选项的默认值（换内核走 chooseType）。
  useEffect(() => {
    if (agent) return
    const initial = selectableTypes.find((type) => type.id === typeId) ?? selectableTypes[0]
    if (!initial) return
    if (!typeId) setTypeId(initial.id)
    setOptions((prev) =>
      Object.keys(prev).length > 0
        ? prev
        : Object.fromEntries(initial.config_fields.map((field) => [field.key, field.default])),
    )
  }, [agent, typeId, selectableTypes])

  const gatewayChoices = useMemo(() => gatewaysFor(selected, gateways), [selected, gateways])
  const gateway = gateways.find((g) => g.id === gatewayId) ?? null
  const modelChoices = gateway?.selected_models ?? []
  // 自定义网关的协议与内核期望的协议不一致时提醒（不拦，由用户判断代理是否兼容）。
  const protocolMismatch = !!(gateway && selected?.protocol && gateway.protocol !== selected.protocol)
  const supportsSkills = !!selected?.capabilities.skills
  const matchingSkills = useMemo(
    () => skills.filter((s) => s.type_id === typeId),
    [skills, typeId],
  )
  const pickedSkills = useMemo(
    () => matchingSkills.filter((skill) => skillIds.includes(skill.id)),
    [matchingSkills, skillIds],
  )
  const otherSkills = skills.length - matchingSkills.length
  const supportsMcp = !!selected?.capabilities.mcp
  // 平台通用的服务器（type_id 为空）可以分配给任意内核；扫描来的只能给同内核。
  const assignableMcp = useMemo(
    () => mcpServers.filter((server) => !server.type_id || server.type_id === typeId),
    [mcpServers, typeId],
  )
  const pickedMcp = useMemo(
    () => assignableMcp.filter((server) => mcpIds.includes(server.id)),
    [assignableMcp, mcpIds],
  )
  const otherMcp = mcpServers.length - assignableMcp.length

  const chooseType = (type: AgentType) => {
    if (type.id === typeId) return
    setTypeId(type.id)
    setOptions(Object.fromEntries(type.config_fields.map((f) => [f.key, f.default])))
    // 换内核后原网关可能不再匹配，清掉让用户重新选。
    setGatewayId('')
    setModel('')
    setSkillIds([])
    setMcpIds([])
    if (!agent) setName((prev) => prev || type.name)
  }

  const toggleSkill = (id: string) =>
    setSkillIds((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))

  const toggleMcp = (id: string) =>
    setMcpIds((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))

  const save = async () => {
    if (!selected) return setError('请先选择内核类型')
    if (!name.trim()) return setError('请填写名称')
    const credentialError = validateCredentials(credentials, library)
    if (credentialError) return setError(credentialError)
    setSaving(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        name: name.trim(),
        group_name: groupName.trim(),
        type_id: selected.id,
        avatar,
        avatar_preset: avatarPreset,
        tagline: tagline.trim() || null,
        description: description.trim() || null,
        system_prompt: systemPrompt,
        gateway_id: gatewayId || null,
        model: model.trim() || null,
        options,
        credentials: draftsToPayload(credentials),
      }
      // 技能分配紧随智能体保存：未分配的技能对这次运行不生效。
      const saved = agent ? await api.updateAgent(agent.id, payload) : await api.createAgent(payload)
      await api.setAgentSkills(saved.id, skillIds)
      // MCP 分配同理：没挂上的服务器对这次运行不生效。
      await api.setAgentMcpServers(saved.id, mcpIds)
      onSaved()
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      wide
      title={agent ? `编辑智能体 · ${agent.name}` : '新建智能体'}
      onClose={onClose}
      escapeDisabled={skillsOpen || mcpOpen}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={() => void save()} disabled={saving || !selected}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error && <div className="banner err">{error}</div>}

      {/* 表单顺序：头像 → 名称 → 内核类型 → 分组 —— 身份（头像 / 名字）在前，
          用什么内核跑是技术细节靠后；分组带着已有分组的快捷建议。 */}
      <AgentAvatarPicker
        agentId={agent?.id ?? null}
        emoji={avatar}
        onEmojiChange={setAvatar}
        preset={avatarPreset}
        onPresetChange={setAvatarPreset}
        avatarFileId={avatarFileId}
        onUploaded={(next: { avatar_file?: string | null }) => setAvatarFileId(next.avatar_file ?? null)}
      />

      <div className="form-grid">
        <div className="field">
          <label className="field-label">名称</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 代码审查员" />
        </div>

        <div className="field">
          <label className="field-label">内核类型</label>
          <select
            className="select"
            value={typeId}
            onChange={(e) => {
              const next = types.find((type) => type.id === e.target.value)
              if (next) chooseType(next)
            }}
          >
            {selectableTypes.map((type) => (
              <option key={type.id} value={type.id}>
                {type.name} · {type.vendor}
                {type.availability.installed ? '' : '（未安装）'}
              </option>
            ))}
          </select>
          <span className="field-help">
            {selected?.description}
            {selected && !selected.availability.installed
              ? ' ⚠️ 该内核的运行时还没安装，可到「运行时」页安装。'
              : ''}
          </span>
        </div>

        <div className="field span-2 group-suggest-wrap">
              <label className="field-label">分组</label>
              <input
                className="input"
                value={groupName}
                onChange={(e) => setGroupName(e.target.value)}
                onFocus={() => setGroupSuggestOpen(true)}
                onBlur={() => setGroupSuggestOpen(false)}
                placeholder="例如 开发 / 写作（留空就归入「未分组」）"
              />
              {/* 已有分组的快捷选项：点一下即填。onMouseDown 而不是 onClick ——
                  blur 先于 click 触发，用 click 的话菜单在收到点击前就收掉了。 */}
              {groupSuggestOpen && groups.length > 0 && (
                <div className="group-suggest" role="listbox" aria-label="已有分组">
                  {groups.map((item) => (
                    <button
                      key={item}
                      type="button"
                      className="group-suggest-item"
                      role="option"
                      aria-selected={item === groupName}
                      onMouseDown={(e) => {
                        e.preventDefault()
                        setGroupName(item)
                        setGroupSuggestOpen(false)
                      }}
                    >
                      {item}
                    </button>
                  ))}
                </div>
              )}
              <span className="field-help">智能体页按它折叠分组展示；留空表示未分组，永远排在最后。</span>
            </div>

            <div className="field span-2">
              <label className="field-label">一句话简介</label>
              <input
                className="input"
                value={tagline}
                onChange={(e) => setTagline(e.target.value)}
                placeholder="显示在对话列表里"
              />
            </div>

            <div className="field span-2">
              <label className="field-label">网关</label>
              <select className="select" value={gatewayId} onChange={(e) => setGatewayId(e.target.value)}>
                <option value="">官方默认（平台不接管路由 · 沿用内核自带的网关与凭据）</option>
                {gatewayChoices.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.base_url}
                    {protocolLabels[item.protocol] ? ` · ${protocolLabels[item.protocol]}` : ''}
                  </option>
                ))}
              </select>
              <span className="field-help">
                选「官方默认」= 平台不接管路由，沿用内核自带的网关与凭据；自定义网关在
                「设置 → 自定义网关」里维护，选中后平台会把它的地址、密钥与模型白名单注入这次运行。
                {gateway && !gateway.api_key_set ? ' ⚠️ 该网关还没配置密钥。' : ''}
                {protocolMismatch && gateway && selected
                  ? ` ⚠️ 该网关协议是 ${protocolLabels[gateway.protocol] ?? gateway.protocol}，与内核期望的 ${
                      protocolLabels[selected.protocol ?? ''] ?? selected.protocol
                    } 不一致，运行可能失败。`
                  : ''}
              </span>
            </div>

            <div className="field">
              <label className="field-label">默认模型</label>
              {gateway && modelChoices.length > 0 ? (
                <select className="select" value={model} onChange={(e) => setModel(e.target.value)}>
                  <option value="">内核默认（{selected?.default_model ?? '由内核决定'}）</option>
                  {modelChoices.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                  {model && !modelChoices.includes(model) && <option value={model}>{model}（不在白名单）</option>}
                </select>
              ) : (
                <input
                  className="input mono"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder={selected?.default_model ?? '例如 sonnet / gpt-5-codex'}
                />
              )}
              <span className="field-help">
                新对话默认使用的模型。
                {gateway ? '只允许选择该网关白名单里的模型。' : '官方默认时可直接填写模型名，留空则用内核默认。'}
              </span>
            </div>

            <div className="field span-2">
              <label className="field-label">
                系统提示词
                <span className="dim small" style={{ marginLeft: 'auto' }}>
                  <button className="btn ghost sm" type="button" onClick={() => setSystemPrompt('')}>
                    清空
                  </button>
                </span>
              </label>
              <textarea
                className="textarea"
                style={{ minHeight: 130 }}
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                placeholder="描述这个智能体的身份、风格与工作方式…"
              />
              <div className="chips">
                {PROMPT_TEMPLATES.map((template) => (
                  <button
                    key={template.label}
                    type="button"
                    className="btn sm"
                    onClick={() => setSystemPrompt(template.body)}
                  >
                    {template.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="field span-2">
              <label className="field-label">
                技能
                <span className="dim small" style={{ marginLeft: 'auto' }}>
                  已选 {skillIds.length} 个
                </span>
              </label>
              {!supportsSkills ? (
                <div className="banner info">该内核不支持技能，这个智能体不会加载任何技能。</div>
              ) : (
                <div className="row wrap" style={{ gap: 8 }}>
                  <button
                    type="button"
                    className="btn sm"
                    onClick={() => setSkillsOpen(true)}
                    disabled={matchingSkills.length === 0}
                  >
                    {skillIds.length > 0 ? '管理技能' : '选择技能'}（可选 {matchingSkills.length} 个）
                  </button>
                  {pickedSkills.map((skill) => (
                    <span className="pill" key={skill.id}>
                      {skill.name}
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => toggleSkill(skill.id)}
                        aria-label={`移除技能「${skill.name}」`}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {supportsSkills && matchingSkills.length === 0 && (
                <span className="field-help">
                  还没有属于「{selected.name}」的技能，请先到「技能」页重新扫描。
                </span>
              )}
              <span className="field-help">
                智能体能用的技能只能来自这里的分配：未勾选的技能不会被加载。换内核时，不匹配的技能会被自动移除。
              </span>
              {otherSkills > 0 && (
                <span className="field-help">另有 {otherSkills} 个技能来自其它内核，与当前内核不匹配，无法选择。</span>
              )}
            </div>

            <div className="field span-2">
              <label className="field-label">
                MCP 服务器
                <span className="dim small" style={{ marginLeft: 'auto' }}>
                  已挂 {mcpIds.length} 个
                </span>
              </label>
              {!supportsMcp ? (
                <div className="banner info">该内核不支持 MCP，这个智能体不会连任何 MCP 服务器。</div>
              ) : (
                <div className="row wrap" style={{ gap: 8 }}>
                  <button
                    type="button"
                    className="btn sm"
                    onClick={() => setMcpOpen(true)}
                    disabled={assignableMcp.length === 0}
                  >
                    {mcpIds.length > 0 ? '管理 MCP' : '选择 MCP'}（可选 {assignableMcp.length} 个）
                  </button>
                  {pickedMcp.map((server) => (
                    <span className="pill" key={server.id}>
                      🔌 {server.name}
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => toggleMcp(server.id)}
                        aria-label={`移除 MCP「${server.name}」`}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {supportsMcp && assignableMcp.length === 0 && (
                <span className="field-help">
                  目录里还没有可分配的服务器，请先到「MCP」页新建或重新扫描。
                </span>
              )}
              <span className="field-help">
                智能体能连的 MCP 服务器只能来自这里的分配：没挂上的不会被加载。换内核时，
                来自其它内核配置的服务器会被自动移除，平台通用的保留。
              </span>
              {otherMcp > 0 && (
                <span className="field-help">
                  另有 {otherMcp} 个服务器来自其它内核的配置，与当前内核不匹配，无法选择。
                </span>
              )}
            </div>

            <div className="field span-2">
              <label className="field-label">备注</label>
              <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
          </div>

          {selected && (
            <div className="stack">
              <div className="card-section-title">适配器设置</div>
              <DynamicForm
                fields={selected.config_fields}
                value={options}
                onChange={setOptions}
                advancedExtra={
                  <div className="field span-2">
                    <label className="field-label">
                      凭据
                      <span className="dim small" style={{ marginLeft: 'auto' }}>
                        已配置 {credentials.length} 条
                      </span>
                    </label>
                    <CredentialEditor drafts={credentials} library={library} onChange={setCredentials} />
                    <span className="field-help">
                      点名字输入框会列出凭据库里已有的数据：挑一条就引用它，也可以输入新名字 + 值
                      顺手新建（保存后进入凭据库）。取用方式是把值注入成环境变量，智能体直接读取，
                      值不会出现在提示词、对话记录与接口响应里；完整清单在「设置 → 凭据库」。
                      与「网关」无关 —— 网关密钥只属于模型提供方。
                    </span>
                  </div>
                }
              />
            </div>
          )}

      {mcpOpen && selected && (
        <Modal
          wide
          title={`选择 MCP 服务器 · ${selected.name}`}
          onClose={() => setMcpOpen(false)}
          footer={
            <button className="btn primary" onClick={() => setMcpOpen(false)}>
              完成（已挂 {mcpIds.length}）
            </button>
          }
        >
          <McpPicker servers={assignableMcp} selected={mcpIds} onChange={setMcpIds} />
        </Modal>
      )}

      {skillsOpen && selected && (
        <Modal
          wide
          title={`选择技能 · ${selected.name}`}
          onClose={() => setSkillsOpen(false)}
          footer={
            <button className="btn primary" onClick={() => setSkillsOpen(false)}>
              完成（已选 {skillIds.length}）
            </button>
          }
        >
          <SkillPicker skills={matchingSkills} selected={skillIds} onChange={setSkillIds} />
        </Modal>
      )}
    </Modal>
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
