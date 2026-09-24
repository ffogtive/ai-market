import { collectChatGPT, collectClaude, historyEvents } from "./sources.js";

const PERIOD_MINUTES = 60;
const MAX_HISTORY_URLS = 800;

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("collect", { periodInMinutes: PERIOD_MINUTES, delayInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener((a) => a.name === "collect" && collectToday());
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg === "collect") {
    collectToday().then(reply);
    return true; // async reply
  }
});

async function fetchJson(url, headers = {}) {
  const res = await fetch(url, { credentials: "include", headers });
  if (res.status === 401 || res.status === 403) throw new Error("로그인 필요");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function browsingHistory(start, end) {
  const items = await chrome.history.search({
    text: "", startTime: start.getTime(), endTime: end.getTime(), maxResults: MAX_HISTORY_URLS,
  });
  const titleByUrl = Object.fromEntries(items.map((i) => [i.url, i.title]));
  const visits = [];
  for (const item of items) {
    for (const v of await chrome.history.getVisits({ url: item.url })) {
      if (v.visitTime >= start.getTime() && v.visitTime < end.getTime()) visits.push({ ...v, url: item.url });
    }
  }
  return historyEvents(visits, titleByUrl);
}

async function collectToday() {
  const { mode = "questions", sources = { chatgpt: true, claude: true, history: true } } =
    await chrome.storage.local.get(["mode", "sources"]);
  const end = new Date();
  const start = new Date(end.getFullYear(), end.getMonth(), end.getDate());
  const jobs = {
    chatgpt: () => collectChatGPT(fetchJson, start, end, mode),
    claude: () => collectClaude(fetchJson, start, end, mode),
    history: () => browsingHistory(start, end),
  };
  const events = [];
  const status = {};
  for (const [name, job] of Object.entries(jobs)) {
    if (!sources[name]) { status[name] = "꺼짐"; continue; }
    try {
      const got = await job();
      events.push(...got);
      status[name] = `${got.length}건`;
    } catch (e) {
      status[name] = `실패: ${e.message}`; // one broken source must not stop the others
    }
  }
  events.sort((a, b) => a.ts.localeCompare(b.ts));
  await save(start, events);
  const summary = { at: new Date().toISOString(), status, total: events.length };
  await chrome.storage.local.set({ last: summary });
  return summary;
}

// Rewrites the whole day each run, so retro can read one file per day without de-duplicating.
async function save(day, events) {
  const pad = (n) => String(n).padStart(2, "0");
  const name = `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
  const body = events.map((e) => JSON.stringify(e)).join("\n") + "\n";
  // service workers have no URL.createObjectURL; a data URL works for a day's worth of events
  const url = "data:application/x-ndjson;charset=utf-8," + encodeURIComponent(body);
  await chrome.downloads.download({
    url, filename: `retro/browser-${name}.jsonl`, conflictAction: "overwrite", saveAs: false,
  });
}
