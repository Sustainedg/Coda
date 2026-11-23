import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../lib/api'
import { CollapsibleGroup } from '../../components/CollapsibleGroup'
import { SkillAssignPanel } from '../../components/SkillAssignPanel'
import { SkillCopyForm } from '../../components/SkillCopyForm'
import { SkillImportForm } from '../../components/SkillImportForm'
import { useExpandMap } from '../../lib/collapse'
import { downloadBlob } from '../../lib/download'
import type { AgentProfile, Skill, SkillDetail, SkillTransferResult } from '../../lib/types'
import { SkillsMarket } from '../../components/SkillsMarket'
import { MarkdownView } from '../../components/MarkdownView'
import { stripFrontmatter } from '../../lib/skillBody'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'user', label: '个人技能' },
  { value: 'project', label: '项目技能' },
  { value: 'plugin', label: '插件技能' },
  { value: 'system', label: '内核系统技能' },
  { value: 'market', label: '腾讯 SkillHub' },
]

/** 技能：本地编目 + 技能市场两个页签；技能只有被智能体分配后才生效。 */
export function MobileSkillsPage() {
  const [tab, setTab] = useState<'local' | 'market'>('local')
  return (
    <div className="m-page">
      <MobileTopBar title="技能" subtitle="统一编目 · 通过智能体分配生效" />
      <div className="m-page-body">
        <div className="tabs" style={{ marginBottom: 12 }}>
          <button
            className={`tab${tab === 'local' ? ' active' : ''}`}
            onClick={() => setTab('local')}
          >
            本地技能
          </button>
          <button
            className={`tab${tab === 'market' ? ' active' : ''}`}
            onClick={() => setTab('market')}
          >
            技能市场
          </button>
        </div>
        {tab === 'market' ? <SkillsMarket variant="mobile" /> : <MobileSkillsLocal />}
      </div>
    </div>
  )
}

