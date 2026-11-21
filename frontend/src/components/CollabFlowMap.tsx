/**
 * 协作「流程图」：把这次协作画成一张分层节点图，替代原先的等距办公室动画。
 *
 * 与编排画布（`CollabCanvas`，编辑态）同一套视觉语言（点阵底纹 + 正交折线 + 箭头），
 * 但这里是**只读的实况**：
 * - 节点只显示头像 + 昵称（头像在左、昵称与状态在右；职责收进点开的详情卡，图上不挤）；
 * - 头像右上角一枚状态徽标：转圈=执行中、气泡=提问/等你决定、感叹号=出错、对勾=完成、
 *   半透明=等待（还没轮到）；连线只画箭头不写「指派 / 审查」文字，图面干净靠颜色区分；
 * - 点节点弹「现在具体在做什么」的详情卡（状态、粗略进度、最近动作、在办的工单），
 *   详情卡里还能跳去该岗位的实时对话；
 * - 回流（审查打回 / 返工）单独画成往回走的虚线，不参与分层。
 *
 * 布局优先还原编排画布保存的版式：节点坐标与连线端口方向原样照搬（所见即所得，
 * 主人在编辑器里排成两排，这里就是两排）；旧图没存坐标时才回退 `buildFlowRows`
 * 的「一行一层」自动分层。连线用正交折线按端口方向走，共享端点的多条线在中段逐条错开。
 * 画布整体可拖动平移（点阵底纹随之移动）；桌面端还支持滚轮缩放。
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import { buildFlowLinks, buildFlowRows, type CollabFlowEdge, type CollabAgentProgress } from '../lib/collabProgress'
import { routePath, type CanvasPoint } from '../lib/collabRouting'
import type { CollabEdgePort } from '../lib/types'
import { AgentAvatar } from './AgentAvatar'

/** 一个节点占的宽高（画布像素）。节点很小（头像 + 名字），细节都收进详情卡。 */
export const FLOW_NODE_W = 132
export const FLOW_NODE_H = 88
/** 层与层的行距：中间那条连线要放得下折线错开的量。 */
const ROW_GAP = 84
/** 同行两个节点的间距。 */
const NODE_GAP = 46
/** 画布四周留的边。 */
const CANVAS_PAD = 34
/** 还原版式的缩放比：进度图节点比编辑画布的小，坐标同比例缩，间距看着才自然。 */
const CANVAS_SCALE = 0.7

/** 把「一行一层」的岗位摆成画布坐标：同层均匀铺开、整层整体居中。 */
export function layoutFlowNodes<T extends { key: string }>(rows: { nodes: T[] }[]): {
  positions: Map<string, { x: number; y: number }>
  width: number
  height: number
} {
  const positions = new Map<string, { x: number; y: number }>()
  const rowWidths = rows.map((row) =>
    row.nodes.length * FLOW_NODE_W + Math.max(0, row.nodes.length - 1) * NODE_GAP,
  )
  const width = Math.max(CANVAS_PAD * 2, ...rowWidths.map((w) => w + CANVAS_PAD * 2))
  rows.forEach((row, rowIndex) => {
    const rowWidth = rowWidths[rowIndex]
    let x = (width - rowWidth) / 2
    const y = CANVAS_PAD + rowIndex * (FLOW_NODE_H + ROW_GAP)
    for (const node of row.nodes) {
      positions.set(node.key, { x, y })
      x += FLOW_NODE_W + NODE_GAP
    }
  })
  const height = rows.length * FLOW_NODE_H + (rows.length - 1) * ROW_GAP + CANVAS_PAD * 2
  return { positions, width, height: Math.max(height, 220) }
}

/**
 * 按编排画布保存的坐标还原版式：大卡片（190×92）等比缩到进度图的小节点（132×88），
 * 再整体平移让内容居中。坐标是「编辑器里拖出来的样子」，原样保留相对位置关系。
 */
