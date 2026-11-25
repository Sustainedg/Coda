/**
 * 空会话中央的形象：选了哪个智能体，就把它的头像**放大了**摆在这里 —— 透明背景、
 * 四周留白，一眼看清是谁在等你开口；底下踩着光晕，整体轻轻地呼吸浮动，
 * 像一个活物站在页面上，而不是一张贴上去的静照。
 *
 * 头像来源与 `AgentAvatar` 同一套优先级：自定义上传 > 内置头像包 > emoji。
 * 内置头像包的图已由 `scripts/make_avatars_transparent.py` 统一抠成透明背景
 * （深色主题下不再是白底补丁）；emoji 是文字天然透明。
 *
 * 曾经试过「3D 全身人物 + 摆手动画帧」的方案（见 git 历史），但生图模型给不出
 * 像素级连续的帧序列，观感是幻灯片不是动画 —— 按需求回退为「清楚的静态大头像 +
 * 呼吸浮动」。相关的生成管线保留在 `scripts/gen_mascot_pack.py`，想再试时有据可依。
 */
import { AgentAvatar } from './AgentAvatar'

export function MascotStage({
  avatar,
  avatarPreset,
  avatarFile,
  agentId,
  agentName,
}: {
  /** 智能体的老 emoji 字段（已下线，仅作兜底挑头像包的种子，见 AgentAvatar） */
  avatar?: string | null
  /** 智能体的内置头像包编号（透明背景版，随前端发布） */
  avatarPreset?: string | null
  /** 智能体的自定义头像文件名 */
  avatarFile?: string | null
  /** 智能体 id：取自定义头像图要用 */
  agentId?: string | null
  /** 智能体名字：悬浮提示 */
  agentName?: string | null
}) {
  return (
    <div className="mascot-stage" role="img" aria-label={`与「${agentName ?? '智能体'}」的对话已就绪`}>
      {/* 大头像：呼吸浮动 + 轻微摇摆由 CSS 动画负责（mascot-stage 那套）。 */}
      <span className="mascot-avatar">
        <AgentAvatar
          avatar={avatar}
          avatarPreset={avatarPreset}
          avatarFile={avatarFile}
          agentId={agentId}
          size={168}
          title={agentName ?? undefined}
        />
      </span>
      {/* 脚下的光晕 + 阴影：「站在」页面上的落点感，跟着呼吸同步缩放。 */}
      <span className="mascot-glow" aria-hidden="true" />
      <span className="mascot-shadow" aria-hidden="true" />
    </div>
  )
}
