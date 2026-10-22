# 05 · API 设计 — AgentFlow

Base URL：`/api` ｜ 认证：`Authorization: Bearer <JWT>`（MVP 单用户可用静态 API Key）｜ 内容类型：`application/json`（SSE 除外）

## 1. 接口总览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 发送消息，启动 run，**SSE 流式返回** |
| POST | `/chat/resume` | HITL 确认/拒绝，恢复 run（SSE） |
| POST | `/chat/{run_id}/stop` | 中断运行 |
| GET | `/sessions` | 会话列表 |
| POST | `/sessions` | 创建会话 |
| PATCH | `/sessions/{id}` | 重命名 / 更新会话配置（模型等） |
| DELETE | `/sessions/{id}` | 删除会话（含消息与检查点） |
| GET | `/sessions/{id}/messages` | 历史消息（分页） |
| GET | `/sessions/{id}/runs` | 运行轨迹（观测） |
| GET | `/mcp/servers` | MCP Server 列表与健康状态 |
| POST | `/mcp/servers` | 注册 MCP Server |
| PATCH | `/mcp/servers/{id}` | 启停 / 改配置 / 风险覆盖 |
| DELETE | `/mcp/servers/{id}` | 注销 |
| GET | `/mcp/servers/{id}/tools` | 已发现工具列表 |
| GET | `/models` | 可用模型与默认配置 |
| GET | `/traces/{run_id}` | 单 run 的 span waterfall（观测调试） |
| GET | `/memories` | 长期记忆条目 |
| DELETE | `/memories/{id}` | 删除记忆条目 |
| GET | `/health` | 存活与依赖健康 |

## 2. 核心接口详细设计

### 2.1 POST /chat（SSE）

请求：

```json
{
  "session_id": "sess_01H...",
  "content": "帮我查一下 orders 表昨天的订单量",
  "attachments": [],
  "model": "gpt-4o-mini",
  "task_contract": {
    "outcome": "只读调研，不修改任何数据",
    "max_risk": "low",
    "allowed_tools": null,
    "denied_tools": null,
    "budgets": {"max_tokens": 50000, "max_tool_calls": 20, "max_iterations": null},
    "gates_enabled": true,
    "invariants": []
  }
}
```

**task_contract（任务契约，可选）**——Harness 核心字段，缺省为默认契约（不收紧、行为与不传时一致）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `outcome` | string? | 期望产出（描述性） |
| `max_risk` | "low" \| "medium" \| "high" | 允许的最高工具风险等级，超出即 DENY（默认 high） |
| `allowed_tools` | string[]? | 工具白名单（限定名 `{server}__{tool}`），配置后名单外全部 DENY |
| `denied_tools` | string[]? | 工具黑名单 |
| `budgets.max_tokens` | int? | token 总预算，超限干净终止（budget_exceeded） |
| `budgets.max_tool_calls` | int? | 工具调用次数上限，调用前拦截 |
| `budgets.max_iterations` | int? | 迭代轮数上限（未配置回退全局 MAX_ITERATIONS） |
| `gates_enabled` | bool | 完成门控开关（默认 true） |

非法契约（如 `max_risk: "extreme"`）由 pydantic 校验直接 **422**。契约随 run 落库、随图状态持久化——中断恢复（resume）后契约继续生效。

响应：`Content-Type: text/event-stream`，事件序列示例：

