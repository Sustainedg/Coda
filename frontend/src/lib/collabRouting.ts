/**
 * 协作画布的连线路由（纯函数）：支持左/上输入、右/下输出，并用正交折线绕开节点与既有连线。
 *
 * 坐标全部是画布内坐标。节点卡片是固定尺寸；路由只负责视觉，不参与协作运行语义。
 * 端口方向会随边保存，旧图没有端口信息时按节点相对位置自动推断。
 */
import type { CollabEdgePort } from './types'

export const COLLAB_NODE_W = 190
export const COLLAB_NODE_H = 92
export const COLLAB_CANVAS_W = 1400
export const COLLAB_CANVAS_H = 900

export interface CanvasPoint { x: number; y: number }
/** 路由只关心节点的坐标；编排画布与协作进度图都传这个最小结构（节点尺寸单独给）。 */
export interface RouteNode { x: number; y: number }
/** 路由只关心边保存的端口方向；编排画布的 CollabEdge 与进度图的连线都满足这个结构。 */
export interface RouteEdgeSpec {
  from_side?: CollabEdgePort | null
  to_side?: CollabEdgePort | null
}
type Axis = 'h' | 'v'
type OutputSide = 'right' | 'bottom'
type InputSide = 'left' | 'top'

interface Rect { left: number; right: number; top: number; bottom: number }
interface Segment { a: CanvasPoint; b: CanvasPoint; axis: Axis }

const OBSTACLE_MARGIN = 16
const PORT_STUB = 24
const BEND_COST = 24
const OVERLAP_COST = 700
const CROSS_COST = 260
const LINE_GAP = 10
const EPS = 0.01

export function portAnchor(
  node: RouteNode,
  side: CollabEdgePort,
  nodeW: number = COLLAB_NODE_W,
  nodeH: number = COLLAB_NODE_H,
): CanvasPoint {
  switch (side) {
    case 'left':
      return { x: node.x, y: node.y + nodeH / 2 }
    case 'right':
      return { x: node.x + nodeW, y: node.y + nodeH / 2 }
    case 'top':
      return { x: node.x + nodeW / 2, y: node.y }
    case 'bottom':
      return { x: node.x + nodeW / 2, y: node.y + nodeH }
  }
}

function normal(side: CollabEdgePort): CanvasPoint {
  if (side === 'left') return { x: -1, y: 0 }
  if (side === 'right') return { x: 1, y: 0 }
  if (side === 'top') return { x: 0, y: -1 }
  return { x: 0, y: 1 }
}

export function normalizedOutputSide(side?: CollabEdgePort | null): OutputSide {
  return side === 'bottom' ? 'bottom' : 'right'
}

export function normalizedInputSide(side?: CollabEdgePort | null): InputSide {
  return side === 'top' ? 'top' : 'left'
}

/** 旧连线没有端口字段时：上下排列优先走「下输出 → 上输入」，左右排列走「右输出 → 左输入」。 */
export function defaultEdgePorts(from: RouteNode, to: RouteNode): { fromSide: OutputSide; toSide: InputSide } {
  const dx = Math.abs(to.x - from.x)
  const dy = Math.abs(to.y - from.y)
  const vertical = dy > dx * 0.75
  return vertical
    ? { fromSide: 'bottom', toSide: 'top' }
    : { fromSide: 'right', toSide: 'left' }
}

export function edgePorts(edge: RouteEdgeSpec, from: RouteNode, to: RouteNode) {
  const fallback = defaultEdgePorts(from, to)
  return {
    fromSide: normalizedOutputSide(edge.from_side ?? fallback.fromSide),
    toSide: normalizedInputSide(edge.to_side ?? fallback.toSide),
  }
}

function rectOf(node: RouteNode, nodeW: number, nodeH: number): Rect {
  return {
    left: node.x - OBSTACLE_MARGIN,
    right: node.x + nodeW + OBSTACLE_MARGIN,
    top: node.y - OBSTACLE_MARGIN,
    bottom: node.y + nodeH + OBSTACLE_MARGIN,
  }
}

