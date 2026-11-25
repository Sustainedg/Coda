/**
 * 智能体头像（两端共用）：自定义上传图 > 内置头像包 > 头像包兜底。
 *
 * **emoji 头像已全面下线**：没有自定义图 / 没选头像包时，按智能体 id 稳定地挑一张
 * 头像包图片（`fallbackAvatarPreset`），界面里不会再出现 emoji 脸，也不会出现裂图。
 *
 * 与 `UserAvatar` 同一套外形约定：尺寸走内联 `width/height`（调用方原样式就是内联的），
 * 图片撑满圆框；加载失败时退到默认头像包，仍失败就留空圆框。
 */
import { useEffect, useState } from 'react'
import {
  DEFAULT_AVATAR_PRESET,
  avatarPresetUrl,
  fallbackAvatarPreset,
  isAvatarPreset,
  useAgentAvatarFileUrl,
} from '../lib/agentAvatar'

export function AgentAvatar({
  avatar,
  avatarPreset,
  avatarFile,
  agentId,
  size,
  className,
  title,
}: {
  /** 老库里只剩 emoji 时，仅拿它当「挑兜底头像包」的种子；emoji 本身不再渲染 */
  avatar?: string | null
  /** 内置头像包编号；有值时渲染 /avatars/<它>.webp */
  avatarPreset?: string | null
  /** 自定义头像文件名；有值时取 /api/agents/{id}/avatar */
  avatarFile?: string | null
  /** 智能体 id：取自定义图要用；没有就跳过自定义图这一层 */
  agentId?: string | null
  /** 边长（px）；不传就撑满父级（CSS 里按 100% 处理） */
  size?: number
  className?: string
  title?: string
}) {
  const fileUrl = useAgentAvatarFileUrl(agentId, avatarFile)
  /** 加载失败的来源：自定义图的标识是 `file`，头像包就是它自己的编号。 */
  const [failed, setFailed] = useState<string[]>([])
  // 换了图重置裂图标记：上一次加载失败不代表这一次也失败
  useEffect(() => setFailed([]), [fileUrl, avatarFile, avatarPreset, agentId])

  // 显式选过头像包就用它；否则按 id（老数据退回 emoji 文本）稳定兜底一张。
  const preferred = isAvatarPreset(avatarPreset) ? avatarPreset : fallbackAvatarPreset(agentId ?? avatar)
  const showFile = Boolean(agentId && avatarFile && fileUrl && !failed.includes('file'))
  // 自定义图挂了退头像包；头像包本身挂了退默认那张，再挂就留空圆框（绝不回 emoji）。
  const presetAfterFile = preferred
  const preset = failed.includes(presetAfterFile)
    ? (presetAfterFile === DEFAULT_AVATAR_PRESET ? null : DEFAULT_AVATAR_PRESET)
    : presetAfterFile
  const showPreset = !showFile && Boolean(preset) && !failed.includes(preset ?? '')
  const style = size
    ? ({ width: size, height: size, fontSize: Math.round(size * 0.47) } as React.CSSProperties)
    : undefined

  const markFailed = (source: string) =>
    setFailed((current) => (current.includes(source) ? current : [...current, source]))

  return (
    <span className={['agent-avatar', className].filter(Boolean).join(' ')} style={style} title={title}>
      {showFile || showPreset ? (
        <img
          src={showFile ? (fileUrl ?? undefined) : avatarPresetUrl(preset ?? preferred)}
          alt=""
          onError={() => markFailed(showFile ? 'file' : (preset ?? preferred))}
        />
      ) : null}
    </span>
  )
}
