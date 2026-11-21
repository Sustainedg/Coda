/**
 * 界面图标：一律是**纯线条**的内联 SVG（`stroke="currentColor"`），不用 emoji。
 *
 * 为什么不用 emoji：各系统字体的 emoji 大小、粗细、颜色都不一样 —— 附件 📎 在 iOS 上尤其大，
 * 同一排图标永远对不齐。线条图标跟文字一个颜色（`currentColor`），深浅色主题自动跟随，
 * 尺寸也统一由 `size` 控制。
 *
 * 桌面端与移动端**共用这一份**（两端样式表各自给尺寸与配色）：新增图标就往这里加，
 * 别再各写一份。
 */
import type { ReactNode } from 'react'

interface IconProps {
  /** 边长（px），默认 20 */
  size?: number
  className?: string
}

/** 所有线条图标的公共外壳：24 的坐标系、圆头圆角、描边跟随文字颜色。 */
function Icon({ size = 20, className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

/** 文件夹：聊天页输入框上方「选工作目录」那个小图标（纯线条，跟文字同色）。 */
export function FolderIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.2 7.6a2 2 0 0 1 2-2h3.1a2 2 0 0 1 1.6.8l.9 1.2h8a2 2 0 0 1 2 2v6.8a2 2 0 0 1-2 2H5.2a2 2 0 0 1-2-2z" />
      <path d="M3.4 10.4h17.2" />
    </Icon>
  )
}

/** 智能体：一个圆头机器人（天线 + 两只眼睛 + 两侧耳朵），同样跟文字同色。 */
export function AgentIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="4.2" y="8.4" width="15.6" height="11" rx="3.4" />
      <path d="M12 8.2V5.6" />
      <circle cx="12" cy="4.3" r="1.2" />
      <path d="M9.3 13.2v1.6" />
      <path d="M14.7 13.2v1.6" />
      <path d="M3 12.6v2.8" />
      <path d="M21 12.6v2.8" />
    </Icon>
  )
}

/** 模型：一枚芯片（外框 + 内核 + 四边引脚），跟在智能体旁边选模型。 */
export function ModelIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="6.2" y="6.2" width="11.6" height="11.6" rx="2.4" />
      <rect x="9.6" y="9.6" width="4.8" height="4.8" rx="1" />
      <path d="M9.2 3.2v3M14.8 3.2v3M9.2 17.8v3M14.8 17.8v3" />
      <path d="M3.2 9.2h3M3.2 14.8h3M17.8 9.2h3M17.8 14.8h3" />
    </Icon>
  )
}

/** 五角星：实心 = 默认智能体（新建对话直接用它），空心 = 不是。 */
export function StarIcon({ on = false, ...props }: IconProps & { on?: boolean }) {
  return (
    <Icon {...props}>
      <path
        d="m12 3.6 2.6 5.3 5.8.85-4.2 4.1 1 5.8-5.2-2.75L6.8 19.65l1-5.8-4.2-4.1 5.8-.85z"
        fill={on ? 'currentColor' : 'none'}
      />
    </Icon>
  )
}

/** 喇叭：关闭时只有喇叭本体，开启时多两道声波（不靠颜色也能看出状态）。 */
export function SpeakerIcon({ on = false, ...props }: IconProps & { on?: boolean }) {
  return (
    <Icon {...props}>
      <path d="M11 4.5 6.5 8.5H3.5v7h3L11 19.5z" />
      {on && (
        <>
          <path d="M15 9.2a4 4 0 0 1 0 5.6" />
          <path d="M17.8 6.6a8 8 0 0 1 0 10.8" />
        </>
      )}
    </Icon>
  )
}

/** 分享：一个方框 + 向上的箭头（把对话导出成长图）。 */
export function ShareIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 15.5V3.8" />
      <path d="m8.2 7.4 3.8-3.8 3.8 3.8" />
      <path d="M5.5 13.5v5.7a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-5.7" />
    </Icon>
  )
}

/** 附件：加号（原回形针换成加号 —— 「往这条消息里添点东西」的语义更直白）。 */
export function PlusIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </Icon>
  )
}

/** 汉堡三横线：展开 / 折叠侧栏抽屉（顶栏左上角）。 */
export function MenuIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </Icon>
  )
}

/** 细线对勾：勾选型气泡里的选中标记（选中显示、未选中隐藏）。 */
export function CheckIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    </Icon>
  )
}

