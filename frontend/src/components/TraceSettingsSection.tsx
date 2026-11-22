import { useState } from 'react'
import { useAuth } from './AuthGate'
import { api, ApiError } from '../lib/api'
import { saveUser } from '../lib/auth'

/**
 * 「设置 → 对话 → 调试模式」开关（方案 §6.1）。
 *
 * 权威真值在 `users.trace_debug`：开关只读 `user.trace_debug`，修改必须走
 * `PUT /api/auth/me` 并由后端回传新值。前端**不**用 localStorage 或组件本地
 * 状态绕过它 —— 采集是后端在运行层判定的，本地改不了。
 *
 * 打开后从**下一轮**运行开始采集；关闭后不再产生新轨迹，历史轨迹仍只读可查。
 */
export function TraceSettingsSection() {
  const { user, refreshUser } = useAuth()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const enabled = user?.trace_debug === true
  // 鉴权关闭的部署里当前用户是虚拟管理员（`__system__`），后端没有个人设置可写。
  const readOnly = !user || user.id === '__system__'

  const toggle = async (next: boolean) => {
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      const updated = await api.updateMe({ trace_debug: next })
      saveUser(updated)
      refreshUser(updated)
      setSaved(next ? '调试模式已开启：下一轮开始采集轨迹' : '调试模式已关闭：下一轮不再采集')
    } catch (cause) {
      setError(cause instanceof ApiError ? `${cause.status} · ${cause.message}` : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card trace-settings">
      <div className="card-title">调试模式</div>
      <p className="dim small">
        开启后，对话页新建图标旁会出现调试按钮，可查看每一轮的模型请求 / 响应、上下文快照与工具调用
        时间线（只读）。开关存在后端（<span className="mono-sm">users.trace_debug</span>），
        关闭时后端同样不采集，不是只把按钮藏起来。
      </p>
      {error && <div className="banner err">{error}</div>}
      {saved && <div className="banner ok">{saved}</div>}
      <label className="switch">
        <input
          type="checkbox"
          checked={enabled}
          disabled={busy || readOnly}
          onChange={(event) => void toggle(event.target.checked)}
        />
        <span>开启调试模式（轨迹调试视图）</span>
      </label>
      {readOnly ? (
        <p className="field-help">
          当前部署关闭了鉴权（<span className="mono-sm">CODA_AUTH_ENABLED=false</span>），
          没有用户级设置可改，调试模式不可用。
        </p>
      ) : (
        <p className="field-help">
          开关按用户生效，只影响你自己的运行；打开后从下一轮开始采集，刷新 / 重启后仍可回看。
        </p>
      )}
    </section>
  )
}
