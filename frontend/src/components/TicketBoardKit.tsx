/**
 * 工单看板的**共享展示件**：工单卡、动作徽章、流转卡片、规则手风琴。
 *
 * 桌面端三栏与移动端单栏用的是同一批零件，所以「哪个动作配哪种颜色」「越权怎么高亮」
 * 只在 `lib/ticketBoard.ts` 与这里定义；两端差的是**外壳与交互**（桌面三栏、移动端分段切换），
 * 不是同一条信息的两套说法。
 *
 * 样式类名 `board-*` 两端各写一份（移动端不加载 index.css）：
 * `frontend/src/index.css` 与 `frontend/src/mobile/mobile.css` 里那段「工单看板」必须逐字一致，
 * 由 `backend/tests/test_mobile_web.py` 守着。
 */
import { useState } from 'react'
import type { BoardEntry, BoardTicket, BoardEntryTone, BoardFace } from '../lib/ticketBoard'
import { BOARD_TONES, BOARD_TONE_LABEL, formatDuration, formatTokens } from '../lib/ticketBoard'
import { AgentAvatar } from './AgentAvatar'

/** 动作徽章：看板上唯一的高饱和色块。 */
export function BoardActionTag({ tone, label }: { tone: BoardEntryTone; label?: string }) {
  return <span className={`board-tag tone-${tone}`}>{label ?? BOARD_TONE_LABEL[tone]}</span>
}

/** 工单状态徽章（沿用既有 `.pill` 的配色档位）。 */
export function BoardStatePill({ ticket }: { ticket: BoardTicket }) {
  return <span className={`pill ${ticket.statePill}`}>{ticket.stateLabel}</span>
}

/** 岗位头像：与智能体设定同一套渲染（自定义图 > 头像包 > emoji）+ 岗位提示。 */
export function BoardAvatar({
  avatar, avatarPreset, avatarFile, agentId, label, duty, size = 'md',
}: BoardFace & {
  label: string
  duty?: string
  size?: 'sm' | 'md'
}) {
  return (
    <span
      className={`board-avatar${size === 'sm' ? ' sm' : ''}`}
      title={duty ? `${label} · ${duty}` : label}
      aria-hidden
    >
      <AgentAvatar avatar={avatar} avatarPreset={avatarPreset} avatarFile={avatarFile} agentId={agentId} />
    </span>
  )
}

/** 左栏（移动端是选择器）里的一张工单：标题 + 状态 + 当前处理人。 */
export function BoardTicketCard({
  ticket, active, onSelect,
}: {
  ticket: BoardTicket
  active: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      className={`board-ticket${active ? ' active' : ''}`}
      style={ticket.depth > 0 ? { marginLeft: Math.min(ticket.depth, 3) * 12 } : undefined}
      aria-current={active}
      onClick={onSelect}
    >
      <div className="board-ticket-head">
        <span className="board-ticket-title">{ticket.title}</span>
        <BoardStatePill ticket={ticket} />
      </div>
      <div className="board-ticket-foot">
        <BoardAvatar
          avatar={ticket.holderFace.avatar}
          avatarPreset={ticket.holderFace.avatarPreset}
          avatarFile={ticket.holderFace.avatarFile}
          agentId={ticket.holderFace.agentId}
          label={ticket.holderLabel}
          duty={ticket.holderDuty}
          size="sm"
        />
        <span className="board-ticket-holder">当前处理人：{ticket.holderLabel}</span>
        {ticket.parentTicketId && <span className="board-ticket-child">子工单</span>}
      </div>
    </button>
  )
}

/** 动作图例：把徽章配色摊开给第一次看的人。 */
export function BoardLegend() {
  return (
    <div className="board-legend">
      {BOARD_TONES.map((tone) => (
        <BoardActionTag key={tone} tone={tone} />
      ))}
    </div>
  )
}

/** 越权审计里被点名的文件，按 新增 / 修改 / 删除 分组。 */
function auditFiles(entry: BoardEntry): { label: string; path: string }[] {
  const audit = entry.audit
  if (!audit) return []
  return [
    ...audit.added.map((path) => ({ label: '新增', path })),
    ...audit.modified.map((path) => ({ label: '修改', path })),
    ...audit.removed.map((path) => ({ label: '删除', path })),
  ]
}

