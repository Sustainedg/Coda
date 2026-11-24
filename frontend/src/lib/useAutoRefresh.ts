/**
 * 跨设备同步的轻量刷新：页面可见时定时拉取，回到前台/切回标签页时立即拉取。
 *
 * 不引入长连接或状态库：移动端与桌面端共用同一个后端，定时对齐详情即可让
 * 另一台设备上产生的新消息、工单与运行状态在数秒内出现。刷新函数通过 ref 读取，
 * 不会因为每次渲染生成新闭包而反复重建定时器。
 */
import { useEffect, useRef } from 'react'
import type { Conversation, Message, TaskCard } from './types'

interface AutoRefreshOptions {
  intervalMs?: number
  enabled?: boolean
  refreshOnFocus?: boolean
}

export function useAutoRefresh(
  refresh: () => void | Promise<unknown>,
  { intervalMs = 4000, enabled = true, refreshOnFocus = true }: AutoRefreshOptions = {},
) {
  const latest = useRef(refresh)
  latest.current = refresh

  useEffect(() => {
    if (!enabled) return
    let timer: number | undefined
    let inFlight = false

    const tick = async () => {
      if (inFlight || document.visibilityState === 'hidden') return
      inFlight = true
      try {
        await latest.current()
      } finally {
        inFlight = false
      }
    }

    timer = window.setInterval(() => void tick(), Math.max(1000, intervalMs))
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void tick()
    }
    const onFocus = () => void tick()
    if (refreshOnFocus) {
      document.addEventListener('visibilitychange', onVisibility)
      window.addEventListener('focus', onFocus)
    }
    return () => {
      if (timer !== undefined) window.clearInterval(timer)
      if (refreshOnFocus) {
        document.removeEventListener('visibilitychange', onVisibility)
        window.removeEventListener('focus', onFocus)
      }
    }
  }, [enabled, intervalMs, refreshOnFocus])
}

export function sameMessageList(left: Message[], right: Message[]): boolean {
  if (left.length !== right.length) return false
  return left.every((message, index) => {
    const next = right[index]
    return (
      message.id === next.id &&
      message.content === next.content &&
      message.status === next.status &&
      message.error === next.error
    )
  })
}

/**
 * 任务进度卡片是否没变：只比 `id` 与 `version`。
 *
 * 卡片状态与消息状态分开比较：`sameMessageList` 只比消息的四个字段，
 * 卡片内容变了不会触发它；这里单独判断，避免 3 秒轮询每次都重渲染整张卡。
 */
export function sameTaskCard(
  left: TaskCard | null | undefined,
  right: TaskCard | null | undefined,
): boolean {
  if (!left || !right) return !left && !right
  return left.id === right.id && left.version === right.version
}

export function sameConversationList(left: Conversation[], right: Conversation[]): boolean {
  if (left.length !== right.length) return false
  return left.every((conversation, index) => {
    const next = right[index]
    return (
      conversation.id === next.id &&
      conversation.updated_at === next.updated_at &&
      conversation.message_count === next.message_count &&
      conversation.title === next.title &&
      Boolean(conversation.unread) === Boolean(next.unread)
    )
  })
}
