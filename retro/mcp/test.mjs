// End-to-end test with the official MCP SDK client against `wrangler dev`.
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const base = process.env.RETRO_URL || "http://127.0.0.1:8787";
const key = "test_" + "k".repeat(30);
const client = new Client({ name: "retro-test", version: "0" });
await client.connect(new StreamableHTTPClientTransport(new URL(`${base}/mcp/${key}`)));

assert.match(client.getInstructions() || "", /log_activity/);
const { tools } = await client.listTools();
assert.deepEqual(tools.map((t) => t.name), ["log_activity", "get_today", "forget"]);
console.log("tool definitions ~", Math.round(JSON.stringify(tools).length / 4), "tokens (rough, chars/4)");

let r = await client.callTool({ name: "log_activity", arguments: {
  app: "claude", topic: "빈티지 카메라", did: "도쿄 필터 A안 확정", kind: "plan", decisions: ["광고는 보류"] } });
assert.equal(r.content[0].text, "ok (1 today)");
r = await client.callTool({ name: "log_activity", arguments: { app: "chatgpt", topic: "pet", did: "스킬 38개 목록 정리", kind: "build" } });
assert.equal(r.content[0].text, "ok (2 today)");
r = await client.callTool({ name: "log_activity", arguments: { app: "claude", topic: "x" } });
assert.equal(r.isError, true, "missing field is a tool error, not a crash");

r = await client.callTool({ name: "get_today", arguments: {} });
const out = r.content[0].text;
console.log("get_today ->\n" + out);
assert.match(out, /2 logs \(claude 1, chatgpt 1\)/);
assert.match(out, /decisions: 광고는 보류/);
const page = out.match(/page: (\S+)/)[1];
const html = await (await fetch(page)).text();
assert.match(html, /도쿄 필터 A안 확정/); assert.match(html, /기획 1/);

const other = await (await fetch(`${base}/p/${"z".repeat(30)}/${page.slice(-10)}`)).text();
assert.match(other, /아직 기록이 없습니다/, "keys are isolated");
assert.equal((await fetch(`${base}/mcp/short`, { method: "POST" })).status, 404);
// forget: last entry, then the day, then everything (other key untouched)
const otherKey = "other_" + "o".repeat(30);
const other2 = new Client({ name: "retro-test-2", version: "0" });
await other2.connect(new StreamableHTTPClientTransport(new URL(`${base}/mcp/${otherKey}`)));
await other2.callTool({ name: "log_activity", arguments: { app: "gemini", topic: "t", did: "d" } });
r = await client.callTool({ name: "forget", arguments: { scope: "last" } });
assert.match(r.content[0].text, /^deleted 1 /);
assert.match((await client.callTool({ name: "get_today", arguments: {} })).content[0].text, /1 logs \(claude 1\)/);
r = await client.callTool({ name: "forget", arguments: { scope: "day" } });
assert.match(r.content[0].text, /^deleted 1 /);
await client.callTool({ name: "log_activity", arguments: { app: "claude", topic: "a", did: "b" } });
r = await client.callTool({ name: "forget", arguments: { scope: "all" } });
assert.equal(r.content[0].text, "deleted 1 (all dates)");
assert.match((await client.callTool({ name: "get_today", arguments: {} })).content[0].text, /no logs yet/);
assert.match((await other2.callTool({ name: "get_today", arguments: {} })).content[0].text, /1 logs/, "forget is per key");
assert.equal((await client.callTool({ name: "forget", arguments: { scope: "x" } })).isError, true);
await other2.close();
await client.close();
console.log("e2e passed");
