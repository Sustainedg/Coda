/**
 * 协作编排画布：手写的「拖节点 + 连有向边」，不引入任何图库（铁律 12）。
 *
 * 节点是 `position: absolute` 的 div；输入端口在左/上，输出端口在右/下。
 * 连线采用正交折线，并绕开其他节点与既有连线，适合横向和纵向两种组织视角。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { addEdgeToGraph, isWarningIssue } from '../lib/collabDraft'
import {
  COLLAB_CANVAS_H, COLLAB_CANVAS_W, COLLAB_NODE_H, COLLAB_NODE_W,
  previewRoute, routeEdge, routeLabelPoint, routePath, type CanvasPoint,
} from '../lib/collabRouting'
import type { CollabEdge, CollabEdgeKind, CollabIssue, CollabNode, AgentProfile } from '../lib/types'
import { AgentAvatar } from './AgentAvatar'

export interface CollabSelection {
  kind: 'node' | 'edge'
  /** 节点用 key，边用 id */
  id: string
}

interface Props {
  nodes: CollabNode[]
  edges: CollabEdge[]
  agents: AgentProfile[]
  entryKey?: string | null
  selected: CollabSelection | null
  issues: CollabIssue[]
  onChange: (graph: { nodes: CollabNode[]; edges: CollabEdge[] }) => void
  onSelect: (selection: CollabSelection | null) => void
  /** 把左栏的某个智能体拖到画布上的落点（坐标为画布内坐标） */
  onDropAgent?: (agentId: string, at: { x: number; y: number }) => void
}

type DragState =
  | { kind: 'node'; key: string; offsetX: number; offsetY: number }
  | { kind: 'link'; fromKey: string; fromSide: 'right' | 'bottom' }

const DUTY_LABEL: Record<string, string> = {
  coordinator: '协调者',
  engineer: '实施者',
  reviewer: '审查者',
}

/* 画布缩放：下限看得见全图，上限够看清节点里的字；一次 10%。 */
const ZOOM_MIN = 0.4
const ZOOM_MAX = 2
const ZOOM_STEP = 0.1
/** 画布底纹的网格边长（px，未缩放时的值）；缩放时跟着放大，缩小时不低于 14px 免得糊成一片 */
const GRID_SIZE = 22

