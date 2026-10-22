# Agent Harness 通道路由改造方案

> **目标**：在现有 Agent Harness 上增加一层「预算分档 + 单向升级」的通道路由，让简单任务不支付复杂任务的成本。
> **定位**：这是 Harness「可靠性控制层」的预算维度扩展，不是把 Harness 改造成另一个项目。
> **边界**：只借鉴「三档预算 + 只由事实触发升级」的设计思想，代码独立实现；不复制任何公司内部代码、提示词或业务逻辑。

---

## 1. 要解决的问题

当前 Harness 的控制点在「能不能做」（权限）和「算不算做完」（门控），但缺少第三个维度：**该花多少成本去做**。

结果是两类浪费：

| 现象 | 代价 |
|---|---|
| 一句话能答的问题，也走完整 tool loop | 多轮工具调用、首字延迟高、token 浪费 |
| 复杂任务与简单任务共用一套预算 | 要么简单任务超时，要么复杂任务被过早掐断 |

改造后：**同一张图，三组运行参数。** 模型仍决定"怎么做"，通道决定"能用多少步、多少秒、哪些工具"。

**这三档不是设计出来的，是被业务逼出来的**——具体场景与评测见 §3。

---

## 2. 三个通道

| | fast | calc | deep |
|---|---|---|---|
| 适用任务 | 单步可答 / 一次工具调用内 | 多步取数、试错、聚合 | 交付物、长链路分析 |
| 步数上限 | 2 | 8 | 50（组装型 12） |
| 时间预算 | 15s | 60s | 600s |
| token 预算 | 4k | 按现有配置 | 按现有配置 |
| tool calling | 否，工具集恒为空 | 是，只挂取数/检索类 | 是，全部工具 |
| 人工审批 | 不允许，遇到即升级 | 允许 | 允许 |
| 下一级 | calc | deep | 无 |

**fast 为什么干脆不挂工具**：工具 schema 本身吃上下文；结构化输出直接拿"要执行什么"比让模型自己循环调工具更稳、更可审计；执行交给程序，失败可程序化兜底。代价是 fast 失去"看目录试错"的能力——这正是它需要升级的原因。

---

## 3. 验证场景：NL2SQL 分析助手

### 3.1 为什么必须有场景

三档编排的价值来自**任务形态的成本差异**，不是来自"有三个通道"。没有场景，三档就是三个参数；有场景，三档才是三个不得不分开的理由。

所以 Harness 不能只作为通用基建存在，必须挂一个真实工作负载来验证它。选 NL2SQL 分析助手，因为它天然包含三种成本差异极大的任务，而且每条既有控制能力都能在这里找到真实触发器。

### 3.2 场景定义

| 项 | 内容 |
|---|---|
| 数据源 | 公开数据集（电商订单 / 电影 / 公交任选其一），DuckDB 单文件，3–5 张表 |
| 交互 | 中文提问 → 模型生成只读 SQL → 程序执行 → 结果表格 / 图表 / 报告 |
| 工具（5 个） | `list_tables` / `describe_table` / `run_sql`（只读）/ `render_chart` / `write_report` |
| 前端 | 复用现有 React + SSE 页面，增加一个对话与结果区 |

**数据必须用公开数据集**，不接公司数据、不碰公司表结构。

### 3.3 三类任务与三档的对应

| 任务类型 | 示例 | 为什么必须单独一档 | 通道 |
|---|---|---|---|
| 单指标查询 | "上个月华东区销售额多少" | 单条 SQL 可答，用户要秒级；挂工具 schema 纯浪费 | fast |
| 多步分析 | "哪些品类连续三个月下滑，原因是什么" | 需先看目录、多轮取数、试错；15 秒不够 | calc |
| 报告交付 | "生成一份 Q3 经营分析报告" | 交付物，需多轮取数 + 组织 + 引用；步数少会烂尾 | deep |

这张表就是 §12 简历文案的合法性来源：**每一档都能指到一个真实任务，不是拍脑袋分的。**

### 3.4 场景让每条既有能力都有触发器

| Harness 能力 | 在本场景中的真实触发 |
|---|---|
| AuthorityController | 非 SELECT、无 LIMIT、跨表白名单外、行级越权 |
| 预算强制 | 大表全扫描、返回结果集过大 |
| 完成门控 | 报告未引用任何 `query_id` → `gate_blocked`，防止编造数字 |
| interrupt 审批 | 高危 SQL（DDL / DML）或大额导出 |
| checkpoint | 报告生成中断后续跑，不必从头取数 |
| SSE | 通道切换、取数步骤、报告流式输出 |

