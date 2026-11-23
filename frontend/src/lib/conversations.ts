/**
 * 「对话有改动」的广播（新建 / 重命名 / 删除）。
 *
 * 会改对话的是对话页（发送、删除），画面却画在左侧栏的列表里 —— 两者不在同一棵组件树上，
 * 用一个自定义事件对齐：改完喊一声，列表立刻重新拉一次，不用干等下一次轮询（最多 8 秒）。
 */
import { useEffect } from 'react'
import { draftOwnerIds } from './composerDraft'
import type { Conversation } from './types'

const EVENT = 'coda:conversations-changed'

/** 兜底标题（与后端 `DEFAULT_TITLE` 同值）：标题还是它 = 用户从没发过第一条消息。 */
export const NEW_CONVERSATION_TITLE = '新对话'

/**
 * 侧栏排序：先「AI 还在回复」，再「已经回复完」，两组内部都按「最近活动」倒序。
 *
 * 为什么不是单纯按时间：一轮长回复期间用户可能又开了好几条新对话，只按时间排会把
 * 还在出字的那条挤到下面去。所以「在跑」整组置顶，组内才比时间。
 *
 * 后端已经按同样的口径排过（见 `runtime/conversations.py:order_by_recent_activity`），
 * 这里再排一次是为了两件事：① 本地乐观插入（新建对话 / 刚发完消息）后不必等下一轮
 * 接口就能立刻到顶；② 万一某个部署的后端还在返回旧顺序，界面也不会跟着错。
 *
 * `runningIds` 用列表接口的 `active_runs` 拼出来（`conversation_id` 去重）；
 * 不传 = 没有正在跑的对话，退化成纯按时间排。
 */
export function sortConversationsByRecent(
  items: Conversation[],
  runningIds: ReadonlySet<string> = new Set(),
): Conversation[] {
  return [...items].sort((left, right) => {
    const leftRunning = runningIds.has(left.id)
    const rightRunning = runningIds.has(right.id)
    if (leftRunning !== rightRunning) return leftRunning ? -1 : 1
    return Date.parse(right.updated_at) - Date.parse(left.updated_at)
  })
}

/** 从列表快照的 `active_runs` 里取出「正在回复」的对话 id（同一条对话可能有多轮运行）。 */
export function runningConversationIds(activeRuns: { conversation_id: string }[]): Set<string> {
  return new Set(activeRuns.map((run) => run.conversation_id))
}

/** 广播一次「对话列表变了」。 */
export function notifyConversationsChanged(): void {
  window.dispatchEvent(new Event(EVENT))
}

/**
 * 从列表里挑一条「从未被使用」的空对话：连点「新建对话」时直接跳回它，不再堆出一排
 * 「新对话」。判定标准（须同时满足）：
 *
 * 1. 一条消息都没发过（`message_count` 为 0）；
 * 2. 标题还是兜底值「新对话」——发第一条消息时后端就把标题换成首行了，所以标题
 *    还没动过 = 从没说过话；用户手动改过名的也跳过，那是人家起了名字要留着的；
 * 3. 输入框里**没有**草稿（本地记录）——空对话但打了字，说明用户正在这条里酝酿，
 *    这时再点新建就得真开一条，别把人家打到一半的字「藏」进旧对话里。
 *
 * 多条都符合时取列表里的第一条（后端按最近活动排过序）。没有符合的返回 `null`。
 */
export function findUnusedConversation(items: Conversation[]): Conversation | null {
  const drafted = draftOwnerIds()
  for (const conversation of items) {
    if (conversation.message_count > 0) continue
    if (conversation.title !== NEW_CONVERSATION_TITLE) continue
    if (drafted.has(conversation.id)) continue
    return conversation
  }
  return null
}

/** 订阅广播（`handler` 请用 `useCallback` 包一层，避免每次渲染重订阅）。 */
export function useConversationsChanged(handler: () => void): void {
  useEffect(() => {
    window.addEventListener(EVENT, handler)
    return () => window.removeEventListener(EVENT, handler)
  }, [handler])
}
