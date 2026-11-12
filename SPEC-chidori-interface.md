# HITL × Chidori 原生接口 — 架构 SPEC

日期：2026-11-12。状态：已批准（复用正文协议方案 + §9 决策定稿）。实现语言 TypeScript + Python（中央侧小改）。

## 1. 目标

让 Chidori agent（我们可控的运行时）以**真挂起**语义联系人类：agent 的最后一个
LLM turn 发起 HITL 询问后，运行时停在信号监听点——不发起新的 LLM 请求、不持有
网络连接、不依赖任何超时预算——人的回答作为信号唤醒 run 并携带终局结果。

MCP 传输层（已实现）继续服务不受控 harness（Claude Code、Codex、Cherry Studio）。
本 SPEC 不改动 MCP 层的任何行为。

## 2. 关键机制（已在 Chidori 文档与 VPS 部署中核实）

| 机制 | 出处 | 用途 |
|---|---|---|
| `defineTool({name, description, parameters, run(args, chidori)})` | llm.txt §Defining Tools | 工具即 TS 函数，`run` 体内可用全部 host 能力 |
| `chidori.signal(name)` | llm.txt §signal；docs/signals.md | **挂起原语**：停在命名监听点，无超时则无限期；mailbox 吸收早到的投递，永不竞态 |
| `POST /agents/detached/{name}/send` `{name, payload, from}` | running-modes.md L98 | 按名投递到 detached agent 的持久邮箱，唤醒休眠 agent；服务器重启后 fleet 全体重挂 |
| 工具通过 `import` 消费 | llm.txt §Agent Shape（本地静态 import） | 消费者 TS `import { makeHitlTools } from "./hitl.ts"` |
| `CHIDORI_HTTP_ALLOW_HOSTS=*` | /etc/chidori/env（已配置） | 工具内 `fetch` 中央（10.10.0.1:9443）不被 SSRF 守卫拦截 |
| 中央 `/mcp` 正文协议（request_id/payload/cancel） | SPEC.md 既有；30+ 契约测试直打该路径 | 工具创建呼叫与取消的 REST 面，复用不新写 |

## 3. 架构

```
消费者 agent（detached，如 "trade-analyst"）
  │ import { makeHitlTools } from "./hitl.ts"
  │ tools: [...hitl.tools]
  │
  │ ① tool run(): POST 中央 /mcp（create call）→ {interaction_id, call_id}
  │ ② await chidori.signal("hitl:" + call_id)     ← 真挂起，VM 释放
  │ ③ 收到信号 → return 终局结果给 LLM
  ▼
中央服务（不变的人类侧：桌面响铃 / 手机网页 / DND / 勿扰 / 多操作员）
  │ 呼叫轮次按现有状态机推进；每次到达终局 → 追加到结果流
  ▼
hitl-postman（detached bridge agent，同台 chidori serve）
  │ 循环：GET 中央 /api/agent/outcomes?after=<cursor>   ← 新增：终局结果流
  │ 对每条 {call_id, target_agent, outcome}：
  │   POST chidori:/agents/detached/<target>/send
  │        {name: "hitl:<call_id>", payload: outcome, from: {kind:"human", id: callsign}}
  │ 游标持久化在 chidori.memory，重启续传
```

### 职责划分（与 AGENTS.md 愿景一致）

- **中央 = 持久权威**：改动收敛为三件小事——① 正文协议 create 分支补穿
  `callsign`（照抄 MCP 路径的穿参）；② `lifecycle_calls` 加 `delivery_json` 列
  （创建时随请求写入）；③ 新增**传输中立**的"终局结果流"只读端点。不知道
  Chidori 的存在，不投递，不回调。MCP 层零改动。
- **Chidori 传输层 = 两个 TS 文件**：工具模块（`hitl.ts`，消费者 import）+
  投递桥（`hitl-postman.ts`，detached agent）。Chidori 特有的知识全部在这层。
- **人侧零改动**。

## 4. 交付物

### 4.1 `hitl.ts`（工具模块，本 SPEC 的核心交付物）

```ts
import { makeHitlTools } from "./hitl.ts";

const hitl = makeHitlTools({
  central: "https://10.10.0.1:9443",
  token: process.env.HITL_TOKEN,          // agent Bearer
  delivery: "detached",                   // 见 §6 运行模式
  agentName: "trade-analyst",             // detached 名；postman 按此投递
  // verifyTLS / callsign 默认值等可选项
});

// hitl.tools: assignTaskToHuman / getUserInput / getUserChoice /
//             getMultilineInput / showConfirmationDialog / getFeedbackFromUser
//             / cancelInteraction / listOperators / getOperatorProfile
```

工具 `run` 体的行为：

1. **create**：POST 中央 `/mcp` 正文协议（既有面，复用；request_id 用
   `chidori-{agentName}-{序号}` 幂等 id）。请求体新增两个字段，对应中央侧补丁：
   `callsign`（目标操作员，可选）与 `delivery`（`{"kind":"chidori-detached",
   "agent":"trade-analyst"}`，原样存入 `delivery_json` 列——postman 从结果流读它）。
   - `busy`：立即返回给 LLM（带 retry_after），**不挂起**。
   - `unknown_callsign` / `declined(do_not_disturb)` 等创建即终局：同上，不挂起。
