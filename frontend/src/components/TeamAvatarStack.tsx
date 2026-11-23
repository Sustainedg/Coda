/**
 * 协作组头像：把「协作组里有哪些智能体」堆成一摞头像，像微信群聊那样。
 *
 * 协作组本身不存头像（只存 `graph.nodes[].agent_profile_id`），头像永远跟着智能体走：
 * 智能体换了头像，协作组卡片下一次渲染就跟着变 —— 所以这里不做缓存、不落库。
 *
 * 堆叠约定（与微信 / Telegram 的群头像一致）：
 * - 成员按图里的节点顺序取，同一个智能体挂多个岗位只出现一次；
 * - 最多露 `max` 张，超出的收进最后一枚「+N」圆片（`max` 含这一枚）；
 * - 从左到右依次叠压，后一张盖住前一张的右半边；外圈描边用卡片底色，叠压处才分得开。
 *
 * 两端共用（`pages/` 与 `mobile/pages/` 都引用它），尺寸全部走 `size` 内联，
 * 样式类 `team-avatar-stack` 在 index.css / mobile.css 各写一份、逐字一致。
 */
import { AgentAvatar } from './AgentAvatar'
import type { AgentProfile, CollabTeam } from '../lib/types'

/** 头像堆叠：`teams` 与 `agents` 都来自各自的列表接口，按 `agent_profile_id` 对上号。 */
export function TeamAvatarStack({
  team,
  agents,
  size = 40,
  max = 4,
}: {
  team: CollabTeam
  /** 已加载的智能体列表（桌面端与移动端都是列表页已有的数据，不必为头像再发一次请求） */
  agents: AgentProfile[]
  /** 每张头像的边长（px） */
  size?: number
  /** 最多露几张头像（含最后的「+N」圆片） */
  max?: number
}) {
  const byId = new Map(agents.map((agent) => [agent.id, agent]))
  // 一个智能体可能在图里挂多个岗位：按智能体去重，保留第一次出现的顺序。
  const seen = new Set<string>()
  const members: AgentProfile[] = []
  for (const node of team.graph.nodes) {
    if (seen.has(node.agent_profile_id)) continue
    seen.add(node.agent_profile_id)
    const agent = byId.get(node.agent_profile_id)
    if (agent) members.push(agent)
  }

  // 没有智能体可显示（新组 / 智能体列表还没加载）时退回原来的握手图标，卡片不会空一块。
  if (members.length === 0) {
    return (
      <span
        className="team-avatar-stack empty"
        style={{ width: size, height: size, fontSize: Math.round(size * 0.47) }}
        aria-hidden="true"
      >
        🤝
      </span>
    )
  }

  // 留一枚位置给「+N」：满员时前面的头像少露一张。
  const overflow = members.length > max
  const shown = overflow ? members.slice(0, max - 1) : members
  const hidden = members.length - shown.length
  // 叠压后整摞的宽度：第一张全宽，其余每张只多出「错开」那一截（「+N」也算一张）。
  const offset = Math.round(size * 0.62)
  const stackWidth = offset * (shown.length + (overflow ? 1 : 0) - 1) + size
  // 叠压靠负边距实现：每张往回拉「自身宽度 - 错开量」。宽度必须显式传变量 ——
  // 百分比边距按**容器**宽度算，会把它拉过头（头像全叠成一摞）。
  const stackStyle = {
    width: stackWidth,
    height: size,
    '--team-avatar-offset': `${offset}px`,
    '--team-avatar-size': `${size}px`,
  } as React.CSSProperties

  return (
    <span
      className="team-avatar-stack"
      style={stackStyle}
      role="img"
      aria-label={`${team.name} 的成员头像`}
    >
      {shown.map((agent) => (
        <AgentAvatar
          key={agent.id}
          agentId={agent.id}
          avatar={agent.avatar}
          avatarPreset={agent.avatar_preset}
          avatarFile={agent.avatar_file}
          size={size}
          className="team-avatar-stack-item"
          title={agent.name}
        />
      ))}
      {overflow && (
        <span
          className="team-avatar-stack-item team-avatar-stack-more"
          style={{ width: size, height: size, fontSize: Math.round(size * 0.36) }}
          title={`还有 ${hidden} 个成员`}
        >
          +{hidden}
        </span>
      )}
    </span>
  )
}
