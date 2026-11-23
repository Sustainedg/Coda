/**
 * 协作组编排面板：左栏协作组列表，中间是画布编排器，右侧是属性面板。
 *
 * 入口在「智能体 → 协作组」：先看到协作组卡片，点卡片才进到这里（老地址 `/collab/settings*`
 * 会重定向过去）。编辑的是**一张完整的图**：拖动/连线只改本地草稿，点「保存」才整图提交
 * （`PUT /teams/{id}`）。每次改动都会防抖地调一次 `POST /teams/{id}/validate`，把「人话提示」
 * 实时显示出来 —— 校验在服务端（与运行时同一套规则），前端不重复实现一遍。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { CollabCanvas, type CollabSelection } from './CollabCanvas'
import { NodeInspector } from './NodeInspector'
import { Modal } from './Modal'
import { api, ApiError } from '../lib/api'
import {
  addNodeToDraft,
  collabTeamPayload,
  isWarningIssue,
  issuesFromValidation,
  removeNodeFromDraft,
  renameNodeInDraft,
  sanitizeKey,
  toTeamDraft,
  type TeamDraft,
} from '../lib/collabDraft'
import type {
  CollabEdge, CollabIssue, CollabNode, CollabTeam, AgentProfile, AgentType, Skill, McpServer,
} from '../lib/types'
import { AgentAvatar } from './AgentAvatar'
import { TeamAvatarStack } from './TeamAvatarStack'

interface Props {
  teamId?: string
  teams: CollabTeam[]
  agents: AgentProfile[]
  /** 技能 / MCP / 内核目录：节点级「协作专属分配」的选择器要用（与智能体表单同一套数据源） */
  skills: Skill[]
  mcpServers: McpServer[]
  types: AgentType[]
  onOpenTeam: (teamId: string | null) => void
  onTeamsChanged: () => void | Promise<void>
}

