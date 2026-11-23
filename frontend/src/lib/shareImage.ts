/**
 * 把一段对话渲染成一张「长图」用于导出或分享。
 *
 * 手写 Canvas 绘制（不引第三方依赖、也不把整页 DOM 塞进 html2canvas）：只画对话本身 ——
 * 标题栏、气泡、代码块、页脚，够分享用。文字按字符折行（中文没有空格），拉丁文尽量按空格断。
 *
 * 画布有尺寸上限（iOS 尤其紧），所以过长时整体降采样（`scale`）而不是掐掉内容。
 */
import { downloadBlob } from './download'

/** 分享一张图需要的对话内容（与界面无关的纯数据）。 */
export interface ShareMessage {
  role: string
  content: string
  /** 形如 `2026-11-11 15:04`；没有就不画 */
  createdAt?: string | null
}

export interface ShareImageInput {
  title: string
  /** 副标题，例如「资深工程师 · Claude Code」 */
  subtitle?: string
  messages: ShareMessage[]
  /** 当前外观：浅色 / 深色（分享图跟着当前主题走） */
  theme: 'dark' | 'light'
}

/** 分享图的配色（与两套主题同色系；画布上不能用 CSS 变量，只能写成字面量）。 */
const PALETTE = {
  dark: {
    bg: '#0b0a12',
    panel: '#17162a',
    border: '#2b2844',
    text: '#ece9f6',
    muted: '#a49fc0',
    dim: '#746f96',
    brand: '#d94fd0',
    userBubble: '#241a35',
    codeBg: '#0e0d17',
  },
  light: {
    bg: '#f4f3fa',
    panel: '#ffffff',
    border: '#dcd8ea',
    text: '#1d1a2b',
    muted: '#5d5878',
    dim: '#8b86a6',
    brand: '#b63ba9',
    userBubble: '#f3e6f6',
    codeBg: '#f2f1f8',
  },
} as const

const WIDTH = 900
const PAD = 46
/** 画布总高的上限：超过就整体降采样（iOS 的 canvas 面积限制比桌面紧得多）。 */
const MAX_HEIGHT = 14000

const FONT = "'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', system-ui, sans-serif"
const MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace"

interface Block {
  height: number
  draw: (ctx: CanvasRenderingContext2D, y: number) => void
}

function fontFor(size: number, weight = 400, mono = false): string {
  return `${weight} ${size}px ${mono ? MONO : FONT}`
}

/** 把 Markdown 的行内标记去掉（表格 / 代码块这些块级结构在分块时已经处理）。 */
function inlineText(markdown: string): string {
  return markdown
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1$2')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s*/, '')
    .replace(/^\s{0,3}>\s?/, '')
}

/** 按宽度折行：逐字符量（中文没有空格），拉丁文尽量断在空格上。 */
function wrapText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const lines: string[] = []
  for (const paragraph of text.split('\n')) {
    if (!paragraph.trim()) {
      lines.push('')
      continue
    }
    let line = ''
    for (const char of paragraph) {
      if (line && ctx.measureText(line + char).width > maxWidth) {
        const space = line.lastIndexOf(' ')
        if (space > line.length * 0.6 && !/[\u4e00-\u9fff，。！？；：）」』]/.test(char)) {
          lines.push(line.slice(0, space))
          line = `${line.slice(space + 1)}${char}`
        } else {
          lines.push(line)
          line = char
        }
      } else {
        line += char
      }
    }
    lines.push(line)
  }
  return lines
}

/** 把一条消息的内容切成分块：段落 / 列表 / 引用 / 代码（行内标记在这一步剥掉）。 */
function splitBlocks(content: string): { kind: 'text' | 'code'; text: string }[] {
  const blocks: { kind: 'text' | 'code'; text: string }[] = []
  let buffer: string[] = []
  let code: string[] | null = null

  const flush = () => {
    const text = buffer.join('\n').trim()
    if (text) blocks.push({ kind: 'text', text })
    buffer = []
  }

  for (const raw of content.split('\n')) {
    if (raw.trim().startsWith('```')) {
      if (code) {
        blocks.push({ kind: 'code', text: code.join('\n').trim() })
        code = null
      } else {
        flush()
        code = []
      }
      continue
    }
    if (code) code.push(raw)
    else buffer.push(inlineText(raw).replace(/^\s{0,3}(?:[-*+]|\d+[.)])\s+/, '• '))
  }
  if (code) blocks.push({ kind: 'code', text: code.join('\n').trim() })
  flush()
  return blocks
}

/**
 * 渲染成 PNG。返回的 Blob 可直接交给 `navigator.share`（`files`）或下载。
 */
