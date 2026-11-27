import { useMemo, useState } from 'react'
import type { McpServer } from '../lib/types'

interface Props {
  servers: McpServer[]
  selected: string[]
  onChange: (next: string[]) => void
}

/** 传输方式的中文说法（编目里存的是 stdio / http / sse）。 */
export function transportLabel(transport: string): string {
  if (transport === 'stdio') return '本地进程'
  if (transport === 'http') return 'HTTP'
  if (transport === 'sse') return 'SSE'
  return transport
}

/**
 * MCP 目录：搜索框 + 勾选列表。
 *
 * 只负责「搜 / 勾」，弹层外壳交给两端各自的弹层组件（桌面端 `Modal`、移动端 `MobileSheet`），
 * 因此两端共用同一份搜索与勾选逻辑（移动端不加载 index.css，但可以复用 components/）。
 */
export function McpPicker({ servers, selected, onChange }: Props) {
  const [query, setQuery] = useState('')
  const keyword = query.trim().toLowerCase()
  // 平台内部能力（任务进度卡）由运行层自动挂载，不参与普通分配。
  const assignable = useMemo(
    () => servers.filter((server) => server.assignable !== false),
    [servers],
  )
  const filtered = useMemo(
    () =>
      keyword
        ? assignable.filter((server) =>
            `${server.name} ${server.slug} ${server.description ?? ''}`
              .toLowerCase()
              .includes(keyword),
          )
        : assignable,
    [assignable, keyword],
  )

  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id])

  const visibleIds = filtered.map((server) => server.id)
  const allVisiblePicked = visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id))

  return (
    <div className="stack" style={{ gap: 10 }}>
      <input
        className="input"
        autoFocus
        placeholder="搜索 MCP 服务器…"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />

      <div className="row wrap" style={{ gap: 8 }}>
        <button
          type="button"
          className="btn sm ghost"
          disabled={visibleIds.length === 0}
          onClick={() =>
            onChange(
              allVisiblePicked
                ? selected.filter((id) => !visibleIds.includes(id))
                : [...new Set([...selected, ...visibleIds])],
            )
          }
        >
          {allVisiblePicked ? '取消全选' : `全选（${visibleIds.length}）`}
        </button>
        <button
          type="button"
          className="btn sm ghost"
          disabled={selected.length === 0}
          onClick={() => onChange([])}
        >
          清空
        </button>
        <span className="dim small">已选 {selected.length} 个</span>
      </div>

      {filtered.length === 0 ? (
        <p className="dim small">
          {keyword ? '没有匹配的 MCP 服务器。' : '目录里还没有可以分配的 MCP 服务器。'}
        </p>
      ) : (
        <div className="skill-picker">
          {filtered.map((server) => (
            <label key={server.id} className="skill-option">
              <input
                type="checkbox"
                checked={selected.includes(server.id)}
                onChange={() => toggle(server.id)}
              />
              <span className="skill-option-body">
                <span className="skill-option-name">
                  {server.name}
                  <span className="pill mono-sm">{transportLabel(server.transport)}</span>
                  {!server.enabled && <span className="pill">已停用</span>}
                  {!server.available && <span className="pill err">来源已消失</span>}
                </span>
                <span className="dim small">
                  {server.description || server.url || server.command || '（无描述）'}
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
