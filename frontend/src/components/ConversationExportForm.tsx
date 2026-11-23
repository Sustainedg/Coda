/**
 * 对话导出弹窗里的选项表单：选格式（图片 / Markdown）与范围（正文 / 完整数据）。
 *
 * 桌面端塞进 `Modal`、移动端塞进 `MobileSheet`，两端共用这一份表单；
 * 真正的导出动作由页面传入（页面手里才有当前对话的消息与主题）。
 */
import { useState } from 'react'
import type { ExportChoice, ExportFormat, ExportScope } from '../lib/conversationExport'

const FORMAT_OPTIONS: { value: ExportFormat; label: string; desc: string }[] = [
  {
    value: 'image',
    label: '图片（PNG）',
    desc: '生成一张长图，适合直接发给别人看',
  },
  {
    value: 'markdown',
    label: 'Markdown（.md）',
    desc: '纯文本格式，方便保存、检索和再次编辑',
  },
]

const SCOPE_OPTIONS: { value: ExportScope; label: string; desc: string }[] = [
  {
    value: 'content',
    label: '对话内容',
    desc: '只包含你和智能体说出来的正文',
  },
  {
    value: 'full',
    label: '完整数据',
    desc: '额外包含思考过程、工具调用入参与工具结果',
  },
]

export function ConversationExportForm({
  busy,
  error,
  onClose,
  onExport,
}: {
  /** 正在生成文件：按钮置灰，避免连点导出两份 */
  busy: boolean
  /** 导出失败时的提示（成功后弹窗会关掉，不留在这里） */
  error?: string | null
  onClose: () => void
  onExport: (choice: ExportChoice) => void
}) {
  const [format, setFormat] = useState<ExportFormat>('image')
  const [scope, setScope] = useState<ExportScope>('content')

  const submit = () => {
    if (busy) return
    onExport({ format, scope })
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      {error && <div className="banner err" style={{ margin: 0 }}>{error}</div>}

      <section className="stack" style={{ gap: 8 }}>
        <span className="field-label">导出格式</span>
        <div className="stack" style={{ gap: 6 }} role="radiogroup" aria-label="导出格式">
          {FORMAT_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`agent-pick${format === option.value ? ' active' : ''}`}
              onClick={() => setFormat(option.value)}
              role="radio"
              aria-checked={format === option.value}
              disabled={busy}
            >
              <span className="agent-pick-main">
                <span className="agent-pick-name">{option.label}</span>
                <span className="card-sub">{option.desc}</span>
              </span>
              {format === option.value && <span className="pill ok">已选</span>}
            </button>
          ))}
        </div>
      </section>

      <section className="stack" style={{ gap: 8 }}>
        <span className="field-label">导出内容</span>
        <div className="stack" style={{ gap: 6 }} role="radiogroup" aria-label="导出内容">
          {SCOPE_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`agent-pick${scope === option.value ? ' active' : ''}`}
              onClick={() => setScope(option.value)}
              role="radio"
              aria-checked={scope === option.value}
              disabled={busy}
            >
              <span className="agent-pick-main">
                <span className="agent-pick-name">{option.label}</span>
                <span className="card-sub">{option.desc}</span>
              </span>
              {scope === option.value && <span className="pill ok">已选</span>}
            </button>
          ))}
        </div>
      </section>

      <div className="export-actions">
        <button className="btn ghost" onClick={onClose} disabled={busy}>
          取消
        </button>
        <button className="btn primary" onClick={submit} disabled={busy}>
          {busy ? '正在导出…' : format === 'image' ? '导出图片' : '导出 Markdown'}
        </button>
      </div>
    </div>
  )
}