/**
 * 时间线上的一条流转：左边头像、右边动作卡片。
 *
 * 技术元数据（token / 耗时 / 工作目录指纹）一律沉到卡片底部的小字；
 * 只有「只读节点却动了工作目录」会翻成红色高亮，别的都保持安静。
 */
export function BoardEntryCard({ entry }: { entry: BoardEntry }) {
  const tokens = formatTokens(entry.tokens)
  const duration = formatDuration(entry.durationMs)
  const meta = [tokens, duration].filter(Boolean).join(' · ')
  const files = entry.violation ? auditFiles(entry) : []

  return (
    <li className={`board-entry${entry.violation ? ' violation' : ''}`}>
      <BoardAvatar
        avatar={entry.avatar}
        avatarPreset={entry.avatarPreset}
        avatarFile={entry.avatarFile}
        agentId={entry.agentId}
        label={entry.agentLabel}
        duty={entry.duty}
      />
      <article className="board-entry-card">
        <div className="board-entry-head">
          <BoardActionTag tone={entry.tone} label={entry.toneLabel} />
          <span className="board-entry-actor">{entry.agentLabel}</span>
          {entry.targetLabel && (
            <>
              <span className="board-entry-arrow">→</span>
              <span className="board-entry-target">{entry.targetLabel}</span>
            </>
          )}
          {entry.at && <span className="board-entry-time">{entry.at}</span>}
        </div>

        <p className="board-entry-title">{entry.title}</p>
        {entry.detail && <p className="board-entry-detail">{entry.detail}</p>}

        {entry.error && <p className="board-entry-error mono">{entry.error}</p>}

        {entry.violation && entry.auditView && (
          <div className="board-entry-violation">
            <div className="board-entry-violation-head">⚠ {entry.auditView.text}</div>
            {files.length > 0 && (
              <ul className="board-entry-violation-files">
                {files.map((file) => (
                  <li key={`${file.label}-${file.path}`} className="mono">
                    {file.label} · {file.path}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {entry.artifacts.length > 0 && (
          <div className="board-entry-tools">
            {entry.artifacts.map((name) => (
              <span key={name} className="board-entry-tool mono">{name}</span>
            ))}
          </div>
        )}

        <footer className="board-entry-foot">
          {entry.auditView && !entry.violation && (
            <span className={`board-audit tone-${entry.auditView.tone}`}>{entry.auditView.text}</span>
          )}
          <span className="board-entry-meta mono">{meta || '无用量数据'}</span>
        </footer>
      </article>
    </li>
  )
}

/** 右栏（移动端第二个分段）里的一节规则：标题 + 条数 + 折叠。 */
function BoardAccordion({
  label, items, open, onToggle,
}: {
  label: string
  items: string[]
  open: boolean
  onToggle: () => void
}) {
  return (
    <section className={`board-accordion${open ? ' open' : ''}`}>
      <button type="button" className="board-accordion-head" aria-expanded={open} onClick={onToggle}>
        <span className="board-accordion-label">{label}</span>
        <span className="board-accordion-count mono">{items.length}</span>
        <span className="board-accordion-chevron" aria-hidden>⌄</span>
      </button>
      <div className="board-accordion-body">
        <div className="board-accordion-clip">
          <ul className="board-accordion-list">
            {items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  )
}

/** 工单的静态规则（目标 / 要求 / 验收标准 / 约束 / 交付物）：派发时写死，运行中不改。 */
export function BoardBriefSections({ ticket }: { ticket: BoardTicket }) {
  const [openKey, setOpenKey] = useState<string | null>(ticket.brief[0]?.key ?? null)
  if (ticket.brief.length === 0) {
    return <p className="dim small">这张工单没有填规则。</p>
  }
  return (
    <div className="board-accordions">
      {ticket.brief.map((section) => (
        <BoardAccordion
          key={section.key}
          label={section.label}
          items={section.items}
          open={openKey === section.key}
          // 手风琴：点开一节就收起上一节，右栏永远只有一屏可读的内容
          onToggle={() => setOpenKey((current) => (current === section.key ? null : section.key))}
        />
      ))}
    </div>
  )
}
