# AgentFlow — Agent Harness 基础设施

基于 **LangGraph + LangChain + MCP + React** 构建的 Agent Harness（运行时控制系统）。

> **核心叙事：Agent = Model + Harness。** 模型能力是给定的，但 Harness 的工程深度决定 Agent 能不能在生产环境活下去。本项目不追求让模型更聪明，而是把**权限、状态、副作用与完成证明从模型手中转移到确定性运行时**——模型可以自由选择如何解决问题，但不能自行扩大权限、信任过时状态或从一段有说服力的摘要中宣布成功。

> 当前阶段：M1-M3 已落地验收（流式对话、MCP 热插拔、HITL 人工确认），**Harness Phase 1（任务契约/权限控制/完成门控/预算强制）已交付并通过 14/14 验收**，**Harness Phase 2（链路追踪/预算仪表化/模型降级）已交付并通过 16/16 验收**，**Harness Phase 3（分级恢复/循环检测/上下文治理/不变量消费）已交付并通过 13/13 验收**，**Harness Phase 4（pytest 评估体系 + pgvector 长期记忆）已交付**，**通道路由改造（fast/calc/deep 三档预算 + 单向升级）已交付并通过真实模型评测：全量 `pytest 68 passed`，20 条 NL2SQL 任务上三档对比单通道基线，分档准确率 100%、单查询 token 降 85%**，**前端已适配 Phase 1-4 与通道路由**（契约配置面板、通道选择器、拦截/降级/门控/升级状态条、运行轨迹查看器）。

## Harness 是什么

Harness 就是模型之外的一切：系统提示词、工具调用、编排逻辑、约束机制、反馈回路。业界已反复验证其杠杆效应——LangChain 在 Terminal Bench 2.0 上仅改动 Harness 层（模型固定不变），排名从第 30 升至第 5；Can.ac 的实验中，同一模型仅更换文件编辑接口的调用方式，编码基准分数从 6.7% 跳到 68.3%。

本项目的 Harness 把原来散落在提示词里的"软约束"实现为**确定性代码**：

| 模块 | 解决的问题 | 实现位置 |
|---|---|---|
| **Task Contract（任务契约）** | Agent 跑偏、越权、失控烧钱 | 启动前显式定义 outcome / max_risk / 工具黑白名单 / token·工具·迭代预算（pydantic 校验，非法契约 422） |
| **Channel Routing（通道路由）** | 简单任务被长链预算拖慢、复杂任务被短链截断 | 同一张图三组参数：fast（2 步/15s/4K/不挂工具）、calc（8 步/60s/32K/取数类）、deep（50 步/600s/128K/全部）。fast 的取数由**结构化输出给出动作、程序执行一次**（不挂工具 schema）；router 判定通道，**只由可观测事实触发单向升级**（步数与时间耗尽、连续拒绝、连续工具失败、结果过大、fast 取数失败、模型申请 `request_deep`、门控证据不足），到顶 `wrap_up` 收尾 |
| **AuthorityController（权限控制）** | 模型自行执行未授权操作 | 每次 tool_call 三段裁决：DENY（黑名单/白名单外/风险超契约→不执行、拒绝消息回喂）/ REQUIRE_HITL（契约内高风险→人工确认）/ ALLOW |
| **CompletionGate（完成门控）** | 模型"自评成功"实际未达标 | 自然结束时代码评估：输出非空、工具成功率 ≥90%、预算未超；未通过 → `gate_blocked` |
| **Budget 强制** | 单次 run 成本失控 | token 超预算干净终止（`budget_exceeded`），检查点已持久化可续；工具调用数/迭代轮数超限在调用前拦截 |
| **Durable Checkpoint** | 服务重启丢任务 | LangGraph Checkpointer 每步快照存 Postgres，**中断态跨进程重启存活**（含契约状态） |
| **HITL（人机协同）** | 高风险操作失控 | interrupt 暂停 → 批准/改参/拒绝 → resume；拒绝理由回喂模型引导换策略；超时自动拒绝 |
| **Observability（可观测性）** | Harness 行为不可度量、不可归因 | 全链路 span（workflow/节点/工具/模型）遵循 OTel GenAI 语义约定；单 run 预算仪表化（剩余预算回注模型促其收敛）；模型瞬态故障指数退避重试、重试耗尽按注册表降级并下发 `fallback` 事件 |
| **Recovery（分级恢复）** | 失败即终止、不确定写重复副作用 | 工具失败按错误类型分级：瞬态→指数退避重试；不确定写操作→先对账（检查副作用是否已落地）再决定重试；模型限流→沿 fallback 链降级 |
| **Loop Guard（循环检测）** | 连续重复调用烧预算不收敛 | action+params 哈希滑窗比对，连续重复自动终止（`stop_reason=loop_detected`） |
| **Context Governance（上下文治理）** | 长任务上下文膨胀劣化 | Compaction（旧消息摘要压缩）+ Reset（状态外化），下发 `context_compressed` 事件 |
| **Evaluation（评估体系）** | 改动后质量回归靠手感 | pytest 68 用例（单测 + 正常/边界/恢复/越权/预算/记忆/通道路由）+ 会话级指标门禁：Task Success>85%、工具成功率>95%、恢复>90%、拦截率=100%、Token/任务<50K |
| **Long-term Memory（长期记忆）** | 跨会话经验不可复用 | 门控通过的 run 结论沉淀 pgvector（dev 模式 SQLite 降级），`/memories` 支持列表与向量召回，新会话按需注入 |

