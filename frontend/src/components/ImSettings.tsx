import { useCallback, useEffect, useMemo, useState } from 'react'
import { Modal } from './Modal'
import { PlatformIcon } from './PlatformIcon'
import { api, ApiError } from '../lib/api'
import type { AgentProfile, ImChat, ImConnection, ImPeer, ImPlatform } from '../lib/types'

/**
 * IM（设置页）：把智能体连到聊天软件。
 *
 * IM 是**通道**，不是成员：一条连接 = 聊天软件里的一个机器人 + 一个绑定的智能体。
 * 私聊直接进那个智能体当前的那段对话，回复再发回来源聊天；群里只有 @ 机器人才算数，
 * 并且可以单独指定它绑哪个智能体。
 *
 * 安全边界与「凭据」一致：凭据值只经后端加密保存，这里只显示「已填」；
 * 没配对的账号发消息只会收到一句「已发出连接请求」，要在这里点「允许」才算配对。
 */
export function ImSettings() {
  const [platforms, setPlatforms] = useState<ImPlatform[]>([])
  const [connections, setConnections] = useState<ImConnection[]>([])
  const [peers, setPeers] = useState<ImPeer[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [chats, setChats] = useState<Record<string, ImChat[]>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState<ImPlatform | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const [nextPlatforms, nextConnections, nextPeers, nextAgents] = await Promise.all([
        api.listImPlatforms(),
        api.listImConnections(),
        api.listImPeers(),
        api.listAgents(),
      ])
      setPlatforms(nextPlatforms)
      setConnections(nextConnections)
      setPeers(nextPeers)
      setAgents(nextAgents)
      setError(null)
      const detail = await Promise.all(
        nextConnections.map(async (item) => [item.id, await api.listImChats(item.id)] as const),
      )
      setChats(Object.fromEntries(detail))
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  // 连接状态是活的（重连、掉线），每 5 秒刷新一次状态与配对请求 —— 与「关于」页同一种做法。
  // 轮询失败不弹窗（会一直闪），但第一次失败要告诉用户，否则界面像「卡住了」。
  useEffect(() => {
    let reported = false
    const poll = () => {
      void Promise.all([api.listImConnections(), api.listImPeers()])
        .then(([nextConnections, nextPeers]) => {
          setConnections(nextConnections)
          setPeers(nextPeers)
        })
        .catch((e) => {
          if (reported) return
          reported = true
          setError(`状态刷新失败：${describe(e)}`)
        })
    }
    const timer = setInterval(poll, 5000)
    return () => clearInterval(timer)
  }, [])

  const pending = peers.filter((item) => item.state === 'pending')
  const paired = peers.filter((item) => item.state === 'allowed')

  const run = async (task: () => Promise<unknown>) => {
    try {
      await task()
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const toggle = (connection: ImConnection) =>
    run(() => api.updateImConnection(connection.id, { enabled: !connection.enabled }))

  const remove = (connection: ImConnection) => {
    if (!confirm(`移除「${connection.account_name || connection.platform_name}」？配对账号与聊天绑定会一起删掉。`)) return
    void run(() => api.deleteImConnection(connection.id))
  }

  return (
    <section className="stack">
      <div className="row" style={{ gap: 10 }}>
        <h2 className="card-section-title" style={{ margin: 0 }}>
          IM
        </h2>
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn sm" onClick={() => void reload()} disabled={loading}>
          {loading ? <span className="spin">◌</span> : '↻'} 刷新
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}

      <div className="banner info">
        把智能体连到聊天软件：配对账号后就能在那里直接对话、在群里 @ 机器人。连接只用平台的
        长连接 / 长轮询，电脑不需要公网地址或开放端口；<strong>只有服务在运行时才能收发消息</strong>。
        凭据加密保存在本机，界面只显示「已填」。
      </div>

      {pending.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          <h3 className="card-section-title" style={{ margin: 0 }}>
            谁想连接
          </h3>
          <div className="stack" style={{ gap: 8 }}>
            {pending.map((peer) => {
              const connection = connections.find((item) => item.id === peer.connection_id)
              return (
                <article className="card" key={peer.id}>
                  <div className="card-head">
                    <div style={{ minWidth: 0 }}>
                      <div className="card-title">{peer.sender_name || peer.sender_id}</div>
                      <div className="card-sub">
                        {connection ? `想通过 ${connection.account_name || connection.platform_name} 和你说话` : '想连接'}
                        {peer.chat_kind === 'group' ? ' · 群聊' : ' · 私聊'}
                      </div>
                    </div>
                    <span className="pill warn" style={{ marginLeft: 'auto' }}>
                      等待允许
                    </span>
                  </div>
                  <div className="card-actions" style={{ marginLeft: 0 }}>
                    <button
                      className="btn sm"
                      onClick={() => void run(() => api.answerImPeer(peer.connection_id, peer.id, false))}
                    >
                      拒绝
                    </button>
                    <button
                      className="btn sm primary"
                      onClick={() => void run(() => api.answerImPeer(peer.connection_id, peer.id, true))}
                    >
                      允许
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        </div>
      )}

      <div className="stack" style={{ gap: 8 }}>
        <h3 className="card-section-title" style={{ margin: 0 }}>
          我的 IM 机器人
        </h3>
        {connections.length === 0 && !loading ? (
          <p className="dim small">还没有连接。从下面的平台里选一个开始。</p>
        ) : (
          <div className="stack" style={{ gap: 10 }}>
            {connections.map((connection) => {
              const platform = platforms.find((item) => item.id === connection.platform)
              const open = expanded === connection.id
              const own = paired.filter((item) => item.connection_id === connection.id)
              const chatList = chats[connection.id] ?? []
              return (
                <article className="card" key={connection.id}>
                  <div className="card-head">
                    <div style={{ minWidth: 0 }}>
                      <div className="card-title">
                        <PlatformIcon icon={platform?.icon} fallback="🔌" />
                        {connection.account_name || connection.platform_name}
                      </div>
                      <div className="card-sub">
                        {connection.platform_name} · {stateText(connection)}
                        {connection.agent_name ? ` · 对话：${connection.agent_name}` : ' · 还没绑定智能体'}
                      </div>
                    </div>
                    <span className={`pill${stateClass(connection.state)}`} style={{ marginLeft: 'auto' }}>
                      <span className={`dot${connection.state === 'ready' ? '' : ' off'}`} />
                      {stateLabel(connection.state)}
                    </span>
                  </div>

                  {connection.error && <div className="banner err">{connection.error}</div>}

                  <div className="chips">
                    <span className="pill">配对账号 {connection.peers}</span>
                    <span className="pill">聊天 {connection.chats}</span>
                    {connection.pending > 0 && <span className="pill warn">待允许 {connection.pending}</span>}
                    {connection.secret_fields.length > 0 && (
                      <span className="pill">🔑 凭据已填（{connection.secret_fields.length}）</span>
                    )}
                  </div>

                  <div className="card-actions" style={{ marginLeft: 0 }}>
                    <button className="btn sm" onClick={() => setExpanded(open ? null : connection.id)}>
                      {open ? '收起' : '详情'}
                    </button>
                    <button className="btn sm" onClick={() => void toggle(connection)}>
                      {connection.enabled ? '停用' : '启用'}
                    </button>
                    {connection.state === 'error' && (
                      <button className="btn sm" onClick={() => void run(() => api.retryImConnection(connection.id))}>
                        重试
                      </button>
                    )}
                    <button className="btn sm danger" onClick={() => void remove(connection)}>
                      移除
                    </button>
                  </div>

                  {open && (
                    <div className="stack" style={{ gap: 10 }}>
                      <label className="field">
                        <span className="field-label">对话的智能体</span>
                        <select
                          className="select"
                          value={connection.agent_profile_id ?? ''}
                          onChange={(event) =>
                            void run(() =>
                              api.updateImConnection(connection.id, {
                                agent_profile_id: event.target.value || null,
                              }),
                            )
                          }
                        >
                          <option value="">（未绑定）</option>
                          {agents.map((agent) => (
                            <option key={agent.id} value={agent.id}>
                              {agent.avatar} {agent.name}
                            </option>
                          ))}
                        </select>
                      </label>

                      <div className="stack" style={{ gap: 8 }}>
                        <span className="card-section-title" style={{ margin: 0 }}>
                          发什么
                        </span>
                        <p className="dim small" style={{ margin: 0 }}>
                          默认只发智能体的回答。打开下面几项后，过程内容会以引用块附在回答后面
                          （单行截断，适合在手机上看）。
                        </p>
                        <div className="row wrap" style={{ gap: 16 }}>
                          {TRACE_OPTIONS.map((option) => (
                            <label className="switch" key={option.key}>
                              <input
                                type="checkbox"
                                checked={connection.options?.[option.key] === true}
                                onChange={(event) =>
                                  void run(() =>
                                    api.updateImConnection(connection.id, {
                                      options: { [option.key]: event.target.checked },
                                    }),
                                  )
                                }
                              />
                              <span>{option.label}</span>
                            </label>
                          ))}
                        </div>
                      </div>

                      <div className="stack" style={{ gap: 6 }}>
                        <span className="card-section-title" style={{ margin: 0 }}>
                          配对账号
                        </span>
                        {own.length === 0 ? (
                          <p className="dim small">还没有配对账号：在聊天软件里给机器人发一句话，这里就会出现请求。</p>
                        ) : (
                          <div className="chips">
                            {own.map((peer) => (
                              <span className="pill ok" key={peer.id}>
                                {peer.sender_name || peer.sender_id}
                                <button
                                  className="link-btn"
                                  onClick={() => void run(() => api.removeImPeer(connection.id, peer.id))}
                                  title="移除这个账号"
                                >
                                  ✕
                                </button>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>

                      <div className="stack" style={{ gap: 6 }}>
                        <span className="card-section-title" style={{ margin: 0 }}>
                          聊天
                        </span>
                        {chatList.length === 0 ? (
                          <p className="dim small">还没有聊天。私聊配对后自动出现；群里 @ 一次机器人也会出现在这里。</p>
                        ) : (
                          chatList.map((chat) => (
                            <div className="row" key={chat.id} style={{ gap: 8, flexWrap: 'wrap' }}>
                              <span style={{ minWidth: 0, flex: 1 }}>
                                {chat.kind === 'group' ? '👥' : '💬'} {chat.title || chat.chat_key}
                              </span>
                              {chat.kind === 'group' ? (
                                <select
                                  className="select"
                                  style={{ width: 'auto' }}
                                  value={chat.agent_profile_id ?? ''}
                                  onChange={(event) =>
                                    void run(() =>
                                      api.updateImChat(connection.id, chat.id, event.target.value || null),
                                    )
                                  }
                                >
                                  <option value="">跟随连接</option>
                                  {agents.map((agent) => (
                                    <option key={agent.id} value={agent.id}>
                                      {agent.avatar} {agent.name}
                                    </option>
                                  ))}
                                </select>
                              ) : (
                                <span className="pill">私聊</span>
                              )}
                              <button
                                className="btn sm ghost"
                                onClick={() => void run(() => api.removeImChat(connection.id, chat.id))}
                              >
                                移除
                              </button>
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  )}
                </article>
              )
            })}
          </div>
        )}
      </div>

      <div className="stack" style={{ gap: 8 }}>
        <h3 className="card-section-title" style={{ margin: 0 }}>
          {connections.length > 0 ? '添加一个 IM 机器人' : '选择一个平台'}
        </h3>
        <div className="grid">
          {platforms.map((platform) => {
            const count = connections.filter((item) => item.platform === platform.id).length
            return (
              <article className={`card${platform.available ? '' : ' muted'}`} key={platform.id}>
                <div className="card-head">
                  <div style={{ minWidth: 0 }}>
                    <div className="card-title">
                      <PlatformIcon icon={platform.icon} />
                      {platform.name}
                    </div>
                    <div className="card-sub">
                      {platform.available
                        ? [platform.groups ? '支持群聊' : '仅私聊', platform.buttons ? '支持卡片按钮' : '用编号回答']
                            .join(' · ')
                        : platform.note ?? '暂不支持'}
                    </div>
                  </div>
                  {count > 0 && (
                    <span className="pill ok" style={{ marginLeft: 'auto' }}>
                      已添加 {count}
                    </span>
                  )}
                </div>
                <div className="card-actions" style={{ marginLeft: 0 }}>
                  <button
                    className="btn sm"
                    disabled={!platform.available}
                    onClick={() => setAdding(platform)}
                  >
                    连接
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      </div>

      {adding && (
        <AddConnectionModal
          platform={adding}
          agents={agents}
          onClose={() => setAdding(null)}
          onSaved={async () => {
            setAdding(null)
            await reload()
          }}
        />
      )}
    </section>
  )
}

function AddConnectionModal({
  platform,
  agents,
  onClose,
  onSaved,
}: {
  platform: ImPlatform
  agents: AgentProfile[]
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const field of platform.fields) {
      if (field.options.length > 0) initial[field.key] = field.options[0].value
    }
    return initial
  })
  const [agentId, setAgentId] = useState(agents[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const missing = useMemo(
    () => platform.fields.some((field) => field.options.length === 0 && !(values[field.key] ?? '').trim()),
    [platform.fields, values],
  )

  const submit = async () => {
    if (busy || !agentId || missing) return
    setBusy(true)
    setError(null)
    try {
      await api.createImConnection({ platform: platform.id, agent_profile_id: agentId, fields: values })
      await onSaved()
    } catch (e) {
      setError(describe(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`连接 ${platform.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={busy || !agentId || missing} onClick={() => void submit()}>
            {busy ? '正在核对…' : '连接'}
          </button>
        </>
      }
    >
      <div className="stack">
        {error && <div className="banner err">{error}</div>}
        {platform.steps.length > 0 && (
          <div className="banner info">
            <ol style={{ margin: 0, paddingLeft: 18 }}>
              {platform.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            {platform.docs && (
              <p style={{ margin: '6px 0 0' }}>
                <a href={platform.docs} target="_blank" rel="noopener noreferrer">
                  打开平台后台
                </a>
              </p>
            )}
          </div>
        )}

        <label className="field">
          <span className="field-label">对话的智能体</span>
          <select className="select" value={agentId} onChange={(event) => setAgentId(event.target.value)}>
            {agents.length === 0 && <option value="">（还没有智能体）</option>}
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.avatar} {agent.name}
              </option>
            ))}
          </select>
        </label>

        {platform.fields.map((field) =>
          field.options.length > 0 ? (
            <label className="field" key={field.key}>
              <span className="field-label">{field.label}</span>
              <select
                className="select"
                value={values[field.key] ?? field.options[0].value}
                onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
              >
                {field.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <label className="field" key={field.key}>
              <span className="field-label">{field.label}</span>
              <input
                className="input"
                type={field.secret ? 'password' : 'text'}
                autoComplete="off"
                spellCheck={false}
                placeholder={field.hint ? `${field.hint}…` : field.label}
                value={values[field.key] ?? ''}
                onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
              />
            </label>
          ),
        )}

        <p className="dim small">
          保存前会先向平台核对一次凭据，核对不通过就不会保存。凭据加密保存在本机，界面只显示「已填」。
        </p>
      </div>
    </Modal>
  )
}

/** 连接级「发什么」的开关：与后端 ``im/base.py`` 的三个键一一对应。 */
const TRACE_OPTIONS = [
  { key: 'show_thinking' as const, label: '思考过程' },
  { key: 'show_tools' as const, label: '工具调用' },
  { key: 'show_logs' as const, label: '日志' },
]

function stateLabel(state: string): string {
  switch (state) {
    case 'ready':
      return '已连接'
    case 'connecting':
      return '连接中'
    case 'retrying':
      return '重试中'
    case 'error':
      return '连不上'
    case 'off':
      return '未启用'
    default:
      return state
  }
}

function stateClass(state: string): string {
  if (state === 'ready') return ' ok'
  if (state === 'error') return ' err'
  if (state === 'retrying') return ' warn'
  return ''
}

function stateText(connection: ImConnection): string {
  if (!connection.enabled) return '已停用'
  return stateLabel(connection.state)
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
