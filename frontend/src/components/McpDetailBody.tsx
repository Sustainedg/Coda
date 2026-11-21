import { AgentAvatar } from './AgentAvatar'
import { AgentIcon, KeyIcon, LinkIcon, PlugIcon } from './Icons'
import { transportLabel } from './McpPicker'
import { hasMissingBuiltinParams } from '../lib/mcp'
import type { McpServerDetail } from '../lib/types'

interface Props {
  server: McpServerDetail
  onAssign: () => void
  onEdit: () => void
  onToggle: () => void
  onDelete: () => void
  /** 详情弹层的标题已经写了名字：隐藏身份头里的名字，别重复两遍。 */
  showName?: boolean
}

/**
 * MCP 服务器详情（**两端共用这一份**）：桌面端放右侧详情面板，移动端放全屏弹层。
 *
 * 结构固定为「身份头 → 操作 → 连接方式 / 密钥 / 分配 → 一句说明」，先回答
 * 「这是什么、能不能用」，再回答「怎么连、给了谁」。样式见两端 `.mcp-*` 的共享段。
 */
export function McpDetailBody({
  server,
  onAssign,
  onEdit,
  onToggle,
  onDelete,
  showName = true,
}: Props) {
  const status = !server.available
    ? { label: '来源已消失', pill: 'err' }
    : hasMissingBuiltinParams(server)
      ? { label: '待填参数', pill: 'warn' }
      : !server.enabled
        ? { label: '已停用', pill: 'warn' }
        : { label: '可用', pill: 'ok' }
  // 元信息压成一行：来源 · 内核 · 传输方式 · 标识。分开是一排标签，读起来是碎片的。
  const meta = [
    server.source_label,
    server.builtin_key ? '内置' : null,
    server.type_name ?? null,
    transportLabel(server.transport),
    server.slug ? `标识 ${server.slug}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  const builtinParams = server.builtin_params ?? []
  const builtinKeys = new Set(builtinParams.map((param) => param.key))
  const secrets = [
    ...server.env_keys
      .filter((key) => !builtinKeys.has(key))
      .map((key) => ({
        id: `env-${key}`,
        label: '环境变量',
        name: key,
        masked: server.env_preview[key] ?? null,
      })),
    ...server.header_keys.map((key) => ({
      id: `header-${key}`,
      label: '请求头',
      name: key,
      masked: server.header_preview[key] ?? null,
    })),
  ]

  return (
    <div className="mcp-detail">
      <div className="mcp-hero">
        <div className="mcp-hero-tile">
          <PlugIcon size={21} />
        </div>
        <div className="mcp-hero-main">
          {showName && <div className="mcp-hero-name">{server.name}</div>}
          <div className="mcp-hero-meta">{meta}</div>
          {server.description && <p className="mcp-hero-desc">{server.description}</p>}
        </div>
        <span className={`pill ${status.pill}`}>{status.label}</span>
      </div>

      <div className="mcp-actions">
        <button className="btn sm primary" onClick={onAssign}>
          分配给智能体
        </button>
        {server.editable ? (
          <>
            <button className="btn sm" onClick={onEdit}>
              编辑
            </button>
            <button className="btn sm" onClick={onToggle}>
              {server.enabled ? '停用' : '启用'}
            </button>
            {server.builtin_key ? (
              <span className="mcp-hint">内置服务器由平台维护，不能删除；不需要时停用即可。</span>
            ) : (
              <button className="btn sm ghost" onClick={onDelete}>
                删除
              </button>
            )}
          </>
        ) : (
          <span className="mcp-hint">内容由该内核自己的配置决定，平台只读收录。</span>
        )}
      </div>

      {server.note && <div className="banner info">{server.note}</div>}

      <section className="mcp-section">
        <div className="mcp-section-head">
          <LinkIcon size={14} />
          <span className="mcp-section-title">连接方式</span>
        </div>
        <div className="mcp-rows">
          <div className="mcp-row">
            <span className="mcp-row-label">传输方式</span>
            <span className="mcp-row-value">{transportLabel(server.transport)}</span>
          </div>
          {server.transport === 'stdio' ? (
            <>
              <div className="mcp-row">
                <span className="mcp-row-label">启动命令</span>
                <span className="mcp-row-value mono">{server.command || '—'}</span>
              </div>
              <div className="mcp-row">
                <span className="mcp-row-label">启动参数</span>
                <span className="mcp-row-value mono">
                  {server.args.length ? server.args.join(' ') : '—'}
                </span>
              </div>
            </>
          ) : (
            <div className="mcp-row">
              <span className="mcp-row-label">服务器地址</span>
              <span className="mcp-row-value mono">{server.url || '—'}</span>
            </div>
          )}
        </div>
      </section>

      {builtinParams.length > 0 && (
        <section className="mcp-section">
          <div className="mcp-section-head">
            <KeyIcon size={14} />
            <span className="mcp-section-title">参数</span>
          </div>
          <div className="mcp-rows">
            {builtinParams.map((param) => {
              const masked = server.env_preview[param.key] ?? param.masked ?? null
              return (
                <div className="mcp-row" key={param.key}>
                  <span className="mcp-row-label">
                    {param.label}
                    {param.required && !masked ? '（必填）' : ''}
                  </span>
                  <span className="mcp-row-value mono">
                    {masked ?? (param.default || '未配置')}
                  </span>
                </div>
              )
            })}
          </div>
          <p className="mcp-hint">
            {builtinParams.some((param) => param.managed)
              ? '标有「平台托管」的参数由平台自动填写并加密保存，不需要手动配置；令牌失效时平台会自动换发一份新的。其余参数点「编辑」填写。'
              : '参数值加密保存，这里只显示掩码；点「编辑」可以重新填写。平台地址、令牌与模型名都不写死，按你的部署填。'}
          </p>
        </section>
      )}

      {secrets.length > 0 && (
        <section className="mcp-section">
          <div className="mcp-section-head">
            <KeyIcon size={14} />
            <span className="mcp-section-title">密钥（只显示掩码）</span>
          </div>
          <div className="mcp-rows">
            {secrets.map((item) => (
              <div className="mcp-row" key={item.id}>
                <span className="mcp-row-label">{item.label}</span>
                <span className="mcp-row-value mono">
                  <span className="mcp-key-name">{item.name}</span>{' '}
                  <span className="mcp-masked">{item.masked ?? '••••'}</span>
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="mcp-section">
        <div className="mcp-section-head">
          <AgentIcon size={14} />
          <span className="mcp-section-title">分配给哪些智能体</span>
        </div>
        {server.agents.length === 0 ? (
          <p className="mcp-hint">
            还没有智能体挂着这个服务器 —— 未分配的服务器不会被任何智能体加载。
          </p>
        ) : (
          <div className="mcp-agents">
            {server.agents.map((agent) => (
              <span className="mcp-agent" key={agent.id}>
                <AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} agentId={agent.id} size={22} className="mcp-agent-avatar" />
                {agent.name}
              </span>
            ))}
          </div>
        )}
      </section>

      <p className="mcp-hint">
        运行时由各内核适配器把它写进该对话的隔离配置：Claude Code 用独立的
        <span className="mono-sm"> --mcp-config </span>并关闭其它 MCP 配置，Codex 只保留分配到的
        <span className="mono-sm"> [mcp_servers] </span>，Gemini CLI 写进工作目录的项目级设置并限定启用范围。
      </p>
    </div>
  )
}
