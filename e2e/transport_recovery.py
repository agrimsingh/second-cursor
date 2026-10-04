#!/usr/bin/env python3
"""Transport isolation and durable retry acceptance, specified before implementation.

SC_E2E_APP=build/SecondCursor.app python3 e2e/transport_recovery.py
Always uses a disposable SC_HOME; --installed is refused.
--off-space leaves scratch documents on their normal Space and requires them to
be off screen. This verifies recovery and the global display lease, but does not
claim coverage of returning windows hosted on the agent display.
"""
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, SCENARIOS, scenario, ToolError, element_index, alive

SCENARIOS.clear()
OFF_SPACE = "--off-space" in sys.argv


def launch_textedit(ctx, filename, text):
    window = ctx.launch_scratch_textedit(filename, text, on_agent_display=not OFF_SPACE)
    if OFF_SPACE:
        ctx.require("off-Space coverage: fixture stays off the agent display",
                    not window.get("on_agent_display"), window)
        ctx.require("off-Space coverage: fixture starts off screen",
                    window.get("is_on_screen") is False, window)
    return window


class Client:
    def __init__(self, instance, session=None, transcript=None):
        self.instance, self.session, self.next_id, self.transcript = instance, session, 1, transcript
        self.lock = threading.Lock()

    def raw(self, method, params=None, http="POST", include_session=True):
        with self.lock:
            payload = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
            self.next_id += 1
        headers = {"Authorization": f"Bearer {self.instance.token}", "Content-Type": "application/json",
                   "Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"}
        if self.session and include_session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.instance.url, data=json.dumps(payload).encode() if http == "POST" else None,
                                         headers=headers, method=http)
        started = time.time()
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if method == "initialize" and response.status == 200:
                self.session = response.headers.get("Mcp-Session-Id")
            body = json.loads(data) if data else {}
            if self.transcript:
                self.transcript.write({**payload, "_http_method": http, "_transport_session": headers.get("Mcp-Session-Id")},
                                      body, response.status, time.time() - started)
            return response.status, body

    def initialize(self):
        status, body = self.raw("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "recovery-e2e", "version": "1"}})
        assert status == 200 and self.session, (status, body)
        return self.session

    def call(self, tool, **args):
        status, body = self.raw("tools/call", {"name": tool, "arguments": args})
        if status != 200 or "error" in body:
            raise ToolError(str((status, body)))
        result = body["result"]
        text = next((item["text"] for item in result.get("content", []) if item["type"] == "text"), "{}")
        if result.get("isError"):
            raise ToolError(text)
        return json.loads(text)

    def close(self):
        return self.raw("", http="DELETE")[0]


def refuses(client, tool, **args):
    try:
        client.call(tool, **args)
        return False
    except ToolError:
        return True


def restart(ctx, window):
    """Crash only this suite's disposable process, preserving its recovery directory."""
    instance, transcript = ctx.run.instance, ctx.run.transcript
    old_pid = instance.pid
    os.kill(old_pid, signal.SIGKILL)
    deadline = time.time() + 10
    while alive(instance.pid) and time.time() < deadline:
        time.sleep(.05)
    assert not alive(old_pid), "Old disposable instance did not exit"
    subprocess.run(["open", "-n", "-g", "--env", f"SC_HOME={instance.home}",
                    "--stdout", str(instance.home / "logs/stdout.log"),
                    "--stderr", str(instance.home / "logs/stderr.log"), "-a", str(instance.app)],
                   check=True, capture_output=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            client = Client(instance, transcript=transcript)
            client.initialize()
            instance.pid = instance._pid_file()
            assert instance.pid and instance.pid != old_pid, "Readiness came from the old instance"
            client.close()
            break
        except (OSError, AssertionError):
            time.sleep(.1)
    else:
        raise AssertionError("Disposable instance did not recover")
    # The harness is an independent task too. Restore its control state immediately, so a
    # later assertion failure in the subject session cannot cascade into unrelated scenarios.
    restored = ctx.call("task", action="resume")["task"]
    ctx.require("fixture host explicitly resumes after runtime restart", restored["status"] == "thinking", restored)
    ctx.call("get_window_state", window_id=window, include_screenshot=False, full=True)


@scenario
def automatic_transport_ownership(ctx):
    a, b = Client(ctx.run.instance, transcript=ctx.run.transcript), Client(ctx.run.instance, transcript=ctx.run.transcript)
    a.initialize()
    b.initialize()
    ctx.cleanups.extend([a.close, b.close])
    ctx.require("initialize assigns distinct real sessions", a.session != b.session)
    window = launch_textedit(ctx, "transport.txt", "transport ownership")["id"]
    a.call("get_window_state", window_id=window)
    b.call("get_window_state", window_id=window)
    a.call("press_key", window_id=window, keys="right")
    ctx.require("ordinary second client cannot mutate first client's window",
                refuses(b, "press_key", window_id=window, keys="right"))
    if OFF_SPACE:
        before_display = b.call("agent_display", action="create")
        before_frame = b.call("get_window_state", window_id=window)["window"]["frame"]
    ctx.require("second client cannot tear down the shared display while another task owns a native window",
                refuses(b, "agent_display", action="teardown"))
    display = b.call("agent_display", action="status")
    if OFF_SPACE:
        ctx.require("off-Space lease coverage: refused teardown preserves the disposable display",
                    display["is_active"] and display["display_id"] == before_display["display_id"]
                    and display["hosted_window_ids"] == before_display["hosted_window_ids"], display)
        after_window = b.call("get_window_state", window_id=window)["window"]
        ctx.require("off-Space lease coverage: the claimed window remains off screen at its original frame",
                    after_window["frame"] == before_frame and after_window["is_on_screen"] is False, after_window)
    else:
        ctx.require("refused teardown leaves the disposable display and hosted window intact",
                    display["is_active"] and window in display["hosted_window_ids"], display)
    ctx.require("explicit legacy cannot bypass MCP ownership", refuses(b, "status", session_id="legacy"))
    task = a.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: b.call("session", action="close", session_id=task))
    ctx.require("trusted host can address explicit task session", "paused" in b.call("status", session_id=task))
    resumed = Client(ctx.run.instance, a.session, ctx.run.transcript)
    ctx.require("initialize with known header resumes same session", resumed.initialize() == a.session)
    ctx.require("missing transport header is explicit HTTP 400", b.raw("tools/list", include_session=False)[0] == 400)
    stale = Client(ctx.run.instance, str(uuid.uuid4()), ctx.run.transcript)
    ctx.require("unknown header is explicit HTTP 404", stale.raw("tools/list")[0] == 404)
    ctx.require("DELETE closes only its own transport", a.close() == 200 and "paused" in b.call("status"))
    ctx.require("closed header stays closed", a.raw("tools/list")[0] == 404)
    ctx.require("explicit task outlives enclosing transport", "paused" in b.call("status", session_id=task))
    ctx.require("teardown is allowed after the owning task closes and its input has drained",
                b.call("agent_display", action="teardown")["is_active"] is False)


@scenario
def durable_operation_recovery(ctx):
    client = Client(ctx.run.instance, transcript=ctx.run.transcript)
    client.initialize()
    ctx.cleanups.append(client.close)
    status, discovery = client.raw("tools/list")
    recovery = next((tool for tool in discovery.get("result", {}).get("tools", [])
                     if tool.get("name") == "operation_status"), None)
    ctx.require("ordinary MCP discovery exposes read-only outcome reconciliation",
                status == 200 and recovery and recovery.get("annotations", {}).get("readOnlyHint") is True,
                recovery)
    ctx.require("discovered recovery schema retains its required operation ID",
                recovery["inputSchema"].get("required") == ["operation_id"]
                and recovery["inputSchema"]["properties"].get("operation_id", {}).get("type") == "string",
                recovery["inputSchema"])
    window = launch_textedit(ctx, "recovery.txt", "before")["id"]
    snapshot = client.call("get_window_state", window_id=window)
    index = element_index(snapshot["tree"], "AXTextArea")
    secret = "private-recovery-content-" + uuid.uuid4().hex
    operation = str(uuid.uuid4())
    args = dict(window_id=window, element_index=index, value=secret, operation_id=operation)
    client.call("set_value", **args)
    journal = ctx.run.instance.home / "operation-journal"
    records = list(journal.glob("operation-*.json"))
    record_path = next(path for path in records if json.loads(path.read_text())["id"] == operation)
    original = json.loads(record_path.read_text())
    ctx.require("journal omits typed text and full response", secret not in "".join(path.read_text() for path in journal.glob("*.json")))
    restart(ctx, window)
    ctx.require("transport resumes after process restart", Client(ctx.run.instance, client.session, ctx.run.transcript).initialize() == client.session)
    info = client.call("operation_status", operation_id=operation)
    ctx.require("committed outcome survives restart without storing payload", info["state"] == "completed" and info["outcome_known"] and info["result_expired"])
    ctx.require("recovered task still requires explicit resume", client.call("task", action="get")["task"]["status"] == "interrupted")
    before_log = ctx.run.instance.action_log.read_bytes()
    replayed = client.call("set_value", **args)
    ctx.require("completed retry reconciles while the task remains interrupted",
                replayed["state"] == "completed" and replayed["result_expired"])
    ctx.require("recovered completed retry dispatches no new action", ctx.run.instance.action_log.read_bytes() == before_log)
    client.call("task", action="resume")
    # Change target through a new operation; retrying the previous ID must not overwrite this value.
    client.call("get_window_state", window_id=window)
    automatic = client.call("set_value", window_id=window, element_index=index, value="temporary-auto-value")
    client.call("set_value", window_id=window, element_index=index, value="after-recovery")
    repeated = client.call("set_value", window_id=window, element_index=index, value="temporary-auto-value",
                           operation_id=automatic["operation_id"])
    ctx.require("a returned automatic retry ID matches the original request",
                repeated["operation_id"] == automatic["operation_id"])
    client.call("set_value", **args)
    read = client.call("inspect_element", window_id=window, element_index=index)
    ctx.require("completed mutation is not redispatched", read["text"] == "after-recovery", read)
    ctx.require("changed payload with same retry ID is refused", refuses(client, "set_value", **{**args, "value": "different"}))
    # Deterministic fault injection at the durable boundary complements the real process kill above.
    for phase, expected, known in [("intent", "interrupted", True), ("dispatched", "uncertain", False)]:
        record = {**original, "state": phase, "finished": False, "outcomeKnown": False}
        record_path.write_text(json.dumps(record))
        restart(ctx, window)
        ctx.require(f"{phase} recovery leaves task interrupted before reconciliation",
                    client.call("task", action="get")["task"]["status"] == "interrupted")
        before_log = ctx.run.instance.action_log.read_bytes()
        info = client.call("set_value", **args)
        ctx.require(f"recovered {phase} prevents redispatch", info["state"] == expected and info["outcome_known"] is known, info)
        ctx.require(f"{phase} retry dispatches no new action", ctx.run.instance.action_log.read_bytes() == before_log)
        client.call("get_window_state", window_id=window)
        ctx.require(f"{phase} retry leaves target unchanged",
                    client.call("inspect_element", window_id=window, element_index=index)["text"] == "after-recovery")
        client.call("task", action="resume")
        client.call("get_window_state", window_id=window, include_screenshot=False, full=True)


@scenario
def journal_write_failure_refuses_input(ctx):
    client = Client(ctx.run.instance, transcript=ctx.run.transcript)
    client.initialize()
    ctx.cleanups.append(client.close)
    window = launch_textedit(ctx, "journal-refusal.txt", "untouched")["id"]
    snapshot = client.call("get_window_state", window_id=window)
    index = element_index(snapshot["tree"], "AXTextArea")
    journal = ctx.run.instance.home / "operation-journal"
    aside = ctx.run.instance.home / "operation-journal-held"
    journal.rename(aside)
    try:
        journal.write_text("a file cannot contain journal entries")
        ctx.require("failed intent persistence refuses input",
                    refuses(client, "set_value", window_id=window, element_index=index, value="must-not-run"))
        ctx.require("read-only diagnosis remains available", "paused" in client.call("status"))
        ctx.require("target was never changed without a durable intent",
                    client.call("inspect_element", window_id=window, element_index=index)["text"] == "untouched")
    finally:
        journal.unlink()
        aside.rename(journal)
    client.call("get_window_state", window_id=window)
    client.call("set_value", window_id=window, element_index=index, value="restored")
    ctx.require("restoring journal access permits a new verified mutation",
                client.call("inspect_element", window_id=window, element_index=index)["text"] == "restored")


@scenario
def late_completion_preserves_new_operation(ctx):
    a, b = Client(ctx.run.instance, transcript=ctx.run.transcript), Client(ctx.run.instance, transcript=ctx.run.transcript)
    a.initialize()
    b.initialize()
    ctx.cleanups.extend([a.close, b.close])
    first = launch_textedit(ctx, "closing-operation.txt", "first")["id"]
    second = launch_textedit(ctx, "new-operation.txt", "second")["id"]
    a.call("get_window_state", window_id=first)
    index = element_index(b.call("get_window_state", window_id=second)["tree"], "AXTextArea")
    operation = str(uuid.uuid4())
    errors = []

    def delayed():
        try:
            a.call("batch", window_id=first, steps=[{"tool": "wait", "args": {"ms": 2500}}],
                   observe="none", operation_id=operation)
        except ToolError as error:
            errors.append(str(error)) # Closing the session is allowed to cancel its remaining batch.

    thread = threading.Thread(target=delayed)
    thread.start()
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            if a.call("operation_status", operation_id=operation)["state"] == "dispatched":
                break
        except ToolError:
            pass
        time.sleep(.025)
    else:
        thread.join(5)
        raise AssertionError("Did not observe the old operation in flight")
    a.close()
    ctx.require("old completion remains in flight during replacement", thread.is_alive())
    args = dict(window_id=second, element_index=index, value="new-generation", operation_id=operation)
    original = b.call("set_value", **args)
    thread.join(5)
    ctx.require("closed operation drains", not thread.is_alive())
    ctx.require("late completion preserves newer operation status",
                b.call("operation_status", operation_id=operation)["state"] == "completed")
    b.call("get_window_state", window_id=second)
    b.call("set_value", window_id=second, element_index=index, value="changed-after-new-generation")
    ctx.require("newer generation retry returns original response", b.call("set_value", **args) == original)
    ctx.require("newer generation was not repeated after old completion",
                b.call("inspect_element", window_id=second, element_index=index)["text"] == "changed-after-new-generation")


if __name__ == "__main__":
    if "--installed" in sys.argv or os.environ.get("SC_E2E_TARGET") == "installed":
        raise SystemExit("This suite requires a disposable instance")
    run = Run(browser=False, installed=False)
    if OFF_SPACE:
        run.notes.append("Off-Space recovery coverage: scratch documents stayed off screen on their normal Space. "
                         "Recovery, retry, isolation, and drain assertions were unchanged. The teardown test verified "
                         "the global lease against an off-Space claimed window and an empty disposable display; "
                         "this run does not establish preservation or return of agent-display-hosted windows.")
    sys.exit(0 if run.run(set(sys.argv[1:])) else 1)