function insideRect(point: CanvasPoint, rect: Rect): boolean {
  return point.x > rect.left + EPS && point.x < rect.right - EPS
    && point.y > rect.top + EPS && point.y < rect.bottom - EPS
}

function segmentBlocked(a: CanvasPoint, b: CanvasPoint, rects: Rect[]): boolean {
  if (Math.abs(a.y - b.y) < EPS) {
    const y = a.y
    const left = Math.min(a.x, b.x)
    const right = Math.max(a.x, b.x)
    return rects.some((rect) => y > rect.top + EPS && y < rect.bottom - EPS
      && Math.max(left, rect.left + EPS) < Math.min(right, rect.right - EPS))
  }
  if (Math.abs(a.x - b.x) < EPS) {
    const x = a.x
    const top = Math.min(a.y, b.y)
    const bottom = Math.max(a.y, b.y)
    return rects.some((rect) => x > rect.left + EPS && x < rect.right - EPS
      && Math.max(top, rect.top + EPS) < Math.min(bottom, rect.bottom - EPS))
  }
  return true
}

function segmentsOf(points: CanvasPoint[]): Segment[] {
  const segments: Segment[] = []
  for (let index = 1; index < points.length; index += 1) {
    const a = points[index - 1]
    const b = points[index]
    if (Math.abs(a.x - b.x) < EPS && Math.abs(a.y - b.y) < EPS) continue
    segments.push({ a, b, axis: Math.abs(a.y - b.y) < EPS ? 'h' : 'v' })
  }
  return segments
}

function existingLinePenalty(a: CanvasPoint, b: CanvasPoint, existing: Segment[]): number {
  const horizontal = Math.abs(a.y - b.y) < EPS
  const left = Math.min(a.x, b.x)
  const right = Math.max(a.x, b.x)
  const top = Math.min(a.y, b.y)
  const bottom = Math.max(a.y, b.y)
  let penalty = 0
  for (const segment of existing) {
    if (horizontal && segment.axis === 'h') {
      if (Math.abs(a.y - segment.a.y) > LINE_GAP) continue
      const overlap = Math.min(right, Math.max(segment.a.x, segment.b.x))
        - Math.max(left, Math.min(segment.a.x, segment.b.x))
      if (overlap > EPS) penalty += OVERLAP_COST + overlap * 2
      continue
    }
    if (!horizontal && segment.axis === 'v') {
      if (Math.abs(a.x - segment.a.x) > LINE_GAP) continue
      const overlap = Math.min(bottom, Math.max(segment.a.y, segment.b.y))
        - Math.max(top, Math.min(segment.a.y, segment.b.y))
      if (overlap > EPS) penalty += OVERLAP_COST + overlap * 2
      continue
    }
    const crossingX = horizontal ? segment.a.x : a.x
    const crossingY = horizontal ? a.y : segment.a.y
    const withinThis = horizontal
      ? crossingX >= left - EPS && crossingX <= right + EPS
      : crossingY >= top - EPS && crossingY <= bottom + EPS
    const withinOther = horizontal
      ? crossingY >= Math.min(segment.a.y, segment.b.y) - EPS && crossingY <= Math.max(segment.a.y, segment.b.y) + EPS
      : crossingX >= Math.min(segment.a.x, segment.b.x) - EPS && crossingX <= Math.max(segment.a.x, segment.b.x) + EPS
    if (withinThis && withinOther) penalty += CROSS_COST
  }
  return penalty
}

function addAlongCoordinate(values: number[], candidates: number[]) {
  const sorted = [...new Set(candidates)].sort((left, right) => left - right)
  for (let index = 1; index < sorted.length; index += 1) {
    const gap = sorted[index] - sorted[index - 1]
    if (gap > 28) values.push(sorted[index - 1] + gap / 2)
  }
  values.push(...sorted)
}