/** 麦克风：语音输入。 */
export function MicIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.2a2.9 2.9 0 0 1 2.9 2.9v5a2.9 2.9 0 0 1-5.8 0v-5A2.9 2.9 0 0 1 12 3.2z" />
      <path d="M5.4 11.2v1a6.6 6.6 0 0 0 13.2 0v-1" />
      <path d="M12 18.8V21" />
    </Icon>
  )
}

/** 发送：向上的箭头。 */
export function SendIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 19.5V5" />
      <path d="m6.6 10.4 5.4-5.4 5.4 5.4" />
    </Icon>
  )
}

/** 停止：实心小方块（运行中替代发送）。 */
export function StopIcon({ size = 20, className }: IconProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="7" y="7" width="10" height="10" rx="2.4" fill="currentColor" />
    </svg>
  )
}

/** 铅笔：重命名 / 编辑。 */
export function PencilIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4.8 19.2l1-3.9L15.7 5.4a2.15 2.15 0 0 1 3 3L8.8 18.3l-4 .9z" />
      <path d="m14.4 6.7 2.9 2.9" />
    </Icon>
  )
}

/** 垃圾桶：删除。 */
export function TrashIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4.5 7h15" />
      <path d="M9.6 7V4.8h4.8V7" />
      <path d="M6.6 7l.8 12.1a1.6 1.6 0 0 0 1.6 1.5h6a1.6 1.6 0 0 0 1.6-1.5L17.4 7" />
      <path d="M10.4 11v6" />
      <path d="M13.6 11v6" />
    </Icon>
  )
}

/** 顺时针箭头：重新检测（不是「刷新页面」，是可以转起来的那种）。 */
export function RefreshIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20.2 12a8.2 8.2 0 1 1-2.5-5.9" />
      <path d="M20.4 3.9v5h-5" />
    </Icon>
  )
}

/** 横着的三个点：更多操作。 */
export function MoreIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M6 12h.01" />
      <path d="M12 12h.01" />
      <path d="M18 12h.01" />
    </Icon>
  )
}

/** 插头：MCP 服务器（一个要与外界对接的「接口」）。 */
export function PlugIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9.2 2.8v4.6" />
      <path d="M14.8 2.8v4.6" />
      <path d="M6.4 7.4h11.2v3.1a5.6 5.6 0 0 1-5.6 5.6 5.6 5.6 0 0 1-5.6-5.6z" />
      <path d="M12 16.1v5.1" />
    </Icon>
  )
}

/** 钥匙：密钥 / 凭据（值只显示掩码）。 */
export function KeyIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="7.8" cy="16.2" r="3.6" />
      <path d="M10.4 13.6 20.2 3.8" />
      <path d="m16.6 7.4 2.6 2.6" />
      <path d="m14 10 2.2 2.2" />
    </Icon>
  )
}

/** 两环相扣：连接方式 / 关联。 */
export function LinkIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 13.2a4.6 4.6 0 0 0 6.9.5l2.5-2.5a4.6 4.6 0 0 0-6.5-6.5l-1.4 1.4" />
      <path d="M14 10.8a4.6 4.6 0 0 0-6.9-.5l-2.5 2.5a4.6 4.6 0 0 0 6.5 6.5l1.4-1.4" />
    </Icon>
  )
}

/** 齿轮：设置（账号菜单里的「设置」）。 */
export function SettingsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
      <circle cx="12" cy="12" r="3" />
    </Icon>
  )
}

/** 剪贴板：工单更新提示（这条消息把需求同步进了几张工单）。 */
export function ClipboardIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M8.4 5.2H6.6A1.6 1.6 0 0 0 5 6.8v12.4a1.6 1.6 0 0 0 1.6 1.6h10.8a1.6 1.6 0 0 0 1.6-1.6V6.8a1.6 1.6 0 0 0-1.6-1.6h-1.8" />
      <rect x="8.4" y="3.2" width="7.2" height="3.4" rx="1.1" />
      <path d="M8.8 10.8h6.4" />
      <path d="M8.8 14.2h6.4" />
    </Icon>
  )
}

/** 出门箭头：退出登录（箭头从门框里向外走，比电源符号更像「离开」）。 */
export function LogoutIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="m16 17 5-5-5-5" />
      <path d="M21 12H9" />
    </Icon>
  )
}
