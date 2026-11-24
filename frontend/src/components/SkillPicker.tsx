import { useMemo, useState } from 'react'
import type { Skill } from '../lib/types'

interface Props {
  skills: Skill[]
  selected: string[]
  onChange: (next: string[]) => void
}

/**
 * 技能目录：搜索框 + 勾选列表。
 *
 * 只负责「搜 / 勾」，弹层外壳交给两端各自的弹层组件（桌面端 `Modal`、移动端 `MobileSheet`），
 * 因此两端共用同一份搜索与勾选逻辑（移动端不加载 index.css，但可以复用 components/）。
 */
export function SkillPicker({ skills, selected, onChange }: Props) {
  const [query, setQuery] = useState('')
  const keyword = query.trim().toLowerCase()
  const filtered = useMemo(
    () =>
      keyword
        ? skills.filter((skill) =>
            `${skill.name} ${skill.description ?? ''}`.toLowerCase().includes(keyword),
          )
        : skills,
    [skills, keyword],
  )

  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id])

  const visibleIds = filtered.map((skill) => skill.id)
  const allVisiblePicked = visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id))

  return (
    <div className="stack" style={{ gap: 10 }}>
      <input
        className="input"
        autoFocus
        placeholder="搜索技能…"
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
        <p className="dim small">{keyword ? '没有匹配的技能。' : '这个内核还没有可用的技能。'}</p>
      ) : (
        <div className="skill-picker">
          {filtered.map((skill) => (
            <label key={skill.id} className="skill-option">
              <input
                type="checkbox"
                checked={selected.includes(skill.id)}
                onChange={() => toggle(skill.id)}
              />
              <span className="skill-option-body">
                <span className="skill-option-name">
                  {skill.name}
                  {!skill.available && <span className="pill err">未扫描到</span>}
                </span>
                <span className="dim small">{skill.description || '（无描述）'}</span>
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