export function layoutFlowNodesFromCanvas<T extends { key: string; x?: number | null; y?: number | null }>(
  nodes: T[],
): {
  positions: Map<string, { x: number; y: number }>
  width: number
  height: number
} {
  const positions = new Map<string, { x: number; y: number }>()
  const placed = nodes.filter((node) => typeof node.x === 'number' && typeof node.y === 'number')
  if (placed.length === 0) return { positions, width: 0, height: 0 }
  // 退化版式（所有节点挤在同一坐标，比如旧图没拖过、全是默认的 0,0）不算有效布局，
  // 让调用方回退自动分层，免得节点全叠成一摞。
  if (new Set(placed.map((node) => `${node.x},${node.y}`)).size <= 1) {
    return { positions: new Map(), width: 0, height: 0 }
  }
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const node of placed) {
    const x = node.x! * CANVAS_SCALE
    const y = node.y! * CANVAS_SCALE
    positions.set(node.key, { x, y })
    minX = Math.min(minX, x)
    minY = Math.min(minY, y)
    maxX = Math.max(maxX, x + FLOW_NODE_W)
    maxY = Math.max(maxY, y + FLOW_NODE_H)
  }
  // 平移到从画布边距开始：编辑器坐标可能从任意位置开始，进度图统一归零再留边。
  const offsetX = minX - CANVAS_PAD
  const offsetY = minY - CANVAS_PAD
  for (const at of positions.values()) {
    at.x -= offsetX
    at.y -= offsetY
  }
  return {
    positions,
    width: maxX - minX + CANVAS_PAD * 2,
    height: Math.max(maxY - minY + CANVAS_PAD * 2, 220),
  }
}

/**
 * 两个进度图节点之间的正交折线：按编排画布保存的端口方向（旧图为空时按相对位置推断）
 * 从输出端口走到输入端口。多条线共享端点时在起终点错开一点，免得叠成一条。
 */
function canvasEdgePath(
  from: { x: number; y: number },
  to: { x: number; y: number },
  fromSide: CollabEdgePort,
  toSide: CollabEdgePort,
  offsetIndex: number,
): { d: string } {
  // 端口锚点按进度图节点尺寸算（编辑器的 190×92 已缩到 132×88，锚点同比例）。
  const anchorOf = (at: { x: number; y: number }, side: CollabEdgePort): CanvasPoint => {
    switch (side) {
      case 'left':
        return { x: at.x, y: at.y + FLOW_NODE_H / 2 }
      case 'right':
        return { x: at.x + FLOW_NODE_W, y: at.y + FLOW_NODE_H / 2 }
      case 'top':
        return { x: at.x + FLOW_NODE_W / 2, y: at.y }
      case 'bottom':
        return { x: at.x + FLOW_NODE_W / 2, y: at.y + FLOW_NODE_H }
    }
  }
  const start = anchorOf(from, fromSide)
  const end = anchorOf(to, toSide)
  // 起终点沿端口方向各退一点：多条线共享端点时逐条错开。
  const nudge = offsetIndex * 6
  const nudged = (point: CanvasPoint, side: CollabEdgePort, forward: boolean): CanvasPoint => {
    const step = forward ? nudge : -nudge
    if (side === 'left') return { x: point.x, y: point.y + step }
    if (side === 'right') return { x: point.x, y: point.y + step }
    if (side === 'top') return { x: point.x + step, y: point.y }
    return { x: point.x + step, y: point.y }
  }
  const startOut = nudged(start, fromSide, true)
  const endIn = nudged(end, toSide, false)
  // 三段折线：出端口 → 中线 → 入端口；水平端口走垂直中线，垂直端口走水平中线。
  const horizontalFrom = fromSide === 'left' || fromSide === 'right'
  const points: CanvasPoint[] = [start, startOut]
  if (horizontalFrom) {
    if (startOut.y !== endIn.y) {
      const midX = (startOut.x + endIn.x) / 2
      points.push({ x: midX, y: startOut.y }, { x: midX, y: endIn.y })
    }
  } else if (startOut.x !== endIn.x) {
    const midY = (startOut.y + endIn.y) / 2
    points.push({ x: startOut.x, y: midY }, { x: endIn.x, y: midY })
  }
  points.push(endIn, end)
  return { d: routePath(points) }
}

/**
 * 旧连线没存端口方向时的推断（与 `collabRouting.ts:defaultEdgePorts` 同一语义）：
 * 上下排列优先走「下输出 → 上输入」，左右排列走「右输出 → 左输入」。
 */
