import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CredentialEditor,
  credentialsToDraft,
  draftsToPayload,
  validateCredentials,
} from '../../components/CredentialEditor'
import { DynamicForm } from '../../components/DynamicForm'
import { AgentAvatar } from '../../components/AgentAvatar'
import { AVATAR_PRESET_GROUPS, avatarPresetUrl, randomAvatarPreset } from '../../lib/agentAvatar'
import { CollapsibleGroup } from '../../components/CollapsibleGroup'
import { StarIcon } from '../../components/Icons'
import { McpPicker } from '../../components/McpPicker'
import { SkillPicker } from '../../components/SkillPicker'
import { api } from '../../lib/api'
import { groupAgents } from '../../lib/agentGroups'
import { useExpandMap } from '../../lib/collapse'
import type {
  AgentProfile,
  AgentType,
  Credential,
  Gateway,
  McpServer,
  Skill,
  TestResult,
} from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const AVATARS = ['🛠️', '🧪', '🔎', '🐳', '🔗', '🎨', '📚', '🧠', '⚡', '🛰️', '🧭', '🦾']

/** 智能体：内核 + 网关 + 模型 + 人格设定；技能只能通过智能体分配获得。 */
export function MobileAgentsPage() {
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [credentialLibrary, setCredentialLibrary] = useState<Credential[]>([])
  const [protocolLabels, setProtocolLabels] = useState<Record<string, string>>({})
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcpServers, setMcpServers] = useState<McpServer[]>([])
  const [agentTypes, setAgentTypes] = useState<AgentType[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<AgentProfile | null>(null)
  const [creating, setCreating] = useState(false)
  const [testing, setTesting] = useState<{ agent: AgentProfile; result: TestResult | null; busy: boolean } | null>(null)

  const reload = useCallback(async () => {
    try {
      const [agentList, gatewayList, skillList, typeList, credentialList, mcpList] =
        await Promise.all([
          api.listAgents(),
          api.listGateways(),
          api.listSkills(),
          api.agentTypes(),
          api.listCredentials(),
          api.listMcpServers(),
        ])
      setAgents(agentList)
      setGateways(gatewayList.items)
      setCredentialLibrary(credentialList.items)
      setProtocolLabels(Object.fromEntries(gatewayList.protocols.map((item) => [item.value, item.label])))
      setSkills(skillList)
      setMcpServers(mcpList)
      setAgentTypes(typeList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  /** 按分组折叠展示（默认展开：一进页面就能看到卡片，想收起来再点标题）。 */
  const { isExpanded, toggle } = useExpandMap(true)
  const groups = useMemo(() => groupAgents(agents), [agents])

  const duplicate = async (agent: AgentProfile) => {
    try {
      await api.duplicateAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const remove = async (agent: AgentProfile) => {
    if (!confirm(`删除智能体「${agent.name}」？绑定它的对话会保留在历史里。`)) return
    try {
      await api.deleteAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  /** 启停：只改 enabled，不动其它配置（停用后仍保留配置、历史对话照旧可读）。 */
  const toggleEnabled = async (agent: AgentProfile) => {
    try {
      await api.updateAgent(agent.id, { enabled: !agent.enabled })
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  /** 点卡片右上角的五角星：设为默认智能体（再点一下取消），全局只有一颗。 */
  const toggleDefault = async (agent: AgentProfile) => {
    try {
      await api.updateAgent(agent.id, { is_default: !agent.is_default })
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const runTest = async (agent: AgentProfile) => {
    setTesting({ agent, result: null, busy: true })
    try {
      const result = await api.testAgent(agent.id)
      setTesting({ agent, result, busy: false })
    } catch (e) {
      setTesting({ agent, result: { ok: false, message: describeError(e), checks: [] }, busy: false })
    }
  }
  /** 一张智能体卡片（分组之后嵌在 `CollapsibleGroup` → `.m-list` 里）。 */
  const renderAgentCard = (agent: AgentProfile) => (
    <div className="card" key={agent.id}>
      <div className="row">
        <AgentAvatar
          avatar={agent.avatar}
          avatarPreset={agent.avatar_preset}
          avatarFile={agent.avatar_file}
          agentId={agent.id}
          size={40}
        />
        <div className="m-item-main">
          <div className="m-item-title">{agent.name}</div>
          <div className="m-item-sub">
            {agent.type_name ?? agent.type_id} ·{' '}
            {agent.gateway_kind === 'official' || !agent.gateway_name ? '官方默认' : agent.gateway_name}
          </div>
          <div className="dim small">模型 {agent.model ?? '默认'}</div>
        </div>
        {agent.skills.length > 0 && agent.enabled && (
          <span className="pill">{agent.skills.length} 技能</span>
        )}
        {!agent.enabled && <span className="pill warn">已停用</span>}
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
          title={agent.is_default ? '默认智能体（点一下取消）' : '设为默认智能体'}
        >
          <StarIcon size={18} on={agent.is_default} />
        </button>
      </div>
      <div className="m-item-actions" style={{ marginTop: 0 }}>
        <button className="btn sm" onClick={() => void runTest(agent)}>
          测试
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
    </div>
  )

  return (
    <div className="m-page">
      <MobileTopBar
        title="智能体"
        subtitle="内核 + 网关 + 模型 + 人格"
        actions={
          <button className="m-icon-btn" onClick={() => setCreating(true)} aria-label="新建智能体">
            ＋
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          {agents.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🤖</div>
              <p>还没有智能体</p>
              <button className="btn primary" onClick={() => setCreating(true)}>
                ＋ 新建智能体
              </button>
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
                  <div className="m-list">{group.items.map(renderAgentCard)}</div>
                </CollapsibleGroup>
              ))}
            </div>
          )}
        </div>
      </div>

      {(creating || editing) && (
        <MobileSheet
          full
          title={editing ? `编辑智能体 · ${editing.name}` : '新建智能体'}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
        >
          <AgentForm
            agent={editing}
            gateways={gateways}
            protocolLabels={protocolLabels}
            skills={skills}
            mcpServers={mcpServers}
            agentTypes={agentTypes}
            library={credentialLibrary}
            onSaved={async () => {
              setCreating(false)
              setEditing(null)
              await reload()
            }}
          />
        </MobileSheet>
      )}

      {testing && (
        <MobileSheet title={`连通性测试 · ${testing.agent.name}`} onClose={() => setTesting(null)}>
          {testing.busy ? (
            <p className="dim small">正在探测…</p>
          ) : (
            <div className="stack" style={{ gap: 10 }}>
              <div className={`banner ${testing.result?.ok ? 'ok' : 'err'}`}>
                {testing.result?.ok ? '✅ 配置可用' : '❌ 存在问题'}：{testing.result?.message}
              </div>
              {testing.result?.checks.map((check) => (
                <div className="m-item" key={check.name} style={{ cursor: 'default' }}>
                  <span>{check.ok ? '✅' : '❌'}</span>
                  <span className="m-item-main">
                    <span className="m-item-title">{check.name}</span>
                    <span className="m-item-sub">{check.detail}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </MobileSheet>
      )}
    </div>
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

function AgentForm({
  agent,
  gateways,
  protocolLabels,
  skills,
  mcpServers,
  agentTypes,
  library,
  onSaved,
}: {
  agent: AgentProfile | null
  gateways: Gateway[]
  protocolLabels: Record<string, string>
  skills: Skill[]
  mcpServers: McpServer[]
  agentTypes: AgentType[]
  library: Credential[]
  onSaved: () => void
}) {
  // 与桌面端同一规则：新建只列可选模板，编辑存量智能体时保留它自己的内核。
  const selectableTypes = useMemo(() => {
    const usable = agentTypes.filter((type) => type.selectable !== false)
    if (agent && !usable.some((type) => type.id === agent.type_id)) {
      const current = agentTypes.find((type) => type.id === agent.type_id)
      if (current) return [...usable, current]
    }
    return usable
  }, [agentTypes, agent])

  const [typeId, setTypeId] = useState<string>(
    agent?.type_id ?? selectableTypes[0]?.id ?? agentTypes[0]?.id ?? '',
  )
  const [name, setName] = useState(agent?.name ?? '')
  const [avatar, setAvatar] = useState(agent?.avatar ?? AVATARS[0])
  /** 内置头像包编号：随机生成 / 画廊挑选写这里，随表单一并保存 */
  const [avatarPreset, setAvatarPreset] = useState<string | null>(agent?.avatar_preset ?? null)
  const [groupName, setGroupName] = useState(agent?.group_name ?? '')
  const [tagline, setTagline] = useState(agent?.tagline ?? '')
  const [description, setDescription] = useState(agent?.description ?? '')
  const [systemPrompt, setSystemPrompt] = useState(agent?.system_prompt ?? '')
  // 官方链路统一显示为「官方默认」（等于平台不接管路由），因此这里归一化成空值。
  const [gatewayId, setGatewayId] = useState(
    agent && agent.gateway_kind !== 'official' ? agent.gateway_id ?? '' : '',
  )
  const [model, setModel] = useState(agent?.model ?? '')
  const [skillIds, setSkillIds] = useState<string[]>(agent?.skill_ids ?? [])
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [mcpIds, setMcpIds] = useState<string[]>(agent?.mcp_ids ?? [])
  const [mcpOpen, setMcpOpen] = useState(false)
  const [options, setOptions] = useState<Record<string, unknown>>(agent?.options ?? {})
  const [credentialDrafts, setCredentialDrafts] = useState(() => credentialsToDraft(agent?.credentials))
  const [saving, setSaving] = useState(false)
  /** 头像包画廊的开合：60 张缩略图没必要常驻 */
  const [galleryOpen, setGalleryOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const selected = agentTypes.find((type) => type.id === typeId) ?? null

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
  const gateway = gateways.find((item) => item.id === gatewayId) ?? null
  const modelChoices = gateway?.selected_models ?? []
  // 自定义网关的协议与内核期望的协议不一致时提醒（不拦，由用户判断代理是否兼容）。
  const protocolMismatch = !!(gateway && selected?.protocol && gateway.protocol !== selected.protocol)
  const supportsSkills = selected?.capabilities.skills ?? false
  const matchingSkills = skills.filter((skill) => skill.type_id === typeId)
  const pickedSkills = matchingSkills.filter((skill) => skillIds.includes(skill.id))
  const otherSkills = skills.length - matchingSkills.length
  const supportsMcp = selected?.capabilities.mcp ?? false
  // 平台通用的服务器（type_id 为空）可以分配给任意内核；扫描来的只能给同内核。
  const assignableMcp = mcpServers.filter((server) => !server.type_id || server.type_id === typeId)
  const pickedMcp = assignableMcp.filter((server) => mcpIds.includes(server.id))
  const otherMcp = mcpServers.length - assignableMcp.length

  const chooseType = (nextType: AgentType) => {
    if (nextType.id === typeId) return
    setTypeId(nextType.id)
    setOptions(Object.fromEntries(nextType.config_fields.map((field) => [field.key, field.default])))
    setGatewayId('')
    setModel('')
    setSkillIds([])
    setMcpIds([])
    if (!agent) setName((prev) => prev || nextType.name)
  }

  const toggleSkill = (skillId: string) =>
    setSkillIds((current) =>
      current.includes(skillId) ? current.filter((id) => id !== skillId) : [...current, skillId],
    )

  const toggleMcp = (mcpId: string) =>
    setMcpIds((current) =>
      current.includes(mcpId) ? current.filter((id) => id !== mcpId) : [...current, mcpId],
    )

  const save = async () => {
    if (!name.trim()) return setError('请填写智能体名称')
    if (!selected) return setError('请选择内核类型')
    const credentialError = validateCredentials(credentialDrafts, library)
    if (credentialError) return setError(credentialError)
    setSaving(true)
    setError(null)
    try {
      const payload = {
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
        credentials: draftsToPayload(credentialDrafts),
      }
      // 技能分配紧随智能体保存：未分配的技能对这次运行不生效。
      const saved = agent ? await api.updateAgent(agent.id, payload) : await api.createAgent(payload)
      await api.setAgentSkills(saved.id, skillIds)
      // MCP 分配同理：没挂上的服务器对这次运行不生效。
      await api.setAgentMcpServers(saved.id, mcpIds)
      onSaved()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      {error && <div className="banner err">{error}</div>}

      <div className="field">
        <label className="field-label">名称</label>
        <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 代码审查员" />
      </div>

      <div className="field">
        <label className="field-label">分组</label>
        <input
          className="input"
          value={groupName}
          onChange={(event) => setGroupName(event.target.value)}
          placeholder="例如 开发 / 写作（留空就归入「未分组」）"
        />
        <span className="field-help">智能体页按它折叠分组展示；留空表示未分组，永远排在最后。</span>
      </div>

      <div className="field">
        <label className="field-label">头像</label>
        <div className="row" style={{ gap: 12, alignItems: 'center' }}>
          <AgentAvatar
            avatar={avatar}
            avatarPreset={avatarPreset}
            size={56}
            className="agent-avatar-preview"
          />
          <button
            type="button"
            className="btn sm"
            onClick={() => setAvatarPreset(randomAvatarPreset(avatarPreset))}
          >
            🎲 随机生成
          </button>
          <button type="button" className="btn sm ghost" onClick={() => setGalleryOpen((open) => !open)}>
            {galleryOpen ? '收起画廊' : '从头像包挑选'}
          </button>
        </div>
        {galleryOpen && (
          <div className="agent-avatar-gallery">
            {AVATAR_PRESET_GROUPS.map((group) => (
              <div key={group.key} className="agent-avatar-group">
                <span className="agent-avatar-group-label">{group.label}</span>
                <div className="agent-avatar-group-grid">
                  {group.presets.map((item) => (
                    <button
                      key={item}
                      type="button"
                      className={`agent-avatar-emoji-btn${item === avatarPreset ? ' picked' : ''}`}
                      onClick={() => setAvatarPreset(item)}
                      aria-label={`${group.label}头像 ${item}`}
                    >
                      <img src={avatarPresetUrl(item)} alt="" loading="lazy" />
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="row wrap" style={{ gap: 6, marginTop: 8 }}>
          {AVATARS.map((item) => (
            <button
              key={item}
              type="button"
              className={`btn sm${item === avatar && !avatarPreset ? ' primary' : ''}`}
              onClick={() => {
                setAvatar(item)
                setAvatarPreset(null)
              }}
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      <div className="field">
        <label className="field-label">内核类型</label>
        <select className="select" value={typeId} onChange={(event) => {
          const next = agentTypes.find((item) => item.id === event.target.value)
          if (next) chooseType(next)
        }}>
          {selectableTypes.map((type) => (
            <option key={type.id} value={type.id}>
              {type.name} · {type.vendor}
            </option>
          ))}
        </select>
        {selected && !selected.availability.installed && (
          <span className="field-help">⚠️ 该内核的运行时还没安装，可到「运行时」页安装。</span>
        )}
      </div>

      <div className="field">
        <label className="field-label">网关</label>
        <select className="select" value={gatewayId} onChange={(event) => setGatewayId(event.target.value)}>
          <option value="">官方默认（平台不接管路由）</option>
          {gatewayChoices.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name} · {item.base_url}
              {protocolLabels[item.protocol] ? ` · ${protocolLabels[item.protocol]}` : ''}
            </option>
          ))}
        </select>
        <span className="field-help">
          选「官方默认」= 平台不接管路由，沿用内核自带的网关与凭据；自定义网关在「设置 → 自定义网关」里维护。
          {gateway && !gateway.api_key_set ? ' ⚠️ 该网关还没配置密钥。' : ''}
          {protocolMismatch
            ? ` ⚠️ 该网关协议是 ${protocolLabels[gateway!.protocol] ?? gateway!.protocol}，与内核期望的 ${
                protocolLabels[selected!.protocol ?? ''] ?? selected!.protocol
              } 不一致，运行可能失败。`
            : ''}
        </span>
      </div>

      <div className="field">
        <label className="field-label">模型</label>
        {gateway && modelChoices.length > 0 ? (
          <select className="select" value={model} onChange={(event) => setModel(event.target.value)}>
            <option value="">默认（{selected?.default_model ?? '内核默认'}）</option>
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
            onChange={(event) => setModel(event.target.value)}
            placeholder={selected?.default_model ?? '例如 sonnet / gpt-5-codex'}
          />
        )}
        <span className="field-help">
          新对话默认使用的模型。
          {gateway ? '只允许选择该网关白名单里的模型。' : '官方默认时可直接填写模型名，留空则用内核默认。'}
        </span>
      </div>

      <div className="field">
        <label className="field-label">一句话简介</label>
        <input
          className="input"
          value={tagline}
          onChange={(event) => setTagline(event.target.value)}
          placeholder="显示在对话列表里"
        />
      </div>

      <div className="field">
        <label className="field-label">系统提示词</label>
        <textarea
          className="textarea"
          value={systemPrompt}
          onChange={(event) => setSystemPrompt(event.target.value)}
          placeholder="描述这个智能体的人格、工作方式与输出要求"
        />
      </div>

      <div className="field">
        <label className="field-label">
          技能<span className="dim small">已选 {skillIds.length} 个</span>
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
          <span className="field-help">还没有属于这个内核的技能，请先到「技能」页重新扫描。</span>
        )}
        <span className="field-help">未勾选的技能不会被这个智能体加载。</span>
        {otherSkills > 0 && (
          <span className="field-help">另有 {otherSkills} 个技能来自其它内核，无法选择。</span>
        )}
      </div>

      <div className="field">
        <label className="field-label">
          MCP 服务器<span className="dim small">已挂 {mcpIds.length} 个</span>
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
          <span className="field-help">目录里还没有可分配的服务器，请先到「MCP」页新建或重新扫描。</span>
        )}
        <span className="field-help">
          未挂上的 MCP 服务器不会被加载；换内核时来自其它内核配置的会被自动移除，平台通用的保留。
        </span>
        {otherMcp > 0 && (
          <span className="field-help">另有 {otherMcp} 个服务器来自其它内核，无法选择。</span>
        )}
      </div>

      <div className="field">
        <label className="field-label">备注</label>
        <input className="input" value={description} onChange={(event) => setDescription(event.target.value)} />
      </div>

      {selected && (
        <div className="stack">
          <div className="card-section-title">适配器设置</div>
          <DynamicForm
            fields={selected.config_fields}
            value={options}
            onChange={setOptions}
            advancedExtra={
              <div className="field">
                <label className="field-label">
                  凭据<span className="dim small">已配置 {credentialDrafts.length} 条</span>
                </label>
                <CredentialEditor drafts={credentialDrafts} library={library} onChange={setCredentialDrafts} />
                <span className="field-help">
                  点名字输入框会列出凭据库里已有的数据：挑一条就引用它，也可以输入新名字 + 值顺手新建。
                  值以环境变量注入，智能体直接读取，不会出现在提示词、对话记录与接口响应里。
                </span>
              </div>
            }
          />
        </div>
      )}

      <button className="btn primary block" onClick={() => void save()} disabled={saving}>
        {saving ? '保存中…' : '保存'}
      </button>

      {mcpOpen && selected && (
        <MobileSheet
          full
          title={`选择 MCP 服务器 · ${selected.name}`}
          onClose={() => setMcpOpen(false)}
          footer={
            <button className="btn primary block" onClick={() => setMcpOpen(false)}>
              完成（已挂 {mcpIds.length}）
            </button>
          }
        >
          <McpPicker servers={assignableMcp} selected={mcpIds} onChange={setMcpIds} />
        </MobileSheet>
      )}

      {skillsOpen && selected && (
        <MobileSheet
          full
          title={`选择技能 · ${selected.name}`}
          onClose={() => setSkillsOpen(false)}
          footer={
            <button className="btn primary block" onClick={() => setSkillsOpen(false)}>
              完成（已选 {skillIds.length}）
            </button>
          }
        >
          <SkillPicker skills={matchingSkills} selected={skillIds} onChange={setSkillIds} />
        </MobileSheet>
      )}
    </>
  )
}
