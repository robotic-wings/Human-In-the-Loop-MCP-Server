// HITL × Chidori 工具模块（SPEC-chidori-interface.md §4.1）
//
// 消费方式（同目录 import）：
//   import { makeHitlTools } from "./hitl.ts";
//   const hitl = makeHitlTools({
//     central: "https://10.10.0.1:9443",
//     token: process.env.HITL_TOKEN!,
//     delivery: "detached",            // 生产默认：真挂起
//     agentName: "trade-analyst",
//   });
//   // 传给 prompt 的 tools 选项：tools: [...hitl.tools]
//
// detached 模式语义：工具体内 chidori.signal("hitl:<call_id>") 挂起整个 run
// （无 LLM 请求、无连接、无超时预算）；hitl-postman 桥把人的终局结果作为
// 该信号送回，run 恢复并拿到结果。
// inline 模式（调试用）：不挂起，长轮询中央直到终局，等价 MCP 语义。

import { defineTool } from "chidori:agent";

export interface HitlOptions {
  central: string;
  token: string;
  /** "detached"（默认，真挂起）或 "inline"（调试，长轮询） */
  delivery?: "detached" | "inline";
  /** detached 模式必填：本 agent 的注册名，postman 按此投递 */
  agentName?: string;
  /** 默认目标操作员呼号；省略 = 目录 primary */
  defaultCallsign?: string;
}

export interface HitlOutcome {
  success?: boolean;
  status: string;
  reason?: string | null;
  call_state?: string | null;
  interaction_id?: string;
  call_id?: string;
  operator?: string;
  result?: unknown;
  report?: string | null;
  attachments?: unknown[];
  retryable?: boolean;
  message?: string;
  retry_after_seconds?: number;
  human_action?: boolean;
  [key: string]: unknown;
}

interface CreateBody {
  request_id: string;
  payload: Record<string, unknown>;
  client_timeout_seconds: number;
  callsign?: string;
  delivery?: { kind: string; agent: string };
}

type AnyChidori = {
  signal: (name: string, opts?: { timeoutMs?: number }) => Promise<
    { name: string; payload: unknown; from: unknown } | { name: string | null; payload: null; from: null; timedOut: true }
  >;
};

async function postJson(url: string, token: string, body: unknown): Promise<Record<string, unknown>> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) throw new Error("HITL central: unauthorized (check HITL_TOKEN)");
  if (!resp.ok) throw new Error(`HITL central: HTTP ${resp.status}`);
  return (await resp.json()) as Record<string, unknown>;
}

