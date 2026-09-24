const NAMES = { chatgpt: "ChatGPT", claude: "Claude", history: "방문 기록" };
const statusEl = document.getElementById("status");
const runBtn = document.getElementById("run");

function show(last) {
  if (!last) {
    statusEl.textContent = "아직 수집 전 · 매시간 자동 수집";
    return;
  }
  const lines = Object.entries(last.status).map(([k, v]) => `${NAMES[k] || k}: ${v}`);
  statusEl.textContent = `마지막 수집 ${new Date(last.at).toLocaleTimeString()} · 총 ${last.total}건\n` + lines.join("\n");
}

const { mode = "questions", sources = { chatgpt: true, claude: true, history: true }, last } =
  await chrome.storage.local.get(["mode", "sources", "last"]);

for (const box of document.querySelectorAll("[data-source]")) {
  box.checked = sources[box.dataset.source] !== false;
  box.addEventListener("change", () => {
    sources[box.dataset.source] = box.checked;
    chrome.storage.local.set({ sources });
  });
}
for (const radio of document.querySelectorAll("[name=mode]")) {
  radio.checked = radio.value === mode;
  radio.addEventListener("change", () => chrome.storage.local.set({ mode: radio.value }));
}
show(last);

runBtn.addEventListener("click", async () => {
  runBtn.disabled = true;
  statusEl.textContent = "수집 중…";
  show(await chrome.runtime.sendMessage("collect"));
  runBtn.disabled = false;
});
