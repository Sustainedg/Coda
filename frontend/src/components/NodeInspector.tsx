/**
 * 编排编辑器的右侧属性面板：节点 / 边 / 团队三种上下文共用一个面板。
 *
 * 节点回答「他在这个协作里干什么」（职责、能力边界、动作、连给谁）；适配器参数仍完全由
 * 它绑定的智能体决定 —— 但**技能与 MCP** 支持协作专属的临时分配（`skill_ids` / `mcp_ids`）：
 * 方便某个协作任务临时给岗位增减技能，而不动智能体页面的常规配置。
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import type { CollabSelection } from './CollabCanvas'
import { AgentAvatar } from './AgentAvatar'
import { SkillPicker } from './SkillPicker'
import { McpPicker } from './McpPicker'
import { Modal } from './Modal'
import { ACTIONS, DUTIES, EDGE_KINDS, SCOPES, isWarningIssue, type TeamDraft } from '../lib/collabDraft'
import type {
  CollabBudget, CollabEdge, CollabEdgeKind, CollabIssue, CollabNode,
  CollabWorkspaceScope, CollabDuty as Duty, AgentProfile, AgentType, Skill, McpServer,
} from '../lib/types'

interface Props {
  draft: TeamDraft
  selection: CollabSelection | null
  agents: AgentProfile[]
  /** 技能 / MCP / 内核目录：节点级「协作专属分配」的选择器要用 */
  skills: Skill[]
  mcpServers: McpServer[]
  types: AgentType[]
  issues: CollabIssue[]
  onPatchTeam: (patch: Partial<TeamDraft>) => void
  onPatchNode: (key: string, patch: Partial<CollabNode>) => void
  onPatchEdge: (id: string, patch: Partial<CollabEdge>) => void
  onRenameNode: (oldKey: string, newKey: string) => void
  onDeleteNode: (key: string) => void
  onDeleteEdge: (id: string) => void
}

export function NodeInspector({
  draft, selection, agents, skills, mcpServers, types, issues,
  onPatchTeam, onPatchNode, onPatchEdge, onRenameNode, onDeleteNode, onDeleteEdge,
}: Props) {
  const node = selection?.kind === 'node' ? draft.nodes.find((item) => item.key === selection.id) : undefined
  const edge = selection?.kind === 'edge' ? draft.edges.find((item) => item.id === selection.id) : undefined

  return (
    <aside className="collab-inspector">
      {node ? (
        <NodePanel
          node={node}
          nodes={draft.nodes}
          entryKey={draft.entry_key}
          agents={agents}
          skills={skills}
          mcpServers={mcpServers}
          types={types}
          issues={issues}
          onPatch={(patch) => onPatchNode(node.key, patch)}
          onRename={(next) => onRenameNode(node.key, next)}
          onDelete={() => onDeleteNode(node.key)}
          onSetEntry={() => onPatchTeam({ entry_key: node.key })}
        />
      ) : edge ? (
        <EdgePanel
          edge={edge}
          nodes={draft.nodes}
          onPatch={(patch) => onPatchEdge(edge.id, patch)}
          onDelete={() => onDeleteEdge(edge.id)}
        />
      ) : (
        <TeamPanel draft={draft} issues={issues} onPatchTeam={onPatchTeam} />
      )}
    </aside>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="collab-section">
      <h3 className="collab-section-title">{title}</h3>
      {children}
    </section>
  )
}