export function CollabSettingsPanel({
  teamId, teams, agents, skills, mcpServers, types, onOpenTeam, onTeamsChanged,
}: Props) {
  const [draft, setDraft] = useState<TeamDraft | null>(null)
  const [selection, setSelection] = useState<CollabSelection | null>(null)
  const [issues, setIssues] = useState<CollabIssue[]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const versionRef = useRef(0)

  const loadTeam = useCallback(async (id: string) => {
    try {
      const loaded = await api.getCollabTeam(id)
      setDraft(toTeamDraft(loaded))
      setIssues(issuesFromValidation(loaded.validation))
      setDirty(false)
      setSelection(null)
      setError(null)
    } catch (e) {
      setError(describe(e))
      setDraft(null)
    }
  }, [])

  useEffect(() => {
    if (teamId) void loadTeam(teamId)
    else {
      setDraft(null)
      setSelection(null)
      setIssues([])
    }
  }, [teamId, loadTeam])

  // 实时校验：改动后防抖 500ms 调一次服务端校验（与运行时同一套规则）
  useEffect(() => {
    if (!teamId || !draft || !dirty) return
    const version = ++versionRef.current
    const timer = window.setTimeout(async () => {
      try {
        const validation = await api.validateCollabTeam(teamId, {
          graph: { nodes: draft.nodes, edges: draft.edges },
          entry_key: draft.entry_key,
        })
        if (version !== versionRef.current) return
        setIssues(issuesFromValidation(validation))
        setError(null)
      } catch (e) {
        if (version !== versionRef.current) return
        setError(describe(e))
      }
    }, 500)
    return () => window.clearTimeout(timer)
  }, [teamId, draft, dirty])

  const patchTeam = (patch: Partial<TeamDraft>) => {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev))
    setDirty(true)
  }

  const patchNode = (key: string, patch: Partial<CollabNode>) => {
    setDraft((prev) =>
      prev ? { ...prev, nodes: prev.nodes.map((node) => (node.key === key ? { ...node, ...patch } : node)) } : prev,
    )
    setDirty(true)
  }

  const patchEdge = (id: string, patch: Partial<CollabEdge>) => {
    setDraft((prev) =>
      prev ? { ...prev, edges: prev.edges.map((edge) => (edge.id === id ? { ...edge, ...patch } : edge)) } : prev,
    )
    setDirty(true)
  }

  const renameNode = (oldKey: string, rawKey: string) => {
    const key = sanitizeKey(rawKey)
    if (!key || key === oldKey) return
    setDraft((prev) => (prev ? renameNodeInDraft(prev, oldKey, rawKey) : prev))
    setSelection({ kind: 'node', id: key })
    setDirty(true)
  }

  const deleteNode = (key: string) => {
    setDraft((prev) => (prev ? removeNodeFromDraft(prev, key) : prev))
    setSelection(null)
    setDirty(true)
  }

  const deleteEdge = (id: string) => {
    setDraft((prev) => (prev ? { ...prev, edges: prev.edges.filter((edge) => edge.id !== id) } : prev))
    setSelection(null)
    setDirty(true)
  }

  /** 从智能体抽屉添加一个岗位节点（点选或拖放都会走到这里） */
  const addNode = (agent: AgentProfile, at?: { x: number; y: number }) => {
    setDraft((prev) => (prev ? addNodeToDraft(prev, agent, at) : prev))
    setDirty(true)
  }

  const save = async () => {
    if (!teamId || !draft) return
    setSaving(true)
    setError(null)
    try {
      const saved = await api.saveCollabTeam(teamId, collabTeamPayload(draft))
      setDraft(toTeamDraft(saved))
      setIssues(issuesFromValidation(saved.validation))
      setDirty(false)
      await onTeamsChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  const createTeam = async () => {
    try {
      const created = await api.createCollabTeam({ name: newName.trim() || '新建协作组' })
      setCreating(false)
      setNewName('')
      await onTeamsChanged()
      onOpenTeam(created.id)
    } catch (e) {
      setError(describe(e))
    }
  }

  const errors = issues.filter((issue) => !isWarningIssue(issue))
  const warnings = issues.filter(isWarningIssue)

  return (
    <div className="collab-settings">
      <aside className="collab-list">
        <button className="btn primary" style={{ justifyContent: 'center' }} onClick={() => setCreating(true)}>
          + 新建协作组
        </button>
        {teams.map((item) => (
          <button
            key={item.id}
            className={`collab-item${item.id === teamId ? ' active' : ''}`}
            onClick={() => onOpenTeam(item.id)}
          >
            <div className="chat-item-title">{item.name}</div>
            <div className="chat-item-meta">
              <TeamAvatarStack team={item} agents={agents} size={20} max={3} />
              <span className={`pill ${item.validation.ok ? 'ok' : 'err'}`}>
                {item.validation.ok ? '可运行' : `${item.validation.errors.length} 个问题`}
              </span>
              <span>{item.node_count} 个岗位</span>
            </div>
          </button>
        ))}
        {teams.length === 0 && (
          <p className="dim small" style={{ padding: '14px 8px', lineHeight: 1.6 }}>
            还没有协作组。新建一个，然后把智能体拖到画布上连起来。
          </p>
        )}
      </aside>

      {draft && teamId ? (
        <div className="collab-editor">
          <div className="collab-toolbar">
            <div className="collab-agent-palette">
              <span className="dim small">智能体</span>
              <div className="collab-agent-chips">
                {agents.map((agent) => (
                  <button
                    key={agent.id}
                    className="collab-agent-chip"
                    draggable
                    title={`拖到画布上，或点击直接添加`}
                    onDragStart={(event) => event.dataTransfer.setData('text/collab-agent', agent.id)}
                    onClick={() => addNode(agent)}
                  >
                    <AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} avatarFile={agent.avatar_file} agentId={agent.id} size={20} />
                    <span>{agent.name}</span>
                  </button>
                ))}
                {agents.length === 0 && <span className="dim small">还没有智能体，请先到「智能体」页创建</span>}
              </div>
            </div>
            <div className="spacer" style={{ flex: 1 }} />
            <span className={`pill ${errors.length === 0 ? 'ok' : 'err'}`}>
              {errors.length === 0 ? '图可以跑' : `${errors.length} 个错误`}
            </span>
            {warnings.length > 0 && <span className="pill warn">{warnings.length} 条提醒</span>}
            {dirty && <span className="pill">未保存</span>}
            <button className="btn sm" onClick={() => teamId && void loadTeam(teamId)} disabled={saving}>
              放弃改动
            </button>
            <button className="btn primary sm" onClick={() => void save()} disabled={saving || !dirty}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>

          <div className="collab-workbench">
            <CollabCanvas
              nodes={draft.nodes}
              edges={draft.edges}
              agents={agents}
              entryKey={draft.entry_key}
              selected={selection}
              issues={issues}
              onChange={(graph) => {
                setDraft((prev) => (prev ? { ...prev, nodes: graph.nodes, edges: graph.edges } : prev))
                setDirty(true)
              }}
              onSelect={setSelection}
              onDropAgent={(agentId, at) => {
                const agent = agents.find((item) => item.id === agentId)
                if (agent) addNode(agent, at)
              }}
            />
            <NodeInspector
              draft={draft}
              selection={selection}
              agents={agents}
              skills={skills}
              mcpServers={mcpServers}
              types={types}
              issues={issues}
              onPatchTeam={patchTeam}
              onPatchNode={patchNode}
              onPatchEdge={patchEdge}
              onRenameNode={renameNode}
              onDeleteNode={deleteNode}
              onDeleteEdge={deleteEdge}
            />
          </div>

          {(errors.length > 0 || warnings.length > 0) && (
            <div className="collab-issues">
              {[...errors, ...warnings].slice(0, 6).map((issue, index) => (
                <div key={`${issue.code}-${index}`} className={`banner ${isWarningIssue(issue) ? 'info' : 'err'}`}>
                  {issue.message}
                </div>
              ))}
            </div>
          )}
          {error && <div className="banner err" style={{ margin: '10px 14px' }}>{error}</div>}
        </div>
      ) : (
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="empty-icon">🤝</div>
          <p>选择一个协作组开始编排，或新建一个</p>
          <p className="dim small" style={{ marginTop: 8 }}>
            画布上每个节点 = 一个智能体在这次协作里的岗位
          </p>
          <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setCreating(true)}>
            + 新建协作组
          </button>
        </div>
      )}

      {creating && (
        <Modal
          title="新建协作组"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setCreating(false)}>
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
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void createTeam()}
              placeholder="例如：客户端研发组"
            />
          </label>
          <span className="field-help">
            创建后进入画布：从上方智能体抽屉拖入（或点选）智能体，再从节点右/下输出圆点拖到目标左/上输入圆点。
          </span>
        </Modal>
      )}
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