**这张表是判断"有没有硬套"的标准**：如果某项能力在场景里找不到触发器，就说明它是为了写简历而加的，应该删掉。

### 3.5 最小实现范围

**要做**：1 份公开数据、5 个工具、复用现有前端、20 条评测任务。

**不要做**：多数据源接入、SQL 编辑器、权限管理后台、图表库选型、向量检索、多租户。这些和"分档为什么成立"无关，只会拖长周期。

### 3.6 评测设计（整段的证据锚）

**任务集**：20 条 = 10 条单查询 + 5 条多步 + 5 条报告。

**指标**：

| 指标 | 定义 |
|---|---|
| 分档准确率 | 入口判定与预期通道一致的比例 |
| 误升级率 | 本可在 fast 完成却被升档的比例 |
| P50 / P95 耗时 | 按通道分组统计 |
| 平均 token | 按通道分组统计 |
| 任务成功率 | 有最终回答且结果正确 |
| 编造数字次数 | 报告中引用的 `query_id` 不存在或不匹配的次数 |

**基线对比**：同一批任务全部走 deep（单通道基线），与三档结果对比。

> 实测环境：SQLite 合成电商数据（1158 订单 / 2270 条明细）、模型 `deepseek-chat`、每条任务独立会话。
> 两轮各 20 条，共 40 次 run；脚本见 `server/scripts/routing_eval.py`。

| 指标 | 全部 deep（基线） | 三档 | 变化 |
|---|---|---|---|
| P50 耗时 | 4.0s | 4.5s | +0.5s |
| P95 耗时 | 16.0s | 21.4s | +5.4s |
| 平均 token | 16677 | 13579 | −19% |
| 分档准确率 | — | 100%（20/20） | — |
| 误升级率 | — | 0% | — |
| 任务成功率 | 95%（d4 被门控拦截） | 100% | +5pp |
| 编造数字次数 | 0 | 0 | — |

整体 P50/P95 被 5 条报告任务主导（约 20s/条），看不出分档收益；**按组拆开才是结论**：

| 组 | 基线 token | 三档 token | 实际通道 | 变化 |
|---|---|---|---|---|
| 单查询（10 条） | 6498 | 998 | fast | **−85%** |
| 多步（5 条） | 14970 | 12112 | calc | −19% |
| 报告（5 条） | 38741 | 40209 | deep | +4%（同通道，为噪声） |

**结论**：收益集中在 §1 说的那件事——**简单任务不再支付复杂任务的成本**：单查询 token 降 85% 且全部落在 fast；多步省掉 deep 的冗余取数；报告本就该走 deep，分组后没有额外开销。**这张表填完，§12 的调参记录才有依据；没有它，三档就只是设计。**

### 3.7 与实习经历的边界

- **业务域相同，实现完全独立。**
- 数据公开、代码公开，不复制公司代码、提示词、客户数据。
- 实习做的是既有平台的职责拆分与链路提速；本项目做的是**问数场景下的运行时失控治理**（越权 SQL、报告编造数字、简单查询烧掉报告级预算）。
- 面试口述："分档思路参考了实习平台后续的编排设计，但那部分不是我做的；这里是我独立实现并用 20 条样本调参的。"

---

## 4. 核心数据结构

### 4.1 通道参数（新增，建议放 `contracts/channels.py` 或现有配置模块）

```python
from dataclasses import dataclass

Channel = str  # "fast" | "calc" | "deep"

@dataclass(frozen=True)
class ChannelLimits:
    max_steps: int
    time_budget_s: float
    token_budget: int
    allowed_tools: frozenset[str] | None   # None 表示全部工具
    allow_human_approval: bool
    escalate_to: Channel | None

CHANNEL_LIMITS: dict[str, ChannelLimits] = {
    "fast": ChannelLimits(
        max_steps=2, time_budget_s=15.0, token_budget=4000,
        allowed_tools=frozenset(), allow_human_approval=False, escalate_to="calc",
    ),
    "calc": ChannelLimits(
        max_steps=8, time_budget_s=60.0, token_budget=32000,
        allowed_tools=frozenset({"list_tables", "describe_table", "run_sql"}),
        allow_human_approval=True, escalate_to="deep",
    ),
    "deep": ChannelLimits(
        max_steps=50, time_budget_s=600.0, token_budget=128000,
        allowed_tools=None, allow_human_approval=True, escalate_to=None,
    ),
}

DEEP_ASSEMBLY_MAX_STEPS = 12   # 复用已有结果时收紧步数
```

