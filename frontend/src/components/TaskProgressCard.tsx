/**
 * 任务进度卡片（只读）。
 *
 * 常驻在输入框上方（见 ChatPage / MobileChatPage 的 composer header），显示
 * 「当前这件事做到哪了」：标题 + 已完成数 + 进度条 + 任务项状态。**不提供任何手动
 * 勾选 / 编辑入口** —— 任务项是纯文本 + 状态标记，服务端只按模型声明更新；唯一
 * 可点击的是整条标题栏，只切换显隐、不改数据。默认收起，只占一行。
 */
import { useCallback, useState } from 'react'
import type { TaskCard, TaskCardItem, TaskCardStatus } from '../lib/types'
import { loadTaskCardCollapsed, saveTaskCardCollapsed } from '../lib/taskCardCollapse'
import { ClipboardIcon } from './Icons'

/** 旧卡片行没有 status：按 done 推导，保证新旧数据渲染一致。 */
function itemStatus(item: TaskCardItem): TaskCardStatus {
  return item.status ?? (item.done ? 'completed' : 'pending')
}

const STATUS_LABELS: Record<TaskCardStatus, string> = {
  completed: '已完成',
  in_progress: '进行中',
  pending: '待办',
}

export function TaskProgressCard({ card }: { card: TaskCard }) {
  // 折叠状态是本机偏好：初始值从 localStorage 读（没记录时默认收起）。
  // 卡片 id 变化时由使用方以 `key={card.id}` 重挂载，重新读取这条对话的偏好。
  const [collapsed, setCollapsed] = useState(() => loadTaskCardCollapsed(card.id, true))
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      saveTaskCardCollapsed(card.id, next)
      return next
    })
  }, [card.id])

  const items = card.items ?? []
  const total = items.length
  const done = items.filter((item) => itemStatus(item) === 'completed').length
  const doing = items.filter((item) => itemStatus(item) === 'in_progress').length
  const percent = total > 0 ? Math.round((done / total) * 100) : 0
  const countText = doing > 0 ? `进度 ${done}/${total} · 进行中 ${doing}` : `进度 ${done}/${total}`

  return (
    <div className="task-progress-card">
      <button
        type="button"
        className="task-progress-head"
        aria-expanded={!collapsed}
        title={collapsed ? '点开查看任务项' : '收起任务项'}
        onClick={toggle}
      >
        <span className="task-progress-icon" aria-hidden="true">
          <ClipboardIcon size={14} />
        </span>
        <span className="task-progress-title">{card.title?.trim() || '任务进度'}</span>
        <span className="task-progress-count">{countText}</span>
        <span className="task-progress-bar" aria-hidden="true">
          <span className="task-progress-bar-fill" style={{ width: `${percent}%` }} />
        </span>
        <span className="task-progress-toggle" aria-hidden="true">
          {collapsed ? '展开' : '收起'}
        </span>
      </button>
      {!collapsed && (
        <ul className="task-progress-items">
          {items.map((item) => {
            const status = itemStatus(item)
            return (
              <li
                key={item.id}
                className={`task-progress-item${
                  status === 'completed' ? ' is-done' : status === 'in_progress' ? ' is-active' : ''
                }`}
              >
                <span
                  className={`task-progress-mark${
                    status === 'completed' ? ' is-done' : status === 'in_progress' ? ' is-active' : ''
                  }`}
                  aria-hidden="true"
                >
                  {status === 'completed' ? '✓' : status === 'in_progress' ? '◐' : '○'}
                </span>
                <span className="task-progress-text">{item.text}</span>
                <span className="task-progress-state">{STATUS_LABELS[status]}</span>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
