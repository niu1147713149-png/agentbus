const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = fs.readFileSync(path.join(__dirname, "../ui/app.js"), "utf8");

function teamPage() {
  const nodes = new Map(), clicks = [], requests = [];
  let events;
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {
      dataset: {}, classList: { remove() {} }, focus() {}, select() {}, remove() {},
      querySelector: node, addEventListener() {},
    });
    return nodes.get(key);
  };
  const doc = { querySelector: node, querySelectorAll: () => [], documentElement: node("html"),
    addEventListener(type, cb) { if (type === "click") clicks.push(cb); },
    createElement: node, body: { appendChild() {} } };
  const scope = {
    document: doc, location: { hash: "#team" }, localStorage: { getItem() {}, setItem() {} },
    addEventListener() {}, setTimeout() {}, setInterval() {}, I18N: { sync() {} },
    AB: { icon: () => "", robot: () => "", avatar: () => "", esc: String, STATE: { off: { c: "", t: "" } } },
    EventSource: class { constructor() { events = this; } },
    fetch(url, options) {
      if (url === "/api/catalog") return Promise.resolve({ json: async () => ({ categories: [] }) });
      assert.equal(url, "/api/api-agent/rename");
      return new Promise(resolve => requests.push({ options, reply(ok, data) { resolve({ ok, json: async () => data }); } }));
    },
  };
  scope.window = scope;
  vm.runInNewContext(app, scope);
  const shared = { installed: true, place: "lounge", state: "off" };
  events.onmessage({ data: JSON.stringify({ settings: { setup_done: true, tour_done: true }, running: [],
    agents: [{ ...shared, id: "api-1", name: "Original", api: true }, { ...shared, id: "cli", name: "CLI" }] }) });
  const form = node("form");
  form.parentElement = {};
  Object.defineProperty(form, "isConnected", { get() { return Boolean(node("#layer").innerHTML); } });
  const click = (selector, dataset) => Promise.all(clicks.map(fn => fn({ preventDefault() {}, stopPropagation() {},
    target: { closest: s => s === selector ? { dataset } : null, matches: () => false } })));
  return { node, form, requests, click, open: () => click("[data-api-rename]", { apiRename: "api-1" }),
    submit: () => form.onsubmit({ preventDefault() {} }) };
}

test("team list and cards offer rename only for API agents; cancelling never writes", async () => {
  const { node, form, requests, click, open } = teamPage();
  for (const mode of ["list", "cards"]) {
    await click("[data-tview]", { tview: mode });
    assert.match(node("#team").innerHTML, /data-api-rename="api-1"/);
    assert.doesNotMatch(node("#team").innerHTML, /data-api-rename="cli"/);
    await open();
    assert.equal(node("input").value, "Original");
    node("input").value = "Ignored";
    if (mode === "list") node('[type="button"]').onclick();
    else form.onkeydown({ key: "Escape", stopPropagation() {} });
    assert.equal(form.isConnected, false);
    assert.equal(requests.length, 0);
  }
});

test("rename preserves duplicate input, retries once and repaints with the same ID", async () => {
  const { node, form, requests, open, submit } = teamPage();
  await open();
  const input = node("input"), error = node('[role="alert"]');
  input.value = "Taken";
  const first = submit();
  await submit();
  assert.equal(requests.length, 1);
  requests[0].reply(false, { error: "Name already exists" }); await first;
  assert.ok(form.isConnected);
  assert.equal(input.value, "Taken");
  assert.equal(error.textContent, "Name already exists");
  assert.equal(error.hidden, false);
  input.value = "  Prototype designer  "; input.oninput();
  assert.equal(error.hidden, true);
  const retry = submit();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), { agent: "api-1", name: "Prototype designer" });
  requests[1].reply(true, { name: "Prototype designer" }); await retry;
  assert.equal(form.isConnected, false);
  assert.match(node("#team").innerHTML, /href="#hero\/api-1"[\s\S]*<h3>Prototype designer<\/h3>/);
  await open();
  assert.equal(input.value, "Prototype designer");
});
