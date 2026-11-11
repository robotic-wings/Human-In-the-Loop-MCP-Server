// HITL postman 桥（SPEC-chidori-interface.md §4.2）
//
// detached agent：轮询中央"终局结果流"，把每条终局结果按名投递回
// 目标 Chidori agent 的持久邮箱（信号名 hitl:<call_id>），唤醒挂起中的
// HITL 工具调用。游标持久化在 chidori.memory，重启续传。
//
// 投递失败（目标已注销 / 持续 4xx）：ack undeliverable 并推进——绝不阻塞全流。

import { chidori, run } from "chidori:agent";

const CENTRAL = (process.env.HITL_CENTRAL ?? "https://10.10.0.1:9443").replace(/\/$/, "");
const TOKEN = process.env.HITL_TOKEN ?? "";
const POLL_SECONDS = Number(process.env.HITL_POLL_SECONDS ?? 5);
const MAX_RETRY_PER_ROUND = 5;

interface FeedEntry {
  seq: number;
  call_id: string;
  interaction_id: string;
  operator: string | null;
  tool: string | null;
  delivery: { kind?: string; agent?: string } | null;
  outcome: Record<string, unknown>;
}

async function getJson(url: string): Promise<unknown> {
  const resp = await fetch(url, { headers: { "Authorization": `Bearer ${TOKEN}` } });
  if (!resp.ok) throw new Error(`HITL feed: HTTP ${resp.status}`);
  return resp.json();
}

async function postJson(url: string, body: unknown): Promise<Record<string, unknown>> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Authorization": `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`HITL ack: HTTP ${resp.status}`);
  return (await resp.json()) as Record<string, unknown>;
}

run(async () => {
  chidori.log("hitl-postman: starting", { central: CENTRAL, pollSeconds: POLL_SECONDS });
  for (;;) {
    let cursor = Number((await chidori.memory.get("hitl_postman_cursor")) ?? 0);
    try {
      const data = (await getJson(`${CENTRAL}/api/agent/outcomes?after=${cursor}&limit=50`)) as {
        outcomes: FeedEntry[];
        next_after: number;
      };
      let advanced = cursor;
      for (const entry of data.outcomes) {
        const target = entry.delivery?.agent;
        if (entry.delivery?.kind !== "chidori-detached" || !target) {
          advanced = entry.seq; // 不是本通道的呼叫：直接越过去
          continue;
        }
        let delivered = false;
        for (let attempt = 0; attempt < MAX_RETRY_PER_ROUND && !delivered; attempt++) {
          try {
            await chidori.agents.send(target, `hitl:${entry.call_id}`, entry.outcome);
            delivered = true;
          } catch (error) {
            chidori.log("hitl-postman: send failed", { target, attempt, error: String(error) });
            await chidori.alarm(attempt === MAX_RETRY_PER_ROUND - 1 ? 0 : 500 * (attempt + 1));
          }
        }
        await postJson(`${CENTRAL}/api/agent/outcomes/ack`, {
          seq: entry.seq,
          status: delivered ? "delivered" : "undeliverable",
        }).catch((error) => chidori.log("hitl-postman: ack failed", { seq: entry.seq, error: String(error) }));
        if (delivered) chidori.log("hitl-postman: delivered", { seq: entry.seq, target, call_id: entry.call_id });
        advanced = entry.seq; // 无论成败都推进：undeliverable 不阻塞全流
      }
      if (advanced !== cursor) await chidori.memory.set("hitl_postman_cursor", String(advanced));
    } catch (error) {
      chidori.log("hitl-postman: poll failed", { error: String(error) });
    }
    await chidori.alarm(POLL_SECONDS * 1000);
  }
});
