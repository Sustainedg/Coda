import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  CredentialEditor,
  credentialsToDraft,
  draftsToPayload,
  validateCredentials,
} from '../../components/CredentialEditor'
import { DynamicForm } from '../../components/DynamicForm'
import { AgentAvatar } from '../../components/AgentAvatar'
import { AVATAR_PRESET_GROUPS, avatarPresetUrl, randomAvatarPreset } from '../../lib/agentAvatar'
import { AgentImSettings } from '../../components/AgentImSettings'
import { AgentUsagePanel } from '../../components/AgentUsagePanel'
import { McpPicker } from '../../components/McpPicker'
import { SkillPicker } from '../../components/SkillPicker'
import { api } from '../../lib/api'
import type {
  AgentProfile,
  AgentType,
  Credential,
  Gateway,
  McpServer,
  Skill,
} from '../../lib/types'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/** 页内标签：基础信息 / 技能 / MCP / IM / 统计（与桌面端 AgentEditPage 同一组）。 */
const EDIT_TABS = [
  { id: 'basic', label: '基础信息' },
  { id: 'skills', label: '技能' },
  { id: 'mcp', label: 'MCP' },
  { id: 'im', label: 'IM' },
  { id: 'usage', label: '统计' },
] as const

type EditTab = (typeof EDIT_TABS)[number]['id']

/**
 * 智能体设定页（移动端）：整页 + 页内标签，与桌面端 `pages/AgentEditPage` 功能对齐、
 * 交互按触屏重排。原「编辑弹层」改为独立路由页 —— 技能 / MCP / IM / 统计都是
 * 独立的一块设定，弹层塞不下；「新建」也走这页（`/agents/new` 占位路由）。
 */
export function MobileAgentEditPage() {
  const rawId = useParams<{ agentId: string }>().agentId
  const creating = rawId === 'new'
  const agentId = creating ? null : rawId
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const rawTab = params.get('tab')
  const tab: EditTab = EDIT_TABS.some((item) => item.id === rawTab) ? (rawTab as EditTab) : 'basic'

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
      setError(describeError(e))
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

  /** 新建完成：拿到真实 id 后把地址替换成正式的设定页。 */
  const onCreated = (created: AgentProfile) => {
    setAgent(created)
    navigate(`/agents/${created.id}`, { replace: true })
  }

  if (creating) {
    return (
      <div className="m-page">
        <MobileTopBar title="新建智能体" subtitle="内核 + 网关 + 人格" onBack={() => navigate('/agents')} />
        <div className="m-page-body stack">
          <AgentCreatePanel onCreated={onCreated} />
        </div>
      </div>
    )
  }

  if (loading && !agent) {
    return (
      <div className="m-page">
        <MobileTopBar title="智能体设定" onBack={() => navigate('/agents')} />
        <div className="m-page-body">
          <p className="dim small">正在加载…</p>
        </div>
      </div>
    )
  }

  if (error && !agent) {
    return (
      <div className="m-page">
        <MobileTopBar title="智能体设定" onBack={() => navigate('/agents')} />
        <div className="m-page-body stack">
          <div className="banner err">{error}</div>
          <button className="btn" onClick={() => navigate('/agents')}>
            ← 返回智能体列表
          </button>
        </div>
      </div>
    )
  }

  if (!agent) return null

  return (
    <div className="m-page">
      <MobileTopBar
        title={agent.name}
        subtitle={agent.type_name ?? agent.type_id}
        onBack={() => navigate('/agents')}
        leading={<AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} avatarFile={agent.avatar_file} agentId={agent.id} size={26} />}
      />
      <div className="m-page-body stack">
        <div className="tabs">
          {EDIT_TABS.map((item) => (
            <button
              key={item.id}
              className={`tab${tab === item.id ? ' active' : ''}`}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        {error && <div className="banner err">{error}</div>}
        {tab === 'basic' && <AgentBasicPanel agent={agent} onSaved={load} />}
        {tab === 'skills' && <AgentSkillsPanel agent={agent} onSaved={load} />}
        {tab === 'mcp' && <AgentMcpPanel agent={agent} onSaved={load} />}
        {tab === 'im' && <AgentImSettings agent={agent} onChanged={load} />}
        {tab === 'usage' && <AgentUsagePanel agent={agent} />}
      </div>
    </div>
  )
}

/* ---------- 新建智能体 ---------- */

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
        setLoadError(describeError(e))
      }
    })()
  }, [])

  const selectableTypes = useMemo(() => types.filter((type) => type.selectable !== false), [types])
  const [typeId, setTypeId] = useState('')
  const selected = types.find((type) => type.id === typeId) ?? selectableTypes[0] ?? null
  const [name, setName] = useState('')
  const [groupName, setGroupName] = useState('')
  const [tagline, setTagline] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [gatewayId, setGatewayId] = useState('')
  const [model, setModel] = useState('')
  const [options, setOptions] = useState<Record<string, unknown>>({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

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
      setSaveError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      {loadError && <div className="banner err">{loadError}</div>}
      {saveError && <div className="banner err">{saveError}</div>}

      <div className="field">
        <label className="field-label">内核类型</label>
        <select
          className="select"
          value={typeId}
          onChange={(event) => {
            const next = types.find((type) => type.id === event.target.value)
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
            </option>
          ))}
        </select>
        {selected && !selected.availability.installed && (
          <span className="field-help">⚠️ 该内核的运行时还没安装，可到「内核管理」页安装。</span>
        )}
      </div>

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
          </select>
        ) : (
          <input
            className="input mono"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder={selected?.default_model ?? '例如 sonnet / gpt-5-codex'}
          />
        )}
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

      {selected && (
        <div className="stack">
          <div className="card-section-title">适配器设置</div>
          <DynamicForm fields={selected.config_fields} value={options} onChange={setOptions} />
        </div>
      )}

      <button className="btn primary block" onClick={() => void save()} disabled={saving || !selected}>
        {saving ? '创建中…' : '创建智能体'}
      </button>
    </div>
  )
}