function NodePanel({
  node, nodes, entryKey, agents, skills, mcpServers, types, issues,
  onPatch, onRename, onDelete, onSetEntry,
}: {
  node: CollabNode
  nodes: CollabNode[]
  entryKey: string | null
  agents: AgentProfile[]
  skills: Skill[]
  mcpServers: McpServer[]
  types: AgentType[]
  issues: CollabIssue[]
  onPatch: (patch: Partial<CollabNode>) => void
  onRename: (next: string) => void
  onDelete: () => void
  onSetEntry: () => void
}) {
  const agent = agents.find((item) => item.id === node.agent_profile_id)
  const nodeIssues = issues.filter((issue) => issue.node_key === node.key)
  const isEntry = entryKey === node.key
  // 历史数据可能给非入口节点勾过 ask_user、给入口节点勾过 escalate；这里统一规整，
  // 任何一次改动都会把它清掉（运行时本来就会忽略，界面别显示成「生效了」）。
  const contract = node.output_contract.filter(
    (item) => (item !== 'ask_user' || isEntry) && (item !== 'escalate' || !isEntry),
  )

  return (
    <>
      <div className="collab-inspector-head">
        <AgentAvatar avatar={agent?.avatar} avatarPreset={agent?.avatar_preset} avatarFile={agent?.avatar_file} agentId={agent?.id} size={30} className="collab-node-avatar" />
        <div style={{ minWidth: 0 }}>
          <div className="collab-inspector-title">{node.label || agent?.name || node.key}</div>
          <div className="dim small mono-sm">{node.key}</div>
        </div>
      </div>

      {nodeIssues.length > 0 && (
        <div className="stack" style={{ gap: 6 }}>
          {nodeIssues.map((issue, index) => (
            <div key={`${issue.code}-${index}`} className={`banner ${isWarningIssue(issue) ? 'info' : 'err'}`}>
              {issue.message}
            </div>
          ))}
        </div>
      )}

      <Section title="这个节点是谁">
        <label className="field">
          <span className="field-label">智能体</span>
          <select className="select" value={node.agent_profile_id} onChange={(e) => onPatch({ agent_profile_id: e.target.value })}>
            {agents.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {item.type_name ?? item.type_id}
              </option>
            ))}
          </select>
          <span className="field-help">
            {agent ? `内核：${agent.type_name ?? agent.type_id} · 模型：${agent.model ?? '默认'}` : '该智能体已不存在，请重新选择'}
          </span>
        </label>
        <label className="field">
          <span className="field-label">显示名</span>
          <input className="input" value={node.label} onChange={(e) => onPatch({ label: e.target.value })} placeholder="显示在画布与时间线上" />
        </label>
        <label className="field">
          <span className="field-label">标识（key）</span>
          <input
            className="input mono"
            value={node.key}
            onChange={(e) => onRename(e.target.value)}
            placeholder="小写字母/数字/-_，例如 cto"
          />
          <span className="field-help">工单里用它指代岗位；改名会同步更新所有连线。</span>
        </label>
      </Section>

      <NodeOverridesSection
        node={node}
        agent={agent}
        skills={skills}
        mcpServers={mcpServers}
        types={types}
        onPatch={onPatch}
      />

      <Section title="在这个协作里干什么">
        <label className="field">
          <span className="field-label">岗位</span>
          <select className="select" value={node.duty} onChange={(e) => onPatch({ duty: e.target.value as Duty })}>
            {DUTIES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <span className="field-help">{DUTIES.find((item) => item.value === node.duty)?.hint}</span>
        </label>
        <label className="field">
          <span className="field-label">职责说明</span>
          <textarea
            className="textarea"
            style={{ minHeight: 74 }}
            value={node.instructions}
            onChange={(e) => onPatch({ instructions: e.target.value })}
            placeholder="追加在智能体人格之后，例如：负责拆解任务与汇总"
          />
        </label>
        <label className="field">
          <span className="field-label">能力边界</span>
          <textarea
            className="textarea"
            style={{ minHeight: 60 }}
            value={node.capability_scope}
            onChange={(e) => onPatch({ capability_scope: e.target.value })}
            placeholder="例如：不得写代码、不得访问工作目录"
          />
          <span className="field-help">会写进该节点的提示词；真正的兜底是工作目录范围与越权审计。</span>
        </label>
        <label className="field">
          <span className="field-label">工作目录范围</span>
          <select
            className="select"
            value={node.workspace_scope ?? ''}
            onChange={(e) => onPatch({ workspace_scope: (e.target.value || null) as CollabWorkspaceScope | null })}
          >
            {SCOPES.map((item) => (
              <option key={String(item.value)} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">同一工单最大返工轮次</span>
          <input
            className="input mono"
            type="number"
            min={0}
            value={node.max_rounds}
            onChange={(e) => onPatch({ max_rounds: Math.max(0, Number(e.target.value) || 0) })}
          />
          <span className="field-help">0 表示用团队默认；与团队上限取小。</span>
        </label>
        <div className="field">
          <span className="field-label">允许发起的动作</span>
          <div className="chips">
            {ACTIONS.map((action) => {
              const checked = contract.includes(action.value)
              const lockHint =
                action.value === 'ask_user' && !isEntry
                  ? '只有入口节点能向用户提问'
                  : action.value === 'escalate'
                    ? isEntry
                      ? '入口节点是最顶端，没有更上级可以上报（由 ask_user 收口）'
                      : '上报是保底动作：任何岗位都保留，不用勾也生效'
                    : undefined
              return (
                <label
                  key={action.value}
                  className={`collab-toggle${checked ? ' on' : ''}${lockHint ? ' disabled' : ''}`}
                  title={lockHint}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={Boolean(lockHint)}
                    onChange={() =>
                      onPatch({
                        output_contract: checked
                          ? contract.filter((item) => item !== action.value)
                          : [...contract, action.value],
                      })
                    }
                  />
                  {action.label}
                </label>
              )
            })}
          </div>
          <span className="field-help">
            一个都不选 = 按岗位取默认动作集合。ask_user 只有入口节点可用（只有它直接面对用户）；
            escalate 是保底动作，非入口节点始终可用。
          </span>
        </div>
        <button className="btn sm" onClick={onSetEntry} disabled={entryKey === node.key}>
          {entryKey === node.key ? '★ 已是入口节点' : '设为入口节点（与用户对话）'}
        </button>
      </Section>

      <div className="collab-inspector-foot">
        <button className="btn sm danger" onClick={onDelete}>
          删除这个节点
        </button>
        <span className="dim small">共 {nodes.length} 个节点</span>
      </div>
    </>
  )
}

/**
 * 协作专属的技能 / MCP 分配：三态选择。
 *
 * - **跟随智能体**（`skill_ids` / `mcp_ids` 为 null，默认）：完全用智能体页面的分配；
 * - **本协作不给**（空列表）：这个岗位在协作运行里不带任何技能 / MCP；
 * - **自定义**：从同内核的可选项里挑（与智能体表单同一套选择器）。
 *
 * 只影响协作运行，保存时写进图（`PUT /teams/{id}`），不写回智能体配置 ——
 * 方便某个任务临时给岗位增减技能，用完删掉即可。
 */
function NodeOverridesSection({
  node, agent, skills, mcpServers, types, onPatch,
}: {
  node: CollabNode
  agent: AgentProfile | undefined
  skills: Skill[]
  mcpServers: McpServer[]
  types: AgentType[]
  onPatch: (patch: Partial<CollabNode>) => void
}) {
  const [pickerOpen, setPickerOpen] = useState<'skills' | 'mcp' | null>(null)
  const agentType = types.find((item) => item.id === agent?.type_id)
  const supportsSkills = !!agentType?.capabilities.skills
  const supportsMcp = !!agentType?.capabilities.mcp
  const matchingSkills = skills.filter((skill) => skill.type_id === agent?.type_id)
  const assignableMcp = mcpServers.filter(
    (server) => !server.type_id || server.type_id === agent?.type_id,
  )
  const skillIds = node.skill_ids ?? null
  const mcpIds = node.mcp_ids ?? null
  const nameOf = (id: string) =>
    matchingSkills.find((skill) => skill.id === id)?.name
    ?? assignableMcp.find((server) => server.id === id)?.name
    ?? id

  const toggleInList = (kind: 'skill_ids' | 'mcp_ids', current: string[] | null, id: string) => {
    // null（跟随智能体）状态下第一次点选：以智能体当前分配为底子，再叠加这次点选。
    const base = current ?? []
    onPatch({
      [kind]: base.includes(id) ? base.filter((item) => item !== id) : [...base, id],
    } as Partial<CollabNode>)
  }

  return (
    <Section title="协作专属技能 / MCP">
      {!agent ? (
        <div className="banner info">先给这个节点绑定一个智能体，才能设置技能与 MCP。</div>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {!supportsSkills && <div className="banner info">「{agent.name}」的内核不支持技能。</div>}
          {supportsSkills && (
            <div className="field">
              <span className="field-label">技能</span>
              <div className="row wrap" style={{ gap: 8 }}>
                <button
                  type="button"
                  className={`collab-toggle${skillIds === null ? ' on' : ''}`}
                  onClick={() => onPatch({ skill_ids: null })}
                  title="用智能体页面的常规技能分配"
                >
                  跟随智能体{skillIds === null ? `（${matchingSkills.filter((s) => agent.skill_ids?.includes(s.id)).length} 个）` : ''}
                </button>
                <button
                  type="button"
                  className={`collab-toggle${skillIds !== null && skillIds.length === 0 ? ' on' : ''}`}
                  onClick={() => onPatch({ skill_ids: [] })}
                  title="这个协作运行里不带任何技能"
                >
                  本协作不给
                </button>
                <button
                  type="button"
                  className={`collab-toggle${skillIds !== null && skillIds.length > 0 ? ' on' : ''}`}
                  onClick={() => setPickerOpen('skills')}
                  disabled={matchingSkills.length === 0}
                >
                  自定义{skillIds && skillIds.length > 0 ? `（${skillIds.length} 个）` : ''}
                </button>
              </div>
              {skillIds !== null && skillIds.length > 0 && (
                <div className="row wrap" style={{ gap: 5, marginTop: 6 }}>
                  {skillIds.map((id) => (
                    <span className="pill" key={id}>
                      {nameOf(id)}
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => toggleInList('skill_ids', skillIds, id)}
                        aria-label={`移除技能「${nameOf(id)}」`}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
          {!supportsMcp && <div className="banner info">「{agent.name}」的内核不支持 MCP。</div>}
          {supportsMcp && (
            <div className="field">
              <span className="field-label">MCP 服务器</span>
              <div className="row wrap" style={{ gap: 8 }}>
                <button
                  type="button"
                  className={`collab-toggle${mcpIds === null ? ' on' : ''}`}
                  onClick={() => onPatch({ mcp_ids: null })}
                  title="用智能体页面的常规 MCP 分配"
                >
                  跟随智能体{mcpIds === null ? `（${assignableMcp.filter((s) => agent.mcp_ids?.includes(s.id)).length} 个）` : ''}
                </button>
                <button
                  type="button"
                  className={`collab-toggle${mcpIds !== null && mcpIds.length === 0 ? ' on' : ''}`}
                  onClick={() => onPatch({ mcp_ids: [] })}
                  title="这个协作运行里不连任何 MCP 服务器"
                >
                  本协作不给
                </button>
                <button
                  type="button"
                  className={`collab-toggle${mcpIds !== null && mcpIds.length > 0 ? ' on' : ''}`}
                  onClick={() => setPickerOpen('mcp')}
                  disabled={assignableMcp.length === 0}
                >
                  自定义{mcpIds && mcpIds.length > 0 ? `（${mcpIds.length} 个）` : ''}
                </button>
              </div>
              {mcpIds !== null && mcpIds.length > 0 && (
                <div className="row wrap" style={{ gap: 5, marginTop: 6 }}>
                  {mcpIds.map((id) => (
                    <span className="pill" key={id}>
                      🔌 {nameOf(id)}
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => toggleInList('mcp_ids', mcpIds, id)}
                        aria-label={`移除 MCP「${nameOf(id)}」`}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
          <span className="field-help">
            只影响这个协作组里的该岗位，不改智能体页面的常规分配 —— 方便某个任务临时增减技能，用完删掉即可。
            「跟随智能体」时显示的是智能体页面的常规分配数。
          </span>
        </div>
      )}

      {pickerOpen === 'skills' && (
        <Modal
          wide
          title={`协作技能 · ${node.label || node.key}`}
          onClose={() => setPickerOpen(null)}
          footer={
            <button className="btn primary" onClick={() => setPickerOpen(null)}>
              完成（已选 {skillIds?.length ?? 0} 个）
            </button>
          }
        >
          <SkillPicker
            skills={matchingSkills}
            selected={skillIds ?? []}
            onChange={(next) => onPatch({ skill_ids: next })}
          />
        </Modal>
      )}
      {pickerOpen === 'mcp' && (
        <Modal
          wide
          title={`协作 MCP · ${node.label || node.key}`}
          onClose={() => setPickerOpen(null)}
          footer={
            <button className="btn primary" onClick={() => setPickerOpen(null)}>
              完成（已挂 {mcpIds?.length ?? 0} 个）
            </button>
          }
        >
          <McpPicker
            servers={assignableMcp}
            selected={mcpIds ?? []}
            onChange={(next) => onPatch({ mcp_ids: next })}
          />
        </Modal>
      )}
    </Section>
  )
}

function EdgePanel({
  edge, nodes, onPatch, onDelete,
}: {
  edge: CollabEdge
  nodes: CollabNode[]
  onPatch: (patch: Partial<CollabEdge>) => void
  onDelete: () => void
}) {
  const label = (key: string) => {
    const node = nodes.find((item) => item.key === key)
    return node ? `${node.label || node.key}（${key}）` : key
  }
  return (
    <>
      <div className="collab-inspector-head">
        <span className="collab-node-avatar">↦</span>
        <div>
          <div className="collab-inspector-title">连线</div>
          <div className="dim small">
            {label(edge.from_key)} → {label(edge.to_key)}
          </div>
        </div>
      </div>
      <Section title="这条连线允许什么">
        <label className="field">
          <span className="field-label">类型</span>
          <select className="select" value={edge.kind} onChange={(e) => onPatch({ kind: e.target.value as CollabEdgeKind })}>
            {EDGE_KINDS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <span className="field-help">{EDGE_KINDS.find((item) => item.value === edge.kind)?.hint}</span>
        </label>
        {edge.kind === 'rework' && (
          <div className="field-help">
            这条线本身已经指定打回目标，运行时不需要额外的 rework_to。只有当同一审查者画了多条
            rework 出边时，审查者才需要在工单里写明 rework_to 来选择退路。
          </div>
        )}
        {edge.kind === 'rework' && (
          <label className="field">
            <span className="field-label">这条边的返工上限</span>
            <input
              className="input mono"
              type="number"
              min={0}
              value={edge.max_rounds}
              onChange={(e) => onPatch({ max_rounds: Math.max(0, Number(e.target.value) || 0) })}
            />
            <span className="field-help">
              0 表示按团队默认；填了就取「边 / 节点 / 团队」三者最小，超过即挂起等你介入。
            </span>
          </label>
        )}
        <label className="field">
          <span className="field-label">备注（可选）</span>
          <input className="input" value={edge.label} onChange={(e) => onPatch({ label: e.target.value })} placeholder="画在连线上的一小段文字" />
        </label>
      </Section>
      <div className="collab-inspector-foot">
        <button className="btn sm danger" onClick={onDelete}>
          删除这条连线
        </button>
      </div>
    </>
  )
}

function TeamPanel({
  draft, issues, onPatchTeam,
}: {
  draft: TeamDraft
  issues: CollabIssue[]
  onPatchTeam: (patch: Partial<TeamDraft>) => void
}) {
  const setBudget = (patch: Partial<CollabBudget>) => onPatchTeam({ budget: { ...draft.budget, ...patch } })
  return (
    <>
      <div className="collab-inspector-head">
        <span className="collab-node-avatar">🤝</span>
        <div>
          <div className="collab-inspector-title">协作组设置</div>
          <div className="dim small">点画布空白处可以回到这里</div>
        </div>
      </div>

      {issues.length > 0 && (
        <div className="stack" style={{ gap: 6 }}>
          {issues.slice(0, 6).map((issue, index) => (
            <div key={`${issue.code}-${index}`} className={`banner ${isWarningIssue(issue) ? 'info' : 'err'}`}>
              {issue.message}
            </div>
          ))}
        </div>
      )}

      <Section title="基本信息">
        <label className="field">
          <span className="field-label">名称</span>
          <input className="input" value={draft.name} onChange={(e) => onPatchTeam({ name: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-label">说明</span>
          <textarea
            className="textarea"
            style={{ minHeight: 60 }}
            value={draft.description}
            onChange={(e) => onPatchTeam({ description: e.target.value })}
            placeholder="这个协作组是干什么的"
          />
        </label>
        <label className="field">
          <span className="field-label">入口节点</span>
          <select
            className="select"
            value={draft.entry_key ?? ''}
            onChange={(e) => onPatchTeam({ entry_key: e.target.value || null })}
          >
            <option value="">（未指定）</option>
            {draft.nodes.map((node) => (
              <option key={node.key} value={node.key}>
                {node.label || node.key}（{node.key}）
              </option>
            ))}
          </select>
          <span className="field-help">唯一能与用户对话的节点，必须是协调者。</span>
        </label>
        <label className="field">
          <span className="field-label">工作路径</span>
          <input
            className="input mono"
            value={draft.workspace_dir}
            onChange={(e) => onPatchTeam({ workspace_dir: e.target.value })}
            placeholder="例如 /vol1/projects/my-app；留空自动分配"
          />
          <span className="field-help">
            作为这个团队的默认工作路径；新建协作任务时可以覆盖。留空则每次运行自动分配。
          </span>
        </label>
      </Section>

      <Section title="预算与上限">
        <div className="collab-budget-grid">
          <NumberField label="最大步数" value={draft.budget.max_steps} onChange={(v) => setBudget({ max_steps: v })} />
          <NumberField label="最大工单数" value={draft.budget.max_tickets} onChange={(v) => setBudget({ max_tickets: v })} />
          <NumberField label="单工单返工上限" value={draft.budget.max_rounds_per_ticket} onChange={(v) => setBudget({ max_rounds_per_ticket: v })} />
          <NumberField label="token 上限（0=不限）" value={draft.budget.max_tokens} onChange={(v) => setBudget({ max_tokens: v })} />
          <NumberField label="运行时限（秒）" value={draft.budget.timeout_seconds} onChange={(v) => setBudget({ timeout_seconds: v })} />
          <NumberField label="单步限时·秒（0=不限）" value={draft.budget.step_timeout_seconds ?? 36000} onChange={(v) => setBudget({ step_timeout_seconds: v })} />
          <NumberField label="并行上限（1=串行）" value={draft.budget.max_parallel ?? 1} onChange={(v) => setBudget({ max_parallel: v })} />
        </div>
        <span className="field-help">任何一项超限都会挂起并明确提示，不会静默烧钱。</span>
      </Section>
    </>
  )
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (next: number) => void }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <input
        className="input mono"
        type="number"
        min={0}
        value={value}
        onChange={(e) => onChange(Math.max(0, Number(e.target.value) || 0))}
      />
    </label>
  )
}