## 核心特性

- **多轮流式对话**：SSE 实时推送 Token、工具调用过程与中间状态（事件追加式契约，含 `channel_selected` / `escalated` / `budget_exhausted` / `wrap_up`、`fallback`、`context_compressed`、denied `tool_end`）
- **分档预算**：同一张图按 fast/calc/deep 三档运行参数执行，通道只升不降；显式指定通道即锁定，预算用尽直接收尾
- **确定性权限边界**：模型决定"做什么"，契约和代码决定"能不能做"——权限校验不在模型手里
- **标准化工具接入**：MCP 协议热插拔工具服务器（注册即用、零代码扩能力），故障自动隔离降级
- **断点可恢复**：检查点持久化 + 中断/恢复原语，服务重启后从断点续跑
- **完成可证明**：任务是否完成由门控代码判定，而非模型自评
- **行为可观测**：全链路 span 可导出 OTLP→Jaeger，`/traces/{run_id}` 内存 waterfall 即时调试；预算/模型降级全程可归因

## 文档目录

| 文档 | 内容 |
|---|---|
| [01-需求文档-PRD](docs/01-需求文档-PRD.md) | 目标用户、功能需求（FR-1~28）、非功能需求、用户故事 |
| [02-系统架构设计](docs/02-系统架构设计.md) | 分层架构（含 Harness Layer）、组件划分、ADR（D1~D10） |
| [03-技术选型与分工](docs/03-技术选型与分工.md) | 各技术栈的职责边界与选型理由 |
| [04-Agent工作流与MCP集成设计](docs/04-Agent工作流与MCP集成设计.md) | LangGraph 状态图（authority_gate）、契约生命周期、MCP 接入 |
| [05-API设计](docs/05-API设计.md) | REST / SSE 接口契约（含 task_contract 字段、门控结果、`/models` `/traces` `/runs`、`fallback` 事件） |
| [06-数据模型设计](docs/06-数据模型设计.md) | 实体关系、表结构与检查点存储 |
| [07-开发路线图](docs/07-开发路线图.md) | Harness Phase 1-4 里程碑与迭代计划 |

## 技术栈一览