### 4.2 状态扩展（在现有 AgentState 上增量添加）

```python
channel: str                      # 当前通道
channel_locked: bool              # 锁定后不再升级（如组装型任务）
step_count: int                   # 模型轮次计数，不是节点访问次数
started_at: float                 # time.monotonic()
escalations: list[dict]           # [{from, to, reason, at_step}]
route_decision: dict | None       # 入口判定结果，便于 SSE 与审计
```

**不要复用 `step_count` 去算 LangGraph 的 `recursion_limit`**，两者语义不同（见 §8）。

### 4.3 升级记录

```python
{
  "from": "fast",
  "to": "calc",
  "reason": "guard_denied_twice",   # 枚举值，见 §6
  "at_step": 2,
  "elapsed_s": 6.3,
}
```

枚举值固定，便于测试断言与前端展示：`guard_denied_twice` / `tool_failed_twice` / `result_too_large` / `steps_exhausted` / `time_exhausted` / `token_exhausted` / `model_requested` / `approval_required` / `tool_risk_exceeded` / `gate_insufficient_evidence` / `fast_action_failed`。

---

## 5. 图结构改造

### 5.1 改造前

```
入口 → model ⇄ tools → 结束
```

### 5.2 改造后

```
入口 → router ─┬─ direct ──────────────→ 结束（fast 内直接作答）
               ├─ clarify ─────────────→ 结束（输出澄清问题）
               ├─ reuse ───────────────→ 恢复 checkpoint → 结束
               ├─ plan ────────────────→ interrupt（人工确认计划）→ model
               └─ escalate(calc/deep) ─→ model ⇄ tools → 结束
                                          │
                                          ├─ 事实触发升级 → 换参数重入 model
                                          └─ 预算耗尽 → wrap_up → 结束
```

**关键约束：图结构只有一张。** 通道差异全部收敛在节点内部：换提示词、换工具集、换预算。不要为三个通道建三张子图，否则升级时要跨图搬状态。

### 5.3 router 节点做什么

1. 显式信号优先：请求里带 `channel` 时直接锁定，不问模型。
2. 否则一次结构化输出（`temperature=0`，**不挂任何工具 schema**），只回答"这个任务需要多少预算"。
3. 解析失败一律按 `escalate → calc` 处理：**判错的代价是慢，不是错。**

### 5.4 入口判定的五种去向

| 判定 | 去向 | 说明 |
|---|---|---|
| `direct` | fast | 单步可答，或只需一次确定性工具调用 |
| `escalate` | calc / deep | 多步、需要试错、或要求交付物 |
| `clarify` | 结束 | 缺关键参数，直接反问，不烧预算 |
| `reuse` | 结束 | 命中已有 checkpoint / 上次结果，恢复而不是重跑 |
| `plan` | interrupt | 高风险任务，先人工确认计划再执行 |

`reuse` 与 `plan` 是 Harness 相比通用编排多出来的两个去向，因为你有 checkpoint 和 interrupt，这两件事别浪费。

---

## 6. 运行中的升级：只由事实触发

模型自评"我需要更多预算"不可靠（它会高估自己），所以升级条件必须是**可观测的事实**：

| 触发事实 | 当前通道 | 处理 |
|---|---|---|
| AuthorityController 连续 DENY 2 次 | fast / calc | 升一级，拒绝原因随消息带走 |
| 工具连续失败 2 次（`ok:false`） | fast / calc | 升一级 |
| 单次工具结果超过阈值（如 50 行 / 8000 字符） | fast | 升 calc，结果包成 tool message 带走 |
| 命中需要人工审批的高风险操作 | fast | 升 calc（fast 无审批预算） |
| `step_count >= max_steps` | fast / calc | 升一级 |
| `elapsed >= time_budget_s` | fast / calc | 升一级 |
| 模型显式调用 `request_deep` | calc | 升 deep |
| fast 的一次确定性取数执行报错 | fast | 升 calc（fast 无试错能力，§2） |
| 已完成门控判定"证据不足" | calc | 升 deep |
| 以上任一条件在 deep 触发 | deep | `wrap_up` |

**单次失败不升级**：SQL/工具报错 1 次只回灌 `ok:false` 让模型自纠。连续 2 次才说明策略失效，而不是措辞问题。

### 6.1 三条硬约束