function outwardPoint(anchor: CanvasPoint, side: CollabEdgePort, rects: Rect[], width: number, height: number) {
  const direction = normal(side)
  const distances = [PORT_STUB, PORT_STUB + 12, PORT_STUB + 28, PORT_STUB + 48]
  // 画布边界只约束「沿端口方向」的分量；正交分量（垂直口的 x / 水平口的 y）保持锚点值。
  // 否则紧凑画布（如协作进度图）上贴边的节点，垂直口的外向点会被钳到节点另一侧，
  // 连线整体被拽歪、被迫绕行。
  const step = (distance: number) => ({
    x: direction.x === 0
      ? anchor.x
      : Math.max(-40, Math.min(width + 40, anchor.x + direction.x * distance)),
    y: direction.y === 0
      ? anchor.y
      : Math.max(-40, Math.min(height + 40, anchor.y + direction.y * distance)),
  })
  let point = step(PORT_STUB)
  for (const distance of distances) {
    const next = step(distance)
    point = next
    if (!rects.some((rect) => insideRect(next, rect))) return next
  }
  return point
}

function simplify(points: CanvasPoint[]): CanvasPoint[] {
  const result: CanvasPoint[] = []
  for (const point of points) {
    const last = result[result.length - 1]
    if (last && Math.abs(last.x - point.x) < EPS && Math.abs(last.y - point.y) < EPS) continue
    while (result.length >= 2) {
      const before = result[result.length - 2]
      const middle = result[result.length - 1]
      const sameHorizontal = Math.abs(before.y - middle.y) < EPS && Math.abs(middle.y - point.y) < EPS
      const sameVertical = Math.abs(before.x - middle.x) < EPS && Math.abs(middle.x - point.x) < EPS
      if (!sameHorizontal && !sameVertical) break
      result.pop()
    }
    result.push(point)
  }
  return result
}

class MinHeap<T> {
  private items: { cost: number; value: T }[] = []

  get size() { return this.items.length }

  push(cost: number, value: T) {
    const item = { cost, value }
    this.items.push(item)
    let index = this.items.length - 1
    while (index > 0) {
      const parent = Math.floor((index - 1) / 2)
      if (this.items[parent].cost <= item.cost) break
      this.items[index] = this.items[parent]
      index = parent
    }
    this.items[index] = item
  }

  pop(): { cost: number; value: T } | undefined {
    if (this.items.length === 0) return undefined
    const first = this.items[0]
    const last = this.items.pop()!
    if (this.items.length > 0) {
      let index = 0
      while (true) {
        const left = index * 2 + 1
        const right = left + 1
        if (left >= this.items.length) break
        let child = left
        if (right < this.items.length && this.items[right].cost < this.items[left].cost) child = right
        if (this.items[child].cost >= last.cost) break
        this.items[index] = this.items[child]
        index = child
      }
      this.items[index] = last
    }
    return first
  }
}

function fallbackRoute(start: CanvasPoint, end: CanvasPoint, startOut: CanvasPoint, endOut: CanvasPoint): CanvasPoint[] {
  if (Math.abs(startOut.y - endOut.y) < EPS || Math.abs(startOut.x - endOut.x) < EPS) {
    return simplify([start, startOut, endOut, end])
  }
  const midX = (startOut.x + endOut.x) / 2
  return simplify([
    start,
    startOut,
    { x: midX, y: startOut.y },
    { x: midX, y: endOut.y },
    endOut,
    end,
  ])
}

