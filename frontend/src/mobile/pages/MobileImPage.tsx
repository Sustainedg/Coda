import { useCallback, useEffect, useState } from 'react'
import { api } from '../../lib/api'
import type { AgentProfile, ImChat, ImConnection, ImPeer, ImPlatform } from '../../lib/types'
import { PlatformIcon } from '../../components/PlatformIcon'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/**
 * IM（移动端）：把智能体连到聊天软件。
 *
 * 与桌面端功能对齐、交互按触屏重做：列表 + 底部弹层。配对请求放最上面，
 * 因为那是**现在要动**的东西；连接详情里可以改绑定的智能体、看聊天与配对账号。
 */
export function MobileImPage() {
  const [platforms, setPlatforms] = useState<ImPlatform[]>([])
  const [connections, setConnections] = useState<ImConnection[]>([])
  const [peers, setPeers] = useState<ImPeer[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [adding, setAdding] = useState<ImPlatform | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)

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
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    let reported = false
    const poll = () => {
      void Promise.all([api.listImConnections(), api.listImPeers()])
        .then(([nextConnections, nextPeers]) => {
          setConnections(nextConnections)
          setPeers(nextPeers)
        })
        .catch((e) => {
          // 轮询失败不反复弹提示，但第一次要让人知道「不是没反应，是刷新失败」。
          if (reported) return
          reported = true
          setError(`状态刷新失败：${describeError(e)}`)
        })
    }
    const timer = setInterval(poll, 5000)
    return () => clearInterval(timer)
  }, [])

  const run = async (task: () => Promise<unknown>) => {
    try {
      await task()
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const pending = peers.filter((item) => item.state === 'pending')
  const open = connections.find((item) => item.id === openId)

  return (
    <div className="m-page">
      <MobileTopBar title="IM" subtitle="在聊天软件里和智能体说话" />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          <div className="banner info">
            连接只用平台的长连接 / 长轮询，不需要公网地址；只有服务在运行时才能收发消息。
            凭据加密保存在本机，界面只显示「已填」。
          </div>

          {pending.length > 0 && (
            <div className="stack" style={{ gap: 8 }}>
              <div className="card-section-title">谁想连接</div>
              {pending.map((peer) => {
                const connection = connections.find((item) => item.id === peer.connection_id)
                return (
                  <div className="card" key={peer.id}>
                    <div className="m-item-title">{peer.sender_name || peer.sender_id}</div>
                    <div className="m-item-sub">
                      {connection ? `想通过 ${connection.account_name || connection.platform_name} 和你说话` : '想连接'}
                    </div>
                    <div className="row wrap" style={{ gap: 8 }}>
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
                  </div>
                )
              })}
            </div>
          )}

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">我的 IM 机器人</div>
            {loading ? (
              <p className="dim small">正在加载…</p>
            ) : connections.length === 0 ? (
              <div className="m-empty">
                <div className="m-empty-icon">💬</div>
                <p>还没有连接</p>
              </div>
            ) : (
              <div className="m-list">
                {connections.map((connection) => (
                  <button className="m-item" key={connection.id} onClick={() => setOpenId(connection.id)}>
                    <span style={{ fontSize: 18 }}>
                      {platforms.find((item) => item.id === connection.platform)?.icon ?? '🔌'}
                    </span>
                    <span className="m-item-main">
                      <span className="m-item-title">
                        {connection.account_name || connection.platform_name}
                      </span>
                      <span className="m-item-sub">
                        {connection.platform_name} · {stateLabel(connection.state)}
                        {connection.agent_name ? ` · ${connection.agent_name}` : ''}
                      </span>
                    </span>
                    <span className="m-item-chevron">›</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">选择一个平台</div>
            <div className="m-list">
              {platforms.map((platform) => (
                <button
                  className="m-item"
                  key={platform.id}
                  disabled={!platform.available}
                  onClick={() => setAdding(platform)}
                >
                  <PlatformIcon icon={platform.icon} size={20} />
                  <span className="m-item-main">
                    <span className="m-item-title">{platform.name}</span>
                    <span className="m-item-sub">
                      {platform.available
                        ? [platform.groups ? '支持群聊' : '仅私聊', platform.buttons ? '卡片按钮' : '编号回答'].join(' · ')
                        : platform.note ?? '暂不支持'}
                    </span>
                  </span>
                  <span className="m-item-chevron">›</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {adding && (
        <MobileSheet full title={`连接 ${adding.name}`} onClose={() => setAdding(null)}>
          <MobileAddConnection
            platform={adding}
            agents={agents}
            onDone={async () => {
              setAdding(null)
              await reload()
            }}
          />
        </MobileSheet>
      )}

      {open && (
        <MobileSheet full title={open.account_name || open.platform_name} onClose={() => setOpenId(null)}>
          <MobileConnectionDetail
            connection={open}
            agents={agents}
            onRun={run}
            onClose={() => setOpenId(null)}
          />
        </MobileSheet>
      )}
    </div>
  )
}

function MobileAddConnection({
  platform,
  agents,
  onDone,
}: {
  platform: ImPlatform
  agents: AgentProfile[]
  onDone: () => Promise<void>
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

  const missing = platform.fields.some(
    (field) => field.options.length === 0 && !(values[field.key] ?? '').trim(),
  )

  const submit = async () => {
    if (busy || !agentId || missing) return
    setBusy(true)
    setError(null)
    try {
      await api.createImConnection({ platform: platform.id, agent_profile_id: agentId, fields: values })
      await onDone()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}
      {platform.steps.length > 0 && (
        <div className="banner info">
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {platform.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
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
      <button className="btn primary" disabled={busy || !agentId || missing} onClick={() => void submit()}>
        {busy ? '正在核对…' : '连接'}
      </button>
    </div>
  )
}

function MobileConnectionDetail({
  connection,
  agents,
  onRun,
  onClose,
}: {
  connection: ImConnection
  agents: AgentProfile[]
  onRun: (task: () => Promise<unknown>) => Promise<void>
  onClose: () => void
}) {
  const [chats, setChats] = useState<ImChat[]>([])
  const [peers, setPeers] = useState<ImPeer[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [nextChats, nextPeers] = await Promise.all([
        api.listImChats(connection.id),
        api.listImPeers({ connection_id: connection.id }),
      ])
      setChats(nextChats)
      setPeers(nextPeers.filter((item) => item.state === 'allowed'))
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }, [connection.id])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}
      {connection.error && <div className="banner err">{connection.error}</div>}
      <div className="row wrap" style={{ gap: 6 }}>
        <span className="pill">{connection.platform_name}</span>
        <span className="pill">{stateLabel(connection.state)}</span>
        <span className="pill">配对 {connection.peers}</span>
        <span className="pill">聊天 {connection.chats}</span>
      </div>

      <label className="field">
        <span className="field-label">对话的智能体</span>
        <select
          className="select"
          value={connection.agent_profile_id ?? ''}
          onChange={(event) =>
            void onRun(() =>
              api.updateImConnection(connection.id, { agent_profile_id: event.target.value || null }),
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
        <div className="card-section-title">发什么</div>
        <p className="dim small" style={{ margin: 0 }}>
          默认只发回答；打开后过程内容会以引用块附在回答后面。
        </p>
        <div className="row wrap" style={{ gap: 16 }}>
          {TRACE_OPTIONS.map((option) => (
            <label className="switch" key={option.key}>
              <input
                type="checkbox"
                checked={connection.options?.[option.key] === true}
                onChange={(event) =>
                  void onRun(() =>
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

      <div className="row wrap" style={{ gap: 8 }}>
        <button
          className="btn sm"
          onClick={() => void onRun(() => api.updateImConnection(connection.id, { enabled: !connection.enabled }))}
        >
          {connection.enabled ? '停用' : '启用'}
        </button>
        {connection.state === 'error' && (
          <button className="btn sm" onClick={() => void onRun(() => api.retryImConnection(connection.id))}>
            重试
          </button>
        )}
        <button
          className="btn sm danger"
          onClick={() => {
            if (!confirm('移除这条连接？配对账号与聊天绑定会一起删掉。')) return
            void onRun(() => api.deleteImConnection(connection.id)).then(onClose)
          }}
        >
          移除
        </button>
      </div>

      <div className="card-section-title">配对账号</div>
      {peers.length === 0 ? (
        <p className="dim small">还没有配对账号。</p>
      ) : (
        <div className="chips">
          {peers.map((peer) => (
            <span className="pill ok" key={peer.id}>
              {peer.sender_name || peer.sender_id}
              <button
                className="link-btn"
                onClick={() => void onRun(() => api.removeImPeer(connection.id, peer.id)).then(load)}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="card-section-title">聊天</div>
      {chats.length === 0 ? (
        <p className="dim small">还没有聊天。</p>
      ) : (
        <div className="m-list">
          {chats.map((chat) => (
            <div className="card" key={chat.id}>
              <div className="m-item-title">
                {chat.kind === 'group' ? '👥' : '💬'} {chat.title || chat.chat_key}
              </div>
              {chat.kind === 'group' && (
                <label className="field">
                  <span className="field-label">绑定的智能体</span>
                  <select
                    className="select"
                    value={chat.agent_profile_id ?? ''}
                    onChange={(event) =>
                      void onRun(() => api.updateImChat(connection.id, chat.id, event.target.value || null)).then(
                        load,
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
                </label>
              )}
              <button
                className="btn sm ghost"
                onClick={() => void onRun(() => api.removeImChat(connection.id, chat.id)).then(load)}
              >
                移除
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
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
