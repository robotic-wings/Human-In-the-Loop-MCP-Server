# HITL 重构 SPEC —— 一轮呼叫的生命周期模型

> 目标：把「一轮呼叫」从"散列 deadline 字段 + `_settle_due` 补丁"重建成一个显式、
> 可证明的生命周期模型。本文档是重构的单一权威；实现不得偏离，偏离需先改本文档。

## 1. 病根（现状为什么反复漏场景）

`lifecycle_calls` 上现在挂着四类 deadline 字段，各自在 `_settle_due` 里一段 if/else：

| 字段 | 语义 | 谁刷新 |
|---|---|---|
| `ring_deadline` | 邀请超时 | 无人（只在 create 设一次） |
| `terminal_disconnect_deadline` | "桌面终端还活着吗" | 仅桌面侧事件（heartbeat/state-sync/submit/draft） |
| `agent_reconnect_deadline` | "agent 还在吗" | 仅读路径（`response_delivered`） |
| `terminal_last_seen_at` | 簿记 | 同上 |

归属被**三套字段同时表达**：`owner_device_id` / `owner_session_id`（桌面）+ `handled_on`（desktop/iphone_web）+ `invited_desktop_id`（路由）。

**没有一个统一的"这一轮是谁的、靠什么保活、怎么结束"的模型**，于是：

- 手机端接单（owner = 浏览器会话）却借用了"桌面终端"的 `terminal_disconnect_deadline`，接单时武装成 `now+180s` 后**无人刷新** → 手机打字超过 3 分钟被误判 `terminal_disconnected`（本次事故）。
- agent 中途消失的 accepted 轮次因 deadline 只在读路径武装 → 永不结算（money-grabber 挂 40 分钟那次）。
- 每次补一个场景 = 加一列 + 加一段分支。

## 2. 目标模型

### 2.1 状态机（保留 `central_call_machine`，几乎不动）

`CallMachine` 的转移表 + 事件总线是干净的，保留。状态集不变。

### 2.2 归属（Ownership）—— 单一事实来源

一轮在 **accept** 时刻获得唯一归属，之后不变：

- `owner_channel`: `"desktop"` | `"mobile"`（accept 前为 `NULL`）
- `owner_id`: 桌面设备 id 或浏览器会话 id

**删除** `owner_device_id` / `owner_session_id` / `handled_on` / `terminal_platform` 的重复表达，
合并为 `owner_channel` + `owner_id`（`terminal_platform` 作为只读展示字段保留，可由 channel 推导）。
`invited_desktop_id` / `mobile_eligible` / `operator_callsign` 是 create 时的路由决策，保留。

### 2.3 保活（Liveness / Lease）—— 按通道各一份，语义先于字段

一轮的保活由**两个正交的 lease** 表达，二者独立、可组合、可各自测试：

**A. 归属通道 lease（`owner_lease_deadline`）** —— 只在持久通道存在

- `desktop`：由该桌面设备的**任何活动**刷新（heartbeat / state-sync / submit / draft）。过期 → `disconnected`（reason `terminal_disconnected`）。
- `mobile`：**没有 lease**。浏览器是无状态的——人可能离线打字 10 分钟再回来提交，不存在"断线"概念。mobile 轮次只由「人提交 / 人取消 / agent lease 过期」终结。

> 这就是本次 bug 的根：mobile 轮次不该有桌面语义的 lease。

**B. agent lease（`agent_lease_deadline`）** —— 只对 inline/长轮询 agent 存在

- 由「向 agent 投递一个 resumable heartbeat」刷新（即 `response_delivered` 的语义，统一为一条规则）。
- 过期 → `disconnected`（reason `agent_disconnected`）。
- **detached 投递（chidori）没有 agent lease**：agent 不在轮询，投递活性由 postman 的 ack/undeliverable 表达，与轮次结算解耦。

### 2.4 结算（Settlement）—— 一张确定性规则表

取代 `_settle_due` 的 if/else。对每个 active 轮次，结算 = 求这张表：

| state | 条件 | 事件 |
|---|---|---|
| ringing | now ≥ `ring_deadline` | `ring_expire` → not_accepted |
| ringing | 无可用通道（create 时判） | `unreachable` |
| accepted / in_progress | `owner_channel=desktop` 且 now ≥ `owner_lease_deadline` | `disconnect` → disconnected (terminal_disconnected) |
| accepted / in_progress | 有 `agent_lease_deadline` 且 now ≥ 它 | `disconnect` → disconnected (agent_disconnected) |
| 任意 | clock 回拨异常 | `clock_expire`（沿用现有语义） |

结算由周期调度器驱动（沿用现有 deadline scheduler），并幂等：重放不重复结算。

### 2.5 忙线 / DND / quiet —— 归属算子

- 忙线 = 该 operator 存在 active round（不变，但收敛为对 `owner`/`state` 的一次查询）。
- DND / quiet 窗口保持，归属按 operator。

## 3. 组件重整

- **删除 legacy**：`central_call_coordinator.py`、`central_terminal_call_service.py`、`central_terminal_http_service.py`（入口已无引用；合同测试早已声明"旧 web coordinator 不是可接受的替代"）。
- `central_http_service.py` 收敛为「配置解析 + 组合 + 移动端渲染」，去掉对 legacy coordinator 的依赖。
- `central_iphone_response_web.py` 保留为纯渲染。
- `hitl_transport.py` / `hitl_mcp.py` 保持薄视图。
- 桌面终端、postman（Python + chidori hitl.ts）：随新模型微调（尤其 mobile keepalive 无需新增——mobile 无 lease；postman 的投递活性已独立）。

## 4. 安全网（先红后绿）

1. 现有 **454 测试**（合同 + 集成）是基线。
2. 新增**表征测试**钉死当前（含错误）行为，作为重写的对照：
   - mobile 打字超时被误杀 `terminal_disconnected`（本次事故，先红）。
   - agent 消失的 accepted 轮次永不结算（先红）。
   - 其余 deadline/结算语义逐条钉死。
3. 每步红-绿；重写完成时删除"钉错误行为"的测试，换成"钉正确行为"。

## 5. 交付顺序

1. ✅ SPEC（本文档）
2. 表征测试（先红，含 §4 两条事故）
3. `hitl_core` 归属/lease/结算重写（绿）
4. 删除 legacy 三件套 + 收敛 http_service
5. 终端 / postman / chidori hitl.ts 微调
6. 全量 + 生产 E2E + 部署

## 6. 显式非目标

- 不重写 `central_call_machine` 的转移表（它是对的）。
- 不改 MCP 工具面（工具名/参数/语义对外不变）。
- 不动 wire 协议（/mcp、/api/* 路径与形状不变，除非为修 §4 事故必须）。
