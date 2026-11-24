export type FieldType =
  | 'string' | 'secret' | 'number' | 'boolean' | 'select' | 'textarea' | 'keyvalue' | 'string_list'

export interface ConfigField {
  key: string
  label: string
  type: FieldType
  required: boolean
  default: unknown
  placeholder?: string | null
  help?: string | null
  options?: { value: string; label: string }[] | null
  advanced: boolean
  /** 键值对字段（`keyvalue`）里每个键对应的「值」占位提示（键 → 提示文字）。
   *  密钥类的值不下发到浏览器，只能把掩码当占位符，编辑时才分得清「空着」还是「已填好」。 */
  entryPlaceholders?: Record<string, string | null> | null
}

export interface Capabilities {
  streaming: boolean
  sessions: boolean
  native_system_prompt: boolean
  tools: boolean
  mcp: boolean
  permission_modes: boolean
  working_directory: boolean
  /** 该内核是否支持技能（技能来自各内核自己的技能目录，由平台统一编目） */
  skills: boolean
}

export interface Availability {
  installed: boolean
  version?: string | null
  detail?: string | null
  /** 这份结论的检测时间（框架缓存的，ISO 字符串；实时探测与从未检测时为 null） */
  checked_at?: string | null
}

/** 运行时维护动作：安装 / 更新 / 卸载。 */
export type RuntimeAction = 'install' | 'update' | 'uninstall' | 'login'

/** 适配器声明的维护命令（由后端原样执行，前端只负责展示与触发）。 */
export interface RuntimeCommands {
  install: string[]
  update: string[]
  uninstall: string[]
  latest: string[]
  /** 账号型内核的登录命令；空数组表示不支持登录动作。 */
  login: string[]
}

/** 多账号内核里的一份已保存登录（平台侧的账号槽位）。 */
export interface RuntimeAccount {
  id: string
  /** 展示名（昵称 / 邮箱 + 企业） */
  label: string
  detail?: string | null
  /** 是否是当前正在使用的账号 */
  active: boolean
}

/** 账号型内核的登录状态（二维码登录 / OAuth）。 */
export interface RuntimeAuth {
  /** 该内核是否由后端提供了登录状态探测 */
  supported: boolean
  required: boolean
  logged_in: boolean | null
  detail?: string | null
  account?: string | null
  /** 已保存的账号；null / 缺省表示该内核不支持多账号切换 */
  accounts?: RuntimeAccount[] | null
}

export interface RuntimeProvisioning {
  /** 该运行时是否由平台管理（进程内 API 内核、自定义 CLI 为 false） */
  supported: boolean
  manager?: string | null
  package?: string | null
  homepage?: string | null
  note?: string | null
  commands?: RuntimeCommands | null
  installed: boolean
  version?: string | null
  /** 已安装时的可执行文件路径 */
  path?: string | null
  /**
   * 这份安装状态的检测时间（ISO 字符串）。
   * 可用性由后端缓存并定时检测（每天定点 + 启动补齐 + 安装后重测），
   * 所以卡片上可以直接写「检测于 …」，而不是每次打开页面去跑一遍 `内核 --version`。
   */
  checked_at?: string | null
}

/** 一个智能体运行时（某个 CLI 内核在本机的那份可执行文件）。 */
export interface AgentRuntime {
  id: string
  name: string
  vendor: string
  description: string
  runtime: 'cli' | 'api'
  docs_url?: string | null
  /** 官方 Logo 的静态资源路径（后端按适配器声明给出，前端只负责显示） */
  logo?: string | null
  /** Logo 渲染方式：mask = 单色 SVG 遮罩染色；image = 真彩色位图按原色显示 */
  logo_style?: 'mask' | 'image' | null
  executable?: string | null
  /** 有多少个智能体在用这个运行时 */
  agents: number
  /**
   * 该内核是否支持登录态探测。
   * 与 `provisioning.commands.login` 分开：有些内核只能在交互式终端里登录
   * （平台代跑不了），但登录状态照样值得展示。
   */
  auth_supported?: boolean
  provisioning: RuntimeProvisioning
}

/** 「检查更新」的结果。 */
export interface RuntimeLatest {
  supported: boolean
  latest?: string | null
  command?: string | null
  error?: string | null
}

/** 运行时维护动作的 SSE 事件（自有的 provision.* 信封，非适配器事件协议）。 */
export interface ProvisionEvent {
  type: string
  runtime_id?: string
  action?: RuntimeAction
  command?: string | null
  manager?: string | null
  text?: string
  level?: string
  ok?: boolean
  status?: 'ok' | 'error' | 'cancelled'
  exit_code?: number | null
  installed?: boolean
  version?: string | null
  path?: string | null
  error?: string | null
  /** 登录动作产出的二维码（data:image/...;base64,...） */
  data_url?: string | null
  /** 双向登录（OAuth 授权码）：登录进程请求用户输入时的提示文本 */
  prompt?: string | null
  /** 登录动作结束时的登录状态 */
  auth?: RuntimeAuth | null
}

/** 后台安装 / 更新 / 卸载任务的摘要（刷新页面后恢复状态用）。 */
export interface ProvisionJobInfo {
  runtime_id: string
  name: string
  action: RuntimeAction
  status: 'running' | 'ok' | 'error'
  error?: string | null
  started_at: number
}

export interface SystemDefaults {
  gateway_url?: string | null
  model?: string | null
  api_key_set: boolean
  api_key_preview?: string | null
  options?: Record<string, unknown>
}

