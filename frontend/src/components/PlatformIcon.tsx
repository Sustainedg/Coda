/**
 * IM 平台小图标：后端在平台元数据里下发静态资源路径（`/icons/im/xxx.webp`），
 * 图形随仓库发布、运行时不外链，这里按原色渲染 —— 品牌图标染成单色就认不出来了。
 *
 * 兜底：路径缺失、不是静态资源路径或加载失败时，回落为文字占位（默认 🔌），
 * 不留裂图与空洞；换了图会重置失败标记，上一次裂图不影响这一次。
 */
import { useEffect, useState } from 'react'

/** 静态资源路径特征：以 `/` 开头的站内路径。 */
const STATIC = /^\//

export function PlatformIcon({
  icon,
  fallback = '🔌',
  size = 16,
}: {
  icon?: string | null
  /** 不是图片路径或加载失败时显示的文字 */
  fallback?: string
  /** 图片边长（px）；文字占位按字号缩放，保持行高一致 */
  size?: number
}) {
  const [broken, setBroken] = useState(false)
  useEffect(() => setBroken(false), [icon])
  if (!icon || broken || !STATIC.test(icon)) {
    return (
      <span
        className="platform-icon-text"
        aria-hidden="true"
        style={{ fontSize: Math.round(size * 0.8) }}
      >
        {icon || fallback}
      </span>
    )
  }
  return (
    <img
      className="platform-icon"
      src={icon}
      alt=""
      width={size}
      height={size}
      decoding="async"
      onError={() => setBroken(true)}
    />
  )
}