export function makeHitlTools(opts: HitlOptions) {
  const mode = opts.delivery ?? "detached";
  if (mode === "detached" && !opts.agentName) {
    throw new Error('makeHitlTools: agentName is required in "detached" delivery mode');
  }
  const central = opts.central.replace(/\/$/, "");
  let seq = 0;
  // Uniqueness note: durable runs use a FIXED Date and seeded Math.random;
  // Math.random is seeded per run, so it differs across runs (no idempotency
  // collisions after a restart) but replays deterministically within one run.
  const nextRequestId = () => `chidori-${opts.agentName ?? "inline"}-${++seq}-${Math.random().toString(36).slice(2, 10)}`;

  // ── 核心一轮：create →（挂起或轮询）→ 终局结果 ──
  async function askHuman(
    tool: string,
    args: Record<string, unknown>,
    callsign: string | undefined,
    ch: AnyChidori,
  ): Promise<HitlOutcome> {
    const body: CreateBody = {
      request_id: nextRequestId(),
      payload: { tool, ...args },
      client_timeout_seconds: 25,
    };
    if (callsign ?? opts.defaultCallsign) body.callsign = callsign ?? opts.defaultCallsign;
    if (mode === "detached") body.delivery = { kind: "chidori-detached", agent: opts.agentName as string };

    const created = (await postJson(`${central}/mcp`, opts.token, body)) as unknown as HitlOutcome;
    // 创建即终局（busy / DND 拒绝 / 未知呼号 / 无通道）：不挂起，直接交给 LLM 决策
    if (created.status !== "heartbeat" || !created.call_id || created.needs_continuation !== true) {
      return created;
    }

    if (mode === "detached") {
      const sig = await ch.signal(`hitl:${created.call_id}`);
      if ("timedOut" in sig && sig.timedOut) {
        return { status: "delivery_error", message: "signal timed out (unexpected: no timeout set)", call_id: created.call_id };
      }
      return (sig.payload ?? { status: "delivery_error" }) as HitlOutcome;
    }

    // inline：长轮询（调试模式，等价 MCP 语义）
    let cursor = typeof created.event_seq === "number" ? (created.event_seq as number) : 0;
    let last: HitlOutcome = created;
    for (let round = 0; round < 12; round++) {
      const cont = (await postJson(`${central}/mcp`, opts.token, {
        request_id: nextRequestId(),
        interaction_id: created.interaction_id,
        call_id: created.call_id,
        after_event_seq: cursor,
        client_timeout_seconds: 25,
      })) as unknown as HitlOutcome;
      last = cont;
      if (cont.needs_continuation !== true) return cont;
      if (typeof cont.event_seq === "number") cursor = cont.event_seq as number;
    }
    return last;
  }

  // ── 工具工厂：一个 schema + 一行核心逻辑 ──
  const interactive = (
    name: string,
    description: string,
    properties: Record<string, unknown>,
    required: string[],
    argsOf: (a: Record<string, unknown>) => Record<string, unknown>,
  ) =>
    defineTool({
      name,
      description,
      parameters: {
        type: "object",
        properties: {
          ...properties,
          callsign: { type: "string", description: "Target operator callsign (from list_operators); omit for the primary operator." },
        },
        required,
      },
      run: async (args: Record<string, unknown>, ch: AnyChidori) =>
        askHuman(name, argsOf(args), typeof args.callsign === "string" ? args.callsign : undefined, ch),
    });

  const assignTaskToHuman = interactive(
    "assign_task_to_human",
    "Hand the operator one physical/real-world task and WAIT (the run suspends until the human answers): the operator accepts, executes, and reports back, and this call returns their final report.",
    { task_description: { type: "string", description: "One actionable instruction for the operator." } },
    ["task_description"],
    (a) => ({ task_description: a.task_description }),
  );
  const getUserInput = interactive(
    "get_user_input",
    "Ask the operator one short question and WAIT (suspends): returns their answer.",
    { prompt: { type: "string", description: "The question shown to the operator." }, required: { type: "boolean" } },
    ["prompt"],
    (a) => ({ prompt: a.prompt, required: a.required }),
  );
  const getUserChoice = interactive(
    "get_user_choice",
    "Offer the operator options and WAIT (suspends): returns their selection.",
    {
      choices: { type: "array", items: { type: "string" }, description: "The options to choose from." },
      prompt: { type: "string" },
      multiple: { type: "boolean" },
    },
    ["choices"],
    (a) => ({ choices: a.choices, prompt: a.prompt, multiple: a.multiple }),
  );
  const getMultilineInput = interactive(
    "get_multiline_input",
    "Ask the operator for a long free-form answer and WAIT (suspends).",
    { prompt: { type: "string" } },
    ["prompt"],
    (a) => ({ prompt: a.prompt }),
  );
  const showConfirmationDialog = interactive(
    "show_confirmation_dialog",
    "Ask the operator to confirm or deny one proposal and WAIT (suspends): returns their decision. Use before irreversible actions.",
    { prompt: { type: "string", description: "The proposal to confirm." } },
    ["prompt"],
    (a) => ({ prompt: a.prompt }),
  );
  const getFeedbackFromUser = interactive(
    "get_feedback_from_user",
    "Collect free-form feedback from the operator and WAIT (suspends).",
    { prompt: { type: "string" } },
    ["prompt"],
    (a) => ({ prompt: a.prompt }),
  );

  const cancelInteraction = defineTool({
    name: "cancel_interaction",
    description: "End one open HITL round (e.g. when the plan changed) to release the operator's line.",
    parameters: {
      type: "object",
      properties: { interaction_id: { type: "string" }, call_id: { type: "string" } },
      required: ["interaction_id", "call_id"],
    },
    run: async (args: { interaction_id: string; call_id: string }) =>
      (await postJson(`${central}/mcp`, opts.token, {
        request_id: nextRequestId(),
        operation: "cancel_interaction",
        interaction_id: args.interaction_id,
        call_id: args.call_id,
      })) as unknown as HitlOutcome,
  });

  const listOperators = defineTool({
    name: "list_operators",
    description: "List human operators (callsigns) with display names and current do-not-disturb state. Use a callsign on interactive tools.",
    parameters: { type: "object", properties: {} },
    run: async () => {
      const r = await postJson(`${central}/mcp`, opts.token, {
        jsonrpc: "2.0", id: nextRequestId(), method: "tools/call",
        params: { name: "list_operators", arguments: {} },
      });
      const result = r.result as { structuredContent?: { result?: unknown } } | undefined;
      return result?.structuredContent?.result ?? r;
    },
  });

  const getOperatorProfile = defineTool({
    name: "get_operator_profile",
    description: "Read one operator's profile (pass callsign; omit for the primary).",
    parameters: { type: "object", properties: { callsign: { type: "string" } } },
    run: async (args: { callsign?: string }) => {
      const r = await postJson(`${central}/mcp`, opts.token, {
        jsonrpc: "2.0", id: nextRequestId(), method: "tools/call",
        params: { name: "get_operator_profile", arguments: args.callsign ? { callsign: args.callsign } : {} },
      });
      const result = r.result as { structuredContent?: { result?: unknown } } | undefined;
      return result?.structuredContent?.result ?? r;
    },
  });

  return {
    tools: [
      assignTaskToHuman, getUserInput, getUserChoice, getMultilineInput,
      showConfirmationDialog, getFeedbackFromUser, cancelInteraction,
      listOperators, getOperatorProfile,
    ],
    askHuman,
  };
}