export interface AgentType {
  id: string
  name: string
  description: string
  vendor: string
  runtime: 'cli' | 'api'
  capabilities: Capabilities
  config_fields: ConfigField[]
  api_key_env?: string | null
  base_url_env?: string | null
  default_gateway?: string | null
  default_model?: string | null
  /** 该内核访问模型时使用的协议（openai / anthropic / gemini），用于匹配自定义网关 */
  protocol?: string | null
  /** 该内核默认的鉴权方式（bearer / x-api-key / query / none） */
  default_auth_style?: string | null
  /** 该内核官方链路的预置模型（网关「必须成功才能保存」时的兜底清单） */
  default_models?: string[]
  docs_url?: string | null
  executable?: string | null
  /** 是否出现在「新建智能体」的内核下拉里；false 的模板只保留给存量智能体 */
  selectable?: boolean
  /** 运行时的安装 / 更新方式（`/api/agent-types` 也带上，与「运行时」页同源） */
  provisioning?: RuntimeProvisioning
  availability: Availability
  system_defaults?: SystemDefaults
}

/** 一个网关：自定义网关或内核的官方链路，是 API 密钥的唯一来源。 */
export interface Gateway {
  id: string
  name: string
  /** official = 内核官方链路（播种生成、不可删）；custom = 用户自建 */
  kind: 'official' | 'custom'
  /** 官方链路对应的内核 id；自定义网关为 null */
  type_id?: string | null
  /** openai / anthropic / gemini */
  protocol: string
  /** bearer / x-api-key / query / none */
  auth_style: string
  base_url: string
  /** 模型白名单（智能体只能从这里面选模型） */
  selected_models: string[]
  /** 最近一次「获取模型」探测到的完整清单 */
  model_catalog: string[]
  extra_headers: Record<string, unknown>
  enabled: boolean
  created_at: string
  updated_at: string
  last_probed_at?: string | null
  type_name?: string | null
  api_key_set: boolean
  api_key_preview?: string | null
  /** 有多少个未删除的智能体在用这个网关 */
  used_by_profiles: number
}

/** 「获取模型」的探测结果（不落库）。 */
export interface GatewayProbeResult {
  ok: boolean
  message: string
  models: string[]
}

/** 下拉选项（协议 / 鉴权方式的候选值）。 */
export interface GatewayOption {
  value: string
  label: string
}

/** `GET /api/gateways` 的返回：网关列表 + 协议 / 鉴权的候选值。 */
export interface GatewayList {
  items: Gateway[]
  protocols: GatewayOption[]
  auth_styles: GatewayOption[]
}

export interface AgentSkillBrief {
  id: string
  name: string
  type_id: string
  source_kind: string
  available: boolean
}

/** 智能体挂着的 MCP 服务器摘要（密钥永不回传）。 */
export interface AgentMcpBrief {
  id: string
  name: string
  slug: string
  transport: string
  /** 来源内核 id；为空表示平台通用 */
  type_id?: string | null
  available: boolean
  enabled: boolean
}

/** 智能体：人格 + 内核 + 网关 + 模型（原「角色」与「智能体实例」合并而来）。 */
/** 凭据库里的凭据：全局一份，智能体按引用使用。值永不回传，只有掩码。 */
export interface Credential {
  id: string
  /** 同时就是要注入的环境变量名 */
  name: string
  description?: string | null
  has_value: boolean
  masked?: string | null
  /** 有多少个智能体在引用它 */
  used_by_agents: number
  created_at: string
  updated_at: string
}

/** 智能体引用到的凭据摘要（卡片 / 弹窗上展示）。 */
export interface AgentCredentialBrief {
  id: string
  name: string
  description?: string | null
  has_value: boolean
  masked?: string | null
}

/**
 * 提交给后端的一条凭据引用。
 * `id` 有值＝挑凭据库里已有的那条；没有 `id` 时按 `name` 找同名凭据，
 * 再找不到就用 `value` 新建一条（于是「直接挑」和「顺手新建」都是一次保存搞定）。
 */
export interface AgentCredentialInput {
  id?: string | null
  name: string
  description?: string | null
  value?: string | null
}

export interface AgentProfile {
  id: string
  name: string
  avatar: string
  /** 内置头像包编号（pack-001 ~，清单见 lib/avatarPack.ts）；有值时渲染 /avatars/<它>.webp，优先级在 avatar_file 之后 */
  avatar_preset?: string | null
  /** 自定义头像文件名；有值时取 /api/agents/{id}/avatar 显示，优先于 emoji */
  avatar_file?: string | null
  tagline?: string | null
  description?: string | null
  system_prompt: string
  type_id: string
  gateway_id?: string | null
  /** 新对话默认使用的模型（具体对话可以在对话里改） */
  model?: string | null
  options: Record<string, unknown>
  enabled: boolean
  /** 默认智能体：新建对话不再先问选谁，直接用这颗星标着的那个（全局唯一） */
  is_default: boolean
  /** 分组名：智能体页按它折叠分组展示（空串 = 未分组，排在最后） */
  group_name: string
  created_at: string
  updated_at: string
  type_name?: string | null
  runtime?: 'cli' | 'api' | null
  availability?: Availability | null
  gateway_name?: string | null
  gateway_base_url?: string | null
  gateway_kind?: string | null
  gateway_api_key_set: boolean
  /** 智能体引用到的凭据（本地加密保存，运行时按环境变量注入；值绝不回传） */
  credentials: AgentCredentialBrief[]
  /** 该智能体分配到的技能（技能只能通过分配获得） */
  skill_ids: string[]
  skills: AgentSkillBrief[]
  /** 该智能体挂着的 MCP 服务器（同样只能通过分配获得） */
  mcp_ids: string[]
  mcp_servers: AgentMcpBrief[]
}

