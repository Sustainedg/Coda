import { useState } from 'react'
import { ACTIONS, DUTIES, SCOPES, sanitizeKey } from '../../lib/collabDraft'
import { SkillPicker } from '../../components/SkillPicker'
import { McpPicker } from '../../components/McpPicker'
import { MobileSheet } from './MobileSheet'
import type {
  CollabDuty, CollabNode, CollabTicketAction, CollabWorkspaceScope,
  AgentProfile, AgentType, Skill, McpServer,
} from '../../lib/types'


/**
 * 协作「岗位」表单（移动端）：底部弹层里的内容，本地编辑、点「保存岗位」才写回草稿。
 *
 * 只编辑这一个节点；改名会同步连线，所以 key 输入框每次按键都按后端规则规整（`sanitizeKey`）。
 * 节点回答「他在这个协作里干什么」；适配器参数由绑定的智能体决定，但技能与 MCP
 * 支持协作专属的临时分配（不改智能体页面的常规配置）。
 */
export function CollabNodeForm({
  node,
  agents,
  skills,
  mcpServers,
  types,
  isEntry,
  onSave,
  onDelete,
  onSetEntry,
}: {
  node: CollabNode
  agents: AgentProfile[]
  skills: Skill[]
  mcpServers: McpServer[]
  types: AgentType[]
  isEntry: boolean
  onSave: (next: CollabNode) => void
  onDelete: () => void
  onSetEntry: () => void
}) {
  const [form, setForm] = useState<CollabNode>(node)
  const [keyInput, setKeyInput] = useState(node.key)
  const [pickerOpen, setPickerOpen] = useState<'skills' | 'mcp' | null>(null)
  const agent = agents.find((item) => item.id === form.agent_profile_id)

  const set = (next: Partial<CollabNode>) => setForm((prev) => ({ ...prev, ...next }))
  // 历史数据可能给非入口节点勾过 ask_user、给入口节点勾过 escalate；这里统一规整，
  // 任何一次改动都会把它清掉（运行时本来就会忽略，界面别显示成「生效了」）。
  const contract = form.output_contract.filter(
    (item) => (item !== 'ask_user' || isEntry) && (item !== 'escalate' || !isEntry),
  )
  const toggleAction = (action: CollabTicketAction) =>
    set({
      output_contract: contract.includes(action)
        ? contract.filter((item) => item !== action)
        : [...contract, action],
    })

  return (
    <>
      <div className="field">
        <label className="field-label">智能体</label>
        <select className="select" value={form.agent_profile_id} onChange={(event) => set({ agent_profile_id: event.target.value })}>
          {agents.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name} · {item.type_name ?? item.type_id}
            </option>
          ))}
        </select>
        <span className="field-help">
          {agent ? `内核：${agent.type_name ?? agent.type_id} · 模型：${agent.model ?? '默认'}` : '该智能体已不存在，请重新选择'}
        </span>
      </div>

      <div className="field">
        <label className="field-label">显示名</label>
        <input className="input" value={form.label} onChange={(event) => set({ label: event.target.value })} />
      </div>

      <div className="field">
        <label className="field-label">标识（key）</label>
        <input
          className="input mono"
          value={keyInput}
          onChange={(event) => {
            const next = sanitizeKey(event.target.value)
            setKeyInput(next)
            set({ key: next })
          }}
          placeholder="小写字母/数字/-_，例如 cto"
        />
        <span className="field-help">工单里用它指代岗位；改名会同步更新所有连线。</span>
      </div>

      <div className="field">
        <label className="field-label">岗位</label>
        <select className="select" value={form.duty} onChange={(event) => set({ duty: event.target.value as CollabDuty })}>
          {DUTIES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <span className="field-help">{DUTIES.find((item) => item.value === form.duty)?.hint}</span>
      </div>

      <div className="field">
        <label className="field-label">职责说明</label>
        <textarea
          className="textarea"
          style={{ minHeight: 74 }}
          value={form.instructions}
          onChange={(event) => set({ instructions: event.target.value })}
          placeholder="追加在智能体人格之后，例如：负责拆解任务与汇总"
        />
      </div>

      <div className="field">
        <label className="field-label">能力边界</label>
        <textarea
          className="textarea"
          style={{ minHeight: 60 }}
          value={form.capability_scope}
          onChange={(event) => set({ capability_scope: event.target.value })}
          placeholder="例如：不得写代码、不得访问工作目录"
        />
        <span className="field-help">会写进该节点的提示词；真正的兜底是工作目录范围与越权审计。</span>
      </div>

      <div className="field">
        <label className="field-label">工作目录范围</label>
        <select
          className="select"
          value={form.workspace_scope ?? ''}
          onChange={(event) => set({ workspace_scope: (event.target.value || null) as CollabWorkspaceScope | null })}
        >
          {SCOPES.map((item) => (
            <option key={String(item.value)} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field-label">同一工单最大返工轮次</label>
        <input
          className="input mono"
          type="number"
          min={0}
          value={form.max_rounds}
          onChange={(event) => set({ max_rounds: Math.max(0, Number(event.target.value) || 0) })}
        />
        <span className="field-help">0 表示用团队默认；与团队上限取小。</span>
      </div>

      <NodeOverridesFields
        form={form}
        agent={agent}
        skills={skills}
        mcpServers={mcpServers}
        types={types}
        set={set}
        onOpenPicker={(kind) => setPickerOpen(kind)}
      />

      <div className="field">
        <label className="field-label">允许发起的动作</label>
        <div className="m-chip-row">
          {ACTIONS.map((action) => {
            const lockHint =
              action.value === 'ask_user' && !isEntry
                ? '只有入口节点能向用户提问'
                : action.value === 'escalate'
                  ? isEntry
                    ? '入口节点是最顶端，没有更上级可以上报'
                    : '上报是保底动作：任何岗位都保留，不用勾也生效'
                  : undefined
            return (
              <label
                key={action.value}
                className={`m-chip${contract.includes(action.value) ? ' on' : ''}`}
                style={lockHint ? { opacity: 0.55 } : undefined}
                title={lockHint}
              >
                <input
                  type="checkbox"
                  checked={contract.includes(action.value)}
                  disabled={Boolean(lockHint)}
                  onChange={() => toggleAction(action.value)}
                />
                {action.label}
              </label>
            )
          })}
        </div>
        <span className="field-help">
          一个都不选 = 按岗位取默认动作集合。ask_user 只有入口节点可用；escalate 是保底动作，始终可用。
        </span>
      </div>

      <button className="btn sm" onClick={onSetEntry} disabled={isEntry}>
        {isEntry ? '★ 已是入口节点' : '设为入口节点（与用户对话）'}
      </button>

      <div className="row" style={{ gap: 8, marginTop: 4 }}>
        <button className="btn danger" onClick={onDelete}>
          删除岗位
        </button>
        <button className="btn primary" style={{ flex: 1 }} onClick={() => onSave(form)} disabled={!form.key}>
          保存岗位
        </button>
      </div>

      {pickerOpen === 'skills' && (
        <MobileSheet full title={`协作技能 · ${form.label || form.key}`} onClose={() => setPickerOpen(null)}>
          <SkillPicker
            skills={skills.filter((skill) => skill.type_id === agent?.type_id)}
            selected={form.skill_ids ?? []}
            onChange={(next) => set({ skill_ids: next })}
          />
        </MobileSheet>
      )}
      {pickerOpen === 'mcp' && (
        <MobileSheet full title={`协作 MCP · ${form.label || form.key}`} onClose={() => setPickerOpen(null)}>
          <McpPicker
            servers={mcpServers.filter((server) => !server.type_id || server.type_id === agent?.type_id)}
            selected={form.mcp_ids ?? []}
            onChange={(next) => set({ mcp_ids: next })}
          />
        </MobileSheet>
      )}
    </>
  )
}

/** 协作专属技能 / MCP 的三态切换（跟随智能体 / 本协作不给 / 自定义），移动端版式。 */
function NodeOverridesFields({
  form, agent, skills, mcpServers, types, set, onOpenPicker,
}: {
  form: CollabNode
  agent: AgentProfile | undefined
  skills: Skill[]
  mcpServers: McpServer[]
  types: AgentType[]
  set: (next: Partial<CollabNode>) => void
  onOpenPicker: (kind: 'skills' | 'mcp') => void
}) {
  const agentType = types.find((item) => item.id === agent?.type_id)
  const supportsSkills = !!agentType?.capabilities.skills
  const supportsMcp = !!agentType?.capabilities.mcp
  const matchingSkills = skills.filter((skill) => skill.type_id === agent?.type_id)
  const assignableMcp = mcpServers.filter((server) => !server.type_id || server.type_id === agent?.type_id)
  const skillIds = form.skill_ids ?? null
  const mcpIds = form.mcp_ids ?? null

  if (!agent) return null
  return (
    <>
      {supportsSkills && (
        <div className="field">
          <label className="field-label">协作技能</label>
          <div className="m-chip-row">
            <label className={`m-chip${skillIds === null ? ' on' : ''}`}>
              <input type="radio" checked={skillIds === null} onChange={() => set({ skill_ids: null })} />
              跟随智能体
            </label>
            <label className={`m-chip${skillIds !== null && skillIds.length === 0 ? ' on' : ''}`}>
              <input type="radio" checked={skillIds !== null && skillIds.length === 0} onChange={() => set({ skill_ids: [] })} />
              本协作不给
            </label>
            <label className={`m-chip${skillIds !== null && skillIds.length > 0 ? ' on' : ''}`}>
              <input
                type="radio"
                checked={skillIds !== null && skillIds.length > 0}
                disabled={matchingSkills.length === 0}
                onChange={() => onOpenPicker('skills')}
              />
              自定义{skillIds && skillIds.length > 0 ? `（${skillIds.length}）` : ''}
            </label>
          </div>
          <span className="field-help">只影响这个协作，不改智能体页面的技能分配。</span>
        </div>
      )}
      {supportsMcp && (
        <div className="field">
          <label className="field-label">协作 MCP</label>
          <div className="m-chip-row">
            <label className={`m-chip${mcpIds === null ? ' on' : ''}`}>
              <input type="radio" checked={mcpIds === null} onChange={() => set({ mcp_ids: null })} />
              跟随智能体
            </label>
            <label className={`m-chip${mcpIds !== null && mcpIds.length === 0 ? ' on' : ''}`}>
              <input type="radio" checked={mcpIds !== null && mcpIds.length === 0} onChange={() => set({ mcp_ids: [] })} />
              本协作不给
            </label>
            <label className={`m-chip${mcpIds !== null && mcpIds.length > 0 ? ' on' : ''}`}>
              <input
                type="radio"
                checked={mcpIds !== null && mcpIds.length > 0}
                disabled={assignableMcp.length === 0}
                onChange={() => onOpenPicker('mcp')}
              />
              自定义{mcpIds && mcpIds.length > 0 ? `（${mcpIds.length}）` : ''}
            </label>
          </div>
          <span className="field-help">只影响这个协作，不改智能体页面的 MCP 分配。</span>
        </div>
      )}
    </>
  )
}
