import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AgentAvatar } from '../../components/AgentAvatar'
import { CollapsibleGroup } from '../../components/CollapsibleGroup'
import { StarIcon } from '../../components/Icons'
import { api } from '../../lib/api'
import { groupAgents } from '../../lib/agentGroups'
import { useExpandMap } from '../../lib/collapse'
import type { AgentProfile, TestResult } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/** 智能体：内核 + 网关 + 模型 + 人格设定；技能只能通过智能体分配获得。 */
export function MobileAgentsPage() {
  const navigate = useNavigate()
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState<{ agent: AgentProfile; result: TestResult | null; busy: boolean } | null>(null)

  const reload = useCallback(async () => {
    try {
      setAgents(await api.listAgents())
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
    </div>
  )

  return (
    <div className="m-page">
      <MobileTopBar
        title="智能体"
        subtitle="内核 + 网关 + 模型 + 人格"
        actions={
          <button className="m-icon-btn" onClick={() => navigate('/agents/new')} aria-label="新建智能体">
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
              <button className="btn primary" onClick={() => navigate('/agents/new')}>
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
