import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  CredentialEditor,
  credentialsToDraft,
  draftsToPayload,
  validateCredentials,
} from '../components/CredentialEditor'
import { DynamicForm } from '../components/DynamicForm'
import { AgentAvatar } from '../components/AgentAvatar'
import { AgentAvatarPicker } from '../components/AgentAvatarPicker'
import { AgentImSettings } from '../components/AgentImSettings'
import { AgentUsagePanel } from '../components/AgentUsagePanel'
import { McpPicker } from '../components/McpPicker'
import { SkillPicker } from '../components/SkillPicker'
import { api, ApiError } from '../lib/api'
import type {
  AgentProfile,
  AgentType,
  Credential,
  Gateway,
  McpServer,
  Skill,
} from '../lib/types'

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

/** 页内标签：基础信息 / 技能 / MCP / IM / 统计。写进 `?tab=`，刷新与分享链接都落回同一处。 */
const EDIT_TABS = [
  { id: 'basic', label: '基础信息' },
  { id: 'skills', label: '技能' },
  { id: 'mcp', label: 'MCP' },
  { id: 'im', label: 'IM' },
  { id: 'usage', label: '统计' },
] as const

type EditTab = (typeof EDIT_TABS)[number]['id']

/**
 * 智能体设定页：点「编辑」跳进来的**整页**（不再是弹窗）。
 *
 * 一个智能体的全部设定按标签分组：基础信息（人格 / 内核 / 网关 / 模型 / 适配器选项 / 凭据）、
 * 技能、MCP、IM（绑定聊天软件）、统计（这个智能体的 token 用量）。
 * IM 与统计都只作用于**这一个**智能体，所以入口收在这里，而不是平台设置页。
 * 「新建智能体」也走这页：先填基础信息保存，再切换到其它标签继续配。
 */
export function AgentEditPage() {
  const routeParams = useParams<{ agentId: string }>()
  const rawId = routeParams.agentId
  /** `/agents/new` 是「新建智能体」的占位路由：还没保存过、没有真实 id。 */
  const creating = rawId === 'new'
  const agentId = creating ? null : rawId
  const navigate = useNavigate()
  const { tab, setParams } = useSearchParamsOfTab()

  const [agent, setAgent] = useState<AgentProfile | null>(null)
  const [loading, setLoading] = useState(creating ? false : true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!agentId) return
    setLoading(true)
    try {
      setAgent(await api.getAgent(agentId))
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [agentId])

  useEffect(() => {
    void load()
  }, [load])

  const setTab = (next: EditTab) => {
    setParams(next === 'basic' ? {} : { tab: next }, { replace: true })
  }

  /** 新建完成：拿到真实 id 后把地址替换成正式的设定页（replace，返回键不会落回 new）。 */
  const onCreated = (created: AgentProfile) => {
    setAgent(created)
    navigate(`/agents/${created.id}`, { replace: true })
  }

  const onSaved = async () => {
    await load()
  }

  if (creating) {
    return (
      <>
        <header className="page-header settings-head">
          <button className="btn ghost" onClick={() => navigate('/agents')} title="返回智能体列表">
            ←
          </button>
          <div style={{ minWidth: 0 }}>
            <h1 className="page-title">新建智能体</h1>
            <p className="page-desc">选择内核、网关，写下人格；保存后可继续配置技能 / MCP / IM。</p>
          </div>
        </header>
        <div className="page-body stack">
          <AgentCreatePanel onCreated={onCreated} />
        </div>
      </>
    )
  }

  if (loading && !agent) {
    return (
      <>
        <header className="page-header">
          <div className="row">
            <span className="spin">◌</span> 正在加载智能体…
          </div>
        </header>
      </>
    )
  }

  if (error && !agent) {
    return (
      <>
        <header className="page-header">
          <div>
            <h1 className="page-title">智能体设定</h1>
          </div>
        </header>
        <div className="page-body stack">
          <div className="banner err">
            <div className="row" style={{ gap: 8 }}>
              <span style={{ flex: 1 }}>{error}</span>
              <button className="btn sm" onClick={() => void load()}>
                重试
              </button>
            </div>
          </div>
          <button className="btn" onClick={() => navigate('/agents')}>
            ← 返回智能体列表
          </button>
        </div>
      </>
    )
  }

  if (!agent) return null

  return (
    <>
      <header className="page-header settings-head">
        <button className="btn ghost" onClick={() => navigate('/agents')} title="返回智能体列表">
          ←
        </button>
        <AgentAvatar
          avatar={agent.avatar}
          avatarPreset={agent.avatar_preset}
          avatarFile={agent.avatar_file}
          agentId={agent.id}
          size={40}
        />
        <div style={{ minWidth: 0 }}>
          <h1 className="page-title">{agent.name}</h1>
          <p className="page-desc">
            {agent.tagline || agent.type_name || agent.type_id} · 设定按标签分组，保存各自的设定
          </p>
        </div>
        <div className="spacer" />
        <span className={`pill ${agent.enabled ? 'ok' : 'warn'}`}>
          {agent.enabled ? '已启用' : '已停用'}
        </span>
      </header>

      <div className="settings-tabbar">
        <div className="tabs">
          {EDIT_TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`tab${tab === item.id ? ' active' : ''}`}
              aria-current={tab === item.id ? 'page' : undefined}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}
        {tab === 'basic' && <AgentBasicPanel agent={agent} onSaved={onSaved} />}
        {tab === 'skills' && <AgentSkillsPanel agent={agent} onSaved={onSaved} />}
        {tab === 'mcp' && <AgentMcpPanel agent={agent} onSaved={onSaved} />}
        {tab === 'im' && <AgentImSettings agent={agent} onChanged={load} />}
        {tab === 'usage' && <AgentUsagePanel agent={agent} />}
      </div>
    </>
  )
}