export async function renderConversationImage(input: ShareImageInput): Promise<Blob> {
  const colors = PALETTE[input.theme]
  const measure = document.createElement('canvas').getContext('2d')
  if (!measure) throw new Error('当前浏览器不支持 Canvas，无法生成分享图')

  const bubbleMax = WIDTH - PAD * 2 - 62

  /** 量一遍，得到每块的高度（真正绘制时再跑一次同样的 `draw`）。 */
  const blocks: Block[] = []

  blocks.push({
    height: 104,
    draw: (ctx, y) => {
      ctx.fillStyle = colors.text
      ctx.font = fontFor(30, 700)
      ctx.textBaseline = 'top'
      ctx.fillText(input.title, PAD, y + 10)
      ctx.font = fontFor(15)
      ctx.fillStyle = colors.muted
      ctx.fillText(input.subtitle ?? '', PAD, y + 52)
      ctx.fillStyle = colors.brand
      ctx.fillRect(PAD, y + 82, 56, 4)
    },
  })

  for (const message of input.messages) {
    const isUser = message.role === 'user'
    const label = isUser ? '你' : (input.subtitle?.split(' · ')[0] ?? '智能体')
    const labelColor = isUser ? colors.dim : colors.brand

    blocks.push({
      height: 30,
      draw: (ctx, y) => {
        ctx.font = fontFor(13, 600)
        ctx.fillStyle = labelColor
        ctx.textBaseline = 'top'
        const text = message.createdAt ? `${label} · ${message.createdAt}` : label
        ctx.fillText(text, isUser ? WIDTH - PAD - ctx.measureText(text).width : PAD, y + 6)
      },
    })

    const bubbleWidth = bubbleMax + 30
    const bubbleX = isUser ? WIDTH - PAD - bubbleWidth : PAD

    for (const part of splitBlocks(message.content)) {
      const mono = part.kind === 'code'
      const fontSize = mono ? 14 : 16
      measure.font = fontFor(fontSize, 400, mono)
      const lines = wrapText(measure, part.text, bubbleMax)
      const lineHeight = mono ? 22 : 26
      const padding = mono ? 12 : 14
      const height = lines.length * lineHeight + padding * 2 + 8

      blocks.push({
        height,
        draw: (ctx, y) => {
          roundRect(ctx, bubbleX, y + 4, bubbleWidth, height - 8, 16)
          ctx.fillStyle = mono ? colors.codeBg : isUser ? colors.userBubble : colors.panel
          ctx.fill()
          ctx.strokeStyle = colors.border
          ctx.lineWidth = 1
          ctx.stroke()

          ctx.font = fontFor(fontSize, 400, mono)
          ctx.fillStyle = colors.text
          ctx.textBaseline = 'top'
          lines.forEach((line, index) => {
            ctx.fillText(line, bubbleX + 15, y + 4 + padding + index * lineHeight)
          })
        },
      })
    }
    blocks.push({ height: 18, draw: () => undefined })
  }

  blocks.push({
    height: 66,
    draw: (ctx, y) => {
      ctx.fillStyle = colors.dim
      ctx.font = fontFor(13)
      ctx.textBaseline = 'top'
      ctx.fillText('由 Coda 生成', PAD, y + 18)
      const stamp = new Date().toLocaleString('zh-CN', { hour12: false })
      ctx.fillText(stamp, WIDTH - PAD - ctx.measureText(stamp).width, y + 18)
    },
  })

  const contentHeight = blocks.reduce((total, block) => total + block.height, 0) + PAD * 2
  const scale = Math.min(2, MAX_HEIGHT / contentHeight)
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(WIDTH * scale)
  canvas.height = Math.round(contentHeight * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('当前浏览器不支持 Canvas，无法生成分享图')
  ctx.scale(scale, scale)
  ctx.fillStyle = colors.bg
  ctx.fillRect(0, 0, WIDTH, contentHeight)

  let cursor = PAD
  for (const block of blocks) {
    block.draw(ctx, cursor)
    cursor += block.height
  }

  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('生成分享图失败'))), 'image/png')
  })
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number): void {
  ctx.beginPath()
  ctx.moveTo(x + radius, y)
  ctx.arcTo(x + width, y, x + width, y + height, radius)
  ctx.arcTo(x + width, y + height, x, y + height, radius)
  ctx.arcTo(x, y + height, x, y, radius)
  ctx.arcTo(x, y, x + width, y, radius)
  ctx.closePath()
}

export type ShareOutcome = 'shared' | 'downloaded' | 'cancelled'

/** 分享图文件名：标题清洗成安全片段（与 Markdown 导出的命名规则保持一致）。 */
export function shareImageFileName(title: string): string {
  const slug = title.trim().replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 40) || 'conversation'
  return `coda-${slug}.png`
}

/**
 * 直接下载分享图（「导出图片」入口用）。
 *
 * 与 `shareConversationImage` 分开：导出是明确的「存到本地」，不该弹系统分享面板；
 * `shareConversationImage` 保留系统分享能力，供需要“发给别人”的入口复用。
 */
export async function downloadConversationImage(input: ShareImageInput): Promise<string> {
  const blob = await renderConversationImage(input)
  const filename = shareImageFileName(input.title)
  downloadBlob(blob, filename)
  return filename
}

/**
 * 分享（或下载）这张长图：优先走系统分享面板（`navigator.share` + 文件），
 * 不支持就退回浏览器下载 —— 两者都不行时抛错，交给调用方提示。
 */
export async function shareConversationImage(input: ShareImageInput): Promise<ShareOutcome> {
  const blob = await renderConversationImage(input)
  const filename = shareImageFileName(input.title)
  const file = new File([blob], filename, { type: 'image/png' })

  if (navigator.canShare?.({ files: [file] }) && navigator.share) {
    try {
      await navigator.share({ files: [file], title: input.title })
      return 'shared'
    } catch (error) {
      // 用户取消分享面板不算失败。
      if ((error as Error)?.name === 'AbortError') return 'cancelled'
      throw error
    }
  }

  downloadBlob(blob, filename)
  return 'downloaded'
}
