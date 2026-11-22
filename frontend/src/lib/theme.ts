/**
 * 外观（主题）设置：深色 / 浅色 / 跟随系统。
 *
 * 两端共用同一份：模式存在 localStorage 的同一个键上，解析出的主题写在
 * `<html data-theme="dark|light">`，CSS 只需要一套 `:root`（深色，作为无脚本时的兜底）
 * + 一个 `[data-theme='light']` 覆盖块（`index.css` 与 `mobile.css` 各一份）。
 * 没设置过时默认「跟随系统」—— 系统是深色就深色，是浅色就浅色。
 *
 * 入口 HTML 里还有一段等价的内联脚本（两个入口各一份，刻意写得极简）：它必须在首屏
 * 渲染前就把 `data-theme` 定下来，否则浅色用户每次打开都会先闪一下深色外壳。
 * 改这里的三件事（存储键 / 主题色 / 判定规则）时，两处内联脚本要一起改。
 */
import { useCallback, useEffect, useState } from 'react'

export type ThemeMode = 'dark' | 'light' | 'auto'
export type ResolvedTheme = 'dark' | 'light'

/** 存储键与深/浅两套主题色（与入口 HTML 内联脚本保持一致）。
 *  取顶栏背景叠在页面底色上的实际色，让 Android standalone 状态栏那一条和顶栏无缝衔接，
 *  不会出现色差断层（深色顶栏 rgba(18,17,28,.92) 叠 #0b0a12 ≈ #11101b；
 *  浅色顶栏 rgba(255,255,255,.92) 叠 #f4f3fa ≈ #fefeff）。 */
export const THEME_STORAGE_KEY = 'coda.theme'
export const THEME_COLORS: Record<ResolvedTheme, string> = { dark: '#11101b', light: '#fefeff' }

/** 设置页里的三个选项（两端共用同一份文案）。 */
export const THEME_OPTIONS: { value: ThemeMode; label: string; hint: string }[] = [
  { value: 'dark', label: '深色', hint: '夜间与暗环境最舒服' },
  { value: 'light', label: '浅色', hint: '白天或强光下更清楚' },
  { value: 'auto', label: '跟随系统', hint: '默认，随系统 / 手机的深色模式自动切换' },
]

const LIGHT_QUERY = '(prefers-color-scheme: light)'

/** 读设置里的模式；没设置过就跟随系统（首次打开按系统深浅色决定）。 */
export function readThemeMode(): ThemeMode {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY)
    if (raw === 'light' || raw === 'dark' || raw === 'auto') return raw
  } catch {
    /* 隐私模式下 localStorage 可能不可用：退回默认值（跟随系统）即可 */
  }
  return 'auto'
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.(LIGHT_QUERY).matches ? 'light' : 'dark'
}

/** 把模式解析成本次真正生效的主题（`auto` 看系统）。 */
export function resolveTheme(mode: ThemeMode): ResolvedTheme {
  return mode === 'auto' ? systemTheme() : mode
}

/**
 * 应用到 `<html>`：`data-theme` 给两套样式表看，`theme-color` 给浏览器 / 系统的
 * 原生外壳看（状态栏、Android 底部的系统导航条都吃这个，改完才不会跟页面两个色）。
 *
 * `color-scheme` 不在这里写：它由 CSS 的 `:root` / `[data-theme='light']` 各自声明
 * （原生控件、滚动条、下拉框跟着变），少一个真值来源。
 */
export function applyTheme(mode: ThemeMode): ResolvedTheme {
  const resolved = resolveTheme(mode)
  document.documentElement.dataset.theme = resolved
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', THEME_COLORS[resolved])
  // iOS 独立应用的状态栏只有黑白两档：深色外壳用「黑底透明白字」，浅色外壳切回系统默认样式，
  // 否则浅色下状态栏的字是白的、压在浅色顶栏上看不见。iOS 一般在文档加载时读一次这个标签，
  // 所以入口 HTML 的内联脚本里也设了同一个值。
  const statusBar = document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]')
  if (statusBar) statusBar.setAttribute('content', resolved === 'light' ? 'default' : 'black-translucent')
  return resolved
}

/** 记忆用户选的模式。 */
export function storeThemeMode(mode: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode)
  } catch {
    /* 存不下也不影响本次生效 */
  }
}

/** 应用启动时调用一次：读设置 → 应用 → 系统切换时实时跟随（只对「跟随系统」生效）。 */
export function initTheme(): void {
  applyTheme(readThemeMode())
  const query = window.matchMedia?.(LIGHT_QUERY)
  query?.addEventListener?.('change', () => {
    if (readThemeMode() === 'auto') applyTheme('auto')
  })
}

/**
 * 设置页用：读当前模式 + 当前生效的主题 + 切换（切换后立即生效并记住，跨页面 / 下次打开都保持）。
 * `theme` 是解析后的结果，界面里要按主题分支时用它。
 */
export function useThemeMode(): {
  mode: ThemeMode
  theme: ResolvedTheme
  setMode: (mode: ThemeMode) => void
} {
  const [mode, setMode] = useState<ThemeMode>(() => readThemeMode())
  const [theme, setTheme] = useState<ResolvedTheme>(() => resolveTheme(readThemeMode()))

  // 「跟随系统」时系统换色要立刻跟上（应用级的监听在 initTheme 里，这里只同步本组件的状态）。
  useEffect(() => {
    if (mode !== 'auto') return
    const query = window.matchMedia?.(LIGHT_QUERY)
    if (!query?.addEventListener) return
    const handler = () => setTheme(applyTheme('auto'))
    query.addEventListener('change', handler)
    return () => query.removeEventListener('change', handler)
  }, [mode])

  const change = useCallback((next: ThemeMode) => {
    storeThemeMode(next)
    setTheme(applyTheme(next))
    setMode(next)
  }, [])

  return { mode, theme, setMode: change }
}
