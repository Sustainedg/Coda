/**
 * 输入框上方的「上下文用量」：状态、校验与取数。
 *
 * **口径只有一个**：数字全部来自后端 `runtime.context`（`GET /conversations/{id}/context`），
 * 前端只负责展示与把用户的配置按原样提交；不在浏览器里另算一套 token，否则
 * 展示 / 阈值判断 / 压缩决策会漂移。上限与阈值用 `GET/PUT /api/system/context`
 * 持久化（后端键值配置表，刷新 / 重启后仍生效）。
 *
 * 前端仍保留一份轻量校验：非法输入当场给提示、不发请求；服务端会再校验一次，
 * 以服务端为准（例如「上限明显低于当前已用」要拿后端的已用量判断）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import type { ContextSettings, ContextUsage } from './types'

/** 把 token 数写成 1.2k / 128k / 1.5M 这类紧凑形式（与用量页同一观感）。 */
export function formatTokenCount(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (value >= 10_000) return `${Math.round(value / 1000)}k`
  if (value >= 1_000) return `${(value / 1000).toFixed(1).replace(/\.0$/, '')}k`
  return String(Math.round(value))
}

/**
 * 用量刷新的轻量键：条数 + 正文总长 + 时间线片段数 + 本轮状态。
 *
 * 只盯条数会漏掉「另一台设备把流式那一轮写完后，消息 id 不变、正文补齐」的那次刷新；
 * 只盯正文又会漏掉「只发生工具调用、正文没变」的那一轮（工具结果同样占上下文）。
 * 两端对话页共用这一个函数，别各写一份表达式。
 */
export function contextRefreshKey(
  messages: ReadonlyArray<{ content: string; parts?: ReadonlyArray<unknown> }>,
  status: string | null | undefined,
): string {
  let text = 0
  let parts = 0
  for (const message of messages) {
    text += message.content.length
    parts += message.parts?.length ?? 0
  }
  return `${messages.length}:${text}:${parts}:${status ?? ''}`
}

export type ContextInputCheck =
  | { ok: true; limit: number; ratio: number }
  | { ok: false; error: string }

/**
 * 校验详情面板里的两个输入框。
 *
 * - 上限：必须是正整数，且不低于合理下限、不低于当前已用量；
 * - 阈值：百分比数字，0 < 阈值 ≤ 100（换算成比例后自然满足「阈值 ≤ 上限」）。
 *
 * 任何一项不合法都返回可读的中文提示，调用方不得发保存请求（不落盘）。
 */
export function checkContextInput(
  limitText: string,
  thresholdPercentText: string,
  usedTokens: number,
  minLimit: number,
): ContextInputCheck {
  const limitRaw = limitText.trim()
  if (!/^\d+$/.test(limitRaw)) return { ok: false, error: '上限必须是正整数（tokens）' }
  const limit = Number(limitRaw)
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    return { ok: false, error: '上限必须大于 0' }
  }
  if (limit < minLimit) {
    return { ok: false, error: `上限至少为 ${minLimit.toLocaleString()} tokens` }
  }
  if (limit < usedTokens) {
    return {
      ok: false,
      error: `上限不能低于当前已用（约 ${usedTokens.toLocaleString()} tokens）`,
    }
  }
  const percentRaw = thresholdPercentText.trim()
  if (!/^\d+(\.\d+)?$/.test(percentRaw)) {
    return { ok: false, error: '触发阈值必须是数字（百分比）' }
  }
  const percent = Number(percentRaw)
  if (!(percent > 0 && percent <= 100)) {
    return { ok: false, error: '触发阈值必须满足 0 < 阈值 ≤ 100%' }
  }
  return { ok: true, limit, ratio: percent / 100 }
}

interface MeterState {
  usage: ContextUsage | null
  settings: ContextSettings | null
  loading: boolean
  /** 读取失败（网络 / 接口），与保存失败分开 */
  error: string | null
  saving: boolean
  /** 保存失败 / 校验失败的具体原因 */
  saveError: string | null
  /** 保存成功的一次性提示 */
  notice: string | null
}

const EMPTY: MeterState = {
  usage: null,
  settings: null,
  loading: false,
  error: null,
  saving: false,
  saveError: null,
  notice: null,
}

