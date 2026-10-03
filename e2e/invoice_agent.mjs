/** Deterministic SDK adapter for the invoice recovery E2E; never reads the server ledger. */
import assert from "node:assert/strict";
import {readFile, writeFile} from "node:fs/promises";
import {SecondCursor} from "../sdk/index.mjs";
import {runAgent, fileCheckpointStore} from "../sdk/agent.mjs";

const [mode, configPath] = process.argv.slice(2);
assert(["start", "resume"].includes(mode), "mode must be start or resume");
const config = JSON.parse(await readFile(configPath, "utf8"));
const runtime = JSON.parse(await readFile(`${process.env.SC_HOME}/config.json`, "utf8"));
const client = new SecondCursor({url: config.proxy, token: runtime.token, transport: "http"});
let bootstrap;
if (mode === "start") {
  await client.connect();
  const browsers = (await client.call("browser_instances")).browsers;
  assert.equal(browsers.length, 1, "only the disposable fixture browser is available");
  const browserId = browsers[0].id;
  const task = await client.createTask({title: "Invoice checkpoint verification", client_name: "Invoice E2E"});
  const opened = await client.call("browser_open", {browser_id: browserId, url: config.fixture});
  bootstrap = {browserId, tabId: opened.tab.id, taskId: task.id, originalSessionId: client.sessionId};
  await writeFile(config.bootstrap, JSON.stringify(bootstrap, null, 2), {mode: 0o600});
} else {
  bootstrap = JSON.parse(await readFile(config.bootstrap, "utf8"));
}
const {browserId, tabId, taskId} = bootstrap;
const targets = config.targets;
const calls = [];
const completed = (history, id) => history.find(item => item.role === "tool" && item.id === id && item.result);
const latestOutline = history => {
  const item = [...history].reverse().find(row => row.role === "tool" && row.name === "browser_snapshot" && row.result?.outline);
  assert(item, "a fresh full snapshot must precede every control lookup");
  return item.result.outline;
};
function ref(outline, role, label) {
  const found = outline.split("\n").map(line => line.match(/\[(e\d+)\]\s+(.+)/)).find(match => match && match[2].startsWith(`${role} "${label}"`));
  assert(found, `missing ${role} ${JSON.stringify(label)} in ${outline}`);
  return found[1];
}
const tool = (id, name, args = {}) => ({calls: [{id, name, arguments: {browser_id: browserId, tab_id: tabId, ...args}}]});
const adapter = {async next({history}) {
  const failed = history.find(item => item.role === "tool" && item.error);
  assert(!failed, `tool failed: ${JSON.stringify(failed)}`);
  for (const id of targets) {
    if (completed(history, `submit-${id}`)) continue;
    if (!completed(history, `list-${id}`)) return tool(`list-${id}`, "browser_snapshot", {full: true});
    if (!completed(history, `open-${id}`)) return tool(`open-${id}`, "browser_click", {ref: ref(latestOutline(history), "button", `${id} — draft`)});
    if (!completed(history, `detail-${id}`)) return tool(`detail-${id}`, "browser_snapshot", {full: true});
    const outline = latestOutline(history);
    assert(outline.includes(`Invoice ${id}`), "selected invoice must match the task target");
    const lines = [...outline.matchAll(/Line: (\d+) x (\d+) cents/g)];
    assert.equal(lines.length, 2, "read both line items through the browser outline");
    const net = lines.reduce((sum, match) => sum + Number(match[1]) * Number(match[2]), 0);
    const rate = outline.match(/Tax rate: (\d+) percent/);
    const account = outline.match(/Expense account: ([A-Za-z ]+)/)?.[1]?.trim();
    assert(rate && account, "read the invoice tax rate and account through its visible controls");
    const tax = Math.floor((net * Number(rate[1]) + 50) / 100);
    const values = {"Net cents": String(net), "Tax cents": String(tax), "Gross cents": String(net + tax), Account: account, Note: `Reconciled ${id}`};
    const steps = Object.entries(values).map(([label, text]) => ({tool: "browser_type", ref: ref(outline, "textbox", label), text}));
    steps.push({tool: "browser_click", ref: ref(outline, "button", `Submit ${id}`)});
    return tool(`submit-${id}`, "batch", {steps});
  }
  if (!completed(history, "verify-final")) return tool("verify-final", "browser_snapshot", {full: true});
  for (const id of targets) assert(latestOutline(history).includes(`${id} — submitted`), `${id} must visibly be submitted`);
  return {text: "All three synthetic invoices reconciled and submitted."};
}};

const result = await runAgent({
  client, adapter,
  task: `Reconcile and submit ${targets.join(", ")} once each; derive amounts from their visible line items and tax, use the listed expense account, and preserve the other invoices.`,
  maxSteps: 30,
  workflow: {id: "invoice-recovery-fixture", store: fileCheckpointStore(config.checkpoint), targets: [{browserId, tabId}], taskId},
  onTurn: turn => calls.push(turn),
  onApproval: call => {
    assert(["browser_snapshot", "browser_click", "batch"].includes(call.name), "fixture tool allowlist");
    assert.equal(call.arguments?.tab_id, tabId, "fixture tab scope");
    assert.equal(call.arguments?.browser_id, browserId, "fixture browser scope");
    if (call.name === "batch") {
      assert(call.arguments.steps.every(step => ["browser_type", "browser_click"].includes(step.tool)), "only fill and submit via browser controls");
    }
    return true;
  },
});
await writeFile(config.result, JSON.stringify({result, turns: calls, pid: process.pid,
  sessionId: client.sessionId, originalSessionId: bootstrap.originalSessionId}, null, 2), {mode: 0o600});
assert(!result.stopped, `workflow did not complete: ${result.stopped}`);
assert.equal(client.sessionId, bootstrap.originalSessionId, "resume must preserve host session ownership");
assert.equal(result.text, "All three synthetic invoices reconciled and submitted.");
console.log(JSON.stringify({completed: true, pid: process.pid, sessionId: client.sessionId, turns: calls.length}));
