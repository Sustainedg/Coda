import { Fragment, useMemo } from 'react'
import { TraceEntry } from './TraceEntry'
import { INVISIBLE_SEGMENT_NOTE, hasInvisibleSegment } from '../lib/trace'
import type { TraceEvent } from '../lib/types'

/**
 * 只读轨迹时间线：按 `turn_index` 分组、组内按（timestamp, seq）升序（方案 §6.4）。
 *
 * 「看不见的环节」必须明说（方案 §11 裁决 1 的落地）：
 * - CLI 内核的一轮只有进程入参与原始输出，子进程内部的模型 HTTP 报文不可见 ——
 *   在对应条目后面渲染明确的占位与来源标注，不伪造、不静默留白；
 * - 某一轮缺 `context_snapshot` 也一样补一条缺失态。
 */
export function TraceTimeline({ events }: { events: TraceEvent[] }) {
  const ordered = useMemo(
    () =>
      [...events].sort(
        (left, right) =>
          left.timestamp.localeCompare(right.timestamp) || left.seq - right.seq,
      ),
    [events],
  )

  const turns = useMemo(() => {
    const groups = new Map<number, TraceEvent[]>()
    for (const event of ordered) {
      const list = groups.get(event.turn_index)
      if (list) list.push(event)
      else groups.set(event.turn_index, [event])
    }
    return [...groups.entries()].sort((left, right) => left[0] - right[0])
  }, [ordered])

  return (
    <div className="trace-timeline">
      {turns.map(([turnIndex, list]) => {
        const runIds = [...new Set(list.map((event) => event.run_id).filter(Boolean))]
        return (
          <section className="trace-turn" key={turnIndex}>
            <div className="trace-turn-head">
              <span className="trace-turn-title">第 {turnIndex + 1} 轮</span>
              <span className="dim small mono-sm">{runIds.join(' · ')}</span>
              <span className="dim small">{list.length} 条</span>
            </div>
            {!list.some((event) => event.type === 'context_snapshot') && (
              <div className="trace-gap">
                <span className="pill warn">缺失</span>
                本轮未采集到 context_snapshot（上下文快照）。
                <span className="trace-gap-source">来源：trace_events 按轮核对，无该类型记录</span>
              </div>
            )}
            {list.map((event) => (
              <Fragment key={event.id}>
                <TraceEntry event={event} />
                {hasInvisibleSegment(event) && (
                  <div className="trace-gap">
                    <span className="pill warn">不可见</span>
                    {INVISIBLE_SEGMENT_NOTE}
                  </div>
                )}
              </Fragment>
            ))}
          </section>
        )
      })}
    </div>
  )
}