```
前端    React 18 + TypeScript + Vite + Zustand + TailwindCSS
Harness  Task Contract / AuthorityController / CompletionGate / Budget（app/harness/）
编排    LangGraph（StateGraph、Checkpointer、interrupt/HITL）
后端    Python FastAPI（BFF 层）+ LangChain 模型抽象
工具    MCP（Model Context Protocol）SDK + 多 MCP Server
存储    PostgreSQL（业务数据 + 契约 + 检查点）+ pgvector（长期记忆向量）
观测    OpenTelemetry（双通道 span：内存环形缓冲 + OTLP→Jaeger）+ tenacity 重试/降级
```

## 快速开始

**前置**：Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)、Node.js 18+（MCP filesystem Server 通过 `npx` 拉起）、Docker（仅用于 Postgres 与 Jaeger）。

```bash
# 1) 环境变量：仓库不含 .env，从示例复制后填写
copy .env.example .env          # Windows；macOS/Linux 用 cp .env.example .env
#   至少填一个模型 Key：OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY
#   DEFAULT_MODEL 默认 deepseek-chat，可改成 gpt-4o-mini / qwen-plus 等

# 2) 起 Postgres（pgvector 镜像，含业务数据与 LangGraph 检查点）
docker compose up -d postgres

# 3) 后端：首次启动自动建表，监听 http://127.0.0.1:8000
cd server && uv sync && uv run python run.py

# 4) 前端：开发服务器 http://localhost:5173（/api 已代理到 8000）
cd web && npm install && npm run dev
```

**不想装 Docker / 没有 Key 时**：

- `.env` 里设 `DEV_SQLITE=true` → 改用 SQLite + 内存检查点（数据在 `server/dev.db`），无需 Postgres。
- `.env` 里设 `DEV_FAKE_MODEL=true` → 用假模型跑通 SSE / 工具调用 / HITL / 门控全链路（无需 API Key，仅开发用）。

接口文档：后端起来后 `http://127.0.0.1:8000/docs`；就绪检查 `GET /api/v1/health`。
跑测试：`cd server && uv run pytest tests`（假模型 + 内存检查点，不依赖 Docker 与 API Key）。

## 快速理解：一次受 Harness 约束的任务执行

1. 用户在 React 界面下达任务（可指定**通道**，可选携带**任务契约**：风险上限、预算、工具白名单）
2. FastAPI 校验契约（非法 422）→ 契约随 run 落库、随 state 注入图（checkpoint 持久化）
3. **router 判定通道**（显式指定即锁定）→ **guard 做通道预算守卫** → Agent 节点调用 LLM 决策；需要工具时 **authority_gate 对每次调用做确定性裁决**：
   - 越权（黑名单/风险超契约/超预算/超通道风险上限）→ DENY 或升级，拒绝消息回喂模型，100% 不执行
   - 契约内高风险 → `interrupt`，前端确认卡片，批准/改参/拒绝后恢复
   - 其余 → 执行，结果回喂
4. token 消耗实时对照预算，超限**干净终止**（检查点可续）；通道步数/时间/token 耗尽、连续被拒、工具连续失败、单次结果过大、fast 取数失败、模型申请 `request_deep`、门控判定证据不足，都会触发**单向升级**（只换参数不重跑）；deep 到顶则 `wrap_up` 收尾
5. 自然结束时 **CompletionGate 代码评估**完成条件（含 `report_cites_queries` 等不变量），通过才 `end_turn`，否则 `gate_blocked`
6. 全程 SSE 流式回传（`channel_selected` / `escalated` / `budget_exhausted` / `wrap_up` 等）；契约、拒绝记录（含真实风险等级）、门控结果随 Run 持久化到 PostgreSQL
7. 全程生成链路 span（router/guard/节点/工具/模型）写入内存缓冲并在配置 OTLP 时导出 Jaeger；单 run 的预算/降级/通道升级情况在 `run_finished` 中可归因

## 验收状态