function inferEdgeSides(
  from: { x: number; y: number },
  to: { x: number; y: number },
): { fromSide: CollabEdgePort; toSide: CollabEdgePort } {
  const dx = Math.abs(to.x - from.x)
  const dy = Math.abs(to.y - from.y)
  return dy > dx * 0.75
    ? { fromSide: 'bottom', toSide: 'top' }
    : { fromSide: 'right', toSide: 'left' }
}

/** 自动分层布局里相邻两层之间的那条「三段折线」：从上游底边中点走到下游顶边中点。 */
function edgePath(
  from: { x: number; y: number },
  to: { x: number; y: number },
  offsetIndex: number,
): { d: string; label: { x: number; y: number } } {
  const startX = from.x + FLOW_NODE_W / 2
  const startY = from.y + FLOW_NODE_H
  const endX = to.x + FLOW_NODE_W / 2
  const endY = to.y
  // 同一个下游节点可能有多条上游连线（或回流多条）：在中段水平线上逐条错开，免得叠成一条。
  const midY = (startY + endY) / 2 + offsetIndex * 14
  const d = `M ${startX} ${startY} L ${startX} ${midY} L ${endX} ${midY} L ${endX} ${endY}`
  return { d, label: { x: (startX + endX) / 2, y: midY - 5 } }
}

interface Props {
  nodes: CollabAgentProgress[]
  edges: CollabFlowEdge[]
  /** 点详情卡上的「查看实时对话」时回调；不传就不显示这个入口。 */
  onOpenNode?: (key: string) => void
}

