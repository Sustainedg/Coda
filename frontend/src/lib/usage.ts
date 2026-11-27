/**
 * 把各家智能体上报的 usage 归一化，并汇总出整段对话的 token 消耗。
 *
 * 不同适配器的字段名并不统一：Claude Code 用 input_tokens /
 * cache_read_input_tokens，OpenAI 兼容端点用 prompt_tokens / cached_tokens，
 * Codex 用 input_tokens / cached_input_tokens。这里统一成一组展示字段。
 */

export interface UsageSummary {
  /** 普通输入 token 数（**不含**缓存；与账本「输入」同一口径） */
  input: number
  /** 输出（补全）token 数 */
  output: number
  /** 总 token 数 = 窗口占用（输入含缓存）+ 输出 */
  total: number
  /** 命中缓存的输入 token 数 */
  cacheRead: number
  /** 写入缓存的输入 token 数 */
  cacheCreation: number
  /** 缓存命中率（0~1），无数据时为 null */
  cacheRatio: number | null
  /** 参与统计的消息条数 */
  messages: number
}

const EMPTY: UsageSummary = {
  input: 0, output: 0, total: 0, cacheRead: 0, cacheCreation: 0, cacheRatio: null, messages: 0,
}

interface NormalizedUsage {
  input: number
  output: number
  cacheRead: number
  cacheCreation: number
  context: number
  total: number
}

