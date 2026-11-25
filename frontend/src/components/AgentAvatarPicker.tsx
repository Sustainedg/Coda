/**
 * 智能体头像选择器（编辑表单第一位）：大头像预览 + 随机生成 3D 黏土风头像 + 自定义上传 + 头像包画廊。
 *
 * - **随机生成**：从内置头像包（`public/avatars/`，由 `scripts/gen_avatar_pack.py` 生成的
 *   3D 黏土风 Q 版半身像）随机抽一张，数据库里存编号（`avatar_preset`）。秒出、离线可用、
 *   不调生图接口。也可以展开画廊自己挑。
 * - **自定义上传**：base64 走 `PUT /api/agents/{id}/avatar`（与用户头像同一条管线）。
 *   新建流程是「先建后传」：表单先保存拿到智能体 id，页面再补一次上传。
 * - **emoji 头像已全部下线**：选择器里不再提供 emoji，预览与各处展示都只画头像包图片。
 *
 * 显示优先级：上传图 > 头像包 > 按智能体 id 稳定兜底的头像包（见 `components/AgentAvatar.tsx`）。
 * 切换到低优先级来源时，高层级的值要同步清掉 / 删文件，不然界面会「看着换了、存的是旧的」。
 *
 * 受控组件：`preset` 由表单持有；`avatarFileId` 是已上传头像的文件名标识
 * （上传成功后由页面写回，保存时不用再传 —— 后端在上传接口里已落库）。
 */
import { useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { AVATAR_PRESET_GROUPS, avatarPresetUrl, randomAvatarPreset } from '../lib/agentAvatar'
import { readFileAsBase64, validateAvatarFile } from '../lib/avatar'
import { RefreshIcon } from './Icons'
import { AgentAvatar } from './AgentAvatar'

export function AgentAvatarPicker({
  agentId,
  preset,
  onPresetChange,
  avatarFileId,
  onUploaded,
}: {
  /** 已保存的智能体 id；新建流程还没保存时为 null（此时「上传」置灰，提示先保存） */
  agentId: string | null
  /** 当前内置头像包编号（有值 = 随机生成 / 画廊选中的那张） */
  preset: string | null
  onPresetChange: (preset: string | null) => void
  /** 当前自定义头像的文件名标识（有值 = 已上传，预览显示图而不是头像包） */
  avatarFileId?: string | null
  /** 上传 / 删除成功后回传更新后的智能体 */
  onUploaded: (agent: { id: string; avatar_file?: string | null }) => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 画廊展开状态：80 张缩略图没必要常驻，点一下再铺开 */
  const [galleryOpen, setGalleryOpen] = useState(false)

  const generate = () => {
    onPresetChange(randomAvatarPreset(preset))
    // 换了头像包编号，自定义头像图就不再显示（后端以 avatar_file 优先，这里同步删掉）。
    if (agentId && avatarFileId) void removeFile()
  }

  const pickPreset = (next: string) => {
    onPresetChange(next)
    if (agentId && avatarFileId) void removeFile()
  }

  const upload = async (file: File) => {
    if (!agentId) {
      setError('先保存智能体，再上传自定义头像')
      return
    }
    const invalid = validateAvatarFile(file)
    if (invalid) {
      setError(invalid)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const base64 = await readFileAsBase64(file)
      const next = await api.uploadAgentAvatar(agentId, { content_base64: base64, name: file.name })
      onUploaded(next)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '头像上传失败')
    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const removeFile = async () => {
    if (!agentId) return
    setBusy(true)
    setError(null)
    try {
      const next = await api.deleteAgentAvatar(agentId)
      onUploaded(next)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '移除失败')
    } finally {
      setBusy(false)
    }
  }

  const hasFile = Boolean(agentId && avatarFileId)

  return (
    <div className="field">
      <label className="field-label">头像</label>
      <div className="agent-avatar-picker">
        {/* 大头像预览：已上传图 > 头像包 > 按智能体 id 稳定兜底（emoji 已下线） */}
        <AgentAvatar
          className="agent-avatar-preview"
          avatarPreset={preset}
          avatarFile={avatarFileId}
          agentId={agentId}
          size={64}
        />
        <div className="agent-avatar-actions">
          <button type="button" className="btn sm" onClick={generate} title="随机抽一张 3D 黏土风头像（离线可用）">
            <RefreshIcon size={13} /> 随机生成
          </button>
          <button
            type="button"
            className="btn sm"
            disabled={busy || !agentId}
            onClick={() => inputRef.current?.click()}
            title={agentId ? '上传自定义头像（PNG / JPEG / WebP，≤2 MB）' : '先保存智能体，再上传头像'}
          >
            上传头像
          </button>
          {hasFile && (
            <button type="button" className="btn sm ghost" disabled={busy} onClick={() => void removeFile()}>
              移除图片
            </button>
          )}
          <button type="button" className="btn sm ghost" onClick={() => setGalleryOpen((open) => !open)}>
            {galleryOpen ? '收起画廊' : '从头像包挑选'}
          </button>
          <input
            ref={inputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void upload(file)
              event.target.value = ''
            }}
          />
        </div>
      </div>
      {error && <span className="field-help" style={{ color: 'var(--danger)' }}>{error}</span>}
      {/* 头像包画廊：按标签分组（人物 / 卡通……），点一下即选中 */}
      {galleryOpen && (
        <div className="agent-avatar-gallery">
          {AVATAR_PRESET_GROUPS.map((group) => (
            <div key={group.key} className="agent-avatar-group">
              <span className="agent-avatar-group-label">{group.label}</span>
              <div className="agent-avatar-group-grid">
                {group.presets.map((item) => (
                  <button
                    key={item}
                    type="button"
                    className={`agent-avatar-emoji-btn${item === preset && !hasFile ? ' picked' : ''}`}
                    onClick={() => pickPreset(item)}
                    aria-label={`${group.label}头像 ${item}`}
                  >
                    <img src={avatarPresetUrl(item)} alt="" loading="lazy" />
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
