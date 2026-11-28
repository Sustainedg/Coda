/** 入口智能体聊天（`collab.chat.*`）的纯归约器与落库回显去重，与工单推进状态互不干扰。 */
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from './agentStream'
import { normalizeCollabChoices, type CollabAsk } from './collabAsk'
import type { CollabChatMessage, CollabChoice, CollabEvent, CollabStep } from './types'

export interface LiveChatReply {
  messageId: string
  roleKey: string
  agentName: string
  agent: AgentStreamState
}

export interface ChatTurnState {
  live: LiveChatReply | null
  /** 最近一次聊天结束后的提示：需求已同步、解析告警或执行错误。 */
  notice: { level: 'info' | 'warn' | 'error'; text: string } | null
  /**
   * CEO 判断「这条消息需要推进任务」时给出的决策卡（聊天结束后渲染）。
   * 来自 chat_finished 事件的 advance 字段，或刷新后从落库 meta 里恢复；
   * 用户点选 / 关闭 / 下一轮聊天开始时清空。
   */
  advance: CollabAsk | null
}

export function emptyChatTurn(): ChatTurnState {
  return { live: null, notice: null, advance: null }
}

/** 落库聊天行的最小形状：只取去重需要的字段，桌面端与移动端共用。 */
interface ChatEchoRow {
  id: string
  sender: string
  status: string
  content: string
}

/**
 * 藏掉落库聊天行里与本地乐观气泡重复的部分。
 *
 * 后端一次聊天会先落库用户消息，再建一条 `status=streaming`、正文为空的助手占位行，
 * 然后才开始生成。详情轮询（定时刷新 / 切回窗口立即补刷）若落在这段窗口里，会把两行都拉回来；
 * 此时本端还挂着 `pendingUser`（乐观用户气泡）和 `chatTurn.live`（实时助手气泡），
 * 不处理就会渲染成「你 → 空气泡 → 你 → 思考中」：同一句话出现两次，中间还夹个空泡。
 *
 * - 助手：实时气泡正在画的那一行（`liveMessageId`）以流式内容为准，落库副本藏掉；
 *   事件还没到达、拿不到 ID 的极短窗口里，按「聊天进行中 + streaming + 无正文」兜底。
 * - 用户：只有落库列表**尾部**那条用户消息与待确认正文一致时才藏 —— 它就是本端刚发出去
 *   那条的落库回显。同文的旧消息后面一定还跟着它自己的回复，不能误伤。
 *
 * 聊天结束后调用方会清掉 `pendingUser` / `chatTurn.live`，这里自然全部放行，不需要额外状态。
 */
export function hideChatEcho<T extends ChatEchoRow>(
  rows: T[],
  {
    liveMessageId,
    pendingUserContent,
    chatActive,
  }: {
    /** `collab.chat.started` 带来的助手消息 ID；事件未到时为空。 */
    liveMessageId?: string | null
    /** 本端待确认的用户气泡正文（附件清单已拼好）；没有待确认消息时为 null。 */
    pendingUserContent?: string | null
    /** 这一轮聊天是否仍在进行：进行中才需要藏「还没吐字的助手占位行」。 */
    chatActive: boolean
  },
): T[] {
  const pendingContent = pendingUserContent ?? ''
  // 回显只可能出现在列表尾部：最后一行是它，或它后面紧跟着本次的助手占位行。
  let echoIndex = -1
  if (pendingContent) {
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      if (rows[index].sender !== 'user') continue
      if (rows[index].content === pendingContent) echoIndex = index
      break
    }
  }
  const tail = rows[rows.length - 1]
  const echoesTail = echoIndex === rows.length - 1
    || (echoIndex === rows.length - 2 && tail?.sender === 'leader' && tail.status === 'streaming')

  const liveId = liveMessageId ?? ''
  return rows.filter((row, index) => {
    if (row.sender === 'user') return !(echoesTail && index === echoIndex)
    if (row.sender !== 'leader') return true
    // 实时气泡正在画这一行：落库版本此时可能还是空的，藏掉副本。
    if (liveId && row.id === liveId) return false
    return !(chatActive && row.status === 'streaming' && !row.content.trim())
  })
}

/** 用户点掉聊天里那条提示（需求已同步 / 解析告警 / 执行错误）：它是一次性的。 */
export function dismissChatNotice(state: ChatTurnState): ChatTurnState {
  return { ...state, notice: null }
}

