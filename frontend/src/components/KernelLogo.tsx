/**
 * 内核 Logo：两种形态，由后端按适配器声明下发。
 *
 * - `mask`（默认）：把一个单色 SVG 当作「遮罩」染成当前文字色。同一个图形要在深色 / 浅色
 *   两套外观下都清楚，遮罩染色会跟着文字色自动切换，不必为每个内核准备两张图。
 * - `image`：真彩色位图（PNG / WebP）或彩色 SVG，按原色显示 —— 品牌图标本来就该是
 *   「实打实」的样子，染成单色反而认不出来。
 *
 * 图形文件：`mask` 用只画形状的单色 SVG（`frontend/public/icons/kernels/*.svg`），`image`
 * 用真彩色位图（`*.png` / `*.webp`）。路径由后端按适配器声明下发，图形随仓库发布，运行时不外链。
 *
 * **图标秒显**：图形在构建期就内嵌成 data URL（`lib/kernelLogos.ts`），这里直接用内嵌
 * 地址渲染 —— 没有请求、没有二次解码，进页面即显示；`lib/kernelIcons.ts` 还会在应用空闲
 * 时把它们预解码好，页面渲染直接命中缓存。加载失败时给一个中性占位，不留空洞。
 */
import { useEffect, useState } from 'react'
import { kernelLogoSource, preloadKernelLogo } from '../lib/kernelIcons'

/** 位图扩展名：没有显式声明 logo_style 时按扩展名推断。 */
const RASTER = /\.(png|webp|jpe?g)$/i

export function KernelLogo({
  logo,
  style,
}: {
  logo?: string | null
  /** 后端下发的 logo_style；缺省时按扩展名推断 */
  style?: 'mask' | 'image' | null
}) {
  const [broken, setBroken] = useState(false)
  // 换了图重置裂图标记：上一次加载失败不代表这一次也失败。
  useEffect(() => setBroken(false), [logo])
  // 提前把图形解好码：卡片渲染时不用等。取不到图形就记下失败，界面给中性占位。
  useEffect(() => {
    if (!logo) return
    let alive = true
    void preloadKernelLogo(logo).then((image) => {
      if (alive && !image) setBroken(true)
    })
    return () => {
      alive = false
    }
  }, [logo])
  if (!logo) return null

  const source = kernelLogoSource(logo)
  const image = (style ?? (RASTER.test(logo) ? 'image' : 'mask')) === 'image'
  if (!image) {
    if (broken) return <span className="kernel-logo kernel-logo-missing" aria-hidden="true" />
    return (
      <span
        className="kernel-logo"
        aria-hidden="true"
        style={{ WebkitMaskImage: `url("${source}")`, maskImage: `url("${source}")` }}
      />
    )
  }
  if (broken) return <span className="kernel-logo kernel-logo-missing" aria-hidden="true" />
  return (
    <img
      className="kernel-logo kernel-logo-image"
      src={source}
      alt=""
      decoding="async"
      onError={() => setBroken(true)}
    />
  )
}
