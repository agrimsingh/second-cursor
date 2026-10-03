// Real HTTP/MCP transport regression; deliberately never touches the desktop.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { App, SecondCursor, SecondCursorError, Tab } from "./index.mjs";

const results = [];
for (const transport of ["http", "mcp"]) {
  const requests = [], operations = new Map();
  let mutations = 0, sessions = 0, snapshot = 0, failClose = false, sessionDelayMs = 0, dropTaskControl = false, failSnapshot = false;
  const server = createServer(async (request, response) => {
    let raw = "";
    for await (const chunk of request) raw += chunk;
    const packet = JSON.parse(raw);
    if (packet.method === "initialize") {
      const session = request.headers["mcp-session-id"] ?? `host-${++sessions}`;
      if (sessionDelayMs) await new Promise((resolve) => setTimeout(resolve, sessionDelayMs));
      response.writeHead(200, { "Content-Type": "application/json", "Mcp-Session-Id": session });
      response.end(JSON.stringify({ jsonrpc: "2.0", id: packet.id, result: { serverInfo: { name: "fixture" }, protocolVersion: "2025-06-18", capabilities: {} } }));
      return;
    }
    const tool = transport === "mcp" ? packet.params.name : packet.tool;
    const args = transport === "mcp" ? packet.params.arguments : packet.args;
    if (transport === "mcp") assert.equal(request.headers["mcp-session-id"], args.session_id, "MCP requests must carry the negotiated host session header");
    requests.push({ tool, args });
    const send = (data, options = {}) => {
      if (response.destroyed) return;
      const result = { content: [{ type: "text", text: JSON.stringify(data) }], ...options };
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(transport === "mcp" ? { jsonrpc: "2.0", id: packet.id, result } : result));
    };
    if (tool === "session") {
      if (args.action === "close" && failClose) return request.socket.destroy();
      if (args.action === "create" && sessionDelayMs) await new Promise((resolve) => setTimeout(resolve, sessionDelayMs));
      return send(args.action === "create" ? { session_id: `host-${++sessions}` } : { closed: true });
    }
    if (tool === "operation_status") return send(operations.get(args.operation_id) ?? { operation_id: args.operation_id, outcome_known: false });
    if (tool === "fault") {
      mutations++;
      operations.set(args.operation_id, { operation_id: args.operation_id, state: "completed", outcome_known: true, result: { value: mutations } });
      if (args.mode === "drop") return request.socket.destroy();
      if (args.mode === "wait") return;
      if (args.mode === "json") return response.end("broken JSON");
      if (args.mode === "shape") return response.end(JSON.stringify({ unrelated: true }));
      if (args.mode === "text") return response.end(JSON.stringify(transport === "mcp" ? { result: { content: [{ type: "text", text: "not-json" }] } } : { content: [{ type: "text", text: "not-json" }] }));
      if (args.mode === "http") { response.writeHead(503); return response.end("unavailable"); }
      if (args.mode === "tool") return send({ error: "postcondition failed" }, { isError: true });
      if (args.mode === "rpc") return response.end(JSON.stringify({ jsonrpc: "2.0", id: packet.id, error: { code: -32602, message: "Invalid params" } }));
    }
    if (tool === "task") {
      if (dropTaskControl) return request.socket.destroy();
      return send(args.action === "list" ? { tasks: [{ id: "task-1", title: "SDK fixture" }] } : { task: { id: args.task_id ?? "task-1", title: args.title ?? "SDK fixture", status: args.action === "pause" ? "paused" : "running" } });
    }
    if (tool === "get_window_state" || tool === "browser_snapshot") {
      if (tool === "browser_snapshot" && failSnapshot) return request.socket.destroy();
      return send({ snapshot_id: `snapshot-${++snapshot}`, text: "observed" });
    }
    // The real extended command endpoint returns control evidence, not a DOM snapshot.
    if (tool === "browser_command") return send({ ok: true, text: "inspected", outcome: { state: "verified", verified: true } });
    if (tool === "click" || tool === "browser_navigate") return send({ ok: true, observation: { snapshot_id: `snapshot-${++snapshot}`, text: "changed" } });
    return send({ ok: true });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const client = new SecondCursor({ url: `http://127.0.0.1:${server.address().port}`, token: "fixture", transport, timeoutMs: 1000 });
  try {
    await Promise.all([client.call("status"), client.call("list_apps")]);
    assert.equal(sessions, 1, "concurrent first calls must share one host-issued session");
    assert.equal(client.sessionId, "host-1");
    assert(requests.filter((r) => r.tool !== "session").every((r) => r.args.session_id === "host-1"));

    sessionDelayMs = 80;
    const connecting = new SecondCursor({ url: client.url, token: "fixture", transport, timeoutMs: 1000 });
    const firstAbort = new AbortController(), secondAbort = new AbortController();
    const firstWait = assert.rejects(connecting.call("first-aborted", {}, { signal: firstAbort.signal }), (error) => error.code === "ABORTED" && !error.outcomeUncertain);
    const secondWait = assert.rejects(connecting.call("second-aborted", {}, { signal: secondAbort.signal }), (error) => error.code === "ABORTED" && !error.outcomeUncertain);
    const survivingWait = connecting.call("surviving-call");
    const firstTimer = setTimeout(() => firstAbort.abort(), 10), secondTimer = setTimeout(() => secondAbort.abort(), 20);
    await Promise.all([firstWait, secondWait]);
    assert.equal(connecting._connected, false, "aborted callers must stop waiting before the shared negotiation completes");
    await survivingWait;
    clearTimeout(firstTimer); clearTimeout(secondTimer);
    assert.equal(sessions, 2, "concurrent callers share a session despite independent aborts");
    assert(!requests.some((r) => ["first-aborted", "second-aborted"].includes(r.tool)));
    await connecting.close();
    const timingOut = new SecondCursor({ url: client.url, token: "fixture", transport, timeoutMs: 1000 });
    await assert.rejects(timingOut.call("never-dispatched", {}, { timeoutMs: 10 }), (error) => error.code === "TIMEOUT" && !error.outcomeUncertain);
    await timingOut.connect();
    assert(!requests.some((r) => r.tool === "never-dispatched"));
    await timingOut.close();
    sessionDelayMs = 0;

    for (const [mode, code] of [["drop", "NETWORK_ERROR"], ["json", "INVALID_RESPONSE"], ["shape", "INVALID_RESPONSE"], ["text", "INVALID_RESPONSE"], ["http", "HTTP_ERROR"], ["tool", "TOOL_ERROR"]]) {
      const before = mutations;
      let failure;
      try { await client.call("fault", { mode }); } catch (error) { failure = error; }
      assert(failure instanceof SecondCursorError, `${mode} must be a typed error`);
      assert.equal(failure.code, code);
      assert.equal(failure.sessionId, "host-1");
      assert.equal(failure.outcomeUncertain, true);
      assert(failure.operationId);
      const reconciled = await client.reconcile(failure);
      assert.equal(reconciled.state, "completed");
      assert.equal(reconciled.result.value, before + 1);
      assert.equal(mutations, before + 1, "reconciliation must not replay a mutation");
    }
    assert.equal((await client.reconcile({ operationId: operations.keys().next().value })).state, "completed");
    for (const abort of [false, true]) {
      const controller = new AbortController();
      const timer = abort ? setTimeout(() => controller.abort(), 25) : undefined;
      try {
        await assert.rejects(client.call("fault", { mode: "wait" }, { timeoutMs: 50, signal: controller.signal }), (error) => {
          assert(error instanceof SecondCursorError);
          assert.equal(error.code, abort ? "ABORTED" : "TIMEOUT");
          assert.equal(error.outcomeUncertain, true);
          assert.equal(error.sessionId, "host-1");
          assert(error.operationId);
          return true;
        });
      } finally { clearTimeout(timer); }
    }
    const preAborted = new AbortController(); preAborted.abort();
    const beforeAbort = requests.length;
    await assert.rejects(client.call("fault", { mode: "drop" }, { signal: preAborted.signal }), (error) => error.code === "ABORTED" && !error.outcomeUncertain);
    assert.equal(requests.length, beforeAbort, "pre-aborted calls must not dispatch");
    if (transport === "mcp") await assert.rejects(client.call("fault", { mode: "rpc" }), (error) => error.code === "RPC_ERROR" && !error.outcomeUncertain && error.sessionId === "host-1");

    const app = new App(client, 42);
    await app.observe(); const nativeToken = app.observation.snapshot_id;
    await app.click(1);
    assert.equal(requests.at(-1).args.observation_id, nativeToken);
    assert.notEqual(app.observation.snapshot_id, nativeToken);
    assert.equal(new App(client, 42).observation.snapshot_id, app.observation.snapshot_id, "aliases must share the newest observation");
    await assert.rejects(app.act("fault", { mode: "drop" }), (error) => error.code === "NETWORK_ERROR");
    assert.equal(app.observation, null);
    const beforeStale = requests.length;
    await assert.rejects(app.click(1), (error) => error.code === "STALE_OBSERVATION" && !error.outcomeUncertain);
    assert.equal(requests.length, beforeStale, "an uncertain action must require a fresh read before a follow-up action");
    await app.observe();
    const tab = new Tab(client.browser("fixture"), 5);
    await tab.observe(); const tabToken = tab.observation.snapshot_id;
    await tab.getByRole("button", { name: "Save" }).click();
    assert.equal(requests.findLast((r) => r.tool === "browser_command").args.observation_id, tabToken);
    assert.equal(requests.at(-1).tool, "browser_snapshot", "extended actions that omit observations require a fresh snapshot");
    assert.notEqual(tab.observation.snapshot_id, tabToken);
    const updatedToken = tab.observation.snapshot_id;
    await tab.goto("https://example.test");
    assert.equal(requests.at(-1).args.observation_id, updatedToken);
    assert.notEqual(tab.observation.snapshot_id, updatedToken);
    const beforeRead = requests.length;
    await tab.getByRole("button").inspect();
    assert.equal(requests.length, beforeRead + 1, "read-only commands need no redundant snapshot");
    assert.equal(requests.at(-1).args.observation_id, undefined, "current-state reads must not be gated by a stale input token");
    assert(tab.observation?.snapshot_id);
    failSnapshot = true;
    const beforeFailedRead = requests.filter((r) => r.tool === "browser_command").length;
    const inputResult = await tab.getByLabel("Name").fill("saved despite lost snapshot");
    assert.equal(inputResult.outcome.state, "verified", "read failure must preserve the known mutation outcome");
    assert.equal(inputResult.observation, null);
    assert.equal(inputResult.observation_error_code, "NETWORK_ERROR");
    assert.equal(tab.observation, null);
    assert.equal(requests.filter((r) => r.tool === "browser_command").length, beforeFailedRead + 1, "a failed post-action read must never replay input");
    await assert.rejects(tab.click([1, 2]), (error) => error.code === "STALE_OBSERVATION");
    failSnapshot = false;
    await tab.observe();
    const beforeNoRead = requests.length;
    await tab.command("click", { x: 1, y: 2, observe: "none" });
    assert.equal(requests.length, beforeNoRead + 1, "explicit observe:none must not add a read");
    assert.equal(tab.observation, null);
    await tab.observe();
    const closingTab = new Tab(client.browser("fixture"), 6);
    await closingTab.observe();
    await closingTab.screenshot();
    await closingTab.close();
    assert.equal(requests.at(-1).tool, "browser_close");
    assert.equal(requests.at(-1).args.observation_id, undefined, "explicit tab cleanup must rely on ownership, not an old DOM snapshot");
    assert.equal(closingTab.observation, null);
    await tab.release();
    assert.equal(requests.at(-1).args.observation_id, undefined);
    assert.equal(tab.observation, null, "released targets lose their cached input authority");
    await tab.observe();

    const task = await client.createTask({ title: "SDK fixture" });
    assert.equal(task.id, "task-1");
    assert.equal((await client.tasks()).length, 1);
    await task.update({ status: "thinking" });
    assert.equal(requests.at(-1).args.status, "thinking");
    await task.pause(); assert.equal(requests.at(-1).args.action, "pause");
    await task.takeover(); assert.equal(requests.at(-1).args.action, "takeover");
    assert.equal(app.observation, null); assert.equal(tab.observation, null);
    await app.observe(); await tab.observe();
    await task.resume(); assert.equal(requests.at(-1).args.action, "resume");
    assert.equal(app.observation, null); assert.equal(tab.observation, null);
    await assert.rejects(tab.click([1, 2]), (error) => error.code === "STALE_OBSERVATION" && !error.outcomeUncertain);
    await task.cancel(); assert.equal(requests.at(-1).args.action, "cancel");
    dropTaskControl = true;
    let taskFailure;
    try { await task.pause(); } catch (error) { taskFailure = error; }
    assert.equal(taskFailure.recovery, "task_state");
    await assert.rejects(client.reconcile(taskFailure), (error) => error.code === "UNTRACKED_OPERATION");
    dropTaskControl = false;
    await task.get();

    client.onResult = () => { throw new Error("consumer hook failed"); };
    await assert.rejects(client.call("status"), (error) => error instanceof SecondCursorError && error.code === "CALLBACK_ERROR" && error.outcomeUncertain === false && error.result.ok === true);
    client.onResult = undefined;
    const wrongSession = new SecondCursor({ url: client.url, transport, sessionId: "another" });
    await assert.rejects(wrongSession.reconcile({ operationId: "old", sessionId: "host-1" }), /session/i);
    failClose = true;
    await assert.rejects(client.close(), (error) => error.code === "NETWORK_ERROR");
    assert.equal(client.sessionId, "host-1", "uncertain close must retain the session identity");
    failClose = false;
    client.onResult = ({ tool, args }) => { if (tool === "session" && args.action === "close") throw new Error("close hook failed"); };
    await assert.rejects(client.close(), (error) => error.code === "CALLBACK_ERROR" && error.result.closed === true);
    assert.equal(client.sessionId, null, "known-success close must clear identity even if the result callback fails");
    assert.equal(client._connected, false);
    await assert.rejects(app.click(1), (error) => error.code === "STALE_OBSERVATION");
    results.push(`${transport}: sessions, disconnect reconciliation, parsing, timeout/abort, observations, tasks, callbacks passed`);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}
console.log(results.join("\n"));