/* ---------- 新建智能体（选择内核 → 名称 → 网关 → 模型 → 人格 → 保存） ---------- */

function AgentCreatePanel({ onCreated }: { onCreated: (agent: AgentProfile) => void }) {
  const [types, setTypes] = useState<AgentType[]>([])
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [protocolLabels, setProtocolLabels] = useState<Record<string, string>>({})
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        const [typeList, gatewayList] = await Promise.all([api.agentTypes(), api.listGateways()])
        setTypes(typeList)
        setGateways(gatewayList.items)
        setProtocolLabels(
          Object.fromEntries(gatewayList.protocols.map((item) => [item.value, item.label])),
        )
        setLoadError(null)
      } catch (e) {
        setLoadError(describe(e))
      }
    })()
  }, [])

  // 「新建智能体」只列可选的内核模板（逃生舱型模板太技术化，不放在推荐入口）。
  const selectableTypes = useMemo(
    () => types.filter((type) => type.selectable !== false),
    [types],
  )
  const [typeId, setTypeId] = useState('')
  const selected = types.find((type) => type.id === typeId) ?? selectableTypes[0] ?? null
  const [name, setName] = useState('')
  const [groupName, setGroupName] = useState('')
  const [tagline, setTagline] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  // 官方链路统一显示为「官方默认」（等于平台不接管路由），因此这里归一化成空值。
  const [gatewayId, setGatewayId] = useState('')
  const [model, setModel] = useState('')
  const [options, setOptions] = useState<Record<string, unknown>>({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  // 选定内核后补齐适配器选项的默认值（换内核会重置）。
  useEffect(() => {
    if (!selected) return
    if (typeId !== selected.id) setTypeId(selected.id)
    setOptions(Object.fromEntries(selected.config_fields.map((field) => [field.key, field.default])))
    setName((prev) => prev || selected.name)
    setGatewayId('')
    setModel('')
  }, [selected, typeId])

  const gatewayChoices = useMemo(() => gatewaysFor(selected, gateways), [selected, gateways])
  const gateway = gateways.find((g) => g.id === gatewayId) ?? null
  const modelChoices = gateway?.selected_models ?? []
  const protocolMismatch = !!(gateway && selected?.protocol && gateway.protocol !== selected.protocol)

  const save = async () => {
    if (!selected) return setSaveError('请选择内核类型')
    if (!name.trim()) return setSaveError('请填写名称')
    setSaving(true)
    setSaveError(null)
    try {
      const created = await api.createAgent({
        name: name.trim(),
        group_name: groupName.trim(),
        type_id: selected.id,
        avatar: '🛠️',
        tagline: tagline.trim() || null,
        description: null,
        system_prompt: systemPrompt,
        gateway_id: gatewayId || null,
        model: model.trim() || null,
        options,
        credentials: [],
      })
      onCreated(created)
    } catch (e) {
      setSaveError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="stack">
      {loadError && <div className="banner err">{loadError}</div>}
      {saveError && <div className="banner err">{saveError}</div>}

      <div className="form-grid">
        <div className="field">
          <label className="field-label">内核类型</label>
          <select
            className="select"
            value={typeId}
            onChange={(e) => {
              const next = types.find((type) => type.id === e.target.value)
              if (next) {
                setTypeId(next.id)
                setOptions(Object.fromEntries(next.config_fields.map((f) => [f.key, f.default])))
                setGatewayId('')
                setModel('')
                setName((prev) => prev || next.name)
              }
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
              ? ' ⚠️ 该内核的运行时还没安装，可到「设置 → 内核管理」安装。'
              : ''}
          </span>
        </div>

        <div className="field">
          <label className="field-label">名称</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 代码审查员" />
        </div>

        <div className="field span-2">
          <label className="field-label">分组</label>
          <input
            className="input"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            placeholder="例如 开发 / 写作（留空就归入「未分组」）"
          />
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
      </div>

      {selected && (
        <div className="stack">
          <div className="card-section-title">适配器设置</div>
          <DynamicForm fields={selected.config_fields} value={options} onChange={setOptions} />
        </div>
      )}

      <div className="row" style={{ gap: 10 }}>
        <button className="btn primary" onClick={() => void save()} disabled={saving || !selected}>
          {saving ? '创建中…' : '创建智能体'}
        </button>
        <span className="dim small">创建后自动进入设定页，可以继续配头像、技能、MCP 与 IM。</span>
      </div>
    </section>
  )
}

/* ---------- 基础信息（含内核 / 网关 / 模型 / 适配器选项 / 凭据） ---------- */

function AgentBasicPanel({ agent, onSaved }: { agent: AgentProfile; onSaved: () => Promise<void> }) {
  const [types, setTypes] = useState<AgentType[]>([])
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [protocolLabels, setProtocolLabels] = useState<Record<string, string>>({})
  const [groups, setGroups] = useState<string[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        const [typeList, gatewayList, credentialList, groupList] = await Promise.all([
          api.agentTypes(),
          api.listGateways(),
          api.listCredentials(),
          api.listAgentGroups(),
        ])
        setTypes(typeList)
        setGateways(gatewayList.items)
        setCredentials(credentialList.items)
        setProtocolLabels(
          Object.fromEntries(gatewayList.protocols.map((item) => [item.value, item.label])),
        )
        setGroups(groupList)
        setLoadError(null)
      } catch (e) {
        setLoadError(describe(e))
      }
    })()
  }, [])

  const [name, setName] = useState(agent.name)
  const [avatar, setAvatar] = useState(agent.avatar)
  const [avatarPreset, setAvatarPreset] = useState<string | null>(agent.avatar_preset ?? null)
  const [avatarFileId, setAvatarFileId] = useState<string | null>(agent.avatar_file ?? null)
  const [groupName, setGroupName] = useState(agent.group_name ?? '')
  const [groupSuggestOpen, setGroupSuggestOpen] = useState(false)
  const [tagline, setTagline] = useState(agent.tagline ?? '')
  const [description, setDescription] = useState(agent.description ?? '')
  const [systemPrompt, setSystemPrompt] = useState(agent.system_prompt ?? '')
  // 官方链路统一显示为「官方默认」（等于平台不接管路由），因此这里归一化成空值。
  const [gatewayId, setGatewayId] = useState(
    agent.gateway_kind !== 'official' ? agent.gateway_id ?? '' : '',
  )
  const [model, setModel] = useState(agent.model ?? '')
  const [options, setOptions] = useState<Record<string, unknown>>(agent.options ?? {})
  const [credentialDrafts, setCredentialDrafts] = useState(() =>
    credentialsToDraft(agent.credentials),
  )
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedHint, setSavedHint] = useState(false)

  const selected = types.find((type) => type.id === agent.type_id) ?? null

  const gatewayChoices = useMemo(() => gatewaysFor(selected, gateways), [selected, gateways])
  const gateway = gateways.find((g) => g.id === gatewayId) ?? null
  const modelChoices = gateway?.selected_models ?? []
  const protocolMismatch = !!(gateway && selected?.protocol && gateway.protocol !== selected.protocol)

  /** 保存：基础信息整包 PATCH；头像图已在 AgentAvatarPicker 的上传接口里落库，这里只存引用值。 */
  const save = async () => {
    if (!name.trim()) return setSaveError('请填写名称')
    const credentialError = validateCredentials(credentialDrafts, credentials)
    if (credentialError) return setSaveError(credentialError)
    setSaving(true)
    setSaveError(null)
    try {
      await api.updateAgent(agent.id, {
        name: name.trim(),
        group_name: groupName.trim(),
        type_id: agent.type_id,
        avatar,
        avatar_preset: avatarPreset,
        tagline: tagline.trim() || null,
        description: description.trim() || null,
        system_prompt: systemPrompt,
        gateway_id: gatewayId || null,
        model: model.trim() || null,
        options,
        credentials: draftsToPayload(credentialDrafts),
      })
      setSavedHint(true)
      window.setTimeout(() => setSavedHint(false), 2000)
      await onSaved()
    } catch (e) {
      setSaveError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="stack">
      {loadError && <div className="banner err">{loadError}</div>}
      {saveError && <div className="banner err">{saveError}</div>}
      {savedHint && <div className="banner ok">已保存 ✓</div>}

      <AgentAvatarPicker
        agentId={agent.id}
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
          <input className="input" value={agent.type_name ?? agent.type_id} readOnly />
          <span className="field-help">
            {selected?.description}
            {selected && !selected.availability.installed
              ? ' ⚠️ 该内核的运行时还没安装，可到「设置 → 内核管理」安装。'
              : ''}
            换内核请到智能体列表「复制」后新建。
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
              {model && !modelChoices.includes(model) && (
                <option value={model}>{model}（不在白名单）</option>
              )}
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
                    已配置 {credentialDrafts.length} 条
                  </span>
                </label>
                <CredentialEditor drafts={credentialDrafts} library={credentials} onChange={setCredentialDrafts} />
                <span className="field-help">
                  点名字输入框会列出凭据库里已有的数据：挑一条就引用它，也可以输入新名字 + 值
                  顺手新建（保存后进入凭据库）。取用方式是把值注入成环境变量，智能体直接读取，
                  值不会出现在提示词、对话记录与接口响应里；完整清单在「设置 → 凭据」。
                  与「网关」无关 —— 网关密钥只属于模型提供方。
                </span>
              </div>
            }
          />
        </div>
      )}

      <div className="row" style={{ gap: 10 }}>
        <button className="btn primary" onClick={() => void save()} disabled={saving}>
          {saving ? '保存中…' : '保存基础信息'}
        </button>
        <span className="dim small">技能 / MCP / IM 的分配在各自标签里单独保存。</span>
      </div>
    </section>
  )
}