/** 「设置 → 对话标题」：自动生成对话标题用哪个智能体、哪个模型（留空即跟随默认智能体）。 */
export interface TitleSettings {
  /** 开着才会自动起标题（默认开） */
  enabled: boolean
  agent_profile_id?: string | null
  model?: string | null
  /** 派生字段：解析后的「默认智能体」与最终生效的智能体（界面只用来显示名字） */
  default_agent_id?: string | null
  default_agent_name?: string | null
  effective_agent_id?: string | null
  effective_agent_name?: string | null
  /** 上一次自动起标题失败的原因与时间（成功一次就清掉）；用于界面上自查 */
  last_error?: string | null
  last_error_at?: string | null
}

export interface Skill {
  id: string
  type_id: string
  type_name?: string | null
  slug: string
  name: string
  description: string
  source_kind: string
  source_label: string
  version?: string | null
  author?: string | null
  when_to_use?: string | null
  available: boolean
  total_bytes: number
  file_count: number
  note?: string | null
  used_by_agents: number
  /** 由「技能市场」装入的技能为 true（可在市场页卸载），其余只读 */
  is_market: boolean
  /** 能否从技能页删除：自己的技能库、且不是内核自带 / 插件只读技能 */
  deletable: boolean
  /** 市场技能对应的市场页地址 */
  source_url?: string | null
  /** 归属用户 id；为空表示内核自带 / 宿主机共享技能 */
  owner_id?: string | null
  /** 归属用户展示名（管理员查看全量目录时用） */
  owner_name?: string | null
  /** 这条技能是否在当前用户自己的技能库里 */
  is_own: boolean
  /** 这份技能实体被哪些内核引用（用户技能库按 slug 只存一份；共享技能为空） */
  mounted_kernels?: string[]
}

export interface SkillDetail extends Skill {
  path: string
  fingerprint: string
  allowed_tools: string[]
  files: { path: string; size: number }[]
  meta: Record<string, unknown>
  body: string
  body_truncated: boolean
  /** 引用了这个技能的智能体 */
  agents: { id: string; name: string; avatar: string; avatar_preset?: string | null }[]
}

export interface SkillRescanResult {
  added: number
  updated: number
  /** 这一轮没扫到（目录还在，或技能根暂时不可用），被标记为失效的条目数（保留分配） */
  missing: number
  /** 目录已从磁盘消失、被重扫自动清掉的条目数 */
  removed: number
  /** 因技能消失而一并解除的智能体分配数 */
  unassigned: number
  total: number
}

/** 导入 / 挂载技能后的结果：新技能的标识与落点内核。 */
export interface SkillTransferResult {
  skill_id: string
  name: string
  slug: string
  type_id: string
}

/** 内置 MCP 的一个业务参数（平台地址、令牌、网关地址、模型名…）；值只回掩码。 */
export interface McpBuiltinParam {
  key: string
  label: string
  help: string
  placeholder: string
  required: boolean
  secret: boolean
  default: string
  masked: string | null
  /** 由平台自动填写 / 刷新（系统管理 MCP 的平台地址与令牌），不需要用户输入 */
  managed: boolean
}

/**
 * 统一编目的一个 MCP 服务器。
 *
 * 两类来源：平台新建的「通用」服务器（`type_id` 为空，可分配给任意内核的智能体），
 * 以及从某个内核自己的配置里只读扫出来的（`type_id` 是那个内核，只能在同内核里分配）。
 * 平台内置的服务器也是平台通用的一类（`builtin_key` 非空），启动时自动播种。
 */
export interface McpServer {
  id: string
  name: string
  slug: string
  description: string
  type_id?: string | null
  type_name?: string | null
  source_kind: string
  source_label: string
  /** stdio / http / sse */
  transport: string
  command?: string | null
  args: string[]
  url?: string | null
  /** 只给键名与掩码，永远没有明文 */
  env_keys: string[]
  env_preview: Record<string, string | null>
  header_keys: string[]
  header_preview: Record<string, string | null>
  enabled: boolean
  available: boolean
  note?: string | null
  /** 平台是否接管它的内容（平台新建的为 true，从内核配置扫来的为 false） */
  editable: boolean
  used_by_agents: number
  /** 内置服务器的稳定标识（例如 image-gen / coda-admin）；普通服务器为空 */
  builtin_key?: string | null
  /** 内置服务器要用户填写的参数说明；普通服务器为空数组 */
  builtin_params?: McpBuiltinParam[]
}

export interface McpServerDetail extends McpServer {
  /** 挂着这个服务器的智能体 */
  agents: { id: string; name: string; avatar: string; avatar_preset?: string | null }[]
}

export interface McpRescanResult {
  added: number
  updated: number
  missing: number
  total: number
}

/** MCP 服务器的「新建 / 编辑」载荷（与后端 McpServerCreate/Update 字段一致）。 */
export interface McpServerPayload {
  name: string
  slug?: string | null
  description?: string | null
  /** stdio / http / sse */
  transport: string
  command?: string | null
  url?: string | null
  args?: string[]
  /** 值加密保存；编辑时留空＝保持原值，删掉整行＝删除 */
  env?: Record<string, string>
  headers?: Record<string, string>
  enabled?: boolean
}

/** 技能市场（SkillHub）里的一个技能。 */
export interface MarketSkill {
  slug: string
  name: string
  description: string
  version?: string | null
  author?: string | null
  category?: string | null
  downloads: number
  stars: number
  score: number
  owner?: string | null
  source?: string | null
  verified: boolean
  requires_api_key: boolean
  security?: string | null
  icon_url?: string | null
  updated_at?: number | null
  url: string
}

export interface MarketSkillList {
  items: MarketSkill[]
  page: number
  page_size: number
  total: number
  has_more: boolean
}

/** 可安装技能的内核（声明了可写技能根的适配器）。 */
export interface MarketKernel {
  type_id: string
  name: string
  path?: string | null
}

