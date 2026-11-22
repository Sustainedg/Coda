/**
 * 内核 Logo：内嵌资源 + 内存缓存 + 预解码。
 *
 * 「内核管理」页一进来就要看到图标。图标文件原本由后端的 SPA 兜底路由返回，带
 * `Cache-Control: no-cache` —— 每次进页面都要把八张图重新校验一遍（条件请求 + 往返），
 * 卡片先出来、图标后补半拍，Windows 桌面端尤其明显。这里改成：
 *
 * - **内嵌**：图形在构建期就进了前端包（`scripts/gen_kernel_logos.py` 生成
 *   `kernelLogos.ts`），运行期不再从外部路径读取；
 * - **预解码**：`<img>` 提前把图形解好码放进浏览器缓存，页面渲染直接命中；
 * - **内存缓存**：同一个图形全局只解码一次，重复进入页面不再重复解码。
 *
 * 键是后端适配器声明的 `logo` 路径（`/icons/kernels/xxx.png`）；清单里没有的图形
 * （新增内核还没重跑生成脚本）退回原始路径，界面照常显示，不会白屏。
 */
import { KERNEL_LOGO_INLINE } from './kernelLogos'

/**
 * 图形资源缓存：logo 路径 → 已解码的 `<img>`。
 *
 * 只有「成功」和「内嵌资源彻底坏了」会记下来；走外部路径的兜底失败不记，
 * 下次还能再试（网络抖动不该让图标永久消失）。
 */
const decoded = new Map<string, HTMLImageElement | null>()
/** 正在解码的图形：同一张图被多处要时只解一次。 */
const inflight = new Map<string, Promise<HTMLImageElement | null>>()

/**
 * 内嵌的图形 → 可直接放进 `src` / `mask-image` 的地址。
 *
 * 清单里两种存法：SVG 存原文（打包时 gzip 压得好），位图存 data URL。
 * 这里统一成 data URL；清单里没有的图形退回它自己的路径（还能正常显示，只是慢一点）。
 */
export function kernelLogoSource(logo: string): string {
  const inline = KERNEL_LOGO_INLINE[logo]
  if (!inline) return logo
  if (inline.startsWith('data:')) return inline
  return `data:image/svg+xml,${encodeURIComponent(inline)}`
}

/**
 * 预取并解码一个图形；已解码过的直接命中内存缓存。
 *
 * 返回的 `<img>` 与页面里用的是同一个地址，浏览器图片缓存会复用解码结果，
 * 因此这里解完码，页面渲染时不用再解一次。
 *
 * 失败分两种：内嵌资源失败说明图形本身坏了（记下来，不再重试）；走外部路径的
 * 兜底失败可能只是网络抖动（不记下来，下次还能再试）。
 */
export function preloadKernelLogo(logo: string): Promise<HTMLImageElement | null> {
  const cached = decoded.get(logo)
  if (cached !== undefined) return Promise.resolve(cached)
  const pending = inflight.get(logo)
  if (pending) return pending

  const inline = logo in KERNEL_LOGO_INLINE
  const task = new Promise<HTMLImageElement | null>((resolve) => {
    const image = new Image()
    const settle = (ok: boolean) => {
      if (ok) {
        decoded.set(logo, image)
      } else if (inline) {
        // 内嵌资源失败 = 图形本身坏了，记下来不再重试。
        decoded.set(logo, null)
      } else {
        // 外部路径失败可能只是网络抖动：清掉在途记录，下次还能再试。
        inflight.delete(logo)
      }
      resolve(ok ? image : null)
    }
    image.onload = () => settle(true)
    image.onerror = () => settle(false)
    image.src = kernelLogoSource(logo)
    // `decode()` 比 `onload` 更早给出「可以画了」的信号：有它就用它。
    if (typeof image.decode === 'function') {
      void image
        .decode()
        .then(() => settle(true))
        .catch(() => {
          /* 解码失败交给 onerror 分支处理 */
        })
    }
  })

  inflight.set(logo, task)
  return task
}

/**
 * 后台预热所有内嵌图形：应用空闲时就把它们解好码，
 * 等用户真的进到「内核管理」页时直接命中缓存，零等待。
 */
export function warmKernelLogoCache(): void {
  const run = () => {
    for (const logo of Object.keys(KERNEL_LOGO_INLINE)) {
      void preloadKernelLogo(logo)
    }
  }
  if (typeof requestIdleCallback === 'function') {
    requestIdleCallback(run, { timeout: 2000 })
  } else {
    setTimeout(run, 200)
  }
}

/** 图标加载耗时的一次采样。 */
export interface KernelLogoTiming {
  /** 页面可见（面板首帧）的时刻 */
  visibleAt: number
  /** 全部图标就绪的时刻 */
  readyAt: number
  /** 页面可见 → 图标全部显示的耗时（ms） */
  durationMs: number
  /** 本次涉及的图标个数 */
  count: number
  /** 内核列表接口的耗时（ms），便于把「接口慢」和「图标慢」分开看 */
  apiMs: number
}

const timings: KernelLogoTiming[] = []

/** 记录一次采样，并挂到 `window.__kernelLogoPerf` 上（自动化验证读取用）。 */
export function recordKernelLogoTiming(timing: KernelLogoTiming): void {
  timings.push(timing)
  if (typeof window !== 'undefined') {
    const scope = window as unknown as { __kernelLogoPerf?: { samples: KernelLogoTiming[] } }
    scope.__kernelLogoPerf = { samples: timings }
  }
}

/**
 * 等这批图标全部就绪，并记一次「页面可见 → 图标显示完成」的耗时。
 *
 * 就绪的判定：图形解码完成（`preloadKernelLogo`），再等两帧 —— 第一帧把图标画上去，
 * 第二帧确认这一帧已经提交到屏幕。这样量到的是「用户真的看到图标」的时刻，
 * 而不是「React 刚把 DOM 建好」的时刻。
 */
export async function trackKernelLogoReady(
  logos: string[],
  visibleAt: number,
  apiMs: number,
): Promise<void> {
  const unique = Array.from(new Set(logos.filter(Boolean)))
  await Promise.all(unique.map((logo) => preloadKernelLogo(logo)))
  await new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
  })
  const readyAt = performance.now()
  recordKernelLogoTiming({
    visibleAt,
    readyAt,
    durationMs: readyAt - visibleAt,
    count: unique.length,
    apiMs,
  })
}