const ZERO_USAGE: NormalizedUsage = {
  input: 0, output: 0, cacheRead: 0, cacheCreation: 0, context: 0, total: 0,
}

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function pick(usage: Record<string, unknown>, keys: string[]): number {
  for (const key of keys) {
    if (key in usage) return num(usage[key])
  }
  return 0
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function nestedNum(usage: Record<string, unknown>, parent: string, key: string): number {
  const child = asRecord(usage[parent])
  return child ? num(child[key]) : 0
}

function sumNestedCacheCreation(usage: Record<string, unknown>): number {
  const nested = asRecord(usage.cache_creation)
  return nested
    ? num(nested.ephemeral_1h_input_tokens) + num(nested.ephemeral_5m_input_tokens)
    : 0
}

function finish(
  input: number,
  output: number,
  cacheRead: number,
  cacheCreation: number,
  context: number,
): NormalizedUsage {
  return { input, output, cacheRead, cacheCreation, context, total: context + output }
}

function normalizeAnthropic(usage: Record<string, unknown>): NormalizedUsage {
  const input = pick(usage, ['input_tokens'])
  const cacheRead = pick(usage, ['cache_read_input_tokens', 'cache_read_tokens'])
  let cacheCreation = pick(usage, ['cache_creation_input_tokens', 'cache_creation_tokens'])
  if (!cacheCreation) cacheCreation = sumNestedCacheCreation(usage)
  const output = pick(usage, ['output_tokens'])
  return finish(input, output, cacheRead, cacheCreation, input + cacheRead + cacheCreation)
}

function normalizeOpenAIChat(usage: Record<string, unknown>): NormalizedUsage {
  const prompt = num(usage.prompt_tokens ?? usage.input_tokens ?? 0)
  let cacheRead = nestedNum(usage, 'prompt_tokens_details', 'cached_tokens')
  if (!cacheRead) cacheRead = pick(usage, ['cached_tokens', 'cached_input_tokens', 'cachedInputTokens'])
  const output = pick(usage, ['completion_tokens', 'output_tokens'])
  return finish(Math.max(0, prompt - cacheRead), output, cacheRead, 0, prompt)
}

function normalizeOpenAIResponses(usage: Record<string, unknown>): NormalizedUsage {
  const prompt = num(usage.input_tokens ?? usage.inputTokens ?? 0)
  let cacheRead = nestedNum(usage, 'input_tokens_details', 'cached_tokens')
  if (!cacheRead) {
    cacheRead = pick(usage, [
      'cached_input_tokens', 'cachedInputTokens', 'cached_tokens', 'cache_read_tokens',
    ])
  }
  const cacheCreation = pick(usage, ['cache_creation_tokens', 'cache_creation_input_tokens'])
  const output = pick(usage, ['output_tokens', 'outputTokens', 'completion_tokens'])
  return finish(Math.max(0, prompt - cacheRead), output, cacheRead, cacheCreation, prompt)
}

/** 驼峰兼容端点；``exclusive`` = DeepSeek 风格的“输入与缓存分离”。 */
function normalizeOpenAICamel(
  usage: Record<string, unknown>,
  exclusive: boolean,
): NormalizedUsage {
  if (exclusive) {
    const input = pick(usage, ['inputTokens', 'input_tokens'])
    const cacheRead = pick(usage, [
      'cacheReadTokens', 'cache_read_tokens', 'cache_read_input_tokens',
    ])
    const cacheCreation = pick(usage, [
      'cacheWriteTokens', 'cache_creation_tokens', 'cache_creation_input_tokens',
    ])
    const output = pick(usage, ['outputTokens', 'output_tokens'])
    return finish(input, output, cacheRead, cacheCreation, input + cacheRead + cacheCreation)
  }

  const prompt = num(usage.inputTokens ?? usage.input_tokens ?? usage.prompt_tokens ?? 0)
  let cacheRead = nestedNum(usage, 'input_tokens_details', 'cached_tokens')
  if (!cacheRead) {
    cacheRead = pick(usage, [
      'cachedInputTokens', 'cached_input_tokens', 'cachedTokens', 'cached_tokens',
      'cache_read_tokens', 'cacheReadTokens',
    ])
  }
  const cacheCreation = pick(usage, [
    'cache_creation_tokens', 'cache_creation_input_tokens', 'cacheWriteTokens',
  ])
  const output = pick(usage, ['outputTokens', 'output_tokens', 'completion_tokens'])
  return finish(Math.max(0, prompt - cacheRead), output, cacheRead, cacheCreation, prompt)
}

function normalizeGemini(usage: Record<string, unknown>): NormalizedUsage {
  const meta = asRecord(usage.usageMetadata) ?? usage
  const prompt = pick(meta, ['promptTokenCount', 'prompt_token_count'])
  const cacheRead = pick(meta, ['cachedContentTokenCount', 'cached_content_token_count'])
  const output = pick(meta, ['candidatesTokenCount', 'candidates_token_count'])
  return finish(Math.max(0, prompt - cacheRead), output, cacheRead, 0, prompt)
}

function normalizeHermes(usage: Record<string, unknown>): NormalizedUsage {
  const input = pick(usage, ['input'])
  const output = pick(usage, ['output'])
  const cacheRead = pick(usage, ['cache_read'])
  const cacheCreation = pick(usage, ['cache_write'])
  return finish(input, output, cacheRead, cacheCreation, input + cacheRead + cacheCreation)
}

/** 没有协议提示时的字段兜底（与后端 `detect_protocol` 同一顺序）。 */
function autoNormalize(usage: Record<string, unknown>): NormalizedUsage {
  if ('prompt_tokens' in usage) return normalizeOpenAIChat(usage)
  if (
    'cache_read_input_tokens' in usage ||
    'cache_creation_input_tokens' in usage ||
    asRecord(usage.cache_creation) !== null
  ) {
    return normalizeAnthropic(usage)
  }
  if ('usageMetadata' in usage || 'promptTokenCount' in usage) return normalizeGemini(usage)
  if ('input_tokens' in usage) return normalizeOpenAIResponses(usage)
  if ('inputTokens' in usage) return normalizeOpenAICamel(usage, false)
  if ('input' in usage || 'cache_read' in usage || 'cache_write' in usage) {
    return normalizeHermes(usage)
  }
  return ZERO_USAGE
}

/**
 * 把一条原始 usage 归一成平台口径（与后端 `app/metering/usage.py` 同一套规则）。
 *
 * ``protocol`` 来自消息的 `usage_protocol` 快照；为空时按字段顺序兜底。
 * ``unknown`` 在展示层按字段兜底（账本侧仍保持 unknown，这是有意的差异）。
 */
export function normalizeUsage(
  raw: Record<string, unknown> | null | undefined,
  protocol?: string | null,
): NormalizedUsage {
  const usage = asRecord(raw)
  if (!usage) return ZERO_USAGE

  const name = (protocol ?? '').trim().toLowerCase()
  if (name === 'anthropic_messages' || name === 'anthropic') return normalizeAnthropic(usage)
  if (name === 'openai_chat' || name === 'chat' || name === 'chat_completions') {
    return normalizeOpenAIChat(usage)
  }
  if (name === 'openai_responses' || name === 'responses') return normalizeOpenAIResponses(usage)
  if (name === 'openai_compat_camel' || name === 'openai_camel') {
    return normalizeOpenAICamel(usage, false)
  }
  if (name === 'deepseek_camel' || name === 'deepseek') {
    return normalizeOpenAICamel(usage, true)
  }
  if (name === 'gemini') return normalizeGemini(usage)
  return autoNormalize(usage)
}

export function summarizeUsage(
  usages: (Record<string, unknown> | null | undefined)[],
  protocols?: (string | null | undefined)[],
): UsageSummary {
  const summary: UsageSummary = { ...EMPTY }
  let cacheBase = 0

  usages.forEach((usage, index) => {
    if (!usage || Object.keys(usage).length === 0) return
    const n = normalizeUsage(usage, protocols?.[index])
    if (n.context === 0 && n.input === 0 && n.output === 0 && n.cacheRead === 0 && n.cacheCreation === 0) {
      return
    }
    summary.messages += 1
    summary.input += n.input
    summary.output += n.output
    summary.total += n.total
    summary.cacheRead += n.cacheRead
    summary.cacheCreation += n.cacheCreation
    // input 已不含缓存，分母统一为 input + cache_read + cache_creation。
    cacheBase += n.input + n.cacheRead + n.cacheCreation
  })

  summary.cacheRatio = cacheBase > 0 ? summary.cacheRead / cacheBase : null
  return summary
}

/**
 * 协作预算的 token 口径：后端把 ``run.usage`` 里所有数值字段直接相加
 * （见 `collab/api.py:patch_run_budget`）。这里用同一套算法，避免界面校验与后端不一致。
 */
export function rawUsageTotal(usage: Record<string, unknown> | null | undefined): number {
  return Object.values(usage ?? {}).reduce<number>(
    (sum, value) => sum + (typeof value === 'number' && Number.isFinite(value) ? value : 0),
    0,
  )
}

/* ---------- 用量页的时间范围（两端共用这一份口径）---------- */

/** 用量页可选的预设范围；`custom` 用用户填的起止日期。 */
export type UsageRangeKind = 'today' | 'week' | 'month' | 'custom' | 'all'

export interface UsageRangeQuery {
  /** 起始时刻（ISO 8601，含）*/
  start?: string
  /** 结束时刻（ISO 8601，不含）*/
  end?: string
  /** 本地时区相对 UTC 的分钟偏移（东为正）；只用于「按天」分桶 */
  tz_offset: number
}

/** 一个本地自然日 00:00（按本机时区，而不是 UTC 零点）。 */
function localMidnight(base: Date, dayOffset: number): Date {
  return new Date(base.getFullYear(), base.getMonth(), base.getDate() + dayOffset)
}

/** 把 `YYYY-MM-DD` 解析成当地那天的 00:00；格式不对返回 null。 */
function parseDay(value: string | undefined): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? '')
  if (!match) return null
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}

/**
 * 把预设范围换算成接口参数。区间一律按**本机时区**的整天取（`end` 是次日的 00:00，开区间），
 * 这样「今日 / 一周」的边界与用户看到的日期一致；`all` 不带上限。
 */
export function usageRangeQuery(
  kind: UsageRangeKind,
  customStart?: string,
  customEnd?: string,
): UsageRangeQuery {
  const tzOffset = -new Date().getTimezoneOffset()
  if (kind === 'all') return { tz_offset: tzOffset }

  const now = new Date()
  const today = localMidnight(now, 0)
  let start = today
  let end = localMidnight(now, 1)

  if (kind === 'week') {
    start = localMidnight(now, -6)
  } else if (kind === 'month') {
    start = localMidnight(now, -29)
  } else if (kind === 'custom') {
    start = parseDay(customStart) ?? today
    // 结束日含当天：取那天的次日 00:00 作为开区间上界。
    end = localMidnight(parseDay(customEnd) ?? now, 1)
  }

  return { start: start.toISOString(), end: end.toISOString(), tz_offset: tzOffset }
}

/** 把日期格式化成 `<input type="date">` 需要的 `YYYY-MM-DD`（按本机时区）。 */
export function dayInputValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