/** 安装市场技能的请求体。 */
export interface MarketInstallInput {
  slug: string
  type_id: string
  version?: string | null
}

/** 安装市场技能的结果。 */
export interface MarketInstallResult {
  skill_id: string
  name: string
  slug: string
  type_id: string
  version?: string | null
  requires_api_key: boolean
  warnings: string[]
}

/** 正在跑的那一次运行（`GET /conversations/{id}` 的 `active_run`）。 */
export interface ActiveRunInfo {
  id: string
  conversation_id: string
  agent_name: string
  /** 本轮作答的智能体 id：@ 召唤轮是被召唤者，普通轮是对话归属者 */
  agent_profile_id?: string | null
  /** 本轮作答智能体的 emoji 头像（与 agent_profile_id 对应） */
  agent_avatar?: string | null
  /** 本轮作答智能体的内置头像包编号（与 agent_avatar 对应） */
  agent_avatar_preset?: string | null
  status: string
  elapsed_seconds: number
  /** 本轮助手消息 id：界面据此隐藏数据库里那条空占位，改用实时卡片渲染 */
  assistant_message_id?: string | null
  user_message_id?: string | null
}

/** 任务进度卡片里的一个任务项（后端只读返回）。 */
export interface TaskCardItem {
  id: string
  text: string
  /** 勾选状态只能由服务端按模型声明变更，界面永远只读 */
  done: boolean
  done_at?: string | null
  updated_at?: string | null
}

/**
 * 一条对话的任务进度卡片（`GET /conversations/{id}` 的 `task_card`）。
 * 一个对话最多一张；没有卡时为 null。
 */
export interface TaskCard {
  id: string
  conversation_id: string
  /** 建卡那一轮的助手消息 id：卡片渲染在这条消息之后 */
  anchor_message_id?: string | null
  title: string
  items: TaskCardItem[]
  /** 乐观锁版本号：前端据此判断要不要重渲染 */
  version: number
  created_at: string
  updated_at: string
}

/** 单条对话的完整读取结果：对话本身 + 全部消息 + 正在跑的那次运行（若有）。 */
export interface ConversationDetail {
  conversation: Conversation
  messages: Message[]
  active_run: ActiveRunInfo | null
  /** 任务进度卡片；null 表示这条对话没有卡片 */
  task_card?: TaskCard | null
}

/**
 * 上下文自动压缩的最近一次状态（进程内记录；重启后回到「尚未压缩」）。
 * 展示在输入框上方的上下文按钮 / 详情面板里。
 */
export interface ContextCompactionState {
  /** compacting = 进行中；ok = 成功；error = 失败（原上下文保留） */
  status: 'compacting' | 'ok' | 'error'
  at: string
  before_tokens?: number
  after_tokens?: number
  dropped?: number
  error?: string | null
}

/**
 * 一条对话的上下文窗口用量（`GET /api/conversations/{id}/context`）。
 *
 * 数字全部由后端 `runtime.context` 估算：展示、阈值判断、压缩决策共用同一口径，
 * 前端不另算一套。`percent` 是真实百分比（可能超过 100，说明已超限；
 * 进度条宽度由界面自己截断）。
 */
export interface ContextUsage {
  used_tokens: number
  limit_tokens: number
  threshold_tokens: number
  /** 触发阈值占上限的比例（0~1） */
  threshold_ratio: number
  percent: number
  /** 已折叠进摘要的消息条数（原文仍保留在消息表里） */
  compacted_count: number
  compacted_at: string | null
  /** 压缩策略简述：给详情面板直出 */
  strategy: string
  /** 估算口径说明 */
  estimation: string
  compaction: ContextCompactionState | null
}

/** 上下文窗口配置（`GET/PUT /api/system/context`，按用户持久化）。 */
export interface ContextSettings {
  limit_tokens: number
  threshold_ratio: number
  threshold_tokens: number
  defaults: { limit_tokens: number; threshold_ratio: number }
  /** 上限的合理下限；低于它不落盘 */
  min_limit_tokens: number
}

/** 这条对话可选的模型（`GET /conversations/{id}/models`）。 */
export interface ConversationModelOptions {
  /** 当前对话单独指定的模型；为空表示跟随智能体默认 */
  current?: string | null
  /** 智能体的默认模型 / 内核默认 */
  default?: string | null
  /** 自定义网关名；为空表示官方默认链路 */
  gateway?: string | null
  /** 网关白名单里的模型；官方默认时为空，前端允许直接填写模型名 */
  models: string[]
}

/* ---------- 轨迹调试视图（`GET /api/conversations/{id}/trace`，只读）---------- */

/** 轨迹事件的五类（后端权威定义见 `docs/trace-debug-view-plan.md` §4.1）。 */
export type TraceEventType =
  | 'request'
  | 'response'
  | 'tool_call'
  | 'tool_result'
  | 'context_snapshot'

/**
 * 一条轨迹事件（只读响应，字段与方案 §4.1 / `TraceEventRead` 对齐）。
 *
 * `session_id` 是**平台对话 id**（= `Conversation.id`），不是内核会话 id；
 * 内核会话 id 在 `payload.kernel_session_id`。`payload` 原样返回（入库前已统一脱敏）。
 */
export interface TraceEvent {
  id: string
  session_id: string
  owner_id?: string | null
  run_id?: string | null
  /** 该对话第几轮用户消息（0 起） */
  turn_index: number
  /** 会话内严格递增：时间线排序与分页的稳定键 */
  seq: number
  /** 采集时刻（UTC ISO8601，带 +00:00） */
  timestamp: string
  type: TraceEventType
  payload: Record<string, unknown>
  duration_ms?: number | null
  error?: string | null
  /** payload 是否因体积上限被截断（截断前大小看 `bytes`） */
  truncated: boolean
  bytes: number
}

