/**
 * 侧边栏折叠状态：App 外壳与对话页顶栏**两处入口**共用同一份真值。
 *
 * 宽窗：折叠是这台设备的长期偏好，存在 localStorage。
 * 窄窗：侧栏（232px）放不下，**默认折叠**；但用户仍能点按钮把它临时展开 ——
 *       这个临时状态只留在内存里，不写 localStorage（否则会把宽窗的偏好改掉），
 *       窗口宽度跨过断点即复位。
 * 两处入口通过一个自定义事件互相通知 —— 没有它的话，顶栏按钮改了状态，
 * 外壳的 `collapsed` 类还蒙在鼓里。
 * （`storage` 事件只在**其它标签页**触发，同一页内必须自己喊一嗓子。）
 */

/** 存储键（值 `collapsed` / `open`，与 `lib/theme.ts` 一样是本机偏好）。 */
const SIDEBAR_KEY = 'coda.sidebar'
/** 同页通知用的事件名（附带的 detail 是最新值）。 */
const SIDEBAR_EVENT = 'coda:sidebar-toggle'

/** 窄窗断点：侧栏 232px 与内容再挤下去就会变形，低于此宽度默认折叠
 *  （用户仍可临时展开）。 */
const NARROW_QUERY = '(max-width: 1100px)'

/** 窄窗下用户临时展开（内存态，不落盘；跨断点自动复位，
 *  回到窄窗又是默认折叠）。 */
let narrowOpen = false

/** 缓存的 `MediaQueryList`：getSnapshot 每帧都会调，不必每帧重新 matchMedia
 *  （老环境没有 matchMedia 时记 null，按宽窗处理）。 */
let narrowMedia: MediaQueryList | null | undefined

function getNarrowMedia(): MediaQueryList | null {
  if (narrowMedia !== undefined) return narrowMedia
  try {
    narrowMedia = window.matchMedia(NARROW_QUERY)
  } catch {
    /* 老环境没有 matchMedia：按宽窗处理（用本机偏好） */
    narrowMedia = null
  }
  return narrowMedia
}

/** 当前视口是否窄到「放不下展开的侧栏」。 */
function isNarrowViewport(): boolean {
  return getNarrowMedia()?.matches ?? false
}

/**
 * 当前侧栏是否折叠（`useSyncExternalStore` 的 getSnapshot）。
 *
 * 窄窗默认折叠、可临时展开；宽窗才去读本机偏好。
 */
export function readSidebarCollapsed(): boolean {
  if (isNarrowViewport()) return !narrowOpen
  try {
    return localStorage.getItem(SIDEBAR_KEY) === 'collapsed'
  } catch {
    /* 隐私模式下 localStorage 可能不可用：默认展开 */
    return false
  }
}

/**
 * 折叠 / 展开，返回切换后的折叠状态。
 *
 * 窄窗只翻内存里的临时态（不落盘）；宽窗写本机偏好。
 * 两边都广播给同页的其它入口。
 */
export function toggleSidebarCollapsed(): boolean {
  if (isNarrowViewport()) {
    narrowOpen = !narrowOpen
  } else {
    const next = !readSidebarCollapsed()
    try {
      localStorage.setItem(SIDEBAR_KEY, next ? 'collapsed' : 'open')
    } catch {
      /* 存不进去也不影响本次会话 */
    }
    /* 回到宽窗操作时顺手清掉窄窗的临时态：下次缩窄仍按默认折叠来 */
    narrowOpen = false
  }
  const collapsed = readSidebarCollapsed()
  window.dispatchEvent(new CustomEvent(SIDEBAR_EVENT, { detail: collapsed }))
  return collapsed
}

/**
 * 订阅折叠变化（`useSyncExternalStore` 的 subscribe 形态）：
 * 同页自定义事件 + 跨标签页 storage 事件 + 窗口跨断点，谁改的都能跟上。
 *
 * 跨断点时把临时展开态复位 —— 窄窗里展开过、拉宽再缩窄，
 * 应当重新是默认折叠，而不是莫名其妙地保持展开。
 */
export function subscribeSidebarCollapsed(onChange: () => void): () => void {
  window.addEventListener(SIDEBAR_EVENT, onChange)
  window.addEventListener('storage', onChange)
  const media = getNarrowMedia()
  const onViewport = () => {
    narrowOpen = false
    onChange()
  }
  media?.addEventListener('change', onViewport)
  return () => {
    window.removeEventListener(SIDEBAR_EVENT, onChange)
    window.removeEventListener('storage', onChange)
    media?.removeEventListener('change', onViewport)
  }
}