2. **suspend**：`await chidori.signal("hitl:" + call_id)`，无 timeoutMs。
3. **resume**：信号 payload 即终局结果（§5 结构）。工具直接 return 它；
   cancelled/not_accepted/declined 同为正常返回值，由 LLM 决策后续——
   与 MCP 层的语义完全一致。

### 4.2 `hitl-postman.ts`（投递桥，detached agent）

- keep_alive detached agent；主循环 `chidori.alarm(poll_seconds)` 驱动。
- 每轮：GET `/api/agent/outcomes?after=<cursor>&limit=100`（agent Bearer）→
  逐条 send 到目标 agent → 成功后推进游标（`chidori.memory` 持久化）。
- 投递幂等：信号名 `hitl:<call_id>` 全局唯一；目标 agent 每个呼叫恰好监听一次。
  Chidori 侧 409（terminal）或重复投递均安全（mailbox 只会被消费一次）。
- **投递失败（目标 agent 已注销 / 连续 5 次 4xx）**：标记该条 `undeliverable`
  并推进游标——绝不阻塞全流；`GET /api/agent/outcomes?undeliverable=1` 可查。
- `from` 字段携带人的 callsign，journal 记录归因。

### 4.3 中央新增：`GET /api/agent/outcomes`（终局结果流）

- 认证：agent Bearer（同 `/mcp`）。
- 请求：`after`（游标 = 已见最大 outcome 序号）、`limit`。
- 响应：`[{seq, call_id, interaction_id, operator, tool, delivery, outcome},
  ...]`，`outcome` 为该呼叫终局事件的完整结构化结果（answered/completed/
  declined/cancelled/not_accepted/disconnected/unreachable）+ `next_after`。
- 实现要点：`seq` 取自 `lifecycle_events` 全局单调序（按 event rowid 派生即可，
  不引入新表）；只暴露**终局**事件（needs_continuation=false 的那批）；
  每条附该呼叫的 `delivery` 元数据（`delivery_json` 列）。
  游标语义 = 至少一次投递 + 幂等消费（两将军纪律：事实在日志里，流只是读视图）。
  投递失败标记由 postman 回写（`POST /api/agent/outcomes/ack`）——中央侧回写，
  便于 `undeliverable=1` 查询。

## 5. 终局结果结构（信号 payload）

```json
{
  "interaction_id": "…", "call_id": "…", "operator": "kefan",
  "status": "completed",            // 与 MCP 层同一套状态词表
  "human_action": true, "reason": null,
  "result": { … }, "report": "…", "attachments": [ … ],
  "retryable": false, "message": ""
}
```

字段与 MCP `structuredContent` 的终局分支同构——单一事实源（事件日志），
两个传输层只是视图。

## 6. 运行模式（工具工厂参数）

| 模式 | 语义 | 场景 |
|---|---|---|
| `detached`（默认） | signal 挂起 + postman 投递 | 生产：fleet 里的员工式 agent |
| `inline` | 不挂起；create 后长轮询中央（复用 MCP 等待语义），拿终局才返回 | `chidori run` 本地调试（裸 run 下 signal 会终止进程）；CI 冒烟 |

`inline` 是显式选择的兼容模式，文档里明确标注"不挂起，等价 MCP"。

## 7. 边界与不做的

- 不做 Chidori 内的第二个 MCP 服务器。
- 中央不做主动回调/webhook 推送（v1 拉流足够；push 留给未来且仍传输中立）。
- 不做 agent 侧对 `opened`/`in_progress` 中间事件的订阅（挂起语义下无意义；
  若未来需要，同一个流端点天然支持）。
- 不改 MCP 层、桌面终端、手机网页、多操作员模型。

## 8. 验证计划

1. **spike**（写正式代码前）：最小 agent 验证 `chidori.signal()` 在 tool run 体内
   可挂起整 run、`/agents/detached/{name}/send` 可唤醒（本地 `chidori run` 用
   alarm+pollSignal 模拟，或直接在 VPS fleet 上小规模试）。
2. 中央结果流端点：单元/契约测试（游标推进、终局过滤、鉴权、幂等重读）。
3. `inline` 模式端到端：本地 central + obedient-agent 式脚本断言单调用拿终局。
4. `detached` 模式端到端（VPS）：fleet 注册消费者 + postman → 真人接听/拒绝/
   勿扰/不接四种路径，各验证一次信号唤醒与 payload 正确。
5. 回归：385 项既有测试全绿；MCP 层行为无 diff。

## 9. 已决事项（原开放问题，2026-11-12 定稿）

- postman 投递失败（目标已注销/持续 4xx）：**跳过并标记 undeliverable，游标推进**，
  绝不阻塞全流；管理可查（`?undeliverable=1`），postman 通过
  `POST /api/agent/outcomes/ack` 回写标记。
- `delivery` 元数据：**独立列 `delivery_json`**，创建时随请求写入，随结果流读出。
- 正文协议 create 分支补穿 `callsign`（与 MCP 路径对齐）；MCP 层零改动。