| 里程碑 | 验收 |
|---|---|
| M1 流式对话 + 工具调用 | e2e_check ALL PASS |
| M2 MCP 热插拔 | m2_check 16/16 PASS |
| M3 HITL 人工确认 | m3_check 14/14 PASS（含跨进程重启恢复） |
| **Harness Phase 1 契约/权限/门控/预算** | **harness_check 14/14 PASS**（越权 100% 拦截、预算干净终止、门控拦截、契约跨进程存活） |
| **Harness Phase 2 追踪/预算仪表/模型降级** | **phase2_check 16/16 PASS**（span waterfall + parent 关联、预算快照、重试不降级、fallback 链、`/models`、`/runs`、会话级模型覆盖） |
| **Harness Phase 3 恢复/循环检测/上下文治理/不变量** | **phase3_check 13/13 PASS**（瞬态重试、不确定写对账、限流降级、loop_detected、Compaction/Reset 事件、invariants 门控消费） |
| **Harness Phase 4 评估体系 + 长期记忆** | **`pytest tests` 68 passed**，指标门禁全 PASS：Task Success 100%（>85%）、Tool Success 97.4%（>95%）、Recovery 100%（>90%）、Interception 100%（=100%）、Token/任务 181（<50K）；`/memories` 沉淀与向量召回用例通过 |
| **通道路由改造** | **`pytest tests` 68 passed**（含 `tests/eval/test_routing.py` 13 条：分档、单查询 fast 单次取数、clarify 零工具、连续拒绝/连续失败/结果过大/模型申请升级、fast→calc→deep 逐级升级后 wrap_up、锁定通道不升级、门控证据不足升档、非法通道 422；`tests/unit/test_nl2sql_guard.py` 5 条只读 SQL 守卫）；新增 NL2SQL 场景 MCP Server（合成电商数据 + 只读 SQL 守卫 + `report_cites_queries` 不变量）；**20 条任务 × 两轮（三档 / 全 deep 基线）真实模型实测：分档准确率 100%、误升级率 0%、单查询 token −85%、平均 token −19%** |
| **前端 Phase 1-4 + 通道路由适配** | `npm run build` 通过；契约面板透传生效、通道选择器（自动/fast/calc/deep，指定即锁定）、`channel_selected`/`escalated`/`wrap_up` 提示条、RunStatusBar（通道/升级/门控/拦截/降级/预算进度）、TraceViewer run 列表 + span waterfall |

## 可观测性（Phase 2）

- **内存 span 缓冲**：服务始终挂内存环形缓冲（按 `run_id` 归集），`GET /api/v1/traces/{run_id}` 直接返回 span waterfall，无需外部依赖即可调试与验收。
- **OTLP 导出（可选）**：设置 `OTEL_ENABLED=true` + `OTEL_EXPORTER_OTLP_ENDPOINT` 后同时经 `BatchSpanProcessor` 导出。内置 Jaeger 一键启动：

  ```bash
  docker compose up -d jaeger
  # Jaeger UI: http://localhost:16686
  ```

- **span 层级**：`invoke_workflow`（根）→ 节点 span（`router` / `guard` / `agent` / `gate` / `authority_gate` / `wait_approval` / `escalate` / `wrap_up` / `tools`）→ 工具 span / 模型 span。属性遵循 OTel GenAI 语义约定（`gen_ai.operation.name`、`gen_ai.request.model`、`gen_ai.tool.name`、`gen_ai.usage.*`）与项目自有键（`agentflow.run.id`、`agentflow.stop_reason`、`agentflow.fallback.used` 等）。
- **模型降级**：请求级/会话级模型优先级解析 → 瞬态故障（超时/连接/限流）经 tenacity 指数退避重试 → 重试耗尽按 `MODEL_REGISTRY.fallback` 降级，下发 `fallback` SSE 事件并在 `run_finished.model_used`/`fallback_count` 记录。仅在尚未产出 chunk 时允许重试/降级，不回放已下发 token。
