/**
 * 实时流里隐藏任务进度卡协议块。
 *
 * 模型输出是 SSE 文本增量，落库前的剥离发生在运行收尾；流式过程中原始
 * ```task_card``` 围栏会短暂出现在气泡里。这里在渲染 text 片段前再剥一次：
 * 已闭合的从起始标记剥到结束围栏，未闭合的一直剥到文本末尾（避免半截 JSON 闪屏）。
 * 历史消息的服务端正文已经剥过，这里再剥一次是幂等的。
 */
const FENCE = /```[ \t]*task_card(?![A-Za-z0-9_])[ \t]*\r?\n?[\s\S]*?(?:```|$)/gi

export function stripTaskCardFence(text: string): string {
  if (!text || !/task_card/i.test(text)) return text
  return text.replace(FENCE, '').replace(/\s+$/, '')
}
