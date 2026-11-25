/**
 * 实时流里隐藏任务进度卡协议块。
 *
 * 模型输出是 SSE 文本增量，落库前的剥离发生在运行收尾；流式过程中原始
 * ```task_card``` 围栏会短暂出现在气泡里。这里在渲染 text 片段前再剥一次：
 * 已闭合的从起始标记剥到结束围栏，未闭合的一直剥到文本末尾（避免半截 JSON 闪屏）。
 * 历史消息的服务端正文已经剥过，这里再剥一次是幂等的。
 *
 * 模型并不总是老老实实写 ```task_card —— 实测见过写成 ```json、甚至直接甩一段
 * 裸 JSON 的。这类块**只在正文收尾处**才认，并且必须「长得像任务卡」（有 action，
 * 或 items 里是 id/text/done 这类字段）：回复中段讲解用的 JSON 示例不能被吞掉。
 */
const TASK_FENCE = /```[ \t]*task_card(?![A-Za-z0-9_])[ \t]*\r?\n?[\s\S]*?(?:```|$)/gi
/** 闭合的 ```json 围栏必须一直顶到正文末尾；中段示例不能当成收尾块。 */
const JSON_FENCE_CLOSED = /```[ \t]*json[ \t]*\r?\n?([\s\S]*?)\r?\n?```[ \t]*$/i
const JSON_FENCE_OPEN = /```[ \t]*json(?![A-Za-z0-9_])[ \t]*\r?\n/gi
/** 流式半截裸 JSON 的判定：尾巴明确以任务卡对象的开头起头。 */
const CARD_OBJECT_START = /\{\s*"(?:action|items)"\s*:/g

/** 这段文本像不像任务卡协议块（`action` 或结构化 `items`）。 */
function looksLikeTaskBlock(raw: string): boolean {
  const text = raw.trim()
  if (!text) return false
  try {
    const parsed: unknown = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false
    const record = parsed as Record<string, unknown>
    if ('action' in record) return true
    const items = record.items
    if (!Array.isArray(items)) return false
    return items.some(
      (item) =>
        item !== null &&
        typeof item === 'object' &&
        ('id' in item || 'text' in item || 'done' in item),
    )
  } catch {
    return false
  }
}

/** 括号 / 引号是否闭合（只用于判断裸 JSON 是不是「流式半截」）。 */
function isBalanced(raw: string): boolean {
  let depth = 0
  let inString = false
  let escaped = false
  for (const char of raw) {
    if (inString) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') inString = false
      continue
    }
    if (char === '"') inString = true
    else if (char === '{' || char === '[') depth += 1
    else if (char === '}' || char === ']') depth -= 1
  }
  return depth === 0 && !inString
}

/** 找「从哪个 `{` 开始是任务卡块」，找不到返回 -1。 */
function findCardBlockStart(text: string, incomplete: boolean): number {
  if (incomplete) {
    // 半截对象：从后往前找最后一个 `{"action"` / `{"items"` 起点，且必须还没闭合。
    let match: RegExpExecArray | null
    let last = -1
    const pattern = new RegExp(CARD_OBJECT_START.source, 'g')
    while ((match = pattern.exec(text)) !== null) last = match.index
    if (last < 0) return -1
    return isBalanced(text.slice(last)) ? -1 : last
  }
  const starts: number[] = []
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '{') starts.push(index)
  }
  for (let index = starts.length - 1; index >= 0; index -= 1) {
    const start = starts[index]
    if (looksLikeTaskBlock(text.slice(start))) return start
  }
  return -1
}

/** 剥掉正文收尾处的 ```json / 裸 JSON 任务卡块；中段示例原样保留。 */
function stripTrailingBlock(text: string): string {
  const trimmed = text.replace(/\s+$/, '')
  if (!trimmed) return text
  const fenced = JSON_FENCE_CLOSED.exec(trimmed)
  if (fenced) {
    // 闭合的收尾块必须能真正解析成任务卡，否则原样保留。
    if (!looksLikeTaskBlock(fenced[1].trim())) return text
    return trimmed.slice(0, fenced.index).replace(/\s+$/, '')
  }
  // 未闭合（流式半截）：最后一个 ```json 之后不能再有 ```，且开头得像任务卡对象。
  let lastOpen: RegExpExecArray | null = null
  const openPattern = new RegExp(JSON_FENCE_OPEN.source, 'gi')
  let candidate: RegExpExecArray | null
  while ((candidate = openPattern.exec(trimmed)) !== null) lastOpen = candidate
  if (lastOpen) {
    const body = trimmed.slice(lastOpen.index + lastOpen[0].length).trim()
    if (body.includes('```')) return text
    if (!/^\{\s*"(?:action|items)"\s*:/.test(body)) return text
    return trimmed.slice(0, lastOpen.index).replace(/\s+$/, '')
  }
  const incomplete = !trimmed.endsWith('}')
  const start = findCardBlockStart(trimmed, incomplete)
  return start >= 0 ? trimmed.slice(0, start).replace(/\s+$/, '') : text
}

export function stripTaskCardFence(text: string): string {
  if (!text || !/task_card|```json|"\s*(?:action|items)\s*"/i.test(text)) return text
  const cleaned = stripTrailingBlock(text.replace(TASK_FENCE, ''))
  return cleaned.replace(/\s+$/, '')
}
