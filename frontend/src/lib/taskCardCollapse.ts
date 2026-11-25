/**
 * 任务进度卡片的折叠状态：本机偏好，按卡片 id 记在 localStorage。
 *
 * 折叠是「这台设备的显示偏好」，与 `coda.sidebar` / `coda.theme` 同一定位，
 * 后端不存、跨设备各记各的。隐私模式 / 坏数据 / 写入失败一律降级为「展开」，
 * 最多保留 50 条（按写入顺序淘汰），避免本地存储无限膨胀（与 composerDraft 同一套路）。
 */
const STORAGE_KEY = 'coda.taskcard.collapsed'
const MAX_ENTRIES = 50

/** 卡片 id → 是否收起；没有记录 = 展开。 */
type Store = Record<string, boolean>

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const store: Store = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!key || typeof value !== 'boolean') continue
      store[key] = value
    }
    return store
  } catch {
    // 本地存储不可用（隐私模式）/ 数据损坏：当作「全部展开」
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  try {
    const keys = Object.keys(store)
    for (const stale of keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES))) delete store[stale]
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch {
    // 写不进去就算了：最坏情况是刷新后回到展开，不影响卡片内容
  }
}

/** 读某张卡的折叠记录；从未记录过（或没有 id）时返回 null。 */
export function readTaskCardCollapsed(cardId: string): boolean | null {
  if (!cardId) return null
  const store = readStore()
  return Object.prototype.hasOwnProperty.call(store, cardId) ? store[cardId] === true : null
}

/**
 * 读某张卡是否收起；没有记录时用 `fallback`。
 *
 * 卡片现在固定在输入框上方常驻，`fallback` 传 true（默认收起）—— 只占一行，
 * 点开才看任务项；老用户存过的偏好仍然优先。
 */
export function loadTaskCardCollapsed(cardId: string, fallback = false): boolean {
  const stored = readTaskCardCollapsed(cardId)
  return stored === null ? fallback : stored
}

/** 记下某张卡的折叠状态；已经展开时顺手把这条记录抹掉，别占着 50 条上限。 */
export function saveTaskCardCollapsed(cardId: string, collapsed: boolean): void {
  if (!cardId) return
  const store = readStore()
  delete store[cardId]
  if (collapsed) {
    // 删掉再写回：把它挪到对象末尾，淘汰时优先清理最久没动过的卡片
    store[cardId] = true
  }
  writeStore(store)
}