function routeOrthogonal({
  from, to, fromSide, toSide, nodes, existingRoutes, width, height, nodeW, nodeH,
}: {
  from: CanvasPoint
  to: CanvasPoint
  fromSide: OutputSide
  toSide: InputSide
  nodes: RouteNode[]
  existingRoutes: CanvasPoint[][]
  width: number
  height: number
  nodeW: number
  nodeH: number
}): CanvasPoint[] {
  const rects = nodes.map((node) => rectOf(node, nodeW, nodeH))
  const start = from
  const end = to
  const startOut = outwardPoint(start, fromSide, rects, width, height)
  const endOut = outwardPoint(end, toSide, rects, width, height)
  if (Math.abs(startOut.x - endOut.x) < EPS && Math.abs(startOut.y - endOut.y) < EPS) {
    return simplify([start, startOut, end])
  }

  const xs: number[] = [startOut.x, endOut.x, start.x, end.x]
  const ys: number[] = [startOut.y, endOut.y, start.y, end.y]
  for (const rect of rects) {
    xs.push(rect.left, rect.right, (rect.left + rect.right) / 2)
    ys.push(rect.top, rect.bottom, (rect.top + rect.bottom) / 2)
    xs.push(rect.left - 10, rect.right + 10)
    ys.push(rect.top - 10, rect.bottom + 10)
  }
  const xValues: number[] = []
  const yValues: number[] = []
  addAlongCoordinate(xValues, xs)
  addAlongCoordinate(yValues, ys)

  const points: CanvasPoint[] = []
  for (const x of xValues) {
    for (const y of yValues) {
      const point = { x, y }
      if (!rects.some((rect) => insideRect(point, rect))) points.push(point)
    }
  }
  const indexOf = new Map<string, number>()
  const keyOf = (point: CanvasPoint) => `${point.x}|${point.y}`
  points.forEach((point, index) => indexOf.set(keyOf(point), index))
  const startIndex = indexOf.get(keyOf(startOut))
  const endIndex = indexOf.get(keyOf(endOut))
  if (startIndex === undefined || endIndex === undefined) {
    return fallbackRoute(start, end, startOut, endOut)
  }

  const existing = existingRoutes.flatMap(segmentsOf)
  const adjacency = points.map(() => [] as { to: number; axis: Axis; cost: number }[])
  const connect = (first: CanvasPoint, firstIndex: number, second: CanvasPoint, secondIndex: number) => {
    if (segmentBlocked(first, second, rects)) return
    const axis: Axis = Math.abs(first.y - second.y) < EPS ? 'h' : 'v'
    const cost = Math.hypot(second.x - first.x, second.y - first.y)
      + existingLinePenalty(first, second, existing)
    adjacency[firstIndex].push({ to: secondIndex, axis, cost })
    adjacency[secondIndex].push({ to: firstIndex, axis, cost })
  }

  const byX = new Map<number, { point: CanvasPoint; index: number }[]>()
  const byY = new Map<number, { point: CanvasPoint; index: number }[]>()
  points.forEach((point, index) => {
    const xsGroup = byX.get(point.x) ?? []
    xsGroup.push({ point, index })
    byX.set(point.x, xsGroup)
    const ysGroup = byY.get(point.y) ?? []
    ysGroup.push({ point, index })
    byY.set(point.y, ysGroup)
  })
  for (const group of byX.values()) {
    group.sort((left, right) => left.point.y - right.point.y)
    for (let index = 1; index < group.length; index += 1) {
      connect(group[index - 1].point, group[index - 1].index, group[index].point, group[index].index)
    }
  }
  for (const group of byY.values()) {
    group.sort((left, right) => left.point.x - right.point.x)
    for (let index = 1; index < group.length; index += 1) {
      connect(group[index - 1].point, group[index - 1].index, group[index].point, group[index].index)
    }
  }

  const initialAxis: Axis = fromSide === 'right' || fromSide === 'bottom' ? (fromSide === 'right' ? 'h' : 'v') : 'h'
  const stateKey = (index: number, axis: Axis) => `${index}:${axis}`
  const distance = new Map<string, number>()
  const previous = new Map<string, string>()
  const heap = new MinHeap<string>()
  const startState = stateKey(startIndex, initialAxis)
  distance.set(startState, 0)
  heap.push(0, startState)
  let endState: string | null = null

  while (heap.size > 0) {
    const current = heap.pop()!
    if (current.cost > (distance.get(current.value) ?? Infinity)) continue
    const [rawIndex, axis] = current.value.split(':') as [string, Axis]
    const index = Number(rawIndex)
    if (index === endIndex) {
      endState = current.value
      break
    }
    for (const next of adjacency[index]) {
      const turn = next.axis === axis ? 0 : BEND_COST
      const cost = current.cost + next.cost + turn
      const nextState = stateKey(next.to, next.axis)
      if (cost >= (distance.get(nextState) ?? Infinity)) continue
      distance.set(nextState, cost)
      previous.set(nextState, current.value)
      heap.push(cost, nextState)
    }
  }

  if (!endState) return fallbackRoute(start, end, startOut, endOut)
  const states: string[] = []
  for (let state: string | undefined = endState; state; state = previous.get(state)) states.push(state)
  states.reverse()
  const gridPoints = states.map((state) => points[Number(state.split(':')[0])])
  return simplify([start, ...gridPoints, end])
}