export function CollabFlowMap({ nodes, edges, onOpenNode }: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const pointersRef = useRef(new Map<number, { x: number; y: number }>())
  const [dragging, setDragging] = useState(false)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [zoom, setZoom] = useState(1)
  const [focusedKey, setFocusedKey] = useState<string | null>(null)

  const rows = useMemo(() => buildFlowRows(nodes, edges), [nodes, edges])
  const links = useMemo(() => buildFlowLinks(nodes, edges), [nodes, edges])
  // 优先还原编排画布保存的版式（主人拖成什么样就显示什么样）；
  // 旧图没存坐标时回退「一行一层」的自动分层。
  const fromCanvas = useMemo(() => layoutFlowNodesFromCanvas(nodes), [nodes])
  const useCanvasLayout = fromCanvas.positions.size === nodes.length && nodes.length > 0
  const { positions, width, height } = useMemo(
    () => (useCanvasLayout ? fromCanvas : layoutFlowNodes(rows)),
    [useCanvasLayout, fromCanvas, rows],
  )
  const focused = nodes.find((node) => node.key === focusedKey) ?? null

  // 详情卡定位收进视口：预估尺寸，画布小（比如手机）时也不飘出去。
  const POPOVER_W = 300
  const POPOVER_H = 250
  const clamped = (value: number, max: number) => Math.max(8, Math.min(value, Math.max(max - POPOVER_W, 8)))
  const clampedTop = (value: number, max: number) => Math.max(8, Math.min(value, Math.max(max - POPOVER_H, 8)))

  // 画布尺寸变化（打开对话侧栏、转屏…）时把内容重新居中（只在没有手动平移过时）。
  const interactedRef = useRef(false)
  const viewportRef = useRef({ width: 0, height: 0 })
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const apply = (w: number, h: number) => {
      viewportRef.current = { width: w, height: h }
      if (!interactedRef.current) setPan({ x: (w - width * zoom) / 2, y: (h - height * zoom) / 2 })
    }
    apply(el.clientWidth, el.clientHeight)
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0].contentRect
      apply(rect.width, rect.height)
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [width, height, zoom])

  // 滚轮缩放（桌面端）：React 的 onWheel 是 passive 的，preventDefault 不生效，手挂原生监听。
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = el.getBoundingClientRect()
      const focusX = event.clientX - rect.left
      const focusY = event.clientY - rect.top
      const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
      const next = Math.min(1.8, Math.max(0.5, zoom * factor))
      if (next === zoom) return
      interactedRef.current = true
      // 以指针为锚点缩放：指针下的内容不动。
      setPan({
        x: focusX - ((focusX - pan.x) / zoom) * next,
        y: focusY - ((focusY - pan.y) / zoom) * next,
      })
      setZoom(next)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoom, pan])

  const endPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointersRef.current.delete(event.pointerId)
    if (pointersRef.current.size === 0) setDragging(false)
  }

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement
    // 点在节点 / 详情卡 / 工具按钮上不算「拖动画布」，也不收起详情卡。
    if (target.closest('.flow-node, .flow-popover, .flow-map-tools')) return
    const el = wrapRef.current
    if (!el) return
    el.setPointerCapture(event.pointerId)
    pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    setDragging(true)
    setFocusedKey(null)
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const previous = pointersRef.current.get(event.pointerId)
    if (!previous) return
    pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    if (pointersRef.current.size !== 1) return
    interactedRef.current = true
    setPan((prev) => ({ x: prev.x + event.clientX - previous.x, y: prev.y + event.clientY - previous.y }))
  }

  const zoomBy = (factor: number) => {
    const next = Math.min(1.8, Math.max(0.5, zoom * factor))
    if (next === zoom) return
    interactedRef.current = true
    const { width: vw, height: vh } = viewportRef.current
    setPan((prev) => ({
      x: vw / 2 - ((vw / 2 - prev.x) / zoom) * next,
      y: vh / 2 - ((vh / 2 - prev.y) / zoom) * next,
    }))
    setZoom(next)
  }

  const resetView = () => {
    interactedRef.current = false
    const { width: vw, height: vh } = viewportRef.current
    setZoom(1)
    setPan({ x: (vw - width) / 2, y: (vh - height) / 2 })
  }

  if (nodes.length === 0) return null

  // 回流与顺流分开：顺流参与分层（buildFlowRows 只认它），回流在图上单独画成虚线。
  const forward = links.filter((link) => !link.isLoop)
  const loops = links.filter((link) => link.isLoop)

  // 多条边可能共享端点：给每对端点分配错开序号。
  const offsetIndex = (index: number) => (index % 3) - 1

  // 画布版式：连线按编排画布保存的端口方向走（旧图为空时按节点相对位置推断）。
  const edgeRoute = (link: (typeof links)[number], index: number): { d: string } | null => {
    const from = positions.get(link.fromKey)
    const to = positions.get(link.toKey)
    if (!from || !to) return null
    if (!useCanvasLayout) {
      return edgePath(from, to, offsetIndex(index))
    }
    // 旧连线没存端口方向时按相对位置推断（与 `collabRouting.ts:defaultEdgePorts` 同一语义）：
    // 上下排列走「下输出 → 上输入」，左右排列走「右输出 → 左输入」。
    const fallback =
      link.fromSide && link.toSide
        ? { fromSide: link.fromSide, toSide: link.toSide }
        : inferEdgeSides(from, to)
    return canvasEdgePath(from, to, fallback.fromSide, fallback.toSide, offsetIndex(index))
  }

  return (
    <div
      className={`flow-map${dragging ? ' dragging' : ''}`}
      ref={wrapRef}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endPointer}
      onPointerCancel={endPointer}
      role="group"
      aria-label="协作流程图：可拖动平移，点节点看岗位进度"
    >
      <div
        className="flow-map-stage"
        style={{ width, height, transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom})` }}
      >
        <svg className="flow-links" width={width} height={height} aria-hidden="true">
          <defs>
            {(['assign', 'review', 'rework'] as const).map((kind) => (
              <marker
                key={kind}
                id={`flow-arrow-${kind}`}
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path className={`flow-arrow kind-${kind}`} d="M 0 0 L 10 5 L 0 10 z" />
              </marker>
            ))}
          </defs>
          {forward.map((link, index) => {
            const route = edgeRoute(link, index)
            if (!route) return null
            return (
              <g key={link.id}>
                <path d={route.d} className={`flow-link kind-${link.kind}`} markerEnd={`url(#flow-arrow-${link.kind})`} />
              </g>
            )
          })}
          {loops.map((link, index) => {
            const route = edgeRoute(link, index)
            if (!route) return null
            return (
              <g key={link.id}>
                <path
                  d={route.d}
                  className={`flow-link kind-${link.kind} loop`}
                  markerEnd={`url(#flow-arrow-${link.kind})`}
                />
              </g>
            )
          })}
        </svg>

        {nodes.map((node) => {
          const at = positions.get(node.key)
          if (!at) return null
          return (
            <button
              key={node.key}
              type="button"
              className={`flow-node state-${node.state}${node.isLive ? ' live' : ''}${node.key === focusedKey ? ' selected' : ''}`}
              style={{ left: at.x, top: at.y } as CSSProperties}
              onClick={() => setFocusedKey((prev) => (prev === node.key ? null : node.key))}
              title={`${node.label} · ${node.stateLabel}`}
              aria-label={`${node.label}，${node.stateLabel}，点开看在做什么`}
            >
              <span className="flow-node-face" aria-hidden="true">
                <AgentAvatar
                  avatar={node.avatar}
                  avatarPreset={node.avatarPreset}
                  avatarFile={node.avatarFile}
                  agentId={node.agentId}
                />
              </span>
              <span className="flow-node-info">
                <span className="flow-node-name">{node.label}</span>
                <span className="flow-node-state">{node.stateLabel}</span>
              </span>
              <span className="flow-node-badge" aria-hidden="true" />
            </button>
          )
        })}
      </div>

      {focused && (() => {
        const at = positions.get(focused.key) ?? { x: 0, y: 0 }
        const anchorX = pan.x + (at.x + FLOW_NODE_W / 2) * zoom
        const anchorY = pan.y + (at.y + FLOW_NODE_H) * zoom
        const { width: vw, height: vh } = viewportRef.current
        const wantRight = anchorX + 20 + POPOVER_W <= vw
        return (
          <div
            className="flow-popover"
            style={{
              left: clamped(wantRight ? anchorX + 20 : anchorX - 20 - POPOVER_W, vw),
              top: clampedTop(anchorY + 12, vh),
            }}
            role="dialog"
            aria-label={`${focused.label} 的进度`}
          >
            <div className="flow-popover-head">
              <span className="flow-popover-face" aria-hidden="true">
                <AgentAvatar
                  avatar={focused.avatar}
                  avatarPreset={focused.avatarPreset}
                  avatarFile={focused.avatarFile}
                  agentId={focused.agentId}
                />
              </span>
              <span className="flow-popover-id">
                <span className="flow-popover-name">{focused.label}</span>
                <span className={`flow-popover-status state-${focused.state}`}>{focused.stateLabel}</span>
              </span>
              <button
                type="button"
                className="flow-popover-close"
                onClick={() => setFocusedKey(null)}
                aria-label="关闭"
              >
                ✕
              </button>
            </div>

            <p className="flow-popover-summary">{focused.summary}</p>
            <div className="flow-popover-meta">
              <span>{focused.stepCount > 0 ? `跑了 ${focused.stepCount} 步` : '还没参与这一轮'}</span>
              {focused.lastAction && <span>最近动作：{focused.lastAction}</span>}
              {focused.activeTickets.length > 0 && (
                <span>{focused.activeTickets.length} 张工单在处理</span>
              )}
            </div>

            {onOpenNode && (
              <button
                type="button"
                className="btn ghost sm flow-popover-more"
                onClick={() => {
                  onOpenNode(focused.key)
                  setFocusedKey(null)
                }}
              >
                查看实时对话 →
              </button>
            )}
          </div>
        )
      })()}

      <div className="flow-map-tools">
        <button type="button" onClick={() => zoomBy(1.15)} aria-label="放大">＋</button>
        <button type="button" onClick={() => zoomBy(1 / 1.15)} aria-label="缩小">－</button>
        <button type="button" onClick={resetView} aria-label="复位视角">⤢</button>
      </div>

      <div className="flow-map-legend" aria-hidden="true">
        <span className="flow-tone-active"><i />执行中</span>
        <span className="flow-tone-waiting"><i />等你决定</span>
        <span className="flow-tone-error"><i />出错</span>
        <span className="flow-tone-pending"><i />等待</span>
        <span className="flow-tone-done"><i />完成</span>
      </div>

      <div className="flow-map-hint" aria-hidden="true">拖动平移 · 滚轮缩放 · 点节点看详情</div>
    </div>
  )
}
