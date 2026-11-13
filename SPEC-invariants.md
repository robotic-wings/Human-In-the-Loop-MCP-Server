# HITL 不变量模型（Invariants）

这是 HITL 系统的**完整跨模块保证**。每一个不变量都必须：
1. 在这里被陈述；
2. 被至少一条**属性测试**（在状态空间上恒成立，而非单个 case）钉死；
3. 任何实现违反它，都视为 bug，而不是"缺一个 case"。

本次会话发现的 8 个症状，7 个是下面某条不变量的违反。补丁史的病根就是：
这些保证从没被当作一个整体写下来、测过，所以每个模块按局部理解实现，缝里全是洞。

## I1 身份与幂等（Identity / Idempotency）

- 每个 `(caller_id, request_id)` 恰好对应一次已记录的操作。
- 用**相同 key + 相同参数**重发 → 原样返回已记录结果（幂等重放）。
- 用**相同 key + 不同参数**重发 → `request_conflict`，绝不执行第二次。
- `request_id` 跨化身（incarnation）唯一，但在同一次化身的重放中稳定。

## I2 归属（Ownership）

- 一轮至多一个 owner；owner 在 **accept 时刻**建立，之后**不可变**。
- owner 通道 ∈ {desktop, mobile}；未 accept 的轮次无 owner。
- owner 通道决定保活语义（见 I3）。

## I3 保活 → 结算（Liveness → Settlement）

- 一个 accepted/in_progress 轮次，**当且仅当**其管辖 lease 过期时才被结算。
- desktop 归属：owner lease（被桌面任何活动刷新）过期 → `terminal_disconnected`。
- mobile 归属：**无 owner lease**（浏览器无状态，人可能离线打字）。
- agent lease：向 agent 投递 resumable heartbeat 时刷新，过期 → `agent_disconnected`。
- **mobile 归属的轮次绝不因 owner lease 过期而结算**（尤其不会 `terminal_disconnected`）。
- 时钟异常（回拨或 >1h 前跳）只作废**依赖 deadline 的**结算；mobile 轮次无 deadline，不因时钟异常被结算。

## I4 结算完备性与确定性（Settlement Completeness & Determinism）

- 时钟推进到所有 deadline 之后，任何 active 轮次**恰好**落到一个终局状态，产生**恰好一个**终局事件（不双重结算、不产生冲突 reason）。
- ringing → `not_accepted`(ring_timeout) | `unreachable` | 人/agent 动作。

## I5 忙线（Busy line）

- 同一 operator 在任一时刻**至多一个** active（ringing/accepted/in_progress）轮次。
- `busy` ⟺ 该 operator 存在 active 轮次。轮次一旦终局，忙线即释放。

## I6 投递（Delivery）

- 每个带投递意图（delivery_json）的**终局事件**和**进度事件**，在 outcome_feed 中按全局 seq **恰好出现一次**，按 caller 隔离。
- ack 标记 delivered/undeliverable；undeliverable 默认不返回，除非显式请求。
- 进度（needs_continuation=true）也进 feed，以便 detached agent 被唤醒。

## I7 路由（Routing）

- 一轮的通知只去**目标 operator** 已开启的通道。
- mobile-ineligible 的轮次绝不推 Bark。
- DND 自动 `declined(do_not_disturb)`，不产生通知、不占用忙线。

## I8 附件生命周期（Attachment lifecycle）

- 出现在已提交结果里的每个 `attachment_id`，在该结果可读期内，可被该轮次的 caller 仅凭 `attachment_id`（全局唯一）读取。
- 读取不要求 `interaction_id`（一轮一枪式完成时 interaction_id 不一定暴露给 agent）。

---

## 覆盖现状（截至本文件建立）

| 不变量 | 属性测试 | 状态 |
|---|---|---|
| I1 幂等 | test_idempotency_is_exact_across_all_operations | 已钉（核心幂等；request_id 跨化身唯一性属 hitl.ts 侧，不在核心测试范围） |
| I2 归属 | test_hitl_rewrite_spec.py | 已钉 |
| I3 保活 | test_hitl_rewrite_spec.py（mobile/desktop/agent 三分支） | 已钉 |
| I4 结算完备 | test_settlement_is_complete_and_deterministic | 已钉 |
| I5 忙线 | test_busy_line_released_by_every_ending / test_never_two_active_rounds_per_operator | 已钉 |
| I6 投递 | test_every_terminal_outcome_is_in_the_feed_exactly_once / test_progress_appears_in_feed_exactly_once_before_terminal | 已钉 |
| I7 路由 | mobile_eligibility 合同 | 已钉 |
| I8 附件 | test_hitl_attachment_continuation_fixes.py | 已钉 |
