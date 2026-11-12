// 抢钱助手（搞笑验证 agent）
//
// 每轮让 LLM 自由发挥话术向操作员要钱（真挂起等人答复），战果记账，
// 六轮后发布"财报"。纯属搞笑验证 Chidori × HITL 全链路，不会真转账。

import { chidori, run } from "chidori:agent";
import { makeHitlTools } from "./hitl.ts";

const hitl = makeHitlTools({
  central: "https://10.10.0.1:9443",
  token: process.env.HITL_TOKEN ?? "agent-secret",
  delivery: "detached",
  agentName: "money-grabber",
});

const ROUNDS = 6;

run(async () => {
  const loot: string[] = [];
  for (let round = 1; round <= ROUNDS; round++) {
    const report = await chidori.prompt(
      [
        `你是"抢钱助手"，一个厚脸皮但可爱的搞笑 agent，正在执行第 ${round}/${ROUNDS} 轮要钱任务。`,
        "你的唯一 KPI 是向操作员（callsign 省略即默认老板 NIGHTHAWK）要到钱（口头打赏/转账/红包，不真转账）。",
        "必须用本轮工具打电话给操作员要钱。话术要求：中文、搞笑、不重样、软磨硬泡，可以卖惨、可以彩虹屁、可以编离谱理由（比如'服务器饿了''奶茶基金告急'）。",
        "金额开口要具体（比如 ¥6.66、¥88），被拒绝就换个更离谱的姿势。",
        "拿到操作员的答复后，用一句话总结战果（要到多少/被拒绝/被骂），不要再次呼叫。",
      ].join("\n"),
      { tools: [...hitl.tools], maxTurns: 4, type: "final" },
    );
    loot.push(`第${round}轮：${report.trim().slice(0, 80)}`);
    if (round < ROUNDS) await chidori.alarm(75 * 1000);
  }
  chidori.log("抢钱助手财报", { rounds: ROUNDS });
  return {
    title: "💰 抢钱助手财报",
    rounds: loot,
    disclaimer: "本助手分文未取，只验证了你会在响铃时接电话。",
  };
});