/** 轨迹分页响应：`next_after_seq` 为空表示没有下一页。 */
export interface TracePage {
  items: TraceEvent[]
  next_after_seq: number | null
}

/** 一条对话里出现过的智能体（主场 + 被 @ 召唤过的），列表行画头像用。 */
export interface ConversationParticipant {
  id: string
  name?: string | null
  avatar?: string | null
  avatar_preset?: string | null
  avatar_file?: string | null
}

export interface Conversation {
  id: string
  title: string
  agent_profile_id: string
  session_id?: string | null
  workspace_dir?: string | null
  /** 这条对话单独指定的模型；为空表示跟随智能体默认 */
  model?: string | null
  archived: boolean
  created_at: string
  updated_at: string
  agent_name?: string | null
  agent_avatar?: string | null
  /** 智能体的内置头像包编号（快照）；有值时渲染 /avatars/<它>.webp */
  agent_avatar_preset?: string | null
  /** 智能体的自定义头像文件名（快照）；有值时取 /api/agents/{agent_profile_id}/avatar */
  agent_avatar_file?: string | null
  agent_type_id?: string | null
  /** 智能体配置的默认模型（对话未单独指定时，输入框上显示它 + 「默认」标签） */
  agent_model?: string | null
  message_count: number
  /** 最后一条是已完成的 AI 回复、且晚于用户最后一次打开它的时间（列表接口算出来） */
  unread?: boolean
  /**
   * 在这条对话里出现过的智能体，第一个是对话归属者。
   * 只有一个 = 单聊；两个及以上 = 用过 @ 召唤，列表行画成群聊式堆叠头像。
   */
  participants?: ConversationParticipant[]
}

/** `GET /conversations` 的返回：对话列表 + 当前正在跑的那些运行。 */
export interface ConversationListSnapshot {
  items: Conversation[]
  /** 有运行在进行中的对话：列表据此显示「正在回复」的动画 */
  active_runs: ActiveRunInfo[]
}

/**
 * 上传到对话工作目录的附件（`POST /api/conversations/{id}/attachments` 的返回）。
 * 发送消息时把 `rel_path` 放进 `attachments`，框架会连同路径一起写进提示词。
 */
export interface AttachmentRef {
  /** 落盘后的文件名（已去掉目录成分） */
  name: string
  /** 相对工作目录的路径，发送消息时用这个值 */
  rel_path: string
  /** 绝对路径（只在界面上展示，供用户复制） */
  path: string
  size: number
}

export interface MessagePart {
  /** text 为正文片段；与 thinking / 工具事件一起按真实发生顺序排列。 */
  kind: 'thinking' | 'text' | 'tool_call' | 'tool_result'
  text?: string
  name?: string
  id?: string | null
  input?: Record<string, unknown>
  output?: string
  is_error?: boolean
}

export interface Message {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  parts: MessagePart[]
  usage: Record<string, number>
  status: string
  error?: string | null
  created_at: string
  /** 这条回复由哪个智能体产出；为空 = 对话归属的智能体，非空 = @ 召唤的 */
  agent_profile_id?: string | null
}

export type AgentEventType =
  | 'turn.started' | 'run.started' | 'message.delta' | 'thinking.delta' | 'tool.call'
  | 'tool.result' | 'usage' | 'session' | 'log' | 'error' | 'run.finished' | 'stream.closed'

export interface AgentEvent {
  type: AgentEventType
  text?: string
  name?: string
  call_id?: string
  payload?: Record<string, unknown>
  run_id?: string
  conversation_id?: string
  assistant_message_id?: string
  agent?: string
}

export interface Overview {
  version: string
  version_history: VersionRecord[]
  app_name: string
  data_dir: string
  workspaces_root: string
  agent_types: number
  installed_types: number
  active_runs: { id: string; conversation_id: string; agent_name: string; status: string; elapsed_seconds: number }[]
}

export interface VersionRecord {
  version: string
  date: string
  title: string
  highlights: string[]
}

/* ---------- 用量页（后端 app/api/usage.py）---------- */

/** 一组归一化后的 token 计数（不同适配器的字段名在后端已统一）。 */
export interface UsageTokens {
  input: number
  output: number
  total: number
  cache_read: number
  cache_creation: number
  /** 缓存命中率（0~1）；没有可用的分母时为 null */
  cache_ratio: number | null
  /** 参与统计的消息条数 */
  messages: number
}

export interface UsageAgentRow {
  id: string
  name: string
  avatar: string
  avatar_preset?: string | null
  type_id?: string | null
  type_name?: string | null
  tokens: UsageTokens
}

export interface UsageConversationRow {
  id: string
  title: string
  agent_name?: string | null
  agent_avatar?: string | null
  updated_at?: string | null
  tokens: UsageTokens
}

export interface UsageDayRow {
  date: string
  input: number
  output: number
  total: number
}

export interface UsageSkillRow {
  id: string
  name: string
  type_id: string
  type_name?: string | null
  source_label: string
  /** 所选时间范围内这个技能被调用的次数 */
  calls: number
}

export interface UsageSkills {
  /** 所选范围内的技能调用总次数 */
  calls: number
  /** 所选范围内被调用过的技能数 */
  skills_used: number
  /** 被调用最多的技能（只含所选范围内调用过至少一次的） */
  top: UsageSkillRow[]
}

export interface UsageOverview {
  tokens: UsageTokens
  /** 产生过 token 消耗的对话数 */
  conversations: number
  by_agent: UsageAgentRow[]
  by_conversation: UsageConversationRow[]
  daily: UsageDayRow[]
  skills: UsageSkills
}