export function routeEdge({
  edge, from, to, nodes, existingRoutes = [], width = COLLAB_CANVAS_W, height = COLLAB_CANVAS_H,
  nodeW = COLLAB_NODE_W, nodeH = COLLAB_NODE_H,
}: {
  edge: RouteEdgeSpec
  from: RouteNode
  to: RouteNode
  nodes: RouteNode[]
  existingRoutes?: CanvasPoint[][]
  width?: number
  height?: number
  /** 节点尺寸：编排画布用 190×92，协作进度图用自己那份（132×88），同一套路由逻辑。 */
  nodeW?: number
  nodeH?: number
}): CanvasPoint[] {
  const ports = edgePorts(edge, from, to)
  return routeOrthogonal({
    from: portAnchor(from, ports.fromSide, nodeW, nodeH),
    to: portAnchor(to, ports.toSide, nodeW, nodeH),
    fromSide: ports.fromSide,
    toSide: ports.toSide,
    nodes,
    existingRoutes,
    width,
    height,
    nodeW,
    nodeH,
  })
}

export function previewRoute({
  from, fromSide, cursor, nodes, width = COLLAB_CANVAS_W, height = COLLAB_CANVAS_H,
  nodeW = COLLAB_NODE_W, nodeH = COLLAB_NODE_H,
}: {
  from: RouteNode
  fromSide: OutputSide
  cursor: CanvasPoint
  nodes: RouteNode[]
  width?: number
  height?: number
  nodeW?: number
  nodeH?: number
}): CanvasPoint[] {
  const start = portAnchor(from, fromSide, nodeW, nodeH)
  const startOut = outwardPoint(start, fromSide, nodes.map((node) => rectOf(node, nodeW, nodeH)), width, height)
  return simplify([
    start,
    startOut,
    { x: cursor.x, y: startOut.y },
    { x: cursor.x, y: cursor.y },
  ])
}

/** 把正交折线的直角变成小圆角，便于阅读；没有中间拐点时就是普通直线。 */
export function routePath(points: CanvasPoint[], radius = 10): string {
  if (points.length === 0) return ''
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`
  let path = `M ${points[0].x} ${points[0].y}`
  for (let index = 1; index < points.length - 1; index += 1) {
    const before = points[index - 1]
    const current = points[index]
    const after = points[index + 1]
    const inLength = Math.hypot(current.x - before.x, current.y - before.y)
    const outLength = Math.hypot(after.x - current.x, after.y - current.y)
    const corner = Math.min(radius, inLength / 2, outLength / 2)
    const inUnit = { x: (current.x - before.x) / inLength, y: (current.y - before.y) / inLength }
    const outUnit = { x: (after.x - current.x) / outLength, y: (after.y - current.y) / outLength }
    const start = { x: current.x - inUnit.x * corner, y: current.y - inUnit.y * corner }
    const end = { x: current.x + outUnit.x * corner, y: current.y + outUnit.y * corner }
    path += ` L ${start.x} ${start.y} Q ${current.x} ${current.y} ${end.x} ${end.y}`
  }
  const last = points[points.length - 1]
  path += ` L ${last.x} ${last.y}`
  return path
}

/** 标签放在整条路线中点，避免总被挤在起点或终点附近。 */
export function routeLabelPoint(points: CanvasPoint[]): CanvasPoint {
  const segments = segmentsOf(points)
  const total = segments.reduce((sum, segment) => sum + Math.hypot(segment.b.x - segment.a.x, segment.b.y - segment.a.y), 0)
  let walked = 0
  for (const segment of segments) {
    const length = Math.hypot(segment.b.x - segment.a.x, segment.b.y - segment.a.y)
    if (walked + length >= total / 2) {
      const ratio = length === 0 ? 0 : (total / 2 - walked) / length
      return {
        x: segment.a.x + (segment.b.x - segment.a.x) * ratio,
        y: segment.a.y + (segment.b.y - segment.a.y) * ratio - 8,
      }
    }
    walked += length
  }
  return points[Math.floor(points.length / 2)] ?? { x: 0, y: 0 }
}
