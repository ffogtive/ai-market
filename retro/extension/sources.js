// Collectors for the retro extension. Everything here runs with the user's own
// logged-in browser session and only reads; results stay on this computer.
//
// ChatGPT and Claude.ai have no public API for chat history. These use the same
// JSON endpoints their web apps load, so they can change without notice — each
// collector fails soft and reports why instead of breaking the whole run.

const MAX_TEXT = 600;
const MAX_CONVERSATIONS = 40; // detail requests per source per run

export function clip(text, n = MAX_TEXT) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  return t.length <= n ? t : t.slice(0, n - 1) + "…";
}

// ISO 8601 with the local UTC offset, matching collect.py's events.
export function localIso(date) {
  const pad = (n) => String(Math.abs(Math.trunc(n))).padStart(2, "0");
  const off = -date.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `${sign}${pad(off / 60)}:${pad(off % 60)}`
  );
}

// Accepts ISO strings, epoch seconds (ChatGPT) or epoch milliseconds.
export function toDate(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number") return new Date(value < 1e12 ? value * 1000 : value);
  const d = new Date(value);
  return isNaN(d) ? null : d;
}

export function event(source, date, text, { project = "" } = {}) {
  return { source, ts: localIso(date), project, text: clip(text), actor: "human", host: "browser" };
}

function inRange(date, start, end) {
  return date && date >= start && date < end;
}

// ---------------------------------------------------------------- Claude.ai
export function claudeEvents(conversation, start, end, mode) {
  const title = conversation.name || "(제목 없음)";
  const out = [];
  for (const m of conversation.chat_messages || []) {
    if (m.sender !== "human") continue;
    const at = toDate(m.created_at);
    if (!inRange(at, start, end)) continue;
    const text =
      m.text || (m.content || []).filter((c) => c.type === "text").map((c) => c.text).join(" ");
    out.push(event("claude.ai", at, mode === "titles" ? `[${title}]` : `[${title}] ${text}`));
  }
  return out;
}

export async function collectClaude(fetchJson, start, end, mode) {
  const orgs = await fetchJson("https://claude.ai/api/organizations");
  const events = [];
  for (const org of orgs || []) {
    const list = await fetchJson(`https://claude.ai/api/organizations/${org.uuid}/chat_conversations?limit=100`);
    const today = (list || [])
      .filter((c) => (toDate(c.updated_at) || 0) >= start)
      .slice(0, MAX_CONVERSATIONS);
    for (const c of today) {
      const detail = await fetchJson(
        `https://claude.ai/api/organizations/${org.uuid}/chat_conversations/${c.uuid}?tree=True&rendering_mode=messages`
      );
      events.push(...claudeEvents({ ...detail, name: detail?.name ?? c.name }, start, end, mode));
    }
  }
  return events;
}

// ---------------------------------------------------------------- ChatGPT
export function chatgptEvents(conversation, start, end, mode) {
  const title = conversation.title || "(제목 없음)";
  const out = [];
  for (const node of Object.values(conversation.mapping || {})) {
    const m = node && node.message;
    if (!m || m.author?.role !== "user") continue;
    const at = toDate(m.create_time);
    if (!inRange(at, start, end)) continue;
    const parts = (m.content?.parts || []).filter((p) => typeof p === "string");
    if (!parts.length && mode !== "titles") continue; // image-only or tool payloads
    out.push(event("chatgpt", at, mode === "titles" ? `[${title}]` : `[${title}] ${parts.join(" ")}`));
  }
  return out.sort((a, b) => a.ts.localeCompare(b.ts));
}

export async function collectChatGPT(fetchJson, start, end, mode) {
  const session = await fetchJson("https://chatgpt.com/api/auth/session");
  if (!session?.accessToken) throw new Error("ChatGPT에 로그인되어 있지 않음");
  const auth = { Authorization: `Bearer ${session.accessToken}` };
  const list = await fetchJson(
    "https://chatgpt.com/backend-api/conversations?offset=0&limit=50&order=updated",
    auth
  );
  const recent = (list?.items || [])
    .filter((c) => (toDate(c.update_time) || 0) >= start)
    .slice(0, MAX_CONVERSATIONS);
  const events = [];
  for (const c of recent) {
    const detail = await fetchJson(`https://chatgpt.com/backend-api/conversation/${c.id}`, auth);
    events.push(...chatgptEvents({ ...detail, title: detail?.title ?? c.title }, start, end, mode));
  }
  return events;
}

// ---------------------------------------------------------------- browsing history
export function historyEvents(visits, titleByUrl) {
  const out = [];
  let last = "";
  for (const v of visits.sort((a, b) => a.visitTime - b.visitTime)) {
    const url = v.url;
    const host = url.replace(/^https?:\/\/(www\.)?/, "").split("/")[0];
    // our own collection traffic is not the user's browsing
    if (!/^https?:/.test(url) || /\/(backend-api|api)\//.test(url)) continue;
    const label = titleByUrl[url] || url;
    const key = host + "|" + label; // collapse reloads and redirects of the same page
    if (key === last) continue;
    last = key;
    out.push(event("chrome", new Date(v.visitTime), label, { project: host }));
  }
  return out;
}