- **单向**：fast → calc → deep，没有降级路径。降级要回滚状态，且已取到的结果和错误消息会污染下一通道的判断。
- **不重来**：已执行的工具调用与结果留在 `messages` 里，`escalations` 只追加。新通道换的是工具集和预算，不是上下文。
- **锁定不升级**：`channel_locked=True`（组装型任务）时，步数用尽直接 `wrap_up`，并摘掉会再次申请扩权的工具。

---

## 7. 与 Harness 既有能力的打通

| 既有能力 | 改造点 |
|---|---|
| **Task Contract / AuthorityController** | DENY 原因照旧回喂；连续 2 次 DENY 转为升级，而不是无限重试。fast 通道的 `allow_human_approval=False`，遇到需审批操作直接升级。 |
| **预算强制** | 现在只有 token 预算，补上步数与时间两个维度；三者任一超限都走同一套终止逻辑。 |
| **完成门控（`gate_blocked`）** | 门控判定"证据不足"时，若还有上级通道则先升级再判，避免把"预算不够"误判成"任务失败"。 |
| **LangGraph interrupt + checkpoint** | `plan` 去向复用 interrupt；升级前后各写一次 checkpoint，并在 checkpoint 里保存 `channel` 与 `escalations`，恢复时**不得回退通道**。 |
| **SSE 七类事件** | 新增 `channel_selected` / `escalated` / `budget_exhausted` / `wrap_up`，前端展示"已从 fast 升级到 calc（原因：工具连续失败 2 次）"。 |
| **MCP 连接池** | 按通道裁剪可用 MCP 工具白名单，fast 不加载任何 MCP 工具 schema。 |

---

## 8. LangGraph recursion_limit 的坑

`recursion_limit` 数的是**节点访问次数**，不是工具轮次。路径是 `router → (model → tools)* → model`，一轮工具占 2 跳；同一请求还可能先走完 calc 再升 deep。

默认值 25 会在约 12 轮工具时被掐死，盖不住 deep 的 `max_steps`。按上限动态计算：

```python
def graph_recursion_limit(calc_steps=8, deep_steps=50, margin=8) -> int:
    return 1 + (calc_steps + deep_steps) * 2 + 1 + margin   # = 126
```

---

## 9. 交付与失败语义

- **交付判据只有一条**：最后一条消息没有 `tool_calls`，即视为结论。
- fast 在 router 内直接写 `answer`，不进循环；calc / deep 由模型输出文本即交付。
- 业务性失败（工具报错、guard 拒绝、超时）**不抛异常**，统一包成 `ok:false` 回灌。
- 环境性失败（连接断开、内部异常）直接让本轮失败，因为换通道也修不好。
- 到顶后 `wrap_up`：清空工具集，再要一次纯文字作答；仍失败则用程序兜底文案。
- 外层成败只看有没有产出回答：有 answer 即成功，整轮无 answer 才是 `no_answer`。

---

## 10. 实施步骤（每步都可独立验证）

| 步骤 | 改动 | 验收 |
|---|---|---|
| 0 | 搭最小场景：公开数据集 + 5 个只读工具 + 对话页 | 手动跑通三类提问，暂不接路由 |
| 1 | 抽 `ChannelLimits` + 状态字段，全部默认 deep | 现有测试全绿，行为无变化 |
| 2 | 加 router 节点，只支持 `direct` / `escalate` 两去向 | 5 个固定输入得到预期去向与通道 |
| 3 | 加步数 / 时间预算守卫 + `wrap_up` | 构造超限用例，断言有 answer 且不抛异常 |
| 4 | 加升级判定 + 与 AuthorityController 联动 | 7 个触发条件各一条用例，断言 `escalations` 序列 |
| 5 | 加 SSE 四个事件 + 前端展示 + 完整测试矩阵 | 前端能看到通道切换与原因 |
| 6 | 跑 §3.6 的 20 条任务，填基线对比表与调参记录 | 表填满，能量化三档收益 |

**先做 0、1、3，再做 2、4。** 场景和预算守卫是确定收益，入口判定涉及提示词调优，容易反复。**没有步骤 0，后面的分档都没有验证对象。**

---

