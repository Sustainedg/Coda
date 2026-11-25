import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { MarketKernel, SkillDetail, SkillTransferResult } from '../lib/types'

/**
 * 「把技能挂载到别的内核」表单（两端共用）：勾选多个目标内核并提交。
 *
 * 技能库按 slug 只存一份文件，这里只是给目标内核登记引用；不复制智能体分配。
 * 目标内核里已有同 slug 的共享技能时由后端拒绝（多选时该项失败、其余照常成功）。
 * 「挂载到全部」直接走批量接口：冲突的内核会被跳过并在结果里汇报。
 */
export function SkillCopyForm({
  skill,
  onCopied,
  onCancel,
}: {
  skill: SkillDetail
  onCopied: (result: SkillTransferResult, kernelName: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [kernels, setKernels] = useState<MarketKernel[]>([])
  const [targets, setTargets] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 操作完成后的汇总（成功 / 跳过情况），与错误 banner 分开展示。 */
  const [outcome, setOutcome] = useState<string | null>(null)

  useEffect(() => {
    api
      .marketKernels()
      .then((items) => {
        const mounted = new Set(skill.mounted_kernels ?? [])
        setKernels(
          items.filter((item) => item.type_id !== skill.type_id && !mounted.has(item.type_id)),
        )
      })
      .catch((e) => setError(describe(e)))
  }, [skill.type_id, skill.mounted_kernels])

  const toggleTarget = (typeId: string) => {
    setTargets((current) => {
      const next = new Set(current)
      if (next.has(typeId)) next.delete(typeId)
      else next.add(typeId)
      return next
    })
  }

  const selectAll = () => {
    setTargets(new Set(kernels.map((item) => item.type_id)))
  }

  const clearAll = () => {
    setTargets(new Set())
  }

  const copyAll = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.copySkillToAll(skill.id)
      const total = result.mounted.length
      const skipped = result.skipped.length
      const parts = [
        total === 0 && skipped === 0
          ? `「${result.name}」已挂在全部支持技能的内核上，无需操作`
          : `已把「${result.name}」挂载到 ${total} 个内核` +
            (skipped > 0 ? `，${skipped} 个内核被跳过（同名冲突等原因）` : ''),
        ...result.warnings,
      ]
      setOutcome(parts.join('\n'))
      await onCopied(
        { skill_id: skill.id, name: result.name, slug: skill.slug, type_id: skill.type_id },
        '全部内核',
      )
    } catch (e) {
      setError(describe(e))
    } finally {
      setBusy(false)
    }
  }

  const submit = async () => {
    if (targets.size === 0) {
      setError('请先勾选至少一个目标内核')
      return
    }
    setBusy(true)
    setError(null)
    const failures: string[] = []
    let last: SkillTransferResult | null = null
    let lastName = ''
    try {
      for (const typeId of targets) {
        try {
          last = await api.copySkill(skill.id, typeId)
          lastName = kernels.find((item) => item.type_id === typeId)?.name ?? typeId
        } catch (e) {
          failures.push(`${kernels.find((item) => item.type_id === typeId)?.name ?? typeId}：${describe(e)}`)
        }
      }
    } finally {
      setBusy(false)
    }
    if (last) {
      if (failures.length > 0) setOutcome(`部分内核挂载失败：\n${failures.join('\n')}`)
      await onCopied(last, lastName)
    } else {
      setError(failures.join('\n') || '挂载失败')
    }
  }

  return (
    <div className="stack" style={{ gap: 14 }}>
      <p className="dim small">
        把「{skill.name}」挂载到其它内核。技能文件在技能库里按 slug 只存一份，
        多个内核共用同一份：市场更新（或重新导入覆盖）后，所有挂载它的内核同时生效。
        只登记引用，不复制智能体分配；目标内核里已有同 slug 的共享技能时不会覆盖。
      </p>
      {kernels.length === 0 ? (
        <p className="dim small">（没有其它可挂载技能的内核）</p>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          <div className="row" style={{ gap: 9 }}>
            <span className="field-label">目标内核（可多选）</span>
            <button className="btn sm ghost" onClick={selectAll} disabled={busy}>
              全选
            </button>
            <button className="btn sm ghost" onClick={clearAll} disabled={busy}>
              清空
            </button>
          </div>
          {kernels.map((item) => (
            <label key={item.type_id} className="row" style={{ gap: 9, alignItems: 'center' }}>
              <input
                type="checkbox"
                checked={targets.has(item.type_id)}
                disabled={busy}
                onChange={() => toggleTarget(item.type_id)}
              />
              <span>{item.name}</span>
            </label>
          ))}
        </div>
      )}

      {outcome && <div className="banner ok">{outcome}</div>}
      {error && <div className="banner err">{error}</div>}

      <div className="row" style={{ gap: 9, justifyContent: 'flex-end' }}>
        <button className="btn ghost" onClick={onCancel} disabled={busy}>
          取消
        </button>
        <button className="btn" onClick={() => void copyAll()} disabled={busy || kernels.length === 0}>
          {busy ? '挂载中…' : '挂载到全部内核'}
        </button>
        <button
          className="btn primary"
          onClick={() => void submit()}
          disabled={busy || targets.size === 0}
        >
          {busy ? '挂载中…' : `挂载到选中的 ${targets.size || ''} 个内核`}
        </button>
      </div>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