function describe(error: unknown, fallback: string): string {
  return error instanceof ApiError || error instanceof Error ? error.message : fallback
}

/**
 * 拉取一条对话的上下文用量。
 *
 * - `refreshKey` 变化（消息增减、本轮结束）就重新取；
 * - `active` 为真（正在出一轮回复）时每 2.5 秒轻量刷新一次：压缩状态与已用量
 *   能跟着运行走，但不会为了每个 token 发一次请求，也不阻塞输入。
 *
 * 「上限 / 阈值」是用户级配置，一条对话内不会变：进入对话时取一次，轮询只取用量
 * （保存后再一起刷新）。这样运行中的轮询只发一个请求，不会每 2.5 秒重复拉配置。
 */
export function useContextMeter(
  conversationId: string | null,
  refreshKey: string,
  active: boolean,
) {
  const [state, setState] = useState<MeterState>(EMPTY)
  /** 只接受最后一次请求的结果：切对话 / 连续刷新时旧响应不能盖回来。 */
  const requestId = useRef(0)
  /** 当前 state 属于哪条对话：切对话时先清掉旧数字，别让新对话闪一下旧用量。 */
  const loadedFor = useRef<string | null>(null)
  /** 配置已经为哪条对话取过：轮询时命中它就不再重复请求 `/api/system/context`。 */
  const settingsFor = useRef<string | null>(null)

  const load = useCallback(
    async (withSettings = false) => {
      if (!conversationId) {
        requestId.current += 1
        loadedFor.current = null
        settingsFor.current = null
        setState(EMPTY)
        return
      }
      const id = ++requestId.current
      if (loadedFor.current !== conversationId) {
        loadedFor.current = conversationId
        // 换了对话就把状态清空：配置也要重新取（否则快速 A→B→A 会沿用旧缓存而状态是空的）。
        settingsFor.current = null
        setState({ ...EMPTY, loading: true })
      } else {
        setState((prev) => ({ ...prev, loading: true }))
      }
      // 配置没取过（或显式要求）才顺带取一次；否则轮询只发用量一个请求。
      const needSettings = withSettings || settingsFor.current !== conversationId
      try {
        const [usage, settings] = await Promise.all([
          api.getConversationContext(conversationId),
          needSettings ? api.getContextSettings() : Promise.resolve(null),
        ])
        if (id !== requestId.current) return
        if (settings) settingsFor.current = conversationId
        setState((prev) => ({
          ...prev,
          usage,
          settings: settings ?? prev.settings,
          loading: false,
          error: null,
        }))
      } catch (error) {
        if (id !== requestId.current) return
        setState((prev) => ({
          ...prev,
          loading: false,
          error: describe(error, '读取上下文用量失败'),
        }))
      }
    },
    [conversationId],
  )

  useEffect(() => {
    void load()
  }, [load, refreshKey])

  useEffect(() => {
    if (!conversationId || !active) return
    const timer = window.setInterval(() => void load(), 2500)
    return () => window.clearInterval(timer)
  }, [conversationId, active, load])

  /** 保存上限 / 阈值：成功立即重新取一次（保存后马上生效）。 */
  const save = useCallback(
    async (limitTokens: number, thresholdRatio: number): Promise<boolean> => {
      if (!conversationId) return false
      setState((prev) => ({ ...prev, saving: true, saveError: null, notice: null }))
      try {
        await api.setContextSettings({
          limit_tokens: limitTokens,
          threshold_ratio: thresholdRatio,
          conversation_id: conversationId,
        })
        const [usage, settings] = await Promise.all([
          api.getConversationContext(conversationId),
          api.getContextSettings(),
        ])
        settingsFor.current = conversationId
        setState((prev) => ({
          ...prev,
          usage,
          settings,
          saving: false,
          error: null,
          saveError: null,
          notice: '已保存，立即生效',
        }))
        return true
      } catch (error) {
        setState((prev) => ({
          ...prev,
          saving: false,
          saveError: describe(error, '保存失败，请重试'),
        }))
        return false
      }
    },
    [conversationId],
  )

  const clearNotice = useCallback(() => {
    setState((prev) => (prev.notice ? { ...prev, notice: null } : prev))
  }, [])

  return { ...state, reload: load, save, clearNotice }
}
