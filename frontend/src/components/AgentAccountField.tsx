import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { RuntimeAccount } from '../lib/types'
import { useAuth } from './AuthGate'

interface Props {
  /** 当前选中的内核 id（账号槽位按内核隔离） */
  typeId: string
  /** 该内核是否支持多账号（`/api/agent-types` 的 `supports_accounts`） */
  supportsAccounts?: boolean
  /** 当前选中的账号槽位；空串 = 跟随内核当前登录的账号 */
  value: string
  onChange: (value: string) => void
  disabled?: boolean
}

/**
 * 智能体设定页的「内核账号」下拉框（多账号内核、官方链路专用）。
 *
 * 账号槽位是**机器级**资产：内核只保留一份登录态，平台把每次登录存成槽位。
 * 只有管理员能查看与指定；普通成员不拉取账号清单（`/runtimes/{id}/auth` 会探测内核登录态），
 * 只读展示当前智能体的设置，编辑其它字段时原样带回、不会改到它。
 */
export function AgentAccountField({
  typeId,
  supportsAccounts,
  value,
  onChange,
  disabled,
}: Props) {
  const { isAdmin } = useAuth()
  const [accounts, setAccounts] = useState<RuntimeAccount[] | null | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // 普通成员拿不到账号清单，也不必为打不开的下拉框去探测内核登录态。
    if (!supportsAccounts || !isAdmin) {
      setAccounts(undefined)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    void api
      .getRuntimeAuth(typeId)
      .then((auth) => {
        if (!cancelled) setAccounts(auth.accounts ?? null)
      })
      .catch((e) => {
        if (!cancelled) {
          setAccounts(null)
          setError(describe(e))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [typeId, supportsAccounts, isAdmin])

  if (!supportsAccounts) return null

  // 机器级账号只对管理员开放；成员只读展示（保存时原样带回，后端会放行「保持原值」）。
  if (!isAdmin) {
    return (
      <div className="field">
        <label className="field-label">内核账号</label>
        <input
          className="input"
          value={value ? `${value}（由管理员指定）` : '跟随内核当前登录的账号'}
          readOnly
        />
        <span className="field-help">
          内核账号是机器级配置，只有管理员能查看与指定；编辑其它设置不会改到它。
        </span>
      </div>
    )
  }

  const known = accounts?.some((account) => account.id === value) ?? false
  const help = (() => {
    if (loading || accounts === undefined) return '正在读取已保存的内核账号…'
    if (error) return `读取账号清单失败：${error}（仍可使用当前登录的账号）`
    if (accounts === null) {
      return '内核账号是机器级配置，只有管理员能查看与指定具体账号；留空表示跟随内核当前登录的账号。'
    }
    if (accounts.length === 0) {
      return '还没有保存的账号：到「设置 → 内核管理」登录一次后会自动保存，再回到这里指定。'
    }
    return '只对之后的对话生效；正在运行的任务不会中途换号。留空则跟随内核当前登录的账号。'
  })()

  return (
    <div className="field">
      <label className="field-label">内核账号</label>
      <select
        className="select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || loading}
      >
        <option value="">跟随当前登录账号（内核默认）</option>
        {accounts?.map((account) => (
          <option key={account.id} value={account.id}>
            {account.label}
            {account.active ? '（当前登录）' : ''}
          </option>
        ))}
        {value && !known && <option value={value}>{value}（账号已失效）</option>}
      </select>
      <span className="field-help">{help}</span>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