/** 用户点选或关掉「推进任务」决策卡：这张卡是一次性的。 */
export function dismissChatAdvance(state: ChatTurnState): ChatTurnState {
  return { ...state, advance: null }
}

/** 把 chat_finished 的 advance（或落库 meta 里恢复的同构数据）规整成决策卡。 */
export function chatAdvanceFrom(raw: unknown, messageId = ''): CollabAsk | null {
  if (!raw || typeof raw !== 'object') return null
  const record = raw as Record<string, unknown>
  const order = typeof record.order === 'string' ? record.order.trim() : ''
  const reason = typeof record.reason === 'string' ? record.reason.trim() : ''
  const options = normalizeCollabChoices(record.options)
  if (!order && !reason && options.length === 0) return null
  const question = reason || order || 'CEO 判断这条消息需要推进任务'
  const choices: CollabChoice[] = options.length > 0
    ? options
    : [
        { label: '按建议推进', value: order, freeform: false },
        { label: '我再想想', value: '', freeform: true },
      ]
  return { ticketId: '', messageId, question, options: choices }
}

/** 后端写的是 UTC 的 naive 时间（`2026-11-16 15:03:28.184362`）：补上 T/Z 再解析。 */
function collabTimeMs(value: string | null | undefined): number | null {
  if (!value) return null
  const normalized = value.includes('T') ? value : value.replace(' ', 'T')
  const ms = Date.parse(normalized.endsWith('Z') ? normalized : `${normalized}Z`)
  return Number.isFinite(ms) ? ms : null
}

/**
 * 这条 CEO 聊天里的推进卡是否已经被之后的用户指令消费掉了。
 *
 * 工单通道的用户消息不在 `chat_messages` 里，而是 `collab_steps` 的 `role_key=user`；
 * 只要有这样一条消息晚于 CEO 的回复，卡上的 `order` 就已经发出去执行了。旧数据里
 * `meta.advance` 不会自动消失，这个判断保证刷新/换设备后不会把旧卡重新弹出来。
 */
export function advanceOvertakenByTicketSteps(
  chat: CollabChatMessage,
  steps: CollabStep[],
): boolean {
  const repliedAt = collabTimeMs(chat.finished_at ?? chat.created_at)
  if (repliedAt === null) return false
  let latestUserStep: number | null = null
  for (const step of steps) {
    if (step.role_key !== 'user') continue
    const at = collabTimeMs(step.started_at)
    if (at !== null && (latestUserStep === null || at > latestUserStep)) latestUserStep = at
  }
  return latestUserStep !== null && latestUserStep > repliedAt
}

export function applyChatEvent(state: ChatTurnState, event: CollabEvent): ChatTurnState {
  switch (event.type) {
    case 'collab.chat.started':
      return {
        ...state,
        notice: null,
        advance: null,
        live: {
          messageId: event.message_id ?? '',
          roleKey: event.role_key ?? '',
          agentName: event.agent_name ?? '领导者',
          agent: emptyAgentStream(),
        },
      }

    case 'collab.chat.event': {
      if (!state.live || !event.event || (event.message_id && event.message_id !== state.live.messageId)) {
        return state
      }
      return { ...state, live: { ...state.live, agent: applyAgentEvent(state.live.agent, event.event) } }
    }

    case 'collab.chat.finished': {
      const applied = event.applied
      let text = event.warning ?? ''
      let level: 'info' | 'warn' | 'error' = text ? 'warn' : 'info'
      if (event.status === 'error') {
        text = event.error ?? 'CEO 这次回复失败了，可以再试一次'
        level = 'error'
      } else if (event.status === 'cancelled') {
        text = event.error ?? '这条 CEO 回复已停止'
        level = 'warn'
      } else if (applied?.ticket_count) {
        const fields = applied.fields && applied.fields.length > 0
          ? `（${applied.fields.join('、')}）`
          : ''
        const line = `已把新要求同步到 ${applied.ticket_count} 张进行中的工单${fields}，下一轮步骤生效`
        text = text ? `${text}；${line}` : line
        level = text === line ? 'info' : 'warn'
      }
      const advance = event.advance ?? null
      if (advance && event.status === 'complete' && !event.error) {
        const line = 'CEO 判断这条消息需要推进任务：点下面的选项确认后才进入工单通道'
        text = text ? `${text}；${line}` : line
        level = 'warn'
      }
      return { live: null, notice: text ? { level, text } : null, advance: chatAdvanceFrom(advance, event.message_id ?? '') }
    }

    default:
      return state
  }
}
