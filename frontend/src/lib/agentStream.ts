/**
 * 智能体事件的纯归约器：把 `message.delta` 之类的规范化事件并进一次流式发言的状态。
 *
 * 对话页与协作页共用这一份逻辑（协作把内嵌的原始事件包在 `collab.step.event` 里透传过来），
 * 避免两处各写一遍导致行为漂移。这里不碰 DOM、不发请求，便于单测与复用。
 */
import type { AgentEvent, MessagePart } from './types'

export type StreamStatus = 'running' | 'done' | 'error' | 'cancelled'

export interface AgentStreamState {
  /** 正文全文（供兜底渲染与复制用） */
  text: string
  /** 按真实发生顺序排列的时间线片段 */
  parts: MessagePart[]
  logs: string[]
  usage: Record<string, number>
  status: StreamStatus
  error?: string | null
  runId?: string
}

export function emptyAgentStream(status: StreamStatus = 'running'): AgentStreamState {
  return { text: '', parts: [], logs: [], usage: {}, status }
}

/** 把增量文本按真实先后顺序并入时间线：与上一段同类则续写，否则另起一段。 */
export function appendDelta(parts: MessagePart[], kind: 'text' | 'thinking', text: string): MessagePart[] {
  const last = parts[parts.length - 1]
  if (last && last.kind === kind) {
    return [...parts.slice(0, -1), { ...last, text: (last.text ?? '') + text }]
  }
  return [...parts, { kind, text }]
}

/** 消费一条规范化事件，返回新的状态（不改原对象）。 */
export function applyAgentEvent(state: AgentStreamState, event: AgentEvent): AgentStreamState {
  switch (event.type) {
    case 'run.started':
      return { ...state, runId: event.payload?.run_id as string }
    case 'message.delta':
      return {
        ...state,
        text: state.text + (event.text ?? ''),
        parts: appendDelta(state.parts, 'text', event.text ?? ''),
      }
    case 'thinking.delta':
      return { ...state, parts: appendDelta(state.parts, 'thinking', event.text ?? '') }
    case 'tool.call':
      return {
        ...state,
        parts: [
          ...state.parts,
          {
            kind: 'tool_call',
            name: event.name,
            id: event.call_id,
            input: (event.payload?.input ?? {}) as Record<string, unknown>,
          },
        ],
      }
    case 'tool.result':
      return {
        ...state,
        parts: [
          ...state.parts,
          {
            kind: 'tool_result',
            id: event.call_id,
            output: String(event.payload?.output ?? ''),
            is_error: Boolean(event.payload?.is_error),
          },
        ],
      }
    case 'usage':
      return { ...state, usage: { ...state.usage, ...(event.payload as Record<string, number>) } }
    case 'context.usage':
      // 单次请求的上下文快照，只用于服务端计量；流式界面不展示，等轮询刷新即可。
      return state
    case 'context.compacted':
      // 内核原生压缩（如 Claude Code auto-compact）：服务端已同步发一条 log 提醒；
      // 上下文数字等轮询刷新即可（服务端已把快照口径按压缩后重置）。
      return state
    case 'log':
      return { ...state, logs: [...state.logs, event.text ?? ''] }
    case 'error':
      return { ...state, error: event.text ?? '未知错误', status: 'error' }
    case 'run.finished': {
      const status = String(event.payload?.status ?? 'ok')
      return {
        ...state,
        status: status === 'cancelled' ? 'cancelled' : status === 'error' ? 'error' : 'done',
      }
    }
    default:
      return state
  }
}
