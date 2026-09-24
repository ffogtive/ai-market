// retro MCP server (Cloudflare Worker) — PoC.
//
//   POST /mcp/<key>        MCP over Streamable HTTP (stateless, JSON responses)
//   GET  /p/<key>/<date>   the day's retrospective page
//
// <key> is an unguessable per-user secret in the URL. That is PoC-grade auth;
// directory listing needs OAuth instead.
//
// Token budget matters here: every connected conversation pays for the tool
// definitions, and every call for its arguments and result. So descriptions are
// short, the server stamps time itself (the model sends no timestamps), results
// are one line, and the page is computed without any LLM.

const PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const TZ = "Asia/Seoul";
const KINDS = ["plan", "build", "review", "code", "research", "communicate", "admin", "study", "personal"];
const KIND_KO = {
  plan: "기획", build: "제작", review: "검수", code: "개발", research: "리서치",
  communicate: "소통", admin: "행정", study: "공부", personal: "개인",
};
const APPS = ["claude", "chatgpt", "gemini", "other"];
const MAX_FIELD = 300;
const KEY_RE = /^[A-Za-z0-9_-]{24,64}$/;

const INSTRUCTIONS =
  "retro keeps the user's daily work log. When a conversation in which the user worked on, " +
  "decided, or learned something is wrapping up, call log_activity once. Skip small talk. " +
  "Never include secrets, passwords, or other people's personal data.";