/** 本地技能：各内核自带技能的统一编目视图（原来的实现）。 */
function MobileSkillsLocal() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [query, setQuery] = useState('')
  const [sourceKind, setSourceKind] = useState('')
  const [selected, setSelected] = useState<SkillDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [loading, setLoading] = useState(true)
  // 三个弹层：导入（列表顶部）、分配与挂载到其它内核（详情页动作条）
  const [importing, setImporting] = useState(false)
  const [assigning, setAssigning] = useState<SkillDetail | null>(null)
  const [copying, setCopying] = useState<SkillDetail | null>(null)
  const [agents, setAgents] = useState<AgentProfile[]>([])
  /** 按内核分组默认折叠：先给一份目录，要看哪个内核点开哪个。 */
  const { isExpanded, toggle } = useExpandMap()

  const reload = useCallback(async (params?: { source_kind?: string; q?: string }) => {
    try {
      // 已卸载的市场技能不该再出现；后端会过滤，这里再兜一层（旧数据里可能还留着失效行）。
      const rows = await api.listSkills(params)
      setSkills(rows.filter((skill) => !(skill.is_market && !skill.available)))
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

  const rescan = async () => {
    setScanning(true)
    setNotice(null)
    try {
      const result = await api.rescanSkills()
      setNotice(`扫描完成：新增 ${result.added}、更新 ${result.updated}、目录已消失 ${result.missing}，共 ${result.total}`)
      await reload({ source_kind: sourceKind || undefined, q: query || undefined })
    } catch (e) {
      setError(describeError(e))
    } finally {
      setScanning(false)
    }
  }

  const openDetail = async (skill: Skill) => {
    setError(null)
    try {
      setSelected(await api.getSkill(skill.id))
    } catch (e) {
      setError(describeError(e))
    }
  }

  const deleteSelected = async () => {
    if (!selected) return
    if (
      !confirm(
        `删除技能「${selected.name}」？\n会摘掉本内核的引用，并解除本内核智能体对它的分配；` +
          '如果还有别的内核在用它，技能文件会保留。',
      )
    ) {
      return
    }
    setDeleting(true)
    setNotice(null)
    try {
      const result = await api.deleteSkill(selected.id)
      const still = result.remaining_kernels ?? []
      setNotice(
        still.length > 0
          ? `已从本内核移除「${result.name}」，仍挂载在 ${still.length} 个内核`
          : `已删除「${result.name}」`,
      )
      setSelected(null)
      await reload({ source_kind: sourceKind || undefined, q: query || undefined })
    } catch (e) {
      setError(describeError(e))
    } finally {
      setDeleting(false)
    }
  }

  const exportSelected = async () => {
    if (!selected) return
    setExporting(true)
    setNotice(null)
    try {
      const blob = await api.exportSkill(selected.id)
      downloadBlob(blob, `${selected.slug}.zip`)
      setNotice(`已导出「${selected.name}」`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setExporting(false)
    }
  }

  const openAssign = async () => {
    if (!selected) return
    setError(null)
    try {
      setAgents(await api.listAgents())
      setAssigning(selected)
    } catch (e) {
      setError(describeError(e))
    }
  }

  const afterTransfer = async (
    action: string,
    result: SkillTransferResult,
    kernelName: string,
  ) => {
    setImporting(false)
    setCopying(null)
    setAssigning(null)
    setNotice(`${action}「${result.name}」到 ${kernelName}；分配给智能体后才会在运行时生效`)
    await reload({ source_kind: sourceKind || undefined, q: query || undefined })
    try {
      setSelected(await api.getSkill(result.skill_id))
    } catch (e) {
      // 新技能已经落盘并刷新过列表；详情读不到只是这一步失败，照常提示用户。
      setError(describeError(e))
    }
  }

  // 按内核分组展示：同类技能放在一起，方便按内核核对。
  const groups = useMemo(() => {
    const map = new Map<string, { typeName: string; items: Skill[] }>()
    for (const skill of skills) {
      const key = skill.type_id
      if (!map.has(key)) map.set(key, { typeName: skill.type_name ?? skill.type_id, items: [] })
      map.get(key)!.items.push(skill)
    }
    return [...map.entries()]
  }, [skills])

  return (
    <div className="stack">
      <div className="row" style={{ gap: 8 }}>
        <span className="dim small" style={{ flex: 1 }}>
          各内核自带技能的统一编目
        </span>
        <button className="btn" onClick={() => void rescan()} disabled={scanning}>
          {scanning ? '扫描中…' : '重新扫描'}
        </button>
        <button className="btn primary" onClick={() => setImporting(true)}>
          导入
        </button>
      </div>
      <div className="stack">
          <input
            className="input"
            value={query}
            placeholder="搜索技能名或说明"
            onChange={(event) => {
              setQuery(event.target.value)
              void reload({ q: event.target.value, source_kind: sourceKind || undefined })
            }}
          />
          <select
            className="select"
            value={sourceKind}
            onChange={(event) => {
              setSourceKind(event.target.value)
              void reload({ source_kind: event.target.value || undefined, q: query || undefined })
            }}
          >
            {SOURCE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          {error && <div className="banner err">{error}</div>}
          {notice && <div className="banner info">{notice}</div>}

          {loading ? (
            <p className="dim small">正在加载…</p>
          ) : skills.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🧩</div>
              <p>没有扫描到技能</p>
              <p className="dim small">点上方「重新扫描」刷新各内核的技能目录。</p>
            </div>
          ) : (
            groups.map(([typeId, group]) => (
              <CollapsibleGroup
                key={typeId}
                title={group.typeName}
                count={group.items.length}
                expanded={isExpanded(typeId)}
                onToggle={() => toggle(typeId)}
              >
                {group.items.map((skill) => (
                  <button key={skill.id} className="m-item" onClick={() => void openDetail(skill)}>
                    <span className="m-item-main">
                      <span className="m-item-title">{skill.name}</span>
                      <span className="m-item-sub">{skill.description || '（无描述）'}</span>
                    </span>
                    <span className="pill">{skill.available ? skill.source_label : '目录已消失'}</span>
                    {skill.owner_name && !skill.is_own && (
                      <span className="pill">用户 {skill.owner_name}</span>
                    )}
                    <span className="m-item-chevron">›</span>
                  </button>
                ))}
              </CollapsibleGroup>
            ))
          )}
        </div>

      {selected && (
        <MobileSheet
          full
          title="技能详情"
          onClose={() => setSelected(null)}
          escapeDisabled={Boolean(assigning || copying)}
          footer={
            selected.deletable ? (
              <button
                className="btn danger block"
                onClick={() => void deleteSelected()}
                disabled={deleting}
              >
                {deleting ? '删除中…' : '删除技能'}
              </button>
            ) : undefined
          }
        >
          <div className="m-skill-detail">
            <header className="m-skill-hero">
              <div className="m-skill-kicker">
                <span className="pill brand">{selected.source_label}</span>
                <span className="m-skill-kernel">{selected.type_name ?? selected.type_id}</span>
                {!selected.available && <span className="pill err">目录已消失</span>}
              </div>
              <h2 className="m-skill-title">{selected.name}</h2>
              {selected.description && <p className="m-skill-desc">{selected.description}</p>}
              <div className="m-skill-chips">
                <span className="m-skill-chip">{selected.file_count} 个文件</span>
                {selected.owner_name && !selected.is_own && (
                  <span className="m-skill-chip">用户 {selected.owner_name}</span>
                )}
                {selected.version && <span className="m-skill-chip">v{selected.version}</span>}
                {selected.author && <span className="m-skill-chip">作者 {selected.author}</span>}
              </div>
            </header>

            <div className="row wrap" style={{ gap: 8 }}>
              <button
                className="btn sm"
                onClick={() => void exportSelected()}
                disabled={exporting || !selected.available}
              >
                {exporting ? '导出中…' : '导出'}
              </button>
              <button
                className="btn sm"
                onClick={() => void openAssign()}
                disabled={!selected.available}
              >
                分配
              </button>
              <button
                className="btn sm"
                onClick={() => setCopying(selected)}
                disabled={!selected.available}
              >
                挂载到内核
              </button>
            </div>

            {selected.note && <div className="banner info">{selected.note}</div>}

            <section className="m-skill-section">
              <div className="m-skill-section-head">
                <h3>使用它的智能体</h3>
                <span>{selected.agents.length}</span>
              </div>
              {selected.agents.length === 0 ? (
                <p className="m-skill-empty">还没有智能体分配这个技能。</p>
              ) : (
                <div className="m-skill-agents">
                  {selected.agents.map((agent) => (
                    <span className="m-skill-agent" key={agent.id}>
                      <span className="m-skill-agent-avatar">{agent.avatar || '🤖'}</span>
                      {agent.name}
                    </span>
                  ))}
                </div>
              )}
            </section>

            {selected.when_to_use && (
              <section className="m-skill-section">
                <div className="m-skill-section-head">
                  <h3>适用场景</h3>
                </div>
                <p className="m-skill-text">{selected.when_to_use}</p>
              </section>
            )}

            <section className="m-skill-section">
              <div className="m-skill-section-head">
                <h3>完整描述</h3>
                <span>SKILL.md</span>
              </div>
              {selected.body ? (
                <>
                  <div className="m-skill-body">
                    <MarkdownView text={stripFrontmatter(selected.body)} />
                  </div>
                  {selected.body_truncated && (
                    <p className="m-skill-empty">内容过长，已截断显示。</p>
                  )}
                </>
              ) : (
                <p className="m-skill-empty">（SKILL.md 为空或不可读）</p>
              )}
            </section>

            <section className="m-skill-section">
              <div className="m-skill-section-head">
                <h3>允许的工具</h3>
              </div>
              {selected.allowed_tools.length === 0 ? (
                <p className="m-skill-empty">未声明允许的工具。</p>
              ) : (
                <div className="row wrap" style={{ gap: 6 }}>
                  {selected.allowed_tools.map((tool) => (
                    <span className="pill" key={tool}>
                      {tool}
                    </span>
                  ))}
                </div>
              )}
            </section>

            {selected.files.length > 0 && (
              <section className="m-skill-section">
                <div className="m-skill-section-head">
                  <h3>文件清单</h3>
                  <span>{selected.files.length}</span>
                </div>
                <ul className="m-skill-file-list">
                  {selected.files.map((file) => (
                    <li key={file.path}>
                      <span className="m-skill-file-name">{file.path}</span>
                      <span className="m-skill-file-size">{formatBytes(file.size)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        </MobileSheet>
      )}

      {importing && (
        <MobileSheet title="导入技能" onClose={() => setImporting(false)}>
          <SkillImportForm
            onCancel={() => setImporting(false)}
            onImported={(result, kernelName) =>
              void afterTransfer('已导入', result, kernelName)
            }
          />
        </MobileSheet>
      )}

      {assigning && (
        <MobileSheet
          title={`分配「${assigning.name}」`}
          onClose={() => setAssigning(null)}
        >
          <SkillAssignPanel
            skill={assigning}
            agents={agents}
            onChanged={async () => {
              const [freshDetail, freshAgents] = await Promise.all([
                api.getSkill(assigning.id),
                api.listAgents(),
              ])
              setAssigning(freshDetail)
              setSelected(freshDetail)
              setAgents(freshAgents)
              await reload({ source_kind: sourceKind || undefined, q: query || undefined })
            }}
          />
        </MobileSheet>
      )}

      {copying && (
        <MobileSheet
          title={`把「${copying.name}」挂载到别的内核`}
          onClose={() => setCopying(null)}
        >
          <SkillCopyForm
            skill={copying}
            onCancel={() => setCopying(null)}
            onCopied={(result, kernelName) =>
              void afterTransfer('已挂载', result, kernelName)
            }
          />
        </MobileSheet>
      )}
    </div>
  )
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