## 11. 测试清单（用 mock 模型，不依赖真实 LLM）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 单步问题 | 通道 fast，1 次模型调用，无工具 |
| 2 | 多步问题 | 通道 calc，`escalations` 为空 |
| 3 | 交付物请求 | 通道 deep |
| 4 | 参数缺失 | `clarify`，0 次工具调用 |
| 5 | 命中 checkpoint | `reuse`，不重跑工具 |
| 6 | guard 连拒 2 次 | fast → calc，原因 `guard_denied_twice` |
| 7 | 工具连败 2 次 | 升一级，且只升级一次 |
| 8 | 单次工具失败 | **不升级**，`ok:false` 回灌 1 次 |
| 9 | fast 遇审批操作 | 直接升 calc，不弹审批 |
| 10 | deep 步数耗尽 | `wrap_up`，有 answer，无异常 |
| 11 | 锁定通道耗尽 | 不升级，直接 `wrap_up` |
| 12 | 任意场景 | `channel` 单调不降，`escalations` 长度 ≤ 2 |
| 13 | 报告未引用 query_id | `gate_blocked`，不产出报告 |
| 14 | 越权 SQL（非 SELECT / 无 LIMIT） | 执行前被拒绝，原因回喂模型改写 |

---

## 12. 调参记录（面试必问，务必真填）

面试官一定会问"阈值为什么是 2 次、15 秒"。**文档不会给你答案，只有你自己跑出来的记录能。**

| 参数 | 取值 | 依据 | 调整历史 |
|---|---|---|---|
| fast 步数 | 2 | 单指标查询只需「判定 → 一次取数 → 作答」；实测 fast 组 10/10 在 1 个模型轮内完成 | 初版 2，未调 |
| fast 时间 | 15s | 实测 fast 组 P50 1.9s / P95 3.5s，15s 留约 4× 余量；超时升 calc | 初版 15s，未调 |
| guard 连拒阈值 | 2 | 单次拒绝多是措辞/参数问题，回灌即可自纠；连续 2 次仍无进展才说明策略失效 | 初版 2，未调 |
| 工具连败阈值 | 2 | 同上；单次工具失败只回灌 `ok:false`，不升级（用例 8 断言） | 初版 2，未调 |
| 结果过大阈值 | 50 行 / 8000 字符 | 与 nl2sql `run_sql` 的结果上限一致——超过即说明结果集需要更高通道的预算来消化 | 初版 50 行，补字符数维度 |
| calc 步数 | 8 | 实测多步组 3–5 轮完成（P95 6 轮），8 步留余量；超出升 deep | 初版 8，未调 |
| fast 取数失败处理 | 失败即升 calc | fast 只有一次取数机会，失败即失去试错能力（§2）；留给 fast 继续作答只会得到编造结果 | 初版失败后仍由 fast 作答 → 模型把 DSML 工具调用标记当成答复、且 `tool_success_rate=0` 触发 `gate_blocked`；改为失败即升 calc（`fast_action_failed`） |
| 报告出处校验对象 | 交付物正文 | `write_report` 的 markdown 才是交付物，出处写在文件里，不应要求对话答复重复引用 | 初版只校验对话答复 → 实测 5/5 报告 `gate_blocked`（报告已引用 12 个真实 query_id）；改为校验交付物正文后 5/5 通过 |

建议做法：跑 §3.6 的 20 条任务，记录误升级率与平均耗时，把数字填进"依据"列。**有这张表，你就从"读过设计"变成"做过取舍"。**

---

## 13. 简历写法（改完之后可用）

> **NL2SQL 分析助手 — 基于 Agent Harness 的可靠问数系统 ｜ 个人项目**
> 以公开销售数据集为数据源，支持"查指标 / 多步分析 / 生成报告"三类提问；模型只生成只读 SQL，执行、权限与完成判定均由代码控制。
>
> - **任务分档**：三类提问成本差异极大——单指标要秒级、多步分析需看目录试错、报告需多轮取数与组织。入口用一次结构化输出判定任务形态，按 15s/2 步、60s/8 步、600s/50 步三档分流，避免简单查询支付报告级预算。
> - **只读与权限**：SQL 经语法树校验后执行（仅 SELECT、强制 LIMIT、表级白名单、行级过滤），越权语句在执行前拒绝，拒绝原因回喂模型改写。
> - **预算与完成门控**：按通道限制步数、时间、扫描行数与 token；报告类交付必须引用真实查询结果，无引用即 `gate_blocked`，避免模型编造数字。
> - **失败升级**：SQL 报错或权限拒绝连续 2 次升档，已取结果随上下文带走不重跑；deep 到顶后清空工具集做纯文本收尾，保证有回答而非空转。
> - **分档评测**：20 条任务（10 单查询 / 5 多步 / 5 报告），对比单通道基线，记录分档准确率、P50 耗时与 token 成本。

**面试可承接的追问**（也是这份设计真正的价值）：