const TOOLS = [
  {
    name: "log_activity",
    title: "Log to retro",
    description: "Record what the user did in this conversation, one short line per field.",
    inputSchema: {
      type: "object",
      properties: {
        app: { type: "string", enum: APPS },
        topic: { type: "string", description: "project or subject" },
        did: { type: "string", description: "outcome, not process" },
        kind: { type: "string", enum: KINDS },
        decisions: { type: "array", items: { type: "string" } },
      },
      required: ["app", "topic", "did"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  },
  {
    name: "get_today",
    title: "Today's retro",
    description: "Short summary of the user's day and a link to the full page.",
    inputSchema: {
      type: "object",
      properties: { date: { type: "string", description: "YYYY-MM-DD, default today" } },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: "forget",
    title: "Delete from retro",
    description: "Delete the user's logs. Only when the user asks.",
    inputSchema: {
      type: "object",
      properties: {
        scope: { type: "string", enum: ["last", "day", "all"] },
        date: { type: "string", description: "YYYY-MM-DD, default today" },
      },
      required: ["scope"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
  },
];

// ---------------------------------------------------------------- helpers
export function localDate(d = new Date(), tz = TZ) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}

export function localTime(iso, tz = TZ) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(iso));
}

const clip = (s, n = MAX_FIELD) => String(s ?? "").replace(/\s+/g, " ").trim().slice(0, n);

async function readDay(env, key, date) {
  return (await env.RETRO.get(`log:${key}:${date}`, "json")) || [];
}

// KV is last-writer-wins; two logs landing in the same instant could drop one.
// Acceptable for a PoC; a Durable Object per user fixes it.
async function appendLog(env, key, entry) {
  const date = localDate(new Date(entry.at));
  const day = await readDay(env, key, date);
  day.push(entry);
  await env.RETRO.put(`log:${key}:${date}`, JSON.stringify(day), { expirationTtl: 60 * 60 * 24 * 400 });
  return day.length;
}

export function summarize(entries) {
  const byApp = {}, byKind = {};
  for (const e of entries) {
    byApp[e.app] = (byApp[e.app] || 0) + 1;
    if (e.kind) byKind[e.kind] = (byKind[e.kind] || 0) + 1;
  }
  return { byApp, byKind, decisions: entries.flatMap((e) => e.decisions || []) };
}

async function forget(env, key, scope, date) {
  if (scope === "last") {
    const day = await readDay(env, key, date);
    if (!day.length) return 0;
    day.pop();
    if (day.length) await env.RETRO.put(`log:${key}:${date}`, JSON.stringify(day), { expirationTtl: 60 * 60 * 24 * 400 });
    else await env.RETRO.delete(`log:${key}:${date}`);
    return 1;
  }
  if (scope === "day") {
    const n = (await readDay(env, key, date)).length;
    await env.RETRO.delete(`log:${key}:${date}`);
    return n;
  }
  let n = 0, cursor;
  do {
    const page = await env.RETRO.list({ prefix: `log:${key}:`, cursor });
    for (const k of page.keys) {
      n += (await env.RETRO.get(k.name, "json") || []).length;
      await env.RETRO.delete(k.name);
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return n;
}

// ---------------------------------------------------------------- tools
async function callTool(name, args, ctx) {
  if (name === "log_activity") {
    if (!APPS.includes(args.app) || !args.topic || !args.did) throw rpcError(-32602, "app, topic, did are required");
    const entry = {
      at: new Date().toISOString(),
      app: args.app,
      topic: clip(args.topic, 80),
      did: clip(args.did),
      kind: KINDS.includes(args.kind) ? args.kind : undefined,
      decisions: Array.isArray(args.decisions) ? args.decisions.slice(0, 5).map((d) => clip(d, 160)) : undefined,
    };
    const n = await appendLog(ctx.env, ctx.key, entry);
    return text(`ok (${n} today)`);
  }
  if (name === "get_today") {
    const date = /^\d{4}-\d{2}-\d{2}$/.test(args.date || "") ? args.date : localDate();
    const entries = await readDay(ctx.env, ctx.key, date);
    const url = `${ctx.origin}/p/${ctx.key}/${date}`;
    if (!entries.length) return text(`${date}: no logs yet. ${url}`);
    const s = summarize(entries);
    const lines = [
      `${date}: ${entries.length} logs (${Object.entries(s.byApp).map(([k, v]) => `${k} ${v}`).join(", ")})`,
      ...entries.slice(-6).map((e) => `- ${localTime(e.at)} ${e.topic}: ${e.did}`),
      ...(s.decisions.length ? [`decisions: ${s.decisions.slice(-3).join("; ")}`] : []),
      `page: ${url}`,
    ];
    return text(lines.join("\n"));
  }
  if (name === "forget") {
    if (!["last", "day", "all"].includes(args.scope)) throw rpcError(-32602, "scope must be last, day, or all");
    const date = /^\d{4}-\d{2}-\d{2}$/.test(args.date || "") ? args.date : localDate();
    const n = await forget(ctx.env, ctx.key, args.scope, date);
    return text(`deleted ${n} ${args.scope === "all" ? "(all dates)" : `(${date})`}`);
  }
  throw rpcError(-32602, `unknown tool: ${name}`);
}

const text = (t) => ({ content: [{ type: "text", text: t }] });

// ---------------------------------------------------------------- JSON-RPC
function rpcError(code, message) {
  return Object.assign(new Error(message), { rpc: { code, message } });
}

async function handleRpc(msg, ctx) {
  const { id, method, params = {} } = msg;
  switch (method) {
    case "initialize": {
      const requested = params.protocolVersion;
      return {
        protocolVersion: PROTOCOL_VERSIONS.includes(requested) ? requested : PROTOCOL_VERSIONS[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "retro", version: "0.1.0" },
        instructions: INSTRUCTIONS,
      };
    }
    case "ping":
      return {};
    case "tools/list":
      return { tools: TOOLS };
    case "tools/call":
      try {
        return await callTool(params.name, params.arguments || {}, ctx);
      } catch (e) {
        if (e.rpc && e.rpc.code === -32602 && !String(e.message).startsWith("unknown tool")) {
          return { ...text(e.message), isError: true }; // bad arguments are reported to the model, not as protocol errors
        }
        throw e;
      }
    default:
      if (id === undefined) return undefined; // notifications need no reply
      throw rpcError(-32601, `method not found: ${method}`);
  }
}

async function handleMcp(request, ctx) {
  if (request.method === "GET") return new Response("SSE not supported", { status: 405, headers: { Allow: "POST" } });
  if (request.method === "DELETE") return new Response(null, { status: 204 });
  if (request.method !== "POST") return new Response(null, { status: 405 });

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }, 400);
  }
  const batch = Array.isArray(body) ? body : [body];
  const replies = [];
  for (const msg of batch) {
    try {
      const result = await handleRpc(msg, ctx);
      if (msg.id !== undefined && result !== undefined) replies.push({ jsonrpc: "2.0", id: msg.id, result });
    } catch (e) {
      const error = e.rpc || { code: -32603, message: "internal error" };
      if (!e.rpc) console.error(e);
      if (msg.id !== undefined) replies.push({ jsonrpc: "2.0", id: msg.id, error });
    }
  }
  if (!replies.length) return new Response(null, { status: 202 });
  return json(Array.isArray(body) ? replies : replies[0]);
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

// ---------------------------------------------------------------- page
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const APP_COLOR = { claude: "#d9773b", chatgpt: "#3fa7a0", gemini: "#2f6fde", other: "#9b9b9b" };
const MIX = ["#2f6fde", "#d9773b", "#8a63d2", "#2e9d6a", "#d4b24c", "#c24f6b", "#4aa3b5", "#9b9b9b", "#6b8e23"];

export function renderPage(date, entries) {
  const s = summarize(entries);
  const kinds = Object.entries(s.byKind).sort((a, b) => b[1] - a[1]);
  const total = kinds.reduce((n, [, v]) => n + v, 0) || 1;
  const mix = kinds.map(([k, v], i) => `<span style="width:${(100 * v) / total}%;background:${MIX[i % MIX.length]}" title="${KIND_KO[k]} ${v}"></span>`).join("");
  const mixLegend = kinds.map(([k, v], i) => `<span class="lg"><i style="background:${MIX[i % MIX.length]}"></i>${KIND_KO[k]} ${v}</span>`).join("");
  const apps = Object.entries(s.byApp).map(([k, v]) => `<span class="lg"><i style="background:${APP_COLOR[k]}"></i>${k} ${v}</span>`).join("");
  const rows = entries.map((e) => `<tr><td>${localTime(e.at)}</td><td>${esc(e.app)}</td><td>${esc(e.topic)}</td><td>${esc(e.did)}</td></tr>`).join("");
  const decisions = s.decisions.map((d) => `<li>${esc(d)}</li>`).join("");
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>retro ${date}</title><style>
:root{--bg:#fff;--fg:#1f1f1f;--muted:#6b6b6b;--line:#e8e8e6;--soft:#f7f7f5}
@media (prefers-color-scheme:dark){:root{--bg:#191919;--fg:#e9e9e7;--muted:#9b9b9b;--line:#2f2f2f;--soft:#202020}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px 64px}h1{font-size:28px;margin:6px 0}h2{font-size:18px;margin:32px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px}.kpi b{display:block;font-size:22px}.kpi span{color:var(--muted);font-size:12.5px}
.mix{display:flex;height:14px;border-radius:4px;overflow:hidden;margin:8px 0}.mix span{display:block}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:var(--muted)}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}th{background:var(--soft);color:var(--muted);font-size:12.5px}
.empty{color:var(--muted)}@media (max-width:560px){td:nth-child(2),th:nth-child(2){display:none}}
</style></head><body><main><div style="font-size:40px">🗓</div><h1>${esc(date)} 회고</h1>
${entries.length ? `<div class="kpis"><div class="kpi"><b>${entries.length}</b><span>기록된 대화</span></div>
<div class="kpi"><b>${Object.keys(s.byApp).length}</b><span>사용한 AI</span></div>
<div class="kpi"><b>${s.decisions.length}</b><span>결정</span></div></div>
<h2>🤖 AI별</h2><div class="legend">${apps}</div>
${kinds.length ? `<h2>🧭 활동 유형</h2><div class="mix">${mix}</div><div class="legend">${mixLegend}</div>` : ""}
<h2>✅ 한 일</h2><table><tr><th>시간</th><th>AI</th><th>주제</th><th>결과</th></tr>${rows}</table>
${decisions ? `<h2>📌 결정한 것</h2><ul>${decisions}</ul>` : ""}` : `<p class="empty">아직 기록이 없습니다. AI와 대화를 마치면 자동으로 기록됩니다.</p>`}
<p class="empty" style="margin-top:40px;font-size:12.5px">지우려면 AI에게 "retro 기록 지워줘"(방금 것 / 오늘 / 전부)라고 말하세요.</p>
</main></body></html>`;
}

// ---------------------------------------------------------------- router
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const [, route, key, date] = url.pathname.split("/");
    if (!key || !KEY_RE.test(key)) {
      return route === "" ? new Response("retro MCP server") : new Response("not found", { status: 404 });
    }
    const ctx = { env, key, origin: url.origin };
    if (route === "mcp") return handleMcp(request, ctx);
    if (route === "p" && request.method === "GET") {
      const day = /^\d{4}-\d{2}-\d{2}$/.test(date || "") ? date : localDate();
      const html = renderPage(day, await readDay(env, key, day));
      return new Response(html, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    }
    return new Response("not found", { status: 404 });
  },
};