/** 「设置 → 关于 → 检测更新」的状态（GET /api/update/check 与 /api/update/status 同形）。 */
export interface UpdateStatus {
  /** 部署形态：managed（托管，可一键更新）/ source（源码模式，只能提示走 git） */
  mode: 'managed' | 'source'
  current_version: string
  /** GitHub 最新 Release（检测失败 / 没有 Release 时为 null） */
  latest: {
    tag: string
    version: string
    title: string
    notes: string
    published_at: string
    asset_size: number
  } | null
  update_available: boolean
  /** 托管模式才允许安装；源码模式界面只提示「用 git 更新」 */
  can_install: boolean
  /** releases/ 里已装好的版本（降序） */
  installed_versions: string[]
  rollback_available: boolean
  /** 上一次「更新 / 回滚 → 重启」的记录（重启完成后读一次，用于展示结果横幅） */
  pending_restart: { action: string; version: string; from_version: string; at: number } | null
}

/** 一键更新 / 回滚的 SSE 事件（`update.*` 信封，见 `api/update.py`）。 */
export type UpdateEvent =
  | { type: 'update.started'; current: string }
  | { type: 'update.downloading'; version: string; size: number; published_at: string; notes: string }
  | { type: 'update.restarting'; action: 'update' | 'rollback'; version: string; restarting: boolean }
  | { type: 'update.error'; message: string }

/**
 * 桌面端更新状态机的阶段（与 `desktop/src/main/updater.ts` 保持同形）。
 * 桌面端只有「下载完整安装包」这一种更新形态，没有后端自更新的轻量通道。
 */
export type DesktopUpdatePhase =
  | 'idle'
  | 'checking'
  | 'downloading'
  | 'downloaded'
  | 'up-to-date'
  | 'error'
  | 'unsupported'

/** 桌面壳更新状态（preload 的 `desktop-update:status` 返回形状）。 */
export interface DesktopUpdateState {
  /** 当前构建是否具备自动更新能力（开发模式 / macOS 未签名 / 不支持的平台为 false）。 */
  supported: boolean
  /** 壳自身版本（desktop/package.json 的 version，形如 0.1.3）。 */
  current_version: string
  phase: DesktopUpdatePhase
  /** 远端可用的新版本；没发现 / 已是最新时为 null。 */
  available_version: string | null
  /** 下载进度（0-100）。 */
  percent: number
  /** 失败原因（phase 为 error / unsupported 时给出）。 */
  error: string | null
}

/** preload 暴露的壳更新 API（`window.codaDesktop.updates`）。 */
export interface DesktopUpdateBridge {
  status: () => Promise<DesktopUpdateState>
  check: () => Promise<DesktopUpdateState>
  install: () => Promise<void>
  onState: (callback: (state: DesktopUpdateState) => void) => () => void
}

export interface TestResult {
  ok: boolean
  message: string
  checks: { name: string; ok: boolean; detail: string }[]
}

/* ============================================================
   协作域（多智能体工单协作）—— 与后端 app/collab/schemas.py 一一对应
   ============================================================ */

/** 节点在协作里的岗位 */
export type CollabDuty = 'coordinator' | 'engineer' | 'reviewer'
/** 有向边的类型：派发 / 审查 / 打回 */
export type CollabEdgeKind = 'assign' | 'review' | 'rework'
/** 节点端口：输入在左/上，输出在右/下。 */
export type CollabEdgePort = 'left' | 'right' | 'top' | 'bottom'
export type CollabWorkspaceScope = 'none' | 'read' | 'write'
/** 一个节点在一轮里能发起的动作 */
export type CollabTicketAction =
  | 'delegate' | 'submit' | 'review' | 'done' | 'ask_user' | 'escalate' | 'reject'

/** `ask_user` 的一条可点选项；`freeform` 表示点它只是把焦点交给输入框。 */
export interface CollabChoice {
  label: string
  value: string
  description?: string | null
  freeform?: boolean
}

export interface CollabNode {
  key: string
  agent_profile_id: string
  label: string
  duty: CollabDuty
  instructions: string
  capability_scope: string
  /** null 表示按 duty 取默认值 */
  workspace_scope: CollabWorkspaceScope | null
  /** 空数组表示按 duty 取默认动作集合 */
  output_contract: CollabTicketAction[]
  max_rounds: number
  /** 协作专属技能分配：null = 跟随智能体页面的分配；给了列表以它为准（不写回智能体） */
  skill_ids?: string[] | null
  /** 协作专属 MCP 分配：语义同 skill_ids */
  mcp_ids?: string[] | null
  x: number
  y: number
}

export interface CollabEdge {
  id: string
  from_key: string
  to_key: string
  kind: CollabEdgeKind
  label: string
  /** 这条边额外的返工轮次上限（0 = 不额外限制）：实际上限取「边 / 节点 / 运行时预算」三者最小 */
  max_rounds: number
  /** 画布端口方向；旧数据为空时由画布按节点相对位置自动推断。 */
  from_side?: CollabEdgePort | null
  to_side?: CollabEdgePort | null
}

export interface CollabGraph {
  nodes: CollabNode[]
  edges: CollabEdge[]
}

export interface CollabBudget {
  max_steps: number
  max_tickets: number
  max_rounds_per_ticket: number
  max_tokens: number
  timeout_seconds: number
  /** 单步限时（秒，0 = 不限）：一个工单步骤 / 聊天回复的执行上限 */
  step_timeout_seconds?: number
  /** 同一波最多并行推进几个工单步骤；1 = 完全串行（默认），上限 8 */
  max_parallel?: number
}

/**
 * `POST /api/collab/runs/{id}/budget` 的请求体：预算超限挂起后只允许调高，
 * 新值必须大于当前已用量（`max_tokens=0` 表示不限）。
 */