1. 为什么 fast 不挂工具 → schema 吃上下文 + 结构化输出更稳 + 程序执行可审计
2. 为什么不允许降级 → 回滚状态会污染上下文，升级只换参数不换上下文
3. 阈值怎么来的 → 拿 §12 的调参记录回答
4. 升级后状态会不会乱 → `escalations` 只追加，工具结果留在 messages，checkpoint 保存通道
5. 三档到底有没有用 → 拿 §12 基线与三档的对比表回答，不给形容词
6. 报告怎么防编造 → 门控校验 `query_id` 是否真实存在，无引用直接 `gate_blocked`

---

## 14. 不要做的事

- 不要为三个通道建三张子图。
- 不要把"通道判定"交给模型自由发挥：必须结构化输出 + `temperature=0`。
- 不要让 fast 具备兜底写报告、调用审批之类的重型能力，那会让分档失去意义。
- 不要为了让简历好看而堆能力：这一层的价值是**省钱和省时**，不是功能多。
- 不要复用 `step_count` 当 `recursion_limit`。
- 不要在场景没跑通之前先做分档：没有 20 条任务，你无法证明三档有用。
- 不要把公司数据、表结构、提示词带进个人项目。

---

## 15. 实现状态（本次迭代）

步骤 0–6 均已完成；§3.6 基线对比表与 §12 调参记录已用真实模型（`deepseek-chat`）实测回填。

| 交付项 | 落点 |
|---|---|
| 通道参数与判定 | `server/app/harness/channels.py`：`CHANNEL_LIMITS` / `limits_of` / `next_channel` / `risk_allowed` / `tools_for_channel` / `result_size_exceeded` / `heuristic_route` / `graph_recursion_limit` |
| 图节点 | `server/app/agent/graph.py`：`router` / `guard` / `gate` / `escalate` / `wrap_up`，通道预算守卫并入 `authority_gate` |
| fast 单次取数 | router 结构化输出可带 `action`，由**程序**执行一次只读取数（不挂工具 schema、不进工具循环），结果回喂 agent 作答（§2/§5.4） |
| 升级触发 | 连续 DENY / 连续工具失败 / 步骤或时间耗尽 / 审批 / 结果过大（`result_too_large`）/ 模型申请（合成工具 `request_deep` → `model_requested`）/ 门控证据不足先升档（`gate_insufficient_evidence`）/ fast 单次取数失败（`fast_action_failed`） |
| 运行时事件 | `server/app/services/runtime.py`：`channel_selected` / `escalated` / `budget_exhausted` / `wrap_up`，`run_finished` 带 `channel` / `escalations` / `wrap_up` |
| 场景 MCP Server | `mcp-servers/nl2sql/server.py`：`list_tables` / `describe_table` / `run_sql` / `render_chart` / `write_report`（`run_sql` 描述内含表结构，供 router 写一次性取数） |
| 完成门控不变量 | `server/app/harness/gates.py`：`report_cites_queries` 校验**交付物正文**（`write_report` 的 markdown）与答复中的 qid，防编造数字 |
| 评测 | `server/scripts/routing_eval.py`：20 条任务 × 两轮（三档 / 全 deep 基线），输出 §3.6 表与分通道/明细 |
| 前端 | 通道选择器（自动 / fast / calc / deep，指定即锁定）、升级/预算/收尾提示条、状态条通道与升级展示 |

**与方案的偏离**：数据源由 DuckDB 改为 SQLite（项目运行环境无 DuckDB 依赖，改用 `sqlite3` + 合成电商数据，表结构与只读守卫语义不变）。该偏离只影响数据层，不影响通道与门控逻辑。

另：§2 规定 `fast` 不挂工具，因此 `result_too_large`（结果过大）实际生效于 calc→deep——触发条件与 §6 一致（单次结果超阈值即升一级），只是起始通道受"fast 无工具"约束而落在 calc。fast 的"一次确定性取数"按 §5.4 实现为 router 结构化输出 + **程序执行**（不绑工具 schema）。

**测试覆盖**：`tests/unit/test_channels.py`（13）、`tests/unit/test_gate_invariants.py`（6）、`tests/unit/test_nl2sql_guard.py`（5）、`tests/eval/test_routing.py`（13）；全量 `pytest tests` 68 passed。§11 清单中 `reuse`（跨 run 复用）与 fast 遇审批直接升级两条仍无独立用例（对应 §5.4 的 `reuse` / `plan` 两个去处尚未实现）。