/* ---------- 基础信息 ---------- */

function AgentBasicPanel({ agent, onSaved }: { agent: AgentProfile; onSaved: () => Promise<void> }) {
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [types, setTypes] = useState<AgentType[]>([])
  const [protocolLabels, setProtocolLabels] = useState<Record<string, string>>({})
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        const [gatewayList, credentialList, typeList] = await Promise.all([
          api.listGateways(),
          api.listCredentials(),
          api.agentTypes(),
        ])
        setGateways(gatewayList.items)
        setCredentials(credentialList.items)
        setTypes(typeList)
        setProtocolLabels(
          Object.fromEntries(gatewayList.protocols.map((item) => [item.value, item.label])),
        )
        setLoadError(null)
      } catch (e) {
        setLoadError(describeError(e))
      }
    })()
  }, [])

  const [name, setName] = useState(agent.name)
  const [avatar, setAvatar] = useState(agent.avatar)
  const [avatarPreset, setAvatarPreset] = useState<string | null>(agent.avatar_preset ?? null)
  const [groupName, setGroupName] = useState(agent.group_name ?? '')
  const [tagline, setTagline] = useState(agent.tagline ?? '')
  const [description, setDescription] = useState(agent.description ?? '')
  const [systemPrompt, setSystemPrompt] = useState(agent.system_prompt ?? '')
  const [gatewayId, setGatewayId] = useState(
    agent.gateway_kind !== 'official' ? agent.gateway_id ?? '' : '',
  )
  const [model, setModel] = useState(agent.model ?? '')
  const [options, setOptions] = useState<Record<string, unknown>>(agent.options ?? {})
  const [credentialDrafts, setCredentialDrafts] = useState(() => credentialsToDraft(agent.credentials))
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  /** 头像包画廊的开合：60 张缩略图没必要常驻 */
  const [galleryOpen, setGalleryOpen] = useState(false)

  const selected = types.find((type) => type.id === agent.type_id) ?? null
  const gatewayChoices = useMemo(() => gatewaysFor(selected, gateways), [selected, gateways])
  const gateway = gateways.find((item) => item.id === gatewayId) ?? null
  const modelChoices = gateway?.selected_models ?? []

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
      await onSaved()
    } catch (e) {
      setSaveError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      {loadError && <div className="banner err">{loadError}</div>}
      {saveError && <div className="banner err">{saveError}</div>}

      {/* 头像：emoji 快选 + 头像包画廊（随机生成 / 自定义上传在桌面端设定页里也有） */}
      <div className="field">
        <label className="field-label">头像</label>
        <div className="row" style={{ gap: 12, alignItems: 'center' }}>
          <AgentAvatar
            avatar={avatar}
            avatarPreset={avatarPreset}
            avatarFile={agent.avatar_file}
            agentId={agent.id}
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
        <label className="field-label">名称</label>
        <input className="input" value={name} onChange={(event) => setName(event.target.value)} />
      </div>

      <div className="field">
        <label className="field-label">内核类型</label>
        <input className="input" value={agent.type_name ?? agent.type_id} readOnly />
        <span className="field-help">换内核请到智能体列表「复制」后新建。</span>
      </div>

      <div className="field">
        <label className="field-label">分组</label>
        <input
          className="input"
          value={groupName}
          onChange={(event) => setGroupName(event.target.value)}
          placeholder="例如 开发 / 写作（留空就归入「未分组」）"
        />
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
          选「官方默认」= 平台不接管路由，沿用内核自带的网关与凭据；自定义网关在「自定义网关」页维护。
          {gateway && !gateway.api_key_set ? ' ⚠️ 该网关还没配置密钥。' : ''}
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
                <CredentialEditor drafts={credentialDrafts} library={credentials} onChange={setCredentialDrafts} />
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
        {saving ? '保存中…' : '保存基础信息'}
      </button>
    </div>
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
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const selected = types.find((type) => type.id === agent.type_id) ?? null
  const supportsSkills = selected?.capabilities.skills ?? false
  const matchingSkills = useMemo(
    () => skills.filter((skill) => skill.type_id === agent.type_id),
    [skills, agent.type_id],
  )
  const pickedSkills = matchingSkills.filter((skill) => skillIds.includes(skill.id))
  const otherSkills = skills.length - matchingSkills.length

  const toggleSkill = (skillId: string) =>
    setSkillIds((current) =>
      current.includes(skillId) ? current.filter((id) => id !== skillId) : [...current, skillId],
    )

  const save = async () => {
    setSaving(true)
    try {
      await api.setAgentSkills(agent.id, skillIds)
      await onSaved()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}
      {!supportsSkills ? (
        <div className="banner info">该内核不支持技能，这个智能体不会加载任何技能。</div>
      ) : (
        <>
          {loading && <p className="dim small">正在加载技能目录…</p>}
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
            <span className="field-help">还没有属于这个内核的技能，请先到「技能」页重新扫描。</span>
          )}
          <span className="field-help">未勾选的技能不会被这个智能体加载。</span>
          {otherSkills > 0 && (
            <span className="field-help">另有 {otherSkills} 个技能来自其它内核，无法选择。</span>
          )}
          <button className="btn primary block" onClick={() => void save()} disabled={saving}>
            {saving ? '保存中…' : `保存技能分配（已选 ${skillIds.length}）`}
          </button>
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
    </div>
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
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const selected = types.find((type) => type.id === agent.type_id) ?? null
  const supportsMcp = selected?.capabilities.mcp ?? false
  const assignable = useMemo(
    () => servers.filter((server) => !server.type_id || server.type_id === agent.type_id),
    [servers, agent.type_id],
  )
  const picked = assignable.filter((server) => mcpIds.includes(server.id))
  const otherMcp = servers.length - assignable.length

  const toggleMcp = (mcpId: string) =>
    setMcpIds((current) =>
      current.includes(mcpId) ? current.filter((id) => id !== mcpId) : [...current, mcpId],
    )

  const save = async () => {
    setSaving(true)
    try {
      await api.setAgentMcpServers(agent.id, mcpIds)
      await onSaved()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}
      {!supportsMcp ? (
        <div className="banner info">该内核不支持 MCP，这个智能体不会连任何 MCP 服务器。</div>
      ) : (
        <>
          {loading && <p className="dim small">正在加载 MCP 目录…</p>}
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
            <span className="field-help">目录里还没有可分配的服务器，请先到「MCP」页新建或重新扫描。</span>
          )}
          <span className="field-help">未挂上的 MCP 服务器不会被加载；平台通用的保留。</span>
          {otherMcp > 0 && (
            <span className="field-help">另有 {otherMcp} 个服务器来自其它内核，无法选择。</span>
          )}
          <button className="btn primary block" onClick={() => void save()} disabled={saving}>
            {saving ? '保存中…' : `保存 MCP 分配（已挂 ${mcpIds.length}）`}
          </button>
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
    </div>
  )
}

/**
 * 可以选择的自定义网关（与桌面端同一规则：官方链路不在下拉里，自定义网关全列出）。
 */
function gatewaysFor(type: AgentType | null, gateways: Gateway[]): Gateway[] {
  const custom = gateways.filter((g) => g.kind === 'custom')
  const protocol = type?.protocol
  if (!protocol) return custom
  const matches = (g: Gateway) => g.protocol === protocol
  return [...custom.filter(matches), ...custom.filter((g) => !matches(g))]
}

const AVATARS = ['🛠️', '🧪', '🔎', '🐳', '🔗', '🎨', '📚', '🧠', '⚡', '🛰️', '🧭', '🦾']
