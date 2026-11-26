/**
 * 输入框上方的「上下文用量」按钮 + 详情面板（两端共用）。
 *
 * 按钮上一眼能看到：已用 token / 上限 token / 百分比。点开是详情：
 * 当前已用、可配置上限、触发阈值、压缩策略，以及最近一次自动压缩的状态
 * （进行中 / 成功 / 失败）。上限与阈值就在这里改，保存后立即生效并持久化。
 *
 * **不在前端估算 token**：`used / limit / threshold` 全部来自后端同一份估算
 * （见 `lib/contextUsage` 的说明）。前端只做输入格式的即时校验，边界值以服务端为准。
 *
 * 面板贴着按钮向上展开（与输入区齿轮的弹出层同一观感），点外面或按 Esc 收起；
 * 打开面板 / 保存配置都不会禁用输入框 —— 用户随时可以继续打字。
 */
import { useEffect, useRef, useState } from 'react'
import { checkContextInput, checkThresholdInput, formatTokenCount } from '../lib/contextUsage'
import type { ContextSettings, ContextUsage } from '../lib/types'
import { UsageIcon } from './Icons'

interface Props {
  usage: ContextUsage | null
  settings: ContextSettings | null
  loading?: boolean
  /** 读取失败（网络 / 接口） */
  error?: string | null
  saving?: boolean
  /** 保存失败 / 校验失败的原因 */
  saveError?: string | null
  /** 保存成功的一次性提示 */
  notice?: string | null
  onSave: (limitTokens: number, thresholdRatio: number, limitMode?: 'auto' | 'manual') => Promise<boolean>
  disabled?: boolean
}

/** 压缩状态徽标文案与色调。 */
function compactionLabel(usage: ContextUsage | null): { text: string; tone: string } | null {
  const state = usage?.compaction
  if (!state) return null
  if (state.status === 'compacting') return { text: '压缩中', tone: 'busy' }
  if (state.status === 'error') return { text: '压缩失败', tone: 'error' }
  return { text: '已压缩', tone: 'ok' }
}