```
event: run_started
data: {"run_id":"run_01H...","thread_id":"sess_01H...","task_contract":{"max_risk":"low","budgets":{...},"gates_enabled":true}}

event: state
data: {"node":"load_context","status":"running"}

event: token
data: {"text":"我来查询"}

event: tool_start
data: {"call_id":"tc_1","tool":"sqlite-query__query","args":{"sql":"SELECT count(*) FROM orders WHERE ..."}}

event: tool_end
data: {"call_id":"tc_1","status":"ok","duration_ms":84,"result_preview":"42"}

event: tool_end
data: {"call_id":"tc_2","status":"denied","duration_ms":0,"result_preview":"操作被 Harness 拒绝：风险等级 high 超出契约允许上限（max_risk=low）。请勿重试该工具..."}

event: token
data: {"text":"昨天共有 42 笔订单。"}

event: fallback
data: {"from":"deepseek-chat","to":"qwen-plus","error":"模型不可用：..."}

event: run_finished
data: {"run_id":"run_01H...","usage":{"input_tokens":1820,"output_tokens":96},"stop_reason":"end_turn","model_used":"qwen-plus","fallback_count":1,"budget":{"tokens_used":1916,"tokens_remaining":48084,"max_tokens":50000,"tool_calls_used":1,"tool_calls_remaining":19,"max_tool_calls":20,"max_iterations":null},"gate_result":{"passed":true,"blocked_by":null,"checked":[{"name":"output_non_empty","passed":true},{"name":"tool_success_rate","passed":true}]}}
```

约定：
- 每个事件带 `id:`（递增序号），客户端断线重连时携带 `Last-Event-ID`，服务端从缓冲/checkpoint 重放
- `tool_end.result_preview` 截断至 2KB；完整结果存消息表
- **`tool_end.status` 取值：ok / error / denied（Harness 权限裁决拒绝，未执行）**
- **`run_finished.stop_reason` 取值：end_turn / waiting_approval / gate_blocked（完成门控未通过）/ budget_exceeded（token 预算耗尽）/ interrupted_stop / error**；`gate_result` 仅契约 `gates_enabled=true` 时附带，`denied_count` 存在本轮 Harness 拒绝数
- **`fallback`（Harness Phase 2 追加事件）**：主模型在产出任何 token 前不可用/重试耗尽时，切换到备用模型；`from`/`to` 为模型 id，`error` 为触发原因（截断 200 字）
- **`run_finished`（Harness Phase 2 追加字段）**：`model_used`（实际产出模型，发生降级时为备用模型）、`fallback_count`（累计降级次数）、`budget`（per-run 预算快照：`tokens_used`/`tokens_remaining`/`max_tokens`/`tool_calls_*`/`max_iterations`）
- 错误统一 `event: error`：`{"code":"mcp_server_unavailable","message":"...","retryable":true}`；预算耗尽为 `{"code":"budget_exceeded","retryable":false}`

### 2.2 POST /chat/resume（SSE）

触发场景：前端收到 `interrupt` 事件后提交决定。

```json
{
  "session_id": "sess_01H...",
  "run_id": "run_01H...",
  "interrupt_id": "int_1",
  "decision": "approve",          // approve | edit | reject
  "edited_args": null,             // decision=edit 时提供
  "reason": null                   // decision=reject 时回喂模型
}
```

响应：同 `/chat` 的 SSE 事件流（run 从中断点继续）。

### 2.3 会话与消息

`GET /sessions`：

```json
{
  "items": [
    {"id":"sess_...","title":"订单查询","updated_at":"2026-10-08T10:00:00Z",
     "model":"gpt-4o-mini","message_count":12}
  ],
  "next_cursor": null
}
```

`GET /sessions/{id}/messages?limit=50&before=msg_...`：

```json
{
  "items": [
    {"id":"msg_1","role":"user","content":"...","created_at":"..."},
    {"id":"msg_2","role":"assistant","content":"...","tool_calls":[
       {"call_id":"tc_1","tool":"sqlite-query__query","args":{...},
        "result":{...},"status":"ok","duration_ms":84}],"created_at":"..."}
  ]
}
```

### 2.4 MCP Server 管理

`POST /mcp/servers`：

```json
{
  "name": "web-search",
  "transport": "stdio",            // stdio | streamable_http
  "command": "npx",                // stdio 时
  "args": ["-y", "@modelcontextprotocol/server-brave-search"],
  "env": {"BRAVE_API_KEY": "..."}, // 服务端加密存储
  "enabled": true,
  "risk_overrides": {"fetch_page": "medium"}
}
```