function isTypingTarget(target: EventTarget | null): boolean {
  const tag = (target as HTMLElement | null)?.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

export function CollabCanvas({ nodes, edges, agents, entryKey, selected, issues, onChange, onSelect, onDropAgent }: Props) {
  const innerRef = useRef<HTMLDivElement | null>(null)
  const [drag, setDrag] = useState<DragState | null>(null)
  const [cursor, setCursor] = useState<CanvasPoint | null>(null)
  const [hoverInput, setHoverInput] = useState<{ nodeKey: string; side: 'left' | 'top' } | null>(null)
  const [zoom, setZoom] = useState(1)

  const byKey = new Map(nodes.map((node) => [node.key, node]))
  const agentsById = new Map(agents.map((agent) => [agent.id, agent]))
  const nodeErrorKeys = new Set(
    issues.filter((issue) => issue.node_key && !isWarningIssue(issue)).map((issue) => issue.node_key),
  )
  const edgeErrorIds = new Set(
    issues.filter((issue) => issue.edge_id && !isWarningIssue(issue)).map((issue) => issue.edge_id),
  )

  // 按边的顺序逐条路由；后画的线会避开前面线已经占用的通道。
  const routes = useMemo(() => {
    const nodeMap = new Map(nodes.map((node) => [node.key, node]))
    const previousRoutes: CanvasPoint[][] = []
    return edges.map((edge) => {
      const from = nodeMap.get(edge.from_key)
      const to = nodeMap.get(edge.to_key)
      if (!from || !to) return null
      const points = routeEdge({
        edge,
        from,
        to,
        nodes,
        existingRoutes: previousRoutes,
        width: COLLAB_CANVAS_W,
        height: COLLAB_CANVAS_H,
      })
      previousRoutes.push(points)
      return { edge, points, path: routePath(points), label: routeLabelPoint(points) }
    }).filter((item): item is NonNullable<typeof item> => item !== null)
  }, [nodes, edges])
  const routeById = new Map(routes.map((route) => [route.edge.id, route]))
  const linkFrom = drag?.kind === 'link' ? byKey.get(drag.fromKey) : undefined
  const previewPoints = drag?.kind === 'link' && linkFrom && cursor
    ? previewRoute({ from: linkFrom, fromSide: drag.fromSide, cursor, nodes })
    : null

  const zoomBy = (delta: number) =>
    setZoom((current) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round((current + delta) * 100) / 100)))

  /** 屏幕坐标 → 画布坐标。节点坐标始终存未缩放的画布坐标，所以要除掉缩放倍数。 */
  const toCanvas = (clientX: number, clientY: number) => {
    const rect = innerRef.current?.getBoundingClientRect()
    return { x: (clientX - (rect?.left ?? 0)) / zoom, y: (clientY - (rect?.top ?? 0)) / zoom }
  }

  /** 根据指针下方元素决定接到哪个输入端口；拖到卡片空白处时按输出方向选择默认输入。 */
  const resolveInputTarget = (event: PointerEvent, fromSide: 'right' | 'bottom') => {
    const under = document.elementFromPoint(event.clientX, event.clientY)
    const nodeElement = under?.closest('[data-node-key]') as HTMLElement | null
    const nodeKey = nodeElement?.dataset.nodeKey
    if (!nodeKey) return null
    const portElement = under?.closest('[data-input-side]') as HTMLElement | null
    const explicit = portElement?.dataset.inputSide
    const side: 'left' | 'top' = explicit === 'top' || explicit === 'left'
      ? explicit
      : (fromSide === 'bottom' ? 'top' : 'left')
    return { nodeKey, side }
  }

  // 拖动 / 连线期间在 window 上监听，鼠标移出画布也不会卡住
  useEffect(() => {
    if (!drag) return
    const onMove = (event: PointerEvent) => {
      const point = toCanvas(event.clientX, event.clientY)
      if (drag.kind === 'node') {
        const next = nodes.map((node) =>
          node.key === drag.key
            ? { ...node, x: Math.max(0, Math.round(point.x - drag.offsetX)), y: Math.max(0, Math.round(point.y - drag.offsetY)) }
            : node,
        )
        onChange({ nodes: next, edges })
      } else {
        setCursor(point)
        setHoverInput(resolveInputTarget(event, drag.fromSide))
      }
    }
    const onUp = (event: PointerEvent) => {
      if (drag.kind === 'link') {
        const target = resolveInputTarget(event, drag.fromSide)
        if (target && target.nodeKey !== drag.fromKey) {
          onChange({
            nodes,
            edges: addEdgeToGraph(nodes, edges, drag.fromKey, target.nodeKey, undefined, {
              fromSide: drag.fromSide,
              toSide: target.side,
            }),
          })
        }
      }
      setDrag(null)
      setCursor(null)
      setHoverInput(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [drag, nodes, edges, onChange, zoom])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return
      if (!selected || isTypingTarget(event.target)) return
      event.preventDefault()
      if (selected.kind === 'node') {
        onChange({
          nodes: nodes.filter((node) => node.key !== selected.id),
          edges: edges.filter((edge) => edge.from_key !== selected.id && edge.to_key !== selected.id),
        })
      } else {
        onChange({ nodes, edges: edges.filter((edge) => edge.id !== selected.id) })
      }
      onSelect(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected, nodes, edges, onChange, onSelect])

  return (
    <div
      className="collab-canvas"
      style={{ backgroundSize: `${Math.max(14, GRID_SIZE * zoom)}px ${Math.max(14, GRID_SIZE * zoom)}px` }}
    >
      <div className="collab-canvas-hud">
        <div className="collab-canvas-legend" aria-hidden="true">
          <span><i className="in" />输入：左 / 上</span>
          <span><i className="out" />输出：右 / 下</span>
          <span className="dim">从输出端口拖到目标输入端口</span>
        </div>
        <div className="collab-canvas-tools" role="group" aria-label="画布缩放">
          <button
            type="button"
            className="collab-zoom-btn"
            onClick={() => zoomBy(-ZOOM_STEP)}
            disabled={zoom <= ZOOM_MIN}
            title="缩小"
            aria-label="缩小画布"
          >
            −
          </button>
          <button
            type="button"
            className="collab-zoom-value"
            onClick={() => setZoom(1)}
            title="回到 100%"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            className="collab-zoom-btn"
            onClick={() => zoomBy(ZOOM_STEP)}
            disabled={zoom >= ZOOM_MAX}
            title="放大"
            aria-label="放大画布"
          >
            ＋
          </button>
        </div>
      </div>
      <div className="collab-canvas-sizer" style={{ width: COLLAB_CANVAS_W * zoom, height: COLLAB_CANVAS_H * zoom }}>
        <div
          ref={innerRef}
          className="collab-canvas-inner"
          style={{ width: COLLAB_CANVAS_W, height: COLLAB_CANVAS_H, transform: `scale(${zoom})` }}
          onPointerDown={(event) => {
            if (event.target === event.currentTarget) onSelect(null)
          }}
          onDragOver={(event) => {
            if (onDropAgent) event.preventDefault()
          }}
          onDrop={(event) => {
            const agentId = event.dataTransfer.getData('text/collab-agent')
            if (!agentId || !onDropAgent) return
            event.preventDefault()
            const point = toCanvas(event.clientX, event.clientY)
            onDropAgent(agentId, { x: Math.max(0, point.x - COLLAB_NODE_W / 2), y: Math.max(0, point.y - 24) })
          }}
        >
          <svg className="collab-links" width={COLLAB_CANVAS_W} height={COLLAB_CANVAS_H}>
            <defs>
              {(['assign', 'review', 'rework'] as CollabEdgeKind[]).map((shape) => (
                <marker
                  key={shape}
                  id={`collab-arrow-${shape}`}
                  viewBox="0 0 10 10"
                  refX="9"
                  refY="5"
                  markerWidth="7"
                  markerHeight="7"
                  orient="auto-start-reverse"
                >
                  <path className={`collab-arrow kind-${shape}`} d="M 0 0 L 10 5 L 0 10 z" />
                </marker>
              ))}
            </defs>
            {edges.map((edge) => {
              const route = routeById.get(edge.id)
              if (!route) return null
              const active = selected?.kind === 'edge' && selected.id === edge.id
              return (
                <g key={edge.id}>
                  <path
                    d={route.path}
                    className="collab-link-hit"
                    onPointerDown={(event) => {
                      event.stopPropagation()
                      onSelect({ kind: 'edge', id: edge.id })
                    }}
                  />
                  <path
                    d={route.path}
                    className={`collab-link kind-${edge.kind}${active ? ' active' : ''}${edgeErrorIds.has(edge.id) ? ' err' : ''}`}
                    strokeDasharray={edge.kind === 'rework' ? '7 5' : undefined}
                    markerEnd={`url(#collab-arrow-${edge.kind})`}
                  />
                  {edge.label && (
                    <text className="collab-link-label" x={route.label.x} y={route.label.y}>
                      {edge.label}
                    </text>
                  )}
                </g>
              )
            })}
            {previewPoints && <path d={routePath(previewPoints)} className="collab-link preview" />}
          </svg>

          {nodes.map((node) => {
            const agent = agentsById.get(node.agent_profile_id)
            const active = selected?.kind === 'node' && selected.id === node.key
            const leftInputActive = hoverInput?.nodeKey === node.key && hoverInput.side === 'left'
            const topInputActive = hoverInput?.nodeKey === node.key && hoverInput.side === 'top'
            return (
              <div
                key={node.key}
                data-node-key={node.key}
                className={`collab-node${active ? ' active' : ''}${nodeErrorKeys.has(node.key) ? ' err' : ''}${hoverInput?.nodeKey === node.key ? ' drop-target' : ''}`}
                style={{ left: node.x, top: node.y, width: COLLAB_NODE_W, height: COLLAB_NODE_H }}
                onPointerDown={(event) => {
                  event.stopPropagation()
                  onSelect({ kind: 'node', id: node.key })
                  const point = toCanvas(event.clientX, event.clientY)
                  setDrag({ kind: 'node', key: node.key, offsetX: point.x - node.x, offsetY: point.y - node.y })
                }}
              >
                <div className="collab-node-head">
                  {/* 与左栏列表 / 节点详情卡同一个头像组件：自定义图 > 头像包 > emoji 三层回退，
                      换了头像这里同步显示（原来只认 emoji 字段，会一直显示旧头像）。 */}
                  <AgentAvatar
                    avatar={agent?.avatar}
                    avatarPreset={agent?.avatar_preset}
                    avatarFile={agent?.avatar_file}
                    agentId={agent?.id}
                    size={18}
                    className="collab-node-avatar"
                  />
                  <span className="collab-node-label">{node.label || agent?.name || node.key}</span>
                  {entryKey === node.key && <span className="collab-node-entry" title="入口节点（唯一能与用户对话）">★</span>}
                </div>
                <div className="collab-node-meta">
                  <span className={`pill ${node.duty === 'reviewer' ? 'ok' : node.duty === 'coordinator' ? 'brand' : ''}`}>
                    {DUTY_LABEL[node.duty] ?? node.duty}
                  </span>
                  <span className="mono-sm dim">{node.key}</span>
                </div>
                <div className="collab-node-desc">{node.capability_scope || agent?.name || '（未设置能力边界）'}</div>

                <span
                  className={`collab-port collab-port-in collab-port-left${leftInputActive ? ' drop-target' : ''}`}
                  data-input-side="left"
                  title="输入端口：从其他节点的输出端拖到这里"
                />
                <span
                  className={`collab-port collab-port-in collab-port-top${topInputActive ? ' drop-target' : ''}`}
                  data-input-side="top"
                  title="输入端口：从其他节点的输出端拖到这里"
                />
                <span
                  className="collab-port collab-port-out collab-port-right"
                  data-output-side="right"
                  title="输出端口：从这里拖到目标输入端口"
                  onPointerDown={(event) => {
                    event.stopPropagation()
                    event.preventDefault()
                    onSelect({ kind: 'node', id: node.key })
                    setDrag({ kind: 'link', fromKey: node.key, fromSide: 'right' })
                    setCursor(toCanvas(event.clientX, event.clientY))
                  }}
                />
                <span
                  className="collab-port collab-port-out collab-port-bottom"
                  data-output-side="bottom"
                  title="输出端口：从这里拖到目标输入端口"
                  onPointerDown={(event) => {
                    event.stopPropagation()
                    event.preventDefault()
                    onSelect({ kind: 'node', id: node.key })
                    setDrag({ kind: 'link', fromKey: node.key, fromSide: 'bottom' })
                    setCursor(toCanvas(event.clientX, event.clientY))
                  }}
                />
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
