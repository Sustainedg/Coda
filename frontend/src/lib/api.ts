import { getAccessToken, notifyUnauthorized } from './auth'
import type {
  ActiveCollabRun,
  AgentEvent, AgentProfile, AgentRuntime, AgentType, AttachmentRef, Availability, CollabChatMessage,
  CollabRun,
  CollabRunDetail, CollabTeam, CollabTeamUpdate, CollabTeamValidateIn, CollabValidation, CollabBudgetPatch, Conversation,
  ContextSettings, ContextUsage,
  ConversationDetail, ConversationListSnapshot, ConversationModelOptions, Credential, Gateway, GatewayList, GatewayProbeResult, McpRescanResult, McpServer,
  McpServerDetail, McpServerPayload, Overview, ProvisionEvent, RuntimeAuth, RuntimeLatest, Skill, UpdateEvent, UpdateStatus,
  ProvisionJobInfo,
  MarketInstallInput,
  MarketInstallResult,
  MarketKernel,
  MarketSkillList,
  MessagePart,
  SkillDetail,
  SkillRescanResult,
  SkillCopyAllResult,
  SkillTransferResult, TestResult,
  TitleSettings,
  TracePage,
  UsageOverview,
  AuthUser,
  AuthSession,
  AuthConfig,
  ImChat,
  ImConnection,
  ImConnectionOptions,
  ImPeer,
  ImPlatform,
} from './types'

const BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

/**
 * 各浏览器对「fetch 传输层失败」的说法（WebKit：Load failed；Chromium：Failed to fetch；
 * Firefox：NetworkError …）。这类失败不是接口给出的业务错误，而是连接真的断了。
 */
const NETWORK_FAILURE = /load failed|failed to fetch|networkerror|network error|network connection was lost|offline|err_internet_disconnected|err_network/i

/**
 * 这个异常是不是「网络中断」（可自愈），而不是接口返回的业务错误。
 *
 * 移动端切到后台、休眠唤醒、地铁里断网时，在途的 SSE / fetch 会以原生 `TypeError` 失败
 * （`ApiError` 是我们自己封装的业务错误，不算）。这类失败下一轮请求通常就恢复了，
 * 界面要把它当成「正在重连」而不是终态错误 —— 否则一个「Load failed」会永远挂在屏幕上。
 */
export function isNetworkError(error: unknown): boolean {
  if (error instanceof ApiError) return false
  return error instanceof TypeError && NETWORK_FAILURE.test(error.message)
}

/** 统一附加 Bearer 令牌；没有令牌时保持匿名（登录 / 注册 / 鉴权关闭时用）。 */
function authHeaders(): Record<string, string> {
  const token = getAccessToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** 登录接口本身返回 401 是「口令不对」，不该触发全局登出。 */
function isLoginAttempt(path: string): boolean {
  return path === '/api/auth/login' || path === '/api/auth/register'
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init?.headers ?? {}) },
  })
  const body = await readBody(response)
  if (!response.ok) {
    if (response.status === 401 && !isLoginAttempt(path)) notifyUnauthorized()
    throw new ApiError(response.status, describeBody(response, body))
  }
  if (response.status === 204 || !body.text.trim()) return undefined as T
  if (!body.isJson) throw new ApiError(response.status, describeBody(response, body))
  return body.data as T
}

interface ResponseBody {
  /** 原始响应文本（空响应体就是空串） */
  text: string
  /** 解析后的 JSON；不是 JSON 时忽略 */
  data: unknown
  isJson: boolean
}

/**
 * 先把响应体读成文本，再自己 `JSON.parse`。
 *
 * 不直接调 `response.json()`：它对「空响应体 / 非 JSON」在 WebKit 上抛的是
 * "The string did not match the expected pattern."，Chromium 上是另一种说法 ——
 * 一句与业务无关的浏览器方言，用户与开发者都无从下手（见 main.py 的 `/api/` 404 说明）。
 */
async function readBody(response: Response): Promise<ResponseBody> {
  const text = await response.text()
  if (!text.trim()) return { text, data: undefined, isJson: false }
  try {
    return { text, data: JSON.parse(text), isJson: true }
  } catch {
    return { text, data: undefined, isJson: false }
  }
}