`GET /mcp/servers` 返回附带 `status`（connected / degraded / disconnected）与 `tools_count`。

`PATCH /mcp/servers/{id}` 支持 `enabled` 切换与配置更新，响应触发热重载。

### 2.5 运行轨迹（观测）

`GET /sessions/{id}/runs?limit=50`（Harness Phase 2，聚合既有 `runs`/`tool_calls` 表，无新表）：

```json
{
  "items": [{
    "id": "run_...",
    "status": "finished",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1820, "output_tokens": 96},
    "tool_calls": 1,
    "task_contract": {"max_risk": "low", "budgets": {...}, "gates_enabled": true},
    "started_at": "...", "finished_at": "..."
  }]
}
```

细粒度时间轴（每个节点/工具的耗时 waterfall）见 `GET /traces/{run_id}`。

### 2.6 模型注册表（Harness Phase 2）

`GET /models`——来源为配置驱动的 `MODEL_REGISTRY`（无新表）：

```json
{
  "items": [
    {"id":"deepseek-chat","label":"DeepSeek Chat","provider":"openai","context_window":65536,"fallback":"qwen-plus","is_default":true},
    {"id":"qwen-plus","label":"通义千问 Plus","provider":"openai","context_window":131072,"fallback":null,"is_default":false}
  ],
  "default": "deepseek-chat"
}
```

`fallback` 为该模型的降级备用模型（Policy 链）；`provider` 由模型名前缀解析（`claude*` → anthropic，其余 OpenAI 兼容）。

### 2.7 链路追踪（Harness Phase 2）

`GET /traces/{run_id}`——返回该 run 的 span waterfall（按开始时间升序）。span 常驻内存环形缓冲（有界，进程内），配置 `OTEL_EXPORTER_OTLP_ENDPOINT` 时同时导出到 Jaeger：

```json
{
  "run_id": "run_...",
  "items": [
    {"name":"invoke_workflow","span_id":"...","parent_id":null,"start_ms":1730000000000,"duration_ms":1300,
     "status":"OK","attributes":{"gen_ai.conversation.id":"sess_...","gen_ai.request.model":"deepseek-chat","agentflow.stop_reason":"end_turn"}},
    {"name":"node:agent","span_id":"...","parent_id":"<workflow span_id>","duration_ms":1200,"status":"OK",
     "attributes":{"agentflow.node":"agent"}},
    {"name":"tool:filesystem__read_text_file","parent_id":"<workflow span_id>","duration_ms":84,"status":"OK",
     "attributes":{"gen_ai.operation.name":"execute_tool","gen_ai.tool.name":"filesystem__read_text_file"}},
    {"name":"chat_model","parent_id":"<workflow span_id>","status":"OK",
     "attributes":{"gen_ai.operation.name":"chat","gen_ai.request.model":"deepseek-chat","gen_ai.usage.input_tokens":40,"gen_ai.usage.output_tokens":96}}
  ]
}
```

## 3. 错误码约定

| code | HTTP | 含义 |
|---|---|---|
| `unauthorized` | 401 | 凭证缺失/失效 |
| `session_not_found` | 404 | 会话不存在 |
| `task_contract_invalid` | 422 | 任务契约字段非法（pydantic 校验失败） |
| `run_not_interrupted` | 409 | resume 目标不在等待确认状态 |
| `interrupt_expired` | 410 | 确认超时已自动拒绝 |
| `mcp_server_unavailable` | 503 | 目标 Server 掉线 |
| `llm_rate_limited` | 429 | 上游限流（retryable） |
| `budget_exceeded` | 200(SSE error 事件) | token 预算耗尽，干净终止（检查点保留，retryable=false） |
| `max_iterations_exceeded` | 200(SSE error 事件) | 循环超限，降级答复 |

## 4. 版本与兼容

- 路径版本：`/api/v1/...`（文档省略 v1 前缀）
- SSE 事件为**追加式契约**：新增事件类型不破坏旧客户端（未知事件忽略）
- 破坏性变更需双版本并行一个迭代周期