export interface CollabBudgetPatch {
  max_steps?: number
  max_tickets?: number
  max_tokens?: number
  timeout_seconds?: number
  step_timeout_seconds?: number
  max_parallel?: number
}

export interface CollabIssue {
  code: string
  message: string
  node_key?: string | null
  edge_id?: string | null
  /** 前端归一化后写入的严重级别；服务端已分别放在 errors / warnings 两个数组里 */
  severity?: 'error' | 'warning'
}

export interface CollabValidation {
  ok: boolean
  errors: CollabIssue[]
  warnings: CollabIssue[]
}

export interface CollabTeam {
  id: string
  name: string
  description?: string | null
  /** 唯一能与用户对话的节点 key */
  entry_key?: string | null
  workspace_dir?: string | null
  graph: CollabGraph
  budget: CollabBudget
  node_count: number
  created_at: string
  updated_at: string
  validation: CollabValidation
}

/** `PUT /api/collab/teams/{id}` 的请求体（对应后端 `TeamUpdate`：整图替换）。 */
export interface CollabTeamUpdate {
  name?: string
  description?: string | null
  entry_key?: string | null
  workspace_dir?: string | null
  graph?: CollabGraph
  budget?: CollabBudget
}

/** `POST /api/collab/teams/{id}/validate` 的请求体（对应后端 `TeamValidateIn`）。 */
export interface CollabTeamValidateIn {
  graph?: CollabGraph
  entry_key?: string | null
}

export type CollabRunStatus = 'running' | 'waiting_user' | 'done' | 'error' | 'cancelled' | 'interrupted'

export type CollabTicketState =
  | 'open' | 'working' | 'waiting_children' | 'review' | 'rework'
  | 'escalated' | 'done' | 'rejected' | 'blocked'

/** 一次上报：`from` 谁上报的、`reason` 卡在哪、`need` 需要上层做什么、`hops` 已经过谁 */
export interface CollabEscalation {
  from?: string
  reason?: string
  need?: string
  hops?: string[]
}

export interface CollabTicket {
  id: string
  run_id: string
  /** 非空表示这是拆解出来的子工单 */
  parent_ticket_id?: string | null
  from_agent_profile_id?: string | null
  to_agent_profile_id: string
  /** 谁拆的谁汇总（子工单完成后回到这里） */
  owner_key: string
  /** 现在谁握着 */
  holder_key: string
  /** 最近一次提交待审的节点（打回时默认退回给它） */
  submitter_key: string
  title: string
  brief: { goal?: string; requirements?: string[]; acceptance?: string[]; constraints?: string[]; deliverables?: string[] }
  state: CollabTicketState
  round: number
  max_rounds: number
  verdict?: string | null
  review_comment?: string | null
  result: Record<string, unknown>
  /** 最近一次上报记录；非空表示这张工单曾被上报过 */
  escalation?: CollabEscalation
  created_at: string
  updated_at: string
  closed_at?: string | null
}

export interface CollabStep {
  id: string
  run_id: string
  ticket_id?: string | null
  /** 用户自己的发言没有智能体 */
  agent_profile_id?: string | null
  profile_name?: string | null
  /** 节点 key；用户自己的发言是 "user" */
  role_key: string
  duty: string
  sequence: number
  action: string
  /** delegate 把活交给了哪个节点 key；空串表示不是派发（用于还原交接链） */
  target_key?: string
  status: string
  content: string
  /** 引擎派生的结构化摘要（下游看到的是它，而不是 content） */
  gist: string
  /**
   * 详情接口不再携带完整过程（长跑一次能积累几十 MB 的工具流水）：
   * `parts` 恒为空数组，`parts_count` 说明这一步有多少过程片段；
   * 点开「过程」折叠块时用 `api.getCollabStepParts` 按需加载。
   */
  parts: MessagePart[]
  parts_count?: number
  /** 这一步调用过的工具名（后端从 parts 提取、去重限量；看板/进度展示用） */
  tool_names?: string[]
  usage: Record<string, number>
  error?: string | null
  audit: Record<string, unknown>
  started_at: string
  finished_at?: string | null
}

/** 入口智能体聊天的一条消息（对应后端 `collab_messages`）。 */
export interface CollabChatMessage {
  id: string
  run_id: string
  /** user = 你；leader = 入口智能体 */
  sender: 'user' | 'leader'
  role_key: string
  agent_profile_id?: string | null
  sequence: number
  status: string
  content: string
  parts: MessagePart[]
  usage: Record<string, number>
  error?: string | null
  /** 需求细化与同步结果 */
  meta: Record<string, unknown>
  created_at: string
  finished_at?: string | null
}

export interface CollabRun {
  id: string
  team_id: string
  team_name?: string | null
  title: string
  goal: string
  status: CollabRunStatus
  entry_key?: string | null
  workspace_dir?: string | null
  /** 本次运行的预算（界面显示「已用 / 上限」） */
  budget: CollabBudget
  steps_used: number
  tickets_used: number
  usage: Record<string, number>
  error?: string | null
  created_at: string
  updated_at: string
  ticket_count: number
  open_tickets: number
}

/**
 * 正在跑的协作智能体（`GET /api/collab/active-runs`）。
 *
 * 协作的运行登记在**协作域自己**那份运行表里，所以对话域的 `/api/system/overview` 看不到它们 ——
 * 设置页的「正在运行的智能体」要两处都取。
 */
export interface ActiveCollabRun {
  /** 协作运行 id（点进去看这条运行） */
  run_id: string
  /** 这条运行的名字（后端补上的，界面好认） */
  title?: string | null
  agent_name: string
  /** step＝工单步骤，chat＝入口智能体与用户的对话回复 */
  kind: 'step' | 'chat'
  node_key?: string | null
  elapsed_seconds: number
}

