/**
 * 任务进度卡片（只读）。
 *
 * 挂在普通对话消息流里，显示「当前这件事做到哪了」：标题 + 已完成数 + 进度条 +
 * 任务项状态。**不提供任何手动勾选 / 编辑入口** —— 任务项是纯文本 + 状态标记，
 * 服务端只按模型声明更新；唯一可点击的是标题栏的折叠按钮，只切换显隐、不改数据。
 */
import { useCallback, useState } from 'react'
import type { TaskCard } from '../lib/types'
import { loadTaskCardCollapsed, saveTaskCardCollapsed } from '../lib/taskCardCollapse'
import { ClipboardIcon } from './Icons'

export function TaskProgressCard({ card }: { card: TaskCard }) {
  // 折叠状态是本机偏好：初始值从 localStorage 读，卡片 id 变化时组件会以新 key 重挂载。
  const [collapsed, setCollapsed] = useState(() => loadTaskCardCollapsed(card.id))
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      saveTaskCardCollapsed(card.id, next)
      return next
    })
  }, [card.id])

  const items = card.items ?? []
  const total = items.length
  const done = items.filter((item) => item.done).length
  const percent = total > 0 ? Math.round((done / total) * 100) : 0

  return (
    <div className="task-progress-card">
      <div className="task-progress-head">
        <span className="task-progress-icon" aria-hidden="true">
          <ClipboardIcon size={14} />
        </span>
        <span className="task-progress-title">{card.title?.trim() || '任务进度'}</span>
        <span className="task-progress-count">进度 {done}/{total}</span>
        <span className="task-progress-bar" aria-hidden="true">
          <span className="task-progress-bar-fill" style={{ width: `${percent}%` }} />
        </span>
        <button
          type="button"
          className="task-progress-toggle"
          aria-expanded={!collapsed}
          onClick={toggle}
        >
          {collapsed ? '展开' : '收起'}
        </button>
      </div>
      {!collapsed && (
        <ul className="task-progress-items">
          {items.map((item) => (
            <li key={item.id} className={`task-progress-item${item.done ? ' is-done' : ''}`}>
              <span className={`task-progress-mark${item.done ? ' is-done' : ''}`} aria-hidden="true">
                {item.done ? '✓' : '○'}
              </span>
              <span className="task-progress-text">{item.text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
