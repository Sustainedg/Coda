/**
 * 「推进任务」决策卡的消费记忆：点过选项或点过「×」的卡，刷新后不要再冒出来。
 *
 * 卡片身份用产生它的 CEO 聊天消息 ID（同一条消息刷新后 ID 不变）。后端在指令真正
 * 进入工单通道后会清掉 `meta.advance`；这里兜住两种后端看不到的情况：
 * ① 用户只点了「×」关掉，没有推进；② 旧数据/换设备前卡片已经被处理过。
 *
 * 存储读写失败（隐私模式、配额用尽）一律降级为「没有记忆」，不影响界面本身。
 */
const STORAGE_KEY = 'coda.collab.advance.consumed'

/**
 * 引用决策卡推进时，时间线上落的短标记（完整指令照发，只是不重显示一遍）。
 *
 * 服务端落库用的是同一句话（`collab/engine.py`），两处需同步修改；这里只用于
 * 发消息后的本地乐观显示，服务端数据回来即被覆盖。
 */
export const ADVANCE_MARKER = '已按决策卡的建议推进'
/** 最多记住多少条运行：超出的按最旧淘汰，避免本地存储无限膨胀。 */
const MAX_RUNS = 20
/** 单个运行最多记多少张卡。 */
const MAX_KEYS = 50

type Store = Record<string, string[]>

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const store: Store = {}
    for (const [runId, keys] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(keys)) store[runId] = keys.filter((key): key is string => typeof key === 'string')
    }
    return store
  } catch {
    // 本地存储不可用（隐私模式 / 数据损坏）：当作没有记忆，界面照常工作
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  try {
    const runIds = Object.keys(store)
    for (const stale of runIds.slice(0, Math.max(0, runIds.length - MAX_RUNS))) delete store[stale]
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch {
    // 写不进去就算了：最坏情况是刷新后卡片又出现，不影响推进
  }
}

/** 读某个运行下已消费/关闭的决策卡（聊天消息 ID 集合）。 */
export function loadConsumedAdvances(runId: string): Set<string> {
  if (!runId) return new Set()
  return new Set(readStore()[runId] ?? [])
}

/** 记住「这张卡用户已经点选或关掉了」。 */
export function rememberConsumedAdvance(runId: string, messageId: string): void {
  if (!runId || !messageId) return
  const store = readStore()
  const keys = new Set(store[runId] ?? [])
  keys.add(messageId)
  store[runId] = [...keys].slice(-MAX_KEYS)
  writeStore(store)
}

/** 运行被删除时清掉它的记忆，别留在本地。 */
export function forgetConsumedAdvances(runId: string): void {
  const store = readStore()
  if (!(runId in store)) return
  delete store[runId]
  writeStore(store)
}