export interface CollabRunDetail extends CollabRun {
  /**
   * 这次运行实际使用的那张编排图（第一次推进时冻结；还没开跑 / 升级前的老运行是协作组现图）。
   * 进度图与岗位名都按它渲染：之后改协作组，老运行也不会张冠李戴。
   */
  graph: CollabGraph
  tickets: CollabTicket[]
  steps: CollabStep[]
  chat_messages: CollabChatMessage[]
  /** 刷新时是否有一条 CEO 回复仍在生成 */
  chat_active: boolean
}

/* ---------- 协作信封事件（app/collab/protocol.py）---------- */

export type CollabEventType =
  | 'collab.run.started' | 'collab.step.started' | 'collab.step.event' | 'collab.step.finished'
  | 'collab.ticket.created' | 'collab.ticket.updated' | 'collab.run.status' | 'collab.notice'
  | 'collab.chat.started' | 'collab.chat.event' | 'collab.chat.finished'

/** 传输层在 SSE 收尾时补发的事件（不属于协作信封）：用来区分「正常结束」与「连接被切断」。 */
export interface StreamClosedEvent {
  type: 'stream.closed'
}

export interface CollabEvent {
  type: CollabEventType
  run_id?: string
  team_id?: string
  title?: string
  entry_key?: string | null
  step_id?: string
  ticket_id?: string | null
  /** 岗位 key（对应图上的节点） */
  node_key?: string
  role_key?: string
  duty?: string
  agent_name?: string
  /** collab.step.event 里内嵌的原始智能体事件 */
  event?: AgentEvent
  status?: string
  reason?: string | null
  action?: string
  usage?: Record<string, number>
  error?: string | null
  ticket?: CollabTicket
  text?: string
  /** notice 级别：info / warn / error / ask */
  level?: string
  /** `collab.notice` 且 level=ask 时一并带上，便于即时渲染决策卡 */
  question?: string
  options?: CollabChoice[]
  /** 聊天事件：这一条回复的消息 ID */
  message_id?: string
  /** 聊天结束时的解析/同步告警 */
  warning?: string | null
  /** 聊天里识别到并同步进工单的需求更新 */
  applied?: {
    ticket_ids?: string[]
    ticket_count?: number
    fields?: string[]
    update?: Record<string, unknown>
  }
  /** CEO 判断这条消息需要推进任务时携带的决策卡数据（chat_finished） */
  advance?: {
    reason?: string
    order?: string
    options?: CollabChoice[]
  } | null
}


/* ---------- 多用户系统（后端 app/api/auth.py）---------- */

/** 当前登录用户（不含口令哈希；接口从不回密钥）。 */
export interface AuthUser {
  id: string
  username: string
  display_name?: string | null
  email?: string | null
  role: 'admin' | 'member'
  is_active: boolean
  created_at: string
  /**
   * 头像标识（文件名摘要）：有值时由 `lib/avatar.ts` 带令牌 fetch 成 blob 渲染；
   * 为空回退到首字母圆。它同时是本地缓存键 —— 换头像就换值。
   */
  avatar?: string | null
  /**
   * 轨迹调试开关（用户级，权威真值在 `users.trace_debug`，默认关闭）。
   * 前端只读它决定调试按钮显隐，改开关必须走 `api.updateMe`，不能只存本地。
   */
  trace_debug?: boolean
}

/** 登录 / 注册成功的响应。 */
export interface AuthSession {
  access_token: string
  token_type: string
  user: AuthUser
}

/** 登录页需要的公开配置（不需要令牌）。 */
export interface AuthConfig {
  auth_enabled: boolean
  registration_open: boolean
  has_users: boolean
}

// --- IM（设置 → IM：在聊天软件里和智能体说话）--------------------------------

/** 一个平台字段；`secret` 的值加密落库，界面只显示「已填」。 */
export interface ImPlatformField {
  key: string
  label: string
  secret: boolean
  hint?: string | null
  options: { value: string; label: string }[]
}

/** 可连接的平台（添加表单由它生成）。 */
export interface ImPlatform {
  id: string
  name: string
  /** 界面上的小图标（后端元数据给，前端不按 id 写映射） */
  icon: string
  fields: ImPlatformField[]
  groups: boolean
  buttons: boolean
  available: boolean
  note?: string | null
  docs?: string | null
  steps: string[]
}

/** 连接级「发什么」的开关：过程内容默认全部关闭，聊天里只有正文。 */
export interface ImConnectionOptions {
  show_thinking: boolean
  show_tools: boolean
  show_logs: boolean
}

/** 一条 IM 连接：一个平台账号 + 绑定的智能体。 */
export interface ImConnection {
  id: string
  platform: string
  platform_name: string
  account_name: string
  account_id: string
  agent_profile_id?: string | null
  agent_name?: string | null
  agent_avatar?: string | null
  enabled: boolean
  /** ready / connecting / retrying / error / off */
  state: string
  error?: string | null
  /** 已填的密钥字段名（值永不回传） */
  secret_fields: string[]
  /** 发什么：过程内容（思考 / 工具 / 日志），默认都不发 */
  options: ImConnectionOptions
  created_at: string
  updated_at: string
  peers: number
  pending: number
  chats: number
}

/** 配对过的账号 / 等待允许的连接请求。 */
export interface ImPeer {
  id: string
  connection_id: string
  sender_id: string
  sender_name: string
  /** pending / allowed / denied */
  state: string
  chat_key: string
  chat_kind: string
  chat_title: string
  created_at: string
}

/** 连接见过的一个聊天。 */
export interface ImChat {
  id: string
  connection_id: string
  chat_key: string
  kind: string
  title: string
  agent_profile_id?: string | null
  agent_name?: string | null
  conversation_id?: string | null
  updated_at: string
}
