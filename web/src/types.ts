// 与后端 docs/05-API设计.md 对齐的数据契约类型

/** 会话 */
export interface Session {
  id: string
  title: string
  updated_at: string
  model?: string
  message_count?: number
}

/** 工具调用状态（denied = Harness 权限门控拦截，未执行） */
export type ToolCallStatus = 'running' | 'ok' | 'error' | 'denied'

/** 一次工具调用（assistant 消息附带） */
export interface ToolCall {
  call_id: string
  tool: string
  args: unknown
  result?: string // 统一为字符串预览（对象会被序列化）
  status: ToolCallStatus
  duration_ms?: number
}

/** token 用量 */
export interface TokenUsage {
  input_tokens: number
  output_tokens: number
}

/** 聊天消息（历史回放与流式渲染共用） */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  tool_calls?: ToolCall[]
  created_at?: string
  usage?: TokenUsage
  /** run 终态元信息（流式 run_finished 附带，仅内存态） */
  run_meta?: RunMeta
}

/** run_finished 中用于渲染的终态元信息 */
export interface RunMeta {
  stop_reason?: string
  gate_result?: GateResult
  budget?: BudgetSnapshot
  model_used?: string
  fallback_count?: number
  denied_count?: number
  /** 通道路由：最终通道、升级记录、是否收尾 */
  channel?: Channel
  escalations?: EscalationRecord[]
  wrap_up?: boolean
}

// ---------- 通道路由（fast / calc / deep） ----------

export type Channel = 'fast' | 'calc' | 'deep'

/** 一次单向升级记录（fast→calc→deep，不降级） */
export interface EscalationRecord {
  from: Channel
  to: Channel
  reason: string
  at_step?: number
  elapsed_s?: number
}

export interface ChannelSelectedEvent {
  channel: Channel
  reason?: string
  decision?: string
  locked?: boolean
  clarify?: boolean
}

export interface BudgetExhaustedEvent {
  reason: string
  channel?: Channel
}

export interface WrapUpEvent {
  channel?: Channel
  reason?: string
}

// ---------- SSE 事件负载 ----------

export interface RunStartedEvent {
  run_id: string
  session_id?: string
  thread_id?: string
}

export interface StateEvent {
  node: 'agent' | 'tools' | string
  status: 'running' | 'finished' | string
}

export interface TokenEvent {
  text: string
}

export interface ToolStartEvent {
  call_id: string
  tool: string
  args: unknown
}

export interface ToolEndEvent {
  call_id: string
  status: ToolCallStatus
  duration_ms?: number
  result_preview?: string
}

export interface RunFinishedEvent {
  run_id: string
  usage?: TokenUsage
  stop_reason?: string
  model_used?: string
  fallback_count?: number
  budget?: BudgetSnapshot
  gate_result?: GateResult
  denied_count?: number
  channel?: Channel
  escalations?: EscalationRecord[]
  wrap_up?: boolean
}

// ---------- Harness 契约 / 门控 / 预算（Phase 1-3） ----------

export type RiskLevel = 'low' | 'medium' | 'high'

export interface Budgets {
  max_tokens?: number | null
  max_tool_calls?: number | null
  max_iterations?: number | null
}

/** 任务契约（与后端 contract.py 对齐；缺省字段=不收紧） */
export interface TaskContract {
  outcome?: string | null
  max_risk?: RiskLevel
  allowed_tools?: string[] | null
  denied_tools?: string[] | null
  budgets?: Budgets
  gates_enabled?: boolean
  invariants?: string[]
}

export interface GateCheck {
  name: string
  passed: boolean
}

export interface GateResult {
  passed: boolean
  blocked_by?: string | null
  checked: GateCheck[]
}

export interface BudgetSnapshot {
  run_id?: string
  tokens_used?: number
  tokens_remaining?: number | null
  max_tokens?: number | null
  tool_calls_used?: number
  tool_calls_remaining?: number | null
  max_tool_calls?: number | null
  max_iterations?: number | null
}

/** 模型降级事件（Phase 2） */
export interface FallbackEvent {
  from: string
  to: string
  error?: string
}

/** 上下文治理事件（Phase 3） */
export interface ContextCompressedEvent {
  strategy: string
  before_msgs: number
  after_msgs: number
}

/** 模型注册表条目（GET /models） */
export interface ModelInfo {
  id: string
  label: string
  provider: string
  context_window?: number | null
  fallback?: string | null
  is_default: boolean
}

/** 运行轨迹列表条目（GET /sessions/{id}/runs） */
export interface RunSummary {
  id: string
  status: string
  stop_reason?: string | null
  usage: TokenUsage
  tool_calls: number
  task_contract?: TaskContract | null
  started_at: string
  finished_at?: string | null
}

/** span waterfall 条目（GET /traces/{run_id}） */
export interface TraceSpan {
  name: string
  span_id?: string | null
  parent_id?: string | null
  start_ms: number
  duration_ms: number
  status: string
  attributes: Record<string, unknown>
}

/** HITL 确认请求事件（docs/04 §2 / docs/05 §2.2） */
export interface InterruptEvent {
  interrupt_id: string
  tool: string
  args: Record<string, unknown>
  risk: 'low' | 'medium' | 'high'
  message?: string
  expires_in?: number
  run_id: string
  session_id: string
}

export type ResumeDecision = 'approve' | 'edit' | 'reject'

export interface SseErrorEvent {
  code: string
  message: string
  retryable?: boolean
}