export function ContextMeter({
  usage,
  settings,
  loading = false,
  error = null,
  saving = false,
  saveError = null,
  notice = null,
  onSave,
  disabled = false,
}: Props) {
  const [open, setOpen] = useState(false)
  const [limitText, setLimitText] = useState('')
  const [thresholdText, setThresholdText] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  /** 上限模式：auto = 跟随内核上报的真实窗口；manual = 用户显式设定。 */
  const [mode, setMode] = useState<'auto' | 'manual'>('manual')
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  /** 表单是否已经用当前配置填过一次：只填一次，避免轮询刷新把用户正在编辑的输入框覆盖掉。 */
  const formSeeded = useRef(false)

  // 点外面 / 按 Esc 收起（与输入区齿轮菜单同一套收法）。
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  /** 打开时把输入框同步成当前值（不在渲染期改状态，避免受控输入来回跳）。 */
  const openPanel = () => {
    const limit = usage?.limit_tokens ?? settings?.limit_tokens
    const ratio = usage?.threshold_ratio ?? settings?.threshold_ratio
    setLimitText(limit ? String(limit) : '')
    setThresholdText(ratio !== undefined ? String(Math.round(ratio * 100)) : '')
    setMode(settings?.limit_mode ?? usage?.limit_mode ?? 'manual')
    formSeeded.current = Boolean(limit && ratio !== undefined)
    setFormError(null)
    setOpen(true)
  }

  // 面板打开得太快（用量还在路上）：等第一次数据到了把空输入框补上。
  // 只补一次 —— 轮询会持续更新 usage，不能拿它覆盖用户正在编辑的内容。
  useEffect(() => {
    if (!open || !usage || formSeeded.current) return
    setLimitText(String(usage.limit_tokens))
    setThresholdText(String(Math.round(usage.threshold_ratio * 100)))
    setMode(settings?.limit_mode ?? usage.limit_mode ?? 'manual')
    formSeeded.current = true
  }, [open, usage, settings])

  /** 切到「跟随内核」：上限输入框变成只读，直接显示内核窗口（没有就显示当前生效值）。 */
  const switchMode = (next: 'auto' | 'manual') => {
    setMode(next)
    setFormError(null)
    if (next === 'auto') {
      const effective = usage?.limit_tokens ?? settings?.limit_tokens
      if (effective) setLimitText(String(effective))
    } else {
      // 手动模式回到用户自己填过的值（没有就用当前生效值当起点）。
      const configured = usage?.configured_limit_tokens ?? settings?.limit_tokens
      if (configured) setLimitText(String(configured))
    }
  }

  const submit = async () => {
    if (!usage || !settings) return
    if (mode === 'auto') {
      // 上限不由用户填（跟随内核真实窗口）：只校验阈值，上限用当前生效值。
      const checked = checkThresholdInput(thresholdText)
      if (!checked.ok) {
        setFormError(checked.error)
        return
      }
      setFormError(null)
      await onSave(usage.limit_tokens, checked.ratio, 'auto')
      return
    }
    const checked = checkContextInput(
      limitText,
      thresholdText,
      usage.used_tokens,
      settings.min_limit_tokens,
    )
    if (!checked.ok) {
      setFormError(checked.error)
      return
    }
    setFormError(null)
    await onSave(checked.limit, checked.ratio, 'manual')
  }

  const used = usage?.used_tokens ?? 0
  const limit = usage?.limit_tokens ?? settings?.limit_tokens ?? 0
  const percent = usage?.percent ?? 0
  const thresholdPercent = Math.round((usage?.threshold_ratio ?? 0) * 100)
  const tone =
    !usage || limit <= 0
      ? ''
      : used >= limit
        ? ' danger'
        : used >= (usage.threshold_tokens ?? limit)
          ? ' warn'
          : ''
  const badge = compactionLabel(usage)
  const state = usage?.compaction

  return (
    <div className={`ctx-meter${open ? ' open' : ''}`} ref={wrapperRef}>
      <button
        type="button"
        className={`ctx-meter-btn${tone}`}
        onClick={() => (open ? setOpen(false) : openPanel())}
        aria-expanded={open}
        aria-haspopup="dialog"
        disabled={disabled}
        title={
          usage
            ? `上下文已用 ${used.toLocaleString()} / ${limit.toLocaleString()} tokens（${percent}%）`
            : '上下文用量'
        }
      >
        <UsageIcon size={15} />
        <span className="ctx-meter-text">
          上下文 {usage ? formatTokenCount(used) : loading ? '…' : '—'}/
          {usage ? formatTokenCount(limit) : '—'}
        </span>
        <span className="ctx-meter-percent">
          {usage ? `${Math.round(percent)}%` : '—'}
        </span>
        {badge && <span className={`ctx-meter-badge ${badge.tone}`}>{badge.text}</span>}
      </button>

      {open && (
        <div className="ctx-panel" role="dialog" aria-label="上下文用量详情">
          <div className="ctx-panel-head">
            <span className="ctx-panel-title">上下文用量</span>
            <button
              type="button"
              className="ctx-panel-close"
              onClick={() => setOpen(false)}
              aria-label="收起"
            >
              ✕
            </button>
          </div>

          {error && <div className="ctx-error">{error}</div>}

          {usage && (
            <>
              <div
                className="ctx-bar"
                title={`${used.toLocaleString()} / ${limit.toLocaleString()} tokens`}
              >
                <span
                  className={`ctx-bar-fill${tone}`}
                  style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
                />
                <span
                  className="ctx-bar-threshold"
                  style={{ left: `${Math.min(100, Math.max(0, thresholdPercent))}%` }}
                  title={`触发阈值 ${thresholdPercent}%`}
                />
              </div>

              <dl className="ctx-rows">
                <div>
                  <dt>当前已用</dt>
                  <dd>{used.toLocaleString()} tokens</dd>
                </div>
                <div>
                  <dt>可配置上限</dt>
                  <dd>{limit.toLocaleString()} tokens</dd>
                </div>
                <div>
                  <dt>内核窗口</dt>
                  <dd>
                    {usage.kernel_window_tokens
                      ? `${usage.kernel_window_tokens.toLocaleString()} tokens`
                      : '内核未上报'}
                  </dd>
                </div>
                <div>
                  <dt>触发阈值</dt>
                  <dd>
                    {(usage.threshold_tokens ?? 0).toLocaleString()} tokens（{thresholdPercent}%）
                  </dd>
                </div>
                <div>
                  <dt>压缩策略</dt>
                  <dd>{usage.strategy}</dd>
                </div>
              </dl>

              {state && (
                <div className={`ctx-status ${state.status}`}>
                  {state.status === 'compacting' && '正在自动压缩较早的上下文…'}
                  {state.status === 'ok' &&
                    `最近一次自动压缩成功：折叠 ${state.dropped ?? 0} 条，约 ${
                      state.before_tokens ?? 0
                    } → ${state.after_tokens ?? 0} tokens`}
                  {state.status === 'error' &&
                    `自动压缩失败，已保留原上下文：${state.error ?? '原因未知'}`}
                </div>
              )}

              <form
                className="ctx-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  void submit()
                }}
              >
                <div className="ctx-mode" role="group" aria-label="上限来源">
                  <button
                    type="button"
                    className={mode === 'auto' ? 'ctx-mode-btn on' : 'ctx-mode-btn'}
                    onClick={() => switchMode('auto')}
                    aria-pressed={mode === 'auto'}
                  >
                    跟随内核
                  </button>
                  <button
                    type="button"
                    className={mode === 'manual' ? 'ctx-mode-btn on' : 'ctx-mode-btn'}
                    onClick={() => switchMode('manual')}
                    aria-pressed={mode === 'manual'}
                  >
                    手动设置
                  </button>
                </div>
                <label className="ctx-field">
                  <span>上限（tokens）</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    value={limitText}
                    onChange={(event) => setLimitText(event.target.value)}
                    aria-label="上下文上限（tokens）"
                    disabled={mode === 'auto'}
                  />
                </label>
                <label className="ctx-field">
                  <span>触发阈值（%）</span>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={thresholdText}
                    onChange={(event) => setThresholdText(event.target.value)}
                    aria-label="触发阈值（百分比）"
                  />
                </label>
                <button type="submit" className="ctx-save" disabled={saving}>
                  {saving ? '保存中…' : '保存'}
                </button>
              </form>

              {mode === 'auto' && (
                <p className="ctx-note">
                  {usage.kernel_window_tokens
                    ? `上限自动跟随内核上报的窗口（${usage.kernel_window_tokens.toLocaleString()} tokens）；内核没上报时按 ${(
                        usage.configured_limit_tokens ?? limit
                      ).toLocaleString()} tokens 计。`
                    : `该内核没有上报真实窗口，上限按 ${(
                        usage.configured_limit_tokens ?? limit
                      ).toLocaleString()} tokens 计；切到「手动设置」可自己指定。`}
                </p>
              )}

              {(formError || saveError) && (
                <div className="ctx-error">{formError || saveError}</div>
              )}
              {notice && <div className="ctx-notice">{notice}</div>}
              <p className="ctx-note">{usage.estimation}</p>
            </>
          )}

          {!usage && !error && <p className="ctx-note">正在读取上下文用量…</p>}
        </div>
      )}
    </div>
  )
}
