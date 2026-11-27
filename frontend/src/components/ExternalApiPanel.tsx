import { useEffect, useMemo, useState } from 'react'
import { api, ApiError, fetchUsageCsv } from '../lib/api'
import type {
  ApiKeyCreateResult,
  ApiKeyRow,
  GatewayList,
  UsagePricingRow,
  UsageRecordRow,
} from '../lib/types'

/**
 * 外部模型 API（P3）：签发 / 吊销 API Key、维护手工单价、审计账本。
 *
 * - **明文只显示一次**：签发响应里的 `cosk_…` 立刻复制保存；列表只有掩码。
 * - Key 只能绑定自定义网关（平台用网关的上游密钥 inject 转发）。
 * - 单价只对**之后写入**的账本行生效，不重算历史。
 * - 审计明细支持导出 CSV（普通用户只导出自己的）。
 */
export function ExternalApiPanel() {
  const [gateways, setGateways] = useState<GatewayList | null>(null)
  const [keys, setKeys] = useState<ApiKeyRow[]>([])
  const [pricing, setPricing] = useState<UsagePricingRow[]>([])
  const [records, setRecords] = useState<UsageRecordRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)

  // —— 签发表单 ——
  const [gatewayId, setGatewayId] = useState('')
  const [name, setName] = useState('')
  const [dailyReq, setDailyReq] = useState('')
  const [dailyTok, setDailyTok] = useState('')
  const [monthlyReq, setMonthlyReq] = useState('')
  const [monthlyTok, setMonthlyTok] = useState('')
  const [newKey, setNewKey] = useState<ApiKeyCreateResult | null>(null)
  const [copied, setCopied] = useState(false)

  // —— 单价表单 ——
  const [pattern, setPattern] = useState('')
  const [inPrice, setInPrice] = useState('')
  const [outPrice, setOutPrice] = useState('')
  const [cacheRead, setCacheRead] = useState('')
  const [cacheWrite, setCacheWrite] = useState('')

  const customGateways = useMemo(
    () => (gateways?.items ?? []).filter((item) => item.kind === 'custom'),
    [gateways],
  )

  const reload = async () => {
    setLoading(true)
    try {
      const [gw, keyList, priceList, recordPage] = await Promise.all([
        api.listGateways(),
        api.listApiKeys(),
        api.listPricing(),
        api.listUsageRecords({ limit: 20 }),
      ])
      setGateways(gw)
      setKeys(keyList.keys)
      setPricing(priceList.pricing)
      setRecords(recordPage.records)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  useEffect(() => {
    if (!gatewayId && customGateways.length > 0) setGatewayId(customGateways[0].id)
  }, [customGateways, gatewayId])

  const createKey = async () => {
    if (!gatewayId) {
      setError('请先创建一个自定义网关，再签发外部 Key')
      return
    }
    try {
      const result = await api.createApiKey({
        name: name.trim() || '外部 Key',
        gateway_id: gatewayId,
        daily_request_limit: numOrNull(dailyReq),
        daily_token_limit: numOrNull(dailyTok),
        monthly_request_limit: numOrNull(monthlyReq),
        monthly_token_limit: numOrNull(monthlyTok),
      })
      setNewKey(result)
      setCopied(false)
      setName('')
      setDailyReq('')
      setDailyTok('')
      setMonthlyReq('')
      setMonthlyTok('')
      setError(null)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const copyKey = async () => {
    if (!newKey) return
    try {
      await navigator.clipboard.writeText(newKey.key)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const revoke = async (row: ApiKeyRow) => {
    if (!confirm(`吊销 Key「${row.key_mask}」？吊销后无法恢复。`)) return
    try {
      await api.revokeApiKey(row.id)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const addPricing = async () => {
    if (!pattern.trim()) {
      setError('请填写模型名匹配规则（精确或前缀，例如 claude-sonnet-4）')
      return
    }
    try {
      await api.createPricing({
        model_pattern: pattern.trim(),
        input_per_million: Number(inPrice) || 0,
        output_per_million: Number(outPrice) || 0,
        cache_read_per_million: Number(cacheRead) || 0,
        cache_creation_per_million: Number(cacheWrite) || 0,
      })
      setPattern('')
      setInPrice('')
      setOutPrice('')
      setCacheRead('')
      setCacheWrite('')
      setError(null)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const removePricing = async (row: UsagePricingRow) => {
    if (!confirm(`删除「${row.model_pattern}」的单价？`)) return
    try {
      await api.deletePricing(row.id)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const exportCsv = async () => {
    setExporting(true)
    try {
      const blob = await fetchUsageCsv()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'usage-records.csv'
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setExporting(false)
    }
  }

  return (
    <section className="stack">
      <div className="row" style={{ gap: 10 }}>
        <h2 className="card-section-title" style={{ margin: 0 }}>
          外部模型 API
        </h2>
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn sm" onClick={() => void reload()} disabled={loading}>
          {loading ? <span className="spin">◌</span> : '↻'} 刷新
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}

      <div className="banner info">
        外部客户端用 <code className="mono-sm">Authorization: Bearer cosk_…</code> 调用
        <code className="mono-sm"> /api/model-gateway</code>；平台用绑定网关的上游密钥转发。
        Key 只存哈希，明文只在签发时显示一次。
      </div>

      {newKey && (
        <div className="banner ok">
          <div className="stack" style={{ gap: 6 }}>
            <strong>新 Key 已生成（只显示这一次）</strong>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <code className="mono" style={{ wordBreak: 'break-all' }}>{newKey.key}</code>
              <button className="btn sm" onClick={() => void copyKey()}>
                {copied ? '已复制' : '复制'}
              </button>
              <button className="btn sm ghost" onClick={() => setNewKey(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

      {/* —— 签发 —— */}
      <article className="card stack">
        <div className="card-title">签发 API Key</div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div className="field" style={{ flex: 2, minWidth: 180 }}>
            <label className="field-label">名称</label>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 自动化脚本" />
          </div>
          <div className="field" style={{ flex: 2, minWidth: 180 }}>
            <label className="field-label">绑定网关（仅自定义）</label>
            <select className="select" value={gatewayId} onChange={(e) => setGatewayId(e.target.value)}>
              {customGateways.length === 0 && <option value="">（还没有自定义网关）</option>}
              {customGateways.map((gateway) => (
                <option key={gateway.id} value={gateway.id}>
                  {gateway.name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <LimitField label="日请求上限" value={dailyReq} onChange={setDailyReq} />
          <LimitField label="日 token 上限" value={dailyTok} onChange={setDailyTok} />
          <LimitField label="月请求上限" value={monthlyReq} onChange={setMonthlyReq} />
          <LimitField label="月 token 上限" value={monthlyTok} onChange={setMonthlyTok} />
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm primary" onClick={() => void createKey()} disabled={customGateways.length === 0}>
            + 签发 Key
          </button>
          <span className="dim small">配额留空 = 不限制；当前请求的 token 要等响应终态才知道，所以按已用量判。</span>
        </div>
      </article>

      {/* —— Key 列表 —— */}
      <article className="card stack">
        <div className="card-title">已签发的 Key</div>
        {keys.length === 0 ? (
          <p className="dim small">还没有外部 Key。</p>
        ) : (
          <div className="audit-table-wrap">
            <table className="audit-table">
              <thead>
                <tr>
                  <th>掩码</th>
                  <th>名称</th>
                  <th>网关</th>
                  <th>配额（日/月 请求 · token）</th>
                  <th>最近使用</th>
                  <th>状态</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {keys.map((row) => (
                  <tr key={row.id}>
                    <td className="mono-sm">{row.key_mask}</td>
                    <td>{row.name || '—'}</td>
                    <td>{row.gateway_name ?? row.gateway_id}</td>
                    <td className="mono-sm">
                      {row.daily_request_limit ?? '∞'} / {row.monthly_request_limit ?? '∞'} 次 ·{' '}
                      {row.daily_token_limit ?? '∞'} / {row.monthly_token_limit ?? '∞'} tok
                    </td>
                    <td className="mono-sm">{fmtTime(row.last_used_at)}</td>
                    <td>
                      {row.enabled ? (
                        <span className="pill ok">启用</span>
                      ) : (
                        <span className="pill warn">已吊销</span>
                      )}
                    </td>
                    <td>
                      {row.enabled && (
                        <button className="btn sm danger" onClick={() => void revoke(row)}>
                          吊销
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </article>

      {/* —— 单价 —— */}
      <article className="card stack">
        <div className="card-title">模型单价（美元 / 每百万 token）</div>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div className="field" style={{ flex: 2, minWidth: 200 }}>
            <label className="field-label">模型名匹配（精确或前缀）</label>
            <input className="input mono" value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="claude-sonnet-4" />
          </div>
          <PriceField label="输入" value={inPrice} onChange={setInPrice} />
          <PriceField label="输出" value={outPrice} onChange={setOutPrice} />
          <PriceField label="缓存读" value={cacheRead} onChange={setCacheRead} />
          <PriceField label="缓存写" value={cacheWrite} onChange={setCacheWrite} />
          <button className="btn sm primary" onClick={() => void addPricing()}>
            + 添加
          </button>
        </div>
        <p className="dim small">只对添加之后写入的账本行计价，不重算历史；更具体的模型名优先。</p>
        {pricing.length === 0 ? (
          <p className="dim small">还没有单价，账本成本保持「未记录」。</p>
        ) : (
          <div className="audit-table-wrap">
            <table className="audit-table">
              <thead>
                <tr>
                  <th>模型</th>
                  <th>输入</th>
                  <th>输出</th>
                  <th>缓存读</th>
                  <th>缓存写</th>
                  <th>范围</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {pricing.map((row) => (
                  <tr key={row.id}>
                    <td className="mono-sm">{row.model_pattern}</td>
                    <td>${row.input_per_million}</td>
                    <td>${row.output_per_million}</td>
                    <td>${row.cache_read_per_million}</td>
                    <td>${row.cache_creation_per_million}</td>
                    <td>{row.owner_global ? <span className="pill">平台级</span> : <span className="pill">个人</span>}</td>
                    <td>
                      <button className="btn sm ghost" onClick={() => void removePricing(row)}>
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </article>

      {/* —— 审计 —— */}
      <article className="card stack">
        <div className="row" style={{ gap: 10 }}>
          <div className="card-title" style={{ margin: 0, flex: 1 }}>
            账本审计（最近 20 条）
          </div>
          <button className="btn sm" onClick={() => void exportCsv()} disabled={exporting}>
            {exporting ? <span className="spin">◌</span> : '⬇'} 导出 CSV
          </button>
        </div>
        {records.length === 0 ? (
          <p className="dim small">还没有账本记录。</p>
        ) : (
          <div className="audit-table-wrap">
            <table className="audit-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>来源</th>
                  <th>归属</th>
                  <th>模型</th>
                  <th title="普通输入，不含缓存；缓存另见右侧两列">输入</th>
                  <th>缓存读</th>
                  <th>缓存写</th>
                  <th>输出</th>
                  <th title="该请求当时的上下文窗口占用，不是累计消耗">窗口</th>
                  <th>成本</th>
                </tr>
              </thead>
              <tbody>
                {records.map((row) => (
                  <tr key={row.id}>
                    <td className="mono-sm">{fmtTime(row.created_at)}</td>
                    <td>
                      <span
                        className="pill"
                        title={[
                          row.telemetry_source ? `采集：${row.telemetry_source}` : '',
                          row.response_shape ? `形状：${row.response_shape}` : '',
                          row.usage_protocol ? `协议：${row.usage_protocol}` : '',
                          row.upstream_request_id ? `上游 ID：${row.upstream_request_id}` : '',
                        ].filter(Boolean).join(' · ') || undefined}
                      >
                        {row.source}
                      </span>
                      {row.origin === 'external' && <span className="pill warn">外部</span>}
                    </td>
                    <td>{row.api_key_id ? <span className="pill">Key</span> : <span className="dim small">内部</span>}</td>
                    <td className="mono-sm">{row.model || '—'}</td>
                    <td className="mono-sm">{fmtTokens(row.input_tokens)}</td>
                    <td className="mono-sm">{fmtTokens(row.cache_read_tokens)}</td>
                    <td className="mono-sm">{fmtTokens(row.cache_creation_tokens)}</td>
                    <td className="mono-sm">{fmtTokens(row.output_tokens)}</td>
                    <td className="mono-sm">{fmtTokens(row.context_tokens)}</td>
                    <td className="mono-sm">{fmtCost(row.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </article>
    </section>
  )
}

function LimitField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <div className="field" style={{ flex: 1, minWidth: 130 }}>
      <label className="field-label">{label}</label>
      <input
        className="input"
        type="number"
        min={0}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="不限"
      />
    </div>
  )
}

function PriceField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <div className="field" style={{ flex: 1, minWidth: 90 }}>
      <label className="field-label">{label} $/1M</label>
      <input
        className="input"
        type="number"
        min={0}
        step="0.01"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0"
      />
    </div>
  )
}

function numOrNull(text: string): number | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  const value = Number(trimmed)
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : null
}

function fmtTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function fmtTokens(value?: number | null): string {
  return value == null ? '—' : value.toLocaleString()
}

function fmtCost(value?: number | null): string {
  return value == null ? '—' : `$${value.toFixed(6)}`
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}