/** 把一次响应翻译成能直接展示的中文：接口自己报的 `detail` 优先，其次说清楚「拿到的不是 JSON」。 */
function describeBody(response: Response, body: ResponseBody): string {
  if (body.isJson) {
    const detail = (body.data as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string') return detail
    return JSON.stringify(detail ?? body.data)
  }
  const contentType = response.headers.get('content-type')?.split(';')[0]?.trim() || '未知类型'
  const reason = body.text.trim() ? '返回的是 HTML 等非 JSON 内容' : '响应体是空的'
  return (
    `接口没有返回 JSON（HTTP ${response.status}，Content-Type: ${contentType}，${reason}）。` +
    '通常是接口不存在，或反向代理 / 静态兜底没有把 /api 转发到后端。'
  )
}

export const api = {
  /** 最轻的探活接口：只回状态与版本号（首屏拿版本号用它，不必汇总系统信息）。 */
  health: () => request<{ status: string; version: string }>('/api/health'),

  // --- 多用户系统 -----------------------------------------------------------
  /** 登录页的公开配置：是否开鉴权 / 是否允许注册 / 是否已有用户。 */
  authConfig: () => request<AuthConfig>('/api/auth/config'),
  me: () => request<AuthUser>('/api/auth/me'),
  login: (body: { username: string; password: string }) =>
    request<AuthSession>('/api/auth/login', { method: 'POST', body: JSON.stringify(body) }),
  register: (body: { username: string; password: string; display_name?: string; email?: string }) =>
    request<AuthSession>('/api/auth/register', { method: 'POST', body: JSON.stringify(body) }),
  updateMe: (body: { display_name?: string | null; email?: string | null; trace_debug?: boolean }) =>
    request<AuthUser>('/api/auth/me', { method: 'PUT', body: JSON.stringify(body) }),
  /** 上传头像：base64 图片（PNG / JPEG / WebP，≤2 MB），返回更新后的用户。 */
  uploadAvatar: (body: { content_base64: string; name?: string }) =>
    request<AuthUser>('/api/auth/me/avatar', { method: 'PUT', body: JSON.stringify(body) }),
  /** 清掉头像，回退到首字母。 */
  deleteAvatar: () => request<AuthUser>('/api/auth/me/avatar', { method: 'DELETE' }),
  changePassword: (body: { old_password: string; new_password: string }) =>
    request<{ ok: boolean; access_token: string }>('/api/auth/me/password', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  /** 生成（或重新生成）个人访问令牌：明文只在本次响应出现一次。 */
  issuePat: () =>
    request<{ ok: boolean; pat: string; issued_at: string; hint: string }>('/api/auth/me/pat', {
      method: 'POST',
    }),
  /** PAT 状态：只回「有没有 / 何时生成」，永不回令牌本体。 */
  patStatus: () => request<{ has_pat: boolean; issued_at: string | null }>('/api/auth/me/pat'),
  /** 吊销个人访问令牌（正在用它的 MCP 服务器会开始收到 401）。 */
  revokePat: () => request<{ ok: boolean }>('/api/auth/me/pat', { method: 'DELETE' }),
  listUsers: () => request<{ items: AuthUser[] }>('/api/auth/users').then((r) => r.items),
  createUser: (body: { username: string; password: string; display_name?: string; email?: string }) =>
    request<AuthUser>('/api/auth/users', { method: 'POST', body: JSON.stringify(body) }),
  updateUser: (id: string, body: { display_name?: string | null; email?: string | null; role?: string; is_active?: boolean }) =>
    request<AuthUser>(`/api/auth/users/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  resetUserPassword: (id: string, newPassword: string) =>
    request<{ ok: boolean; access_token?: string }>(
      `/api/auth/users/${encodeURIComponent(id)}/reset-password`,
      {
        method: 'POST',
        body: JSON.stringify({ old_password: '-', new_password: newPassword }),
      },
    ),
  overview: () => request<Overview>('/api/system/overview'),
  /** 用量页：token 消耗与技能调用汇总；可传时间范围（见 `lib/usage.ts:usageRangeQuery`）。
   *  `agent_id` 只统计这一个智能体（智能体设定页「统计」标签用）。 */
  usageOverview: (params?: { start?: string; end?: string; tz_offset?: number; agent_id?: string }) => {
    const search = new URLSearchParams()
    if (params?.start) search.set('start', params.start)
    if (params?.end) search.set('end', params.end)
    if (params?.tz_offset !== undefined) search.set('tz_offset', String(params.tz_offset))
    if (params?.agent_id) search.set('agent_id', params.agent_id)
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<UsageOverview>(`/api/usage/overview${suffix}`)
  },
  /** 「设置 → 关于」的版本更新：检测（GitHub Release）、本地状态。 */
  updateCheck: () => request<UpdateStatus>('/api/update/check'),
  updateStatus: () => request<UpdateStatus>('/api/update/status'),
  getProxy: () => request<{ proxy_url: string | null }>('/api/system/proxy'),
  setProxy: (proxyUrl: string | null) =>
    request<{ proxy_url: string | null }>('/api/system/proxy', {
      method: 'PUT',
      body: JSON.stringify({ proxy_url: proxyUrl }),
    }),
  /** 「设置 → 对话标题」：自动生成对话标题用哪个智能体 / 模型。 */
  getTitleSettings: () => request<TitleSettings>('/api/system/title'),
  setTitleSettings: (body: { enabled: boolean; agent_profile_id?: string | null; model?: string | null }) =>
    request<TitleSettings>('/api/system/title', { method: 'PUT', body: JSON.stringify(body) }),
  agentTypes: () => request<{ items: AgentType[] }>('/api/agent-types').then((r) => r.items),

  // --- 网关（API 密钥的唯一来源；官方链路 + 自定义网关）-----------------------
  listGateways: () => request<GatewayList>('/api/gateways'),
  createGateway: (body: unknown) => request<Gateway>('/api/gateways', { method: 'POST', body: JSON.stringify(body) }),
  updateGateway: (id: string, body: unknown) =>
    request<Gateway>(`/api/gateways/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteGateway: (id: string) => request<void>(`/api/gateways/${id}`, { method: 'DELETE' }),
  /** 「获取模型」：探测一次并返回模型清单（不落库）。密钥留空且带 gateway_id 时复用已存密钥。 */
  probeGateway: (body: {
    base_url: string
    protocol: string
    auth_style: string
    api_key?: string | null
    extra_headers?: Record<string, unknown>
    gateway_id?: string | null
  }) => request<GatewayProbeResult>('/api/gateways/probe', { method: 'POST', body: JSON.stringify(body) }),
  /** 用已存的配置重新拉一次模型，更新 model_catalog（不动白名单）。 */
  refreshGatewayModels: (id: string) =>
    request<GatewayProbeResult>(`/api/gateways/${id}/refresh-models`, { method: 'POST' }),

  // --- 智能体（人格 + 内核 + 网关 + 模型 + 技能）-----------------------------
  /** 列表（`groups` 走 `listAgentGroups`）。 */
  listAgents: () =>
    request<{ items: AgentProfile[]; groups: string[] }>('/api/agents').then((r) => r.items),
  /** 单个智能体的完整数据（智能体设定页用）。 */
  getAgent: (id: string) => request<AgentProfile>(`/api/agents/${encodeURIComponent(id)}`),
  listAgentGroups: () => request<{ items: AgentProfile[]; groups: string[] }>('/api/agents').then((r) => r.groups),
  createAgent: (body: unknown) => request<AgentProfile>('/api/agents', { method: 'POST', body: JSON.stringify(body) }),
  updateAgent: (id: string, body: unknown) =>
    request<AgentProfile>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAgent: (id: string) => request<void>(`/api/agents/${id}`, { method: 'DELETE' }),
  duplicateAgent: (id: string) => request<AgentProfile>(`/api/agents/${id}/duplicate`, { method: 'POST' }),
  testAgent: (id: string) => request<TestResult>(`/api/agents/${id}/test`, { method: 'POST' }),
  /** 上传 / 替换智能体的自定义头像（base64，PNG / JPEG / WebP ≤2MB）。 */
  uploadAgentAvatar: (id: string, body: { content_base64: string; name?: string }) =>
    request<AgentProfile>(`/api/agents/${id}/avatar`, { method: 'PUT', body: JSON.stringify(body) }),
  /** 清掉智能体的自定义头像，回退到内置头像包。 */
  deleteAgentAvatar: (id: string) =>
    request<AgentProfile>(`/api/agents/${id}/avatar`, { method: 'DELETE' }),
  // 凭据库（全局一份，智能体按引用使用）
  listCredentials: () => request<{ items: Credential[] }>('/api/credentials'),
  createCredential: (body: unknown) =>
    request<Credential>('/api/credentials', { method: 'POST', body: JSON.stringify(body) }),
  updateCredential: (id: string, body: unknown) =>
    request<Credential>(`/api/credentials/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteCredential: (id: string) => request<void>(`/api/credentials/${id}`, { method: 'DELETE' }),

  /** 整表替换智能体分配到的技能。 */
  setAgentSkills: (id: string, skillIds: string[]) =>
    request<AgentProfile>(`/api/agents/${id}/skills`, {
      method: 'PUT',
      body: JSON.stringify({ skill_ids: skillIds }),
    }),

  /** 整表替换智能体挂着的 MCP 服务器。 */
  setAgentMcpServers: (id: string, mcpIds: string[]) =>
    request<AgentProfile>(`/api/agents/${id}/mcp-servers`, {
      method: 'PUT',
      body: JSON.stringify({ mcp_ids: mcpIds }),
    }),

  // --- 运行时（各 CLI 内核的安装 / 更新 / 卸载）-----------------------------
  listRuntimes: () => request<{ items: AgentRuntime[] }>('/api/runtimes').then((r) => r.items),
  /** 强制重测一次运行时的安装状态与版本（「重新检测」按钮；后端会把结果写回缓存）。 */
  refreshRuntime: (typeId: string) =>
    request<Availability>(`/api/runtimes/${typeId}/refresh`, { method: 'POST' }),
  checkRuntimeLatest: (typeId: string) =>
    request<RuntimeLatest>(`/api/runtimes/${typeId}/check`, { method: 'POST' }),
  /** 账号型内核的登录状态（打开详情弹层 / 登录结束后调用）。 */
  getRuntimeAuth: (typeId: string) =>
    request<RuntimeAuth>(`/api/runtimes/${typeId}/auth`),
  /** 安装 / 更新不改动已有配置，直接发起；卸载是破坏性操作，必须带 confirm（服务端会再校验一次）。 */
  installRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/install`, {}, onEvent, signal),
  updateRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/update`, {}, onEvent, signal),
  uninstallRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/uninstall`, { confirm: true }, onEvent, signal),
  /** 账号型内核扫码登录：二维码通过 provision.qr 事件推回来。 */
  loginRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/login`, {}, onEvent, signal),
  /**
   * 双向登录：把界面提交的文本（OAuth 授权码）写进正在进行的登录进程。
   * 登录进程通过 provision.input 事件声明「需要输入」。
   */
  loginRuntimeInput: (typeId: string, text: string) =>
    request<{ ok: boolean }>(`/api/runtimes/${typeId}/login/input`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  /** 多账号内核：切换到一个已保存的账号，返回切换后的登录状态。 */
  activateRuntimeAccount: (typeId: string, accountId: string) =>
    request<RuntimeAuth>(
      `/api/runtimes/${typeId}/accounts/${encodeURIComponent(accountId)}/activate`,
      { method: 'POST' },
    ),
  /** 多账号内核：删除一个已保存的账号（不影响当前正在用的登录态）。 */
  removeRuntimeAccount: (typeId: string, accountId: string) =>
    request<RuntimeAuth>(
      `/api/runtimes/${typeId}/accounts/${encodeURIComponent(accountId)}`,
      { method: 'DELETE' },
    ),
  /** 列出所有进行中 / 刚完成的安装任务（刷新页面后恢复状态用）。 */
  listProvisionJobs: () =>
    request<{ items: ProvisionJobInfo[] }>('/api/runtimes/provision/jobs').then((r) => r.items),
  /** 重连到一个进行中任务的输出流（刷新后查看实时进度）。 */
  streamProvisionJob: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamGet<ProvisionEvent>(`/api/runtimes/${typeId}/provision/stream`, onEvent, signal),
  /** 清除一个已完成的任务记录。 */
  clearProvisionJob: (typeId: string) =>
    request<{ ok: boolean }>(`/api/runtimes/${typeId}/provision`, { method: 'DELETE' }),

  listSkills: (params?: {
    type_id?: string
    source_kind?: string
    q?: string
    available?: boolean
  }) => {
    const search = new URLSearchParams()
    if (params?.type_id) search.set('type_id', params.type_id)
    if (params?.source_kind) search.set('source_kind', params.source_kind)
    if (params?.q) search.set('q', params.q)
    if (params?.available !== undefined) search.set('available', String(params.available))
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<{ items: Skill[] }>(`/api/skills${suffix}`).then((r) => r.items)
  },
  getSkill: (id: string) => request<SkillDetail>(`/api/skills/${encodeURIComponent(id)}`),
  deleteSkill: (id: string) =>
    request<{ ok: boolean; name: string; remaining_kernels?: string[] }>(
      `/api/skills/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  rescanSkills: () => request<SkillRescanResult>('/api/skills/rescan', { method: 'POST' }),
  /** 导出技能：拿回 zip 的原始字节（接口要鉴权，不能直接塞进 <a href>）。 */
  exportSkill: async (id: string): Promise<Blob> => {
    const response = await fetch(`${BASE}/api/skills/${encodeURIComponent(id)}/export`, {
      headers: authHeaders(),
    })
    if (!response.ok) {
      if (response.status === 401) notifyUnauthorized()
      const body = await readBody(response)
      throw new ApiError(response.status, describeBody(response, body))
    }
    return response.blob()
  },
  /** 导入技能：内容走 base64（与附件上传同一套做法，避免 multipart 依赖）。 */
  importSkill: (body: { type_id: string; name?: string; content_base64: string }) =>
    request<SkillTransferResult>('/api/skills/import', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  /** 把技能挂载到另一个内核（技能库按 slug 只存一份，不复制智能体分配）。 */
  copySkill: (id: string, typeId: string) =>
    request<SkillTransferResult>(`/api/skills/${encodeURIComponent(id)}/copy`, {
      method: 'POST',
      body: JSON.stringify({ type_id: typeId }),
    }),
  /** 把技能挂载到全部支持技能的内核（冲突的内核只跳过并汇报）。 */
  copySkillToAll: (id: string) =>
    request<SkillCopyAllResult>(`/api/skills/${encodeURIComponent(id)}/copy-all`, {
      method: 'POST',
    }),

  // --- MCP 服务器（统一编目 + 通过智能体分配生效）-----------------------------
  listMcpServers: (params?: {
    type_id?: string
    source_kind?: string
    available?: boolean
    q?: string
  }) => {
    const search = new URLSearchParams()
    if (params?.type_id) search.set('type_id', params.type_id)
    if (params?.source_kind) search.set('source_kind', params.source_kind)
    if (params?.q) search.set('q', params.q)
    if (params?.available !== undefined) search.set('available', String(params.available))
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<{ items: McpServer[] }>(`/api/mcp-servers${suffix}`).then((r) => r.items)
  },
  getMcpServer: (id: string) => request<McpServerDetail>(`/api/mcp-servers/${encodeURIComponent(id)}`),
  createMcpServer: (body: McpServerPayload) =>
    request<McpServer>('/api/mcp-servers', { method: 'POST', body: JSON.stringify(body) }),
  updateMcpServer: (id: string, body: Partial<McpServerPayload>) =>
    request<McpServer>(`/api/mcp-servers/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteMcpServer: (id: string) => request<void>(`/api/mcp-servers/${id}`, { method: 'DELETE' }),
  rescanMcpServers: () => request<McpRescanResult>('/api/mcp-servers/rescan', { method: 'POST' }),

  /** 技能市场（SkillHub）：浏览 / 安装到技能中心 / 整体卸载。 */
  listMarketSkills: (params?: {
    page?: number
    page_size?: number
    sort_by?: string
    order?: string
    keyword?: string
    category?: string
  }) => {
    const search = new URLSearchParams()
    if (params?.page) search.set('page', String(params.page))
    if (params?.page_size) search.set('page_size', String(params.page_size))
    if (params?.sort_by) search.set('sort_by', params.sort_by)
    if (params?.order) search.set('order', params.order)
    if (params?.keyword) search.set('keyword', params.keyword)
    if (params?.category) search.set('category', params.category)
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<MarketSkillList>(`/api/skillhub/skills${suffix}`)
  },
  marketKernels: () =>
    request<{ items: MarketKernel[] }>('/api/skillhub/kernels').then((r) => r.items),
  installMarketSkill: (body: MarketInstallInput) =>
    request<MarketInstallResult>('/api/skillhub/install', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  uninstallMarketSkill: (skillId: string) =>
    request<{ ok: boolean; name: string; remaining_kernels?: string[] }>(
      '/api/skillhub/uninstall',
      {
        method: 'POST',
        body: JSON.stringify({ skill_id: skillId }),
      },
    ),

  /** 列表 + 正在跑的运行：后者让左侧栏知道哪些对话正在回复。 */
  listConversations: () => request<ConversationListSnapshot>('/api/conversations'),
  /** 打开 / 正在看某条对话时标为已读（只改 last_read_at，不影响列表排序）。 */
  markConversationRead: (id: string) =>
    request<void>(`/api/conversations/${id}/read`, { method: 'POST' }),
  createConversation: (body: unknown) =>
    request<Conversation>('/api/conversations', { method: 'POST', body: JSON.stringify(body) }),
  getConversation: (id: string) => request<ConversationDetail>(`/api/conversations/${id}`),
  /** 这条对话可选的模型（自定义网关取白名单；官方默认时返回空清单，界面改为手填）。 */
  getConversationModels: (id: string) =>
    request<ConversationModelOptions>(`/api/conversations/${id}/models`),
  updateConversation: (id: string, body: unknown) =>
    request<Conversation>(`/api/conversations/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteConversation: (id: string) => request<void>(`/api/conversations/${id}`, { method: 'DELETE' }),
  cancelRun: (conversationId: string) =>
    request<{ ok: boolean }>(`/api/conversations/${conversationId}/cancel`, { method: 'POST' }),
  /**
   * 只读轨迹分页：按 `seq` 升序取 `after_seq` 之后的至多 `limit` 条。
   * `next_after_seq` 为空表示没有下一页。**只有 GET**：打开轨迹不发任何写请求。
   */
  fetchTrace: (conversationId: string, afterSeq = 0, limit = 100) =>
    request<TracePage>(
      `/api/conversations/${conversationId}/trace?after_seq=${encodeURIComponent(String(afterSeq))}` +
        `&limit=${encodeURIComponent(String(limit))}`,
    ),
  resetSession: (conversationId: string) =>
    request<{ ok: boolean }>(`/api/conversations/${conversationId}/session/reset`, { method: 'POST' }),
  /** 输入框上方「上下文用量」：已用 / 上限 / 阈值 / 最近一次压缩状态（后端统一估算）。 */
  getConversationContext: (conversationId: string) =>
    request<ContextUsage>(`/api/conversations/${conversationId}/context`),
  /** 上下文窗口配置：上限与触发阈值（按用户持久化）。 */
  getContextSettings: () => request<ContextSettings>('/api/system/context'),
  /** 保存配置：非法输入由后端 422 拦下（前端也会先拦一道）。 */
  setContextSettings: (body: {
    limit_tokens: number
    threshold_ratio: number
    conversation_id?: string | null
  }) =>
    request<ContextSettings>('/api/system/context', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  /** 上传附件：内容走 base64，省掉 multipart 依赖；服务端落到对话工作目录的 attachments/ */
  uploadAttachment: (conversationId: string, body: { name: string; content_base64: string }) =>
    request<AttachmentRef>(`/api/conversations/${conversationId}/attachments`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // --- IM（智能体设定页的「IM」标签：把智能体连到微信 / 飞书 / 钉钉 / 企业微信 / QQ / Telegram / Discord / Slack）
  listImPlatforms: () => request<{ items: ImPlatform[] }>('/api/im/platforms').then((r) => r.items),
  listImConnections: () =>
    request<{ items: ImConnection[] }>('/api/im/connections').then((r) => r.items),
  createImConnection: (body: {
    platform: string
    agent_profile_id?: string | null
    fields: Record<string, unknown>
    options?: Partial<ImConnectionOptions>
    enabled?: boolean
  }) =>
    request<ImConnection>('/api/im/connections', { method: 'POST', body: JSON.stringify(body) }),
  updateImConnection: (id: string, body: {
    agent_profile_id?: string | null
    fields?: Record<string, unknown>
    options?: Partial<ImConnectionOptions>
    enabled?: boolean
  }) =>
    request<ImConnection>(`/api/im/connections/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteImConnection: (id: string) => request<void>(`/api/im/connections/${id}`, { method: 'DELETE' }),
  retryImConnection: (id: string) =>
    request<{ ok: boolean; state: { state: string; error?: string | null } | null }>(
      `/api/im/connections/${id}/retry`,
      { method: 'POST' },
    ),
  listImPeers: (params?: { state?: string; connection_id?: string }) => {
    const search = new URLSearchParams()
    if (params?.state) search.set('state', params.state)
    if (params?.connection_id) search.set('connection_id', params.connection_id)
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<{ items: ImPeer[] }>(`/api/im/peers${suffix}`).then((r) => r.items)
  },
  answerImPeer: (connectionId: string, peerId: string, allow: boolean) =>
    request<ImPeer>(`/api/im/connections/${connectionId}/peers/${peerId}`, {
      method: 'POST',
      body: JSON.stringify({ allow }),
    }),
  removeImPeer: (connectionId: string, peerId: string) =>
    request<void>(`/api/im/connections/${connectionId}/peers/${peerId}`, { method: 'DELETE' }),
  listImChats: (connectionId: string) =>
    request<{ items: ImChat[] }>(`/api/im/connections/${connectionId}/chats`).then((r) => r.items),
  updateImChat: (connectionId: string, chatId: string, agentProfileId: string | null) =>
    request<ImChat>(`/api/im/connections/${connectionId}/chats/${chatId}`, {
      method: 'PATCH',
      body: JSON.stringify({ agent_profile_id: agentProfileId }),
    }),
  removeImChat: (connectionId: string, chatId: string) =>
    request<void>(`/api/im/connections/${connectionId}/chats/${chatId}`, { method: 'DELETE' }),

  // --- 协作域 ---------------------------------------------------------------
  // 协作组（可视化编排图）
  listCollabTeams: () => request<{ items: CollabTeam[] }>('/api/collab/teams').then((r) => r.items),
  getCollabTeam: (id: string) => request<CollabTeam>(`/api/collab/teams/${id}`),
  createCollabTeam: (body: unknown) =>
    request<CollabTeam>('/api/collab/teams', { method: 'POST', body: JSON.stringify(body) }),
  saveCollabTeam: (id: string, body: CollabTeamUpdate) =>
    request<CollabTeam>(`/api/collab/teams/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  /** 只校验不保存：编辑器每改一笔就调一次 */
  validateCollabTeam: (id: string, body: CollabTeamValidateIn) =>
    request<CollabValidation>(`/api/collab/teams/${id}/validate`, { method: 'POST', body: JSON.stringify(body) }),
  duplicateCollabTeam: (id: string) =>
    request<CollabTeam>(`/api/collab/teams/${id}/duplicate`, { method: 'POST' }),
  deleteCollabTeam: (id: string) => request<void>(`/api/collab/teams/${id}`, { method: 'DELETE' }),

  // 协作运行
  listCollabRuns: (teamId?: string) => {
    const suffix = teamId ? `?team_id=${encodeURIComponent(teamId)}` : ''
    return request<{ items: CollabRun[] }>(`/api/collab/runs${suffix}`).then((r) => r.items)
  },
  /** 正在跑的协作智能体（设置页「正在运行的智能体」用：协作的运行不在对话域那份运行表里）。 */
  listActiveCollabRuns: () =>
    request<{ items: ActiveCollabRun[] }>('/api/collab/active-runs').then((r) => r.items),
  createCollabRun: (body: { team_id: string; title?: string; workspace_dir?: string | null }) =>
    request<CollabRun>('/api/collab/runs', { method: 'POST', body: JSON.stringify(body) }),
  getCollabRun: (id: string) => request<CollabRunDetail>(`/api/collab/runs/${id}`),
  /**
   * 单个步骤的完整过程（工具调用/思考/结果）。详情接口只带 `parts_count`，
   * 点开「过程」折叠块时才按需取这一步的 parts——长跑一次能积累几十 MB 的工具流水。
   */
  getCollabStepParts: (runId: string, stepId: string) =>
    request<{ step_id: string; run_id: string; parts: MessagePart[] }>(
      `/api/collab/runs/${runId}/steps/${stepId}/parts`,
    ),
  /** 上传协作附件：落到这次协作的工作目录，发消息时随聊天通道带上（同对话附件） */
  uploadCollabAttachment: (runId: string, body: { name: string; content_base64: string }) =>
    request<AttachmentRef>(`/api/collab/runs/${runId}/attachments`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  listCollabChat: (id: string) =>
    request<{ items: CollabChatMessage[] }>(`/api/collab/runs/${id}/chat`).then((r) => r.items),
  deleteCollabRun: (id: string) => request<void>(`/api/collab/runs/${id}`, { method: 'DELETE' }),
  cancelCollabRun: (id: string) =>
    request<{ ok: boolean; cancelled: boolean }>(`/api/collab/runs/${id}/cancel`, { method: 'POST' }),
  /** 运行中调高预算（步数/工单/token 超限挂起后，调高再点「继续」救回来） */
  patchCollabRunBudget: (id: string, body: CollabBudgetPatch) =>
    request<CollabRun>(`/api/collab/runs/${id}/budget`, { method: 'POST', body: JSON.stringify(body) }),
  /** 把某一步所在的工单放回队列（解析失败/中断后修复，随后续跑） */
  retryCollabStep: (stepId: string) => request<CollabRun>(`/api/collab/steps/${stepId}/retry`, { method: 'POST' }),
}

/**
 * 工作目录文件的读取地址（图片预览、附件点开）：直接塞进 `<img src>`，不走 `request`。
 *
 * 路径按段编码但**保留 `/`**（后端路由是 `{rel_path:path}`）；中文文件名要能正确编码。
 * 这是浏览器唯一能取到工作目录内文件的方式 —— 本地绝对路径在页面里必然是裂图。
 */
function fileUrl(prefix: string, id: string, relPath: string): string {
  const clean = relPath.replace(/\\/g, '/')
  // 绝对路径整体编码：既保留开头的 `/`，又不会拼出 `/files//app/...` 这种
  // 可能被反向代理折叠成相对路径的地址。相对路径仍按段编码、保留分隔符。
  const encoded = clean.startsWith('/')
    ? encodeURIComponent(clean)
    : clean.split('/').map((segment) => encodeURIComponent(segment)).join('/')
  const token = getAccessToken()
  // <img src> 没法带 Authorization 头，只能把会话令牌放进查询参数；后端 current_user
  // 只在没有 Bearer 头时才读它（见 app/auth_deps.py）。
  const suffix = token ? `?access_token=${encodeURIComponent(token)}` : ''
  return `${BASE}/api/${prefix}/${encodeURIComponent(id)}/files/${encoded}${suffix}`
}

/** 对话工作目录里的文件地址（附件预览 / 智能体产出的图片）。 */
export function conversationFileUrl(conversationId: string, relPath: string): string {
  return fileUrl('conversations', conversationId, relPath)
}

/** 协作运行工作目录里的文件地址。 */
export function collabRunFileUrl(runId: string, relPath: string): string {
  return fileUrl('collab/runs', runId, relPath)
}

/**
 * 用 POST 发起 Server-Sent Events 请求。
 * 原生 `EventSource` 无法携带请求体，因此这里手动读取流，
 * 把每个解析出的事件交给 `onEvent`。
 */
/** 消费一条 SSE 事件流（POST 推进 / GET 订阅共用这一份读取逻辑）。 */
async function streamRequest<T>(
  path: string,
  init: RequestInit,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: 'text/event-stream', ...authHeaders(), ...(init.headers ?? {}) },
    signal,
  })
  if (!response.ok || !response.body) {
    if (response.status === 401) notifyUnauthorized()
    throw new ApiError(response.status, describeBody(response, await readBody(response)))
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const data = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim())
        .join('\n')
      if (data) {
        try {
          onEvent(JSON.parse(data) as T)
        } catch {
          /* 心跳或半包数据 */
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}

export function streamPost<T = AgentEvent>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamRequest<T>(
    path,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    onEvent,
    signal,
  )
}

/**
 * 一键更新 / 回滚：POST SSE，不需要请求体。
 * 事件信封是 `update.*`（自有信封，见 `api/update.py`）；流结束时进程会原地重启，
 * 这里拿到全部事件后 resolve —— 之后的「等后端回来」由调用方轮询 health。
 */
export function updateStream(
  path: '/api/update/install' | '/api/update/rollback',
  onEvent: (event: UpdateEvent) => void,
): Promise<void> {
  return streamRequest<UpdateEvent>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' } }, onEvent)
}

/** 订阅一条 GET SSE（用于断开后重连协作运行、或在另一台设备上实时围观）。 */
export function streamGet<T = AgentEvent>(
  path: string,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamRequest<T>(path, { method: 'GET' }, onEvent, signal)
}