/* ---------- 技能 ---------- */

function AgentSkillsPanel({ agent, onSaved }: { agent: AgentProfile; onSaved: () => Promise<void> }) {
  const [skills, setSkills] = useState<Skill[]>([])
  const [types, setTypes] = useState<AgentType[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [skillIds, setSkillIds] = useState<string[]>(agent.skill_ids ?? [])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const [skillList, typeList] = await Promise.all([api.listSkills(), api.agentTypes()])
      setSkills(skillList)
      setTypes(typeList)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const selected = types.find((type) => type.id === agent.type_id) ?? null
  const supportsSkills = !!selected?.capabilities.skills
  const matchingSkills = useMemo(
    () => skills.filter((skill) => skill.type_id === agent.type_id),
    [skills, agent.type_id],
  )
  const pickedSkills = useMemo(
    () => matchingSkills.filter((skill) => skillIds.includes(skill.id)),
    [matchingSkills, skillIds],
  )
  const otherSkills = skills.length - matchingSkills.length

  const toggleSkill = (id: string) =>
    setSkillIds((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))

  const save = async () => {
    setSaving(true)
    try {
      // 技能分配整表替换：未勾选的技能对这次运行不生效。
      await api.setAgentSkills(agent.id, skillIds)
      await onSaved()
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="stack">
      {error && <div className="banner err">{error}</div>}
      {!supportsSkills ? (
        <div className="banner info">该内核不支持技能，这个智能体不会加载任何技能。</div>
      ) : (
        <>
          {loading && <div className="banner info">正在加载技能目录…</div>}
          <div className="row wrap" style={{ gap: 8 }}>
            <button
              type="button"
              className="btn sm"
              onClick={() => setPickerOpen(true)}
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
          {matchingSkills.length === 0 && !loading && (
            <span className="field-help">
              还没有属于「{selected?.name}」的技能，请先到「技能」页重新扫描。
            </span>
          )}
          <span className="field-help">
            智能体能用的技能只能来自这里的分配：未勾选的技能不会被加载。
          </span>
          {otherSkills > 0 && (
            <span className="field-help">另有 {otherSkills} 个技能来自其它内核，与当前内核不匹配，无法选择。</span>
          )}
          <div className="row" style={{ gap: 10 }}>
            <button className="btn primary" onClick={() => void save()} disabled={saving}>
              {saving ? '保存中…' : `保存技能分配（已选 ${skillIds.length}）`}
            </button>
          </div>
        </>
      )}
      {pickerOpen && (
        <div className="card" style={{ gap: 10 }}>
          <div className="row" style={{ gap: 8 }}>
            <div className="card-section-title" style={{ margin: 0, flex: 1 }}>
              选择技能 · {selected?.name}
            </div>
            <button className="btn sm" onClick={() => setPickerOpen(false)}>
              收起
            </button>
          </div>
          <SkillPicker skills={matchingSkills} selected={skillIds} onChange={setSkillIds} />
        </div>
      )}
    </section>
  )
}

/* ---------- MCP ---------- */

function AgentMcpPanel({ agent, onSaved }: { agent: AgentProfile; onSaved: () => Promise<void> }) {
  const [servers, setServers] = useState<McpServer[]>([])
  const [types, setTypes] = useState<AgentType[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [mcpIds, setMcpIds] = useState<string[]>(agent.mcp_ids ?? [])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const [serverList, typeList] = await Promise.all([api.listMcpServers(), api.agentTypes()])
      setServers(serverList)
      setTypes(typeList)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const selected = types.find((type) => type.id === agent.type_id) ?? null
  const supportsMcp = !!selected?.capabilities.mcp
  // 平台通用的服务器（type_id 为空）可以分配给任意内核；扫描来的只能给同内核。
  const assignable = useMemo(
    () => servers.filter((server) => !server.type_id || server.type_id === agent.type_id),
    [servers, agent.type_id],
  )
  const picked = useMemo(
    () => assignable.filter((server) => mcpIds.includes(server.id)),
    [assignable, mcpIds],
  )
  const otherMcp = servers.length - assignable.length

  const toggleMcp = (id: string) =>
    setMcpIds((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))

  const save = async () => {
    setSaving(true)
    try {
      // MCP 分配整表替换：没挂上的服务器对这次运行不生效。
      await api.setAgentMcpServers(agent.id, mcpIds)
      await onSaved()
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="stack">
      {error && <div className="banner err">{error}</div>}
      {!supportsMcp ? (
        <div className="banner info">该内核不支持 MCP，这个智能体不会连任何 MCP 服务器。</div>
      ) : (
        <>
          {loading && <div className="banner info">正在加载 MCP 目录…</div>}
          <div className="row wrap" style={{ gap: 8 }}>
            <button
              type="button"
              className="btn sm"
              onClick={() => setPickerOpen(true)}
              disabled={assignable.length === 0}
            >
              {mcpIds.length > 0 ? '管理 MCP' : '选择 MCP'}（可选 {assignable.length} 个）
            </button>
            {picked.map((server) => (
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
          {assignable.length === 0 && !loading && (
            <span className="field-help">
              目录里还没有可分配的服务器，请先到「MCP」页新建或重新扫描。
            </span>
          )}
          <span className="field-help">
            智能体能连的 MCP 服务器只能来自这里的分配：没挂上的不会被加载；平台通用的服务器可以分给任意内核。
          </span>
          {otherMcp > 0 && (
            <span className="field-help">另有 {otherMcp} 个服务器来自其它内核的配置，与当前内核不匹配，无法选择。</span>
          )}
          <div className="row" style={{ gap: 10 }}>
            <button className="btn primary" onClick={() => void save()} disabled={saving}>
              {saving ? '保存中…' : `保存 MCP 分配（已挂 ${mcpIds.length}）`}
            </button>
          </div>
        </>
      )}
      {pickerOpen && (
        <div className="card" style={{ gap: 10 }}>
          <div className="row" style={{ gap: 8 }}>
            <div className="card-section-title" style={{ margin: 0, flex: 1 }}>
              选择 MCP 服务器 · {selected?.name}
            </div>
            <button className="btn sm" onClick={() => setPickerOpen(false)}>
              收起
            </button>
          </div>
          <McpPicker servers={assignable} selected={mcpIds} onChange={setMcpIds} />
        </div>
      )}
    </section>
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

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}

/* ---------- 小工具：页内 ?tab= 的受控封装 ---------- */

function useSearchParamsOfTab() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab: EditTab = EDIT_TABS.some((item) => item.id === raw) ? (raw as EditTab) : 'basic'
  return { tab, setParams }
}
