// Replay the actual chat page's SSE/fetch handlers without starting a browser or dispatching a job.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = fs.readFileSync(path.join(__dirname, "../ui/app.js"), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));

function page() {
  const nodes = new Map(), requests = [], handlers = new Map();
  let events;
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, classList: { remove() {}, add() {}, toggle() {} },
      scrollTop: 10, scrollHeight: 300, clientHeight: 100, writes: 0,
      set innerHTML(html) { this.html = html; this.writes++; },
      querySelector: () => node(".badge"), remove() {}, addEventListener() {},
    });
    return nodes.get(selector);
  };
  const doc = { querySelector: node, querySelectorAll: () => [], documentElement: node("html"),
    createElement: () => node(".toast"), body: { appendChild() {} },
    addEventListener(type, fn) { if (!handlers.has(type)) handlers.set(type, []); handlers.get(type).push(fn); } };
  const scope = {
    location: { hash: "#chat" }, document: doc, localStorage: { getItem() {}, setItem() {} },
    EventSource: class { constructor() { events = this; } },
    fetch(url, options) {
      if (url === "/api/catalog") return Promise.resolve({ json: () => ({ categories: [] }) });
      assert.match(url, /^\/api\/chat(?:\?channel=|\/upload$|\/send$)/);
      return new Promise(resolve => requests.push({ url, options, reply(data, ok = true) { resolve({ ok, json: async () => data }); } }));
    },
    addEventListener() {}, setInterval() {}, setTimeout() {}, I18N: { sync() {} },
    AB: { icon: () => "", avatar: () => "", esc: s => String(s).replaceAll("<", "&lt;") },
  };
  scope.window = scope;
  vm.runInNewContext(app, scope);
  const push = (running, chat_rev = 1) => events.onmessage({ data: JSON.stringify({
    project: "test", settings: { setup_done: true, tour_done: true }, offices: [], agents: [], running, chat_rev,
  }) });
  const event = (type, target = {}, props = {}) => Promise.all(handlers.get(type).map(fn => fn({
    target: { closest: () => null, matches: () => false, ...target }, preventDefault() {}, ...props })));
  const click = (selector, dataset = {}) => event("click", { closest: s => s === selector ? { dataset } : null });
  return { push, node, requests, event, click, select: name => click("[data-ch]", { ch: name }) };
}

const snapshot = (channel = "大厅", status = "running") => ({
  channel, channels: [{ name: channel }], running_tasks: [], meta: {}, messages: [{
    id: "m1", job: "a", agent: "alpha", system: true, text: "alpha 接活了",
    task_state: { status, label: status === "failed" ? "失败" : "执行中" },
  }],
});
const job = (detail, extra) => ({
  channel: "大厅", job: "a", agent: "alpha", start: 123, worker: 1, progress: { detail }, ...extra,
});

test("live stages require no history fetch or redraw; disappearance fetches the actual terminal state", async () => {
  const { node, requests, push } = page();
  push([job("", { worker: null, progress: null })]);
  assert.equal(requests.length, 1);
  requests[0].reply(snapshot());
  await flush();
  const strip = node("#ch-run"), history = node("#ch-msgs");
  assert.match(strip.html, /已接单/);
  const writes = history.writes;
  node("#ch-text").value = "draft";
  for (const detail of ["会话已连接", "模型正在处理任务", "正在使用工具：Read", "正在保存结果"]) {
    push([job(detail), job("other channel", { channel: "别处", job: "b" })]);
    assert.ok(strip.html.includes(detail));
    assert.ok(!strip.html.includes("other channel"));
  }
  push([job("obsolete", { queued: "等待额度" })]);
  assert.match(strip.html, /排队中 · 等待额度/);
  assert.doesNotMatch(strip.html, /obsolete/);
  assert.equal(requests.length, 1);
  assert.equal(history.writes, writes);
  assert.equal(history.scrollTop, 10);
  assert.equal(node("#ch-text").value, "draft");
  push([]);
  assert.equal(requests.length, 2);
  assert.equal(strip.html, "");
  requests[1].reply(snapshot("大厅", "failed"));
  await flush();
  assert.match(history.html, /失败/);
});

test("a late response cannot replace the newly selected channel", async () => {
  const { node, requests, push, select } = page();
  push([]);
  const selected = select("产品交付");
  assert.equal(requests.length, 2);
  requests[1].reply(snapshot("产品交付"));
  await selected;
  requests[0].reply(snapshot());
  await flush();
  assert.match(node("#ch-bar").html, /# 产品交付/);
});

test("pending or failed attachments block sending; retry is scoped to the original channel", async () => {
  const { node, requests, push, select, click, event } = page();
  push([]); requests.at(-1).reply(snapshot()); await flush();
  const file = { name: "测试.md" }, uploaded = { name: file.name, path: "stored.md", image: false };
  await event("change", { id: "ch-file", files: [file] });
  const first = requests.at(-1);
  assert.equal(first.url, "/api/chat/upload");
  assert.equal(first.options.body, file);
  assert.match(node("#ch-att").html, /上传中/);
  node("#ch-text").value = "请评审";
  await click("#ch-send");
  assert.equal(requests.length, 2);
  first.reply({ error: "failed" }, false); await flush();
  assert.match(node("#ch-att").html, /上传失败/);
  await event("keydown", { id: "ch-text" }, { key: "Enter" });
  assert.equal(requests.length, 2);
  const retry = click("[data-attretry]", { attretry: 0 });
  const second = requests.at(-1);
  const switched = select("其他");
  requests.at(-1).reply(snapshot("其他")); await switched;
  second.reply(uploaded); await retry;
  assert.equal(node("#ch-att").html, "");
  await click("#ch-send");
  assert.equal(requests.filter(r => r.url === "/api/chat/send").length, 0);
  const back = select("大厅");
  requests.at(-1).reply(snapshot()); await back;
  assert.ok(node("#ch-att").html.includes(file.name));
  node("#ch-text").value = "@claude 请评审";
  const sending = click("#ch-send");
  const sent = requests.at(-1);
  assert.deepEqual(JSON.parse(sent.options.body), { channel: "大厅", text: "@claude 请评审", files: [uploaded] });
  await click("#ch-send");
  assert.equal(sent, requests.at(-1));
  sent.reply({ error: "retry later" }, false); await sending;
  assert.equal(node("#ch-text").value, "@claude 请评审");
  assert.ok(node("#ch-att").html.includes(file.name));
  const again = click("#ch-send"); requests.at(-1).reply({ ok: true, jobs: ["test"] }); await again;
  assert.equal(node("#ch-att").html, "");
  assert.equal(node("#ch-text").value, "");
});
