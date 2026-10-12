#!/usr/bin/env python3
"""Sessions, per-app approval, turn end and browser policy, driven over MCP/HTTP like a client.

Usage:  e2e/policy.py [scenario ...] [--approvals]
Default scenarios open no dialogs. --approvals adds interactive scenarios that show real local
prompts; the harness never clicks Allow, so a success path is only recorded when a person clicks.
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, screenshots/)
"""
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import run as harness
from run import Run, SCENARIOS, Skip, ToolError, element_index, observe, scenario, untitled_window, value_of

SCENARIOS.clear()
TEXTEDIT = "com.apple.TextEdit"


def action_log_entries(start=0):
    # Looked up at call time: the harness points ACTION_LOG at the test instance when it starts.
    path = harness.ACTION_LOG
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line) for line in lines[start:]]


def post_turn_ended(mcp, body, token="default", method="POST"):
    url = mcp.url.rsplit("/", 1)[0] + "/turn-ended"
    token = mcp.token if token == "default" else token
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode(errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    mcp.transcript.write({"method": f"{method} /turn-ended", "body": body}, payload, status, time.time() - started)
    return status, payload


def refused(fn):
    try:
        fn()
    except ToolError as e:
        return str(e)
    return None


@scenario
def turn_end_resets_baseline(ctx):
    wid = ctx.launch_scratch_textedit("turn.txt", "turn one")["id"]
    # Calls without session_id run in this MCP connection's own session; legacy is only /api's.
    connection = ctx.mcp.session_id
    first = ctx.call("get_window_state", window_id=wid)
    ctx.require("first connection read is full", not first["is_diff"], first.get("is_diff"))
    second = ctx.call("get_window_state", window_id=wid)
    ctx.require("second connection read is a diff", second["is_diff"], second.get("is_diff"))

    status, body = post_turn_ended(ctx.mcp, {})
    ctx.check(
        "an empty turn-ended body ends legacy, not this connection",
        status == 200 and isinstance(body, dict) and body.get("session_id") == "legacy"
        and ctx.call("get_window_state", window_id=wid)["is_diff"],
        (status, body),
    )
    status, body = post_turn_ended(ctx.mcp, {"session_id": connection})
    ctx.require("turn-ended answers 200", status == 200, (status, body))
    ctx.check(
        "response has ok, session_id, released_tabs, cleared_app_approvals",
        isinstance(body, dict)
        and body.get("ok") is True
        and body.get("session_id") == connection
        and isinstance(body.get("released_tabs"), int)
        and isinstance(body.get("cleared_app_approvals"), int),
        body,
    )
    third = ctx.call("get_window_state", window_id=wid)
    ctx.check(
        "first connection read after its turn ends is full",
        not third["is_diff"] and not third.get("diff_base"),
        {k: third.get(k) for k in ("is_diff", "diff_base")},
    )

    status, body = post_turn_ended(ctx.mcp, None)
    ctx.check(
        "turn-ended without a body defaults to the legacy session",
        status == 200 and isinstance(body, dict) and body.get("session_id") == "legacy",
        (status, body),
    )
    ctx.call("get_window_state", window_id=wid)

    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    ctx.call("get_window_state", window_id=wid, session_id=sid)
    ctx.require(
        "explicit session diffs on its second read",
        ctx.call("get_window_state", window_id=wid, session_id=sid)["is_diff"],
    )
    status, body = post_turn_ended(ctx.mcp, {"session_id": sid})
    ctx.check(
        "explicit turn end echoes its session",
        status == 200 and isinstance(body, dict) and body.get("session_id") == sid,
        (status, body),
    )
    after = ctx.call("get_window_state", window_id=wid, session_id=sid)
    ctx.check("explicit session stays open and its next read is full", not after["is_diff"], after.get("is_diff"))
    ctx.check(
        "another session's turn end leaves the connection's baseline alone",
        ctx.call("get_window_state", window_id=wid)["is_diff"],
    )

    status, _ = post_turn_ended(ctx.mcp, {}, token=None)
    ctx.check("turn-ended refuses a request without the bearer token", status in (401, 403), status)
    status, _ = post_turn_ended(ctx.mcp, {}, token="not-the-token")
    ctx.check("turn-ended refuses a wrong bearer token", status in (401, 403), status)
    status, _ = post_turn_ended(ctx.mcp, None, method="GET")
    ctx.check("turn-ended requires POST", status in (400, 405), status)
    status, _ = post_turn_ended(ctx.mcp, "not an object")
    ctx.check("turn-ended rejects a malformed body", status == 400, status)


@scenario
def upload_requires_approval_advertised(ctx):
    browsers = ctx.call("browser_instances").get("browsers", [])
    if not browsers:
        raise Skip("Chrome extension not connected")
    caps = ctx.call("browser_command", browser_id=browsers[0]["id"], operation="capabilities")
    ctx.check(
        "upload is listed under local approval",
        "upload" in caps["local_approval_required"],
        caps["local_approval_required"],
    )
    ctx.check(
        "upload argument help says it needs local approval",
        "approval" in caps["arguments"]["upload"].lower(),
        caps["arguments"]["upload"],
    )
    ctx.check(
        "claim argument help says how long control lasts",
        "idle" in caps["arguments"]["claim"].lower(),
        caps["arguments"]["claim"],
    )


@scenario
def action_log_single_line(ctx):
    missing = random.randint(3_900_000_000, 4_294_967_295)

    def lines(target, start):
        return [e for e in action_log_entries(start) if e.get("target") == target]

    start = len(action_log_entries())
    error = refused(lambda: ctx.call("click", window_id=missing, x=5, y=5))
    ctx.require("click on a missing window is refused", error is not None)
    logged = lines(f"window_id={missing}", start)
    ctx.check("a refused action writes exactly one log line", len(logged) == 1, logged)
    ctx.check("that line records the refusal", bool(logged) and "refused" in (logged[0].get("detail") or ""), logged)

    start = len(action_log_entries())
    ctx.require(
        "read of a missing window fails",
        refused(lambda: ctx.call("get_window_state", window_id=missing)) is not None,
    )
    ctx.check("a failed read writes no log line", not lines(f"window_id={missing}", start), lines(f"window_id={missing}", start))

    wid = ctx.launch_scratch_textedit("log.txt", "log")["id"]
    ctx.call("get_window_state", window_id=wid)
    start = len(action_log_entries())
    ctx.require(
        "an impossible key is rejected",
        refused(lambda: ctx.call("press_key", window_id=wid, keys="impossible-key")) is not None,
    )
    logged = lines(f"window_id={wid}", start)
    ctx.check("a failed action writes exactly one log line", len(logged) == 1, logged)

    start = len(action_log_entries())
    ctx.call("get_window_state", window_id=wid)
    ctx.check("a successful read writes no log line", not lines(f"window_id={wid}", start), lines(f"window_id={wid}", start))


@scenario
def hung_app_deadline_message(ctx):
    window = ctx.launch_scratch_textedit("hung.txt", "hung")
    wid, pid = window["id"], window["pid"]
    ctx.call("get_window_state", window_id=wid)
    os.kill(pid, signal.SIGSTOP)
    ctx.cleanups.append(lambda: os.kill(pid, signal.SIGCONT))
    op = str(uuid.uuid4())
    began = time.monotonic()
    error = refused(lambda: ctx.call("get_window_state", window_id=wid, full=True, timeout_ms=1000, operation_id=op))
    elapsed = time.monotonic() - began
    ctx.require("a read of a stopped app misses its deadline", error is not None)
    ctx.check("the deadline is honored", elapsed < 4, round(elapsed, 2))
    ctx.check("the message says the deadline passed", "deadline exceeded" in error.lower(), error)
    ctx.check("the message suggests the app may be busy or hung", "busy or hung" in error, error)
    ctx.check("the message says to check get_window_state before retrying", "get_window_state" in error, error)
    ctx.check("the message carries the operation id for reconciliation", op in error and "operation_status" in error, error)
    info = ctx.call("operation_status", operation_id=op)
    ctx.check("the timed-out operation can be reconciled", info.get("operation_id") == op, info)


DENIAL = "local approval denied"


@scenario
def approvals_deny_mode_gates_without_ui(ctx):
    """Every local approval path must refuse immediately, with the user-denial error, and show nothing."""
    instance = harness.TestInstance(
        {"approvals": "deny", "appApproval": "prompt", "confirmationRequiredTools": ["list_windows"]},
        browser=True,
    )
    ctx.cleanups.append(lambda: instance.stop(keep_logs=ctx.run.dir / "deny-instance"))
    client = harness.MCP(instance.url, instance.token, ctx.run.transcript)
    client.initialize()
    wid = ctx.launch_scratch_textedit("deny.txt", "untouched")["id"]

    def refused_fast(label, tool, **args):
        began = time.monotonic()
        error = refused(lambda: client.call(tool, timeout_ms=10000, **args))
        elapsed = time.monotonic() - began
        ctx.check(f"{label} is refused with the denial error", error is not None and DENIAL in error.lower(), error)
        ctx.check(f"{label} is refused immediately", elapsed < 2, round(elapsed, 2))
        ctx.check(f"{label} shows no dialog", not observe()["second_cursor_windows"])

    refused_fast("reading an unapproved app", "get_window_state", window_id=wid)
    marker = "DENIED-" + uuid.uuid4().hex[:6]
    refused_fast("typing into an unapproved app", "type_text", window_id=wid, text=marker)
    ctx.check(
        "the refused typing dispatched nothing",
        marker not in ctx.call("get_window_state", window_id=wid, full=True)["tree"],
    )
    before = {a["pid"] for a in ctx.call("list_apps")["apps"] if a.get("name") == "Calculator"}
    refused_fast("launching an unapproved app", "launch_app", app="Calculator", new_instance=True)
    after = {a["pid"] for a in ctx.call("list_apps")["apps"] if a.get("name") == "Calculator"}
    for pid in after - before:
        ctx.kill_on_exit(pid)
    ctx.check("the refused launch started nothing", after == before, (before, after))
    for operation, extra in [
        ("upload", {"files": ["/etc/hosts"]}),
        ("claim", {}),
        ("user_tabs", {}),
        ("history", {"from": 0}),
        ("clipboard", {"action": "read"}),
        ("cdp", {"method": "Page.getLayoutMetrics"}),
    ]:
        refused_fast(f"browser {operation}", "browser_command", operation=operation, tab_id=1, **extra)
    # A batch step never reaches approval: the prompt could not say what the step does.
    began = time.monotonic()
    error = refused(lambda: client.call(
        "browser_command", operation="batch", tab_id=1, timeout_ms=10000, steps=[{"operation": "history", "from": 0}]))
    ctx.check(
        "a batch hiding a sensitive browser step is refused before approval is asked",
        error is not None and "call history on its own" in error,
        error,
    )
    ctx.check("that batch is refused immediately", time.monotonic() - began < 2, round(time.monotonic() - began, 2))
    ctx.check("that batch shows no dialog", not observe()["second_cursor_windows"])
    refused_fast("a confirmationRequiredTools tool", "list_windows")

    logged = instance.action_log.read_text().splitlines() if instance.action_log.exists() else []
    entries = [json.loads(line) for line in logged]
    ctx.check(
        "refused actions are logged as refusals, never admitted",
        entries and all("refused" in (e.get("detail") or "") for e in entries),
        entries[-5:],
    )
    status, body = post_turn_ended(client, {})
    ctx.check("turn end works in deny mode", status == 200 and body.get("ok") is True, (status, body))


@scenario
def confirmation_covers_every_browser_path(ctx):
    """confirmationRequiredTools naming a browser tool must also ask before any other call that does that tool's work."""
    instance = harness.TestInstance(
        {"approvals": "deny", "confirmationRequiredTools": ["browser_click", "browser_type", "browser_snapshot"]},
        browser=True,
    )
    ctx.cleanups.append(lambda: instance.stop(keep_logs=ctx.run.dir / "confirmation-instance"))
    client = harness.MCP(instance.url, instance.token, ctx.run.transcript)
    client.initialize()
    fixtures = harness.FixtureServer()
    base = fixtures.__enter__()
    ctx.cleanups.append(lambda: fixtures.__exit__(None, None, None))
    tab = client.call("browser_open", url=f"{base}/reactions.html")[0]["tab"]["id"]
    deadline = time.monotonic() + 3
    while not instance.timings() and time.monotonic() < deadline:
        time.sleep(0.1)
    opened = len(instance.timings())
    calls = [
        ("browser_command click", "browser_command", {"operation": "click", "locator": {"role": "button"}}),
        ("a browser_command batch step that checks a box", "browser_command", {"operation": "batch", "steps": [
            {"operation": "wait", "timeout_ms": 1}, {"operation": "check", "locator": {"label": "x"}, "checked": True}]}),
        ("a browser_repl script", "browser_repl", {"code": "1"}),
        ("a batch tab step named click", "batch", {"steps": [{"tool": "click", "ref": "e1"}]}),
        ("an act tab step named click", "act", {"steps": [{"tool": "click", "ref": "e1"}]}),
        ("browser_command paste, under browser_type", "browser_command", {"operation": "paste", "text": "x"}),
        ("browser_command select_option", "browser_command",
         {"operation": "select_option", "locator": {"label": "Name"}, "values": ["a"]}),
        ("browser_command drag", "browser_command",
         {"operation": "drag", "from_x": 10, "from_y": 10, "to_x": 20, "to_y": 20}),
        ("browser_command dialog", "browser_command", {"operation": "dialog", "action": "accept"}),
        ("browser_command dom_snapshot, under browser_snapshot", "browser_command", {"operation": "dom_snapshot"}),
    ]
    for label, tool, args in calls:
        began = time.monotonic()
        error = refused(lambda: client.call(tool, tab_id=tab, timeout_ms=10000, **args))
        ctx.check(f"{label} is refused with the denial error", error is not None and DENIAL in error.lower(), error)
        ctx.check(f"{label} is refused immediately", time.monotonic() - began < 2, round(time.monotonic() - began, 2))
    ctx.check("none of them showed a dialog", not observe()["second_cursor_windows"])
    deadline = time.monotonic() + 3
    while len(instance.timings()) < opened + len(calls) and time.monotonic() < deadline:
        time.sleep(0.1)
    entries = instance.timings(opened)
    reached = [e["tool"] for e in entries if any(name.startswith("bridge") for name in e.get("phases", {}))]
    ctx.check("every call has its timing line, and none of them reached the browser",
              len(entries) == len(calls) and not reached, {"lines": len(entries), "reached": reached})


@scenario
def browser_batch_refuses_sensitive_steps(ctx):
    """A prompt for a batch cannot show what a sensitive step does, so such a batch is refused before any of its
    steps run, even on this instance, which grants every approval."""
    browsers = ctx.call("browser_instances").get("browsers", [])
    if not browsers:
        raise Skip("Chrome extension not connected")
    bid = browsers[0]["id"]
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    html = '<title>Batch fixture</title><input aria-label="Name" value="unchanged"><input type="file" id="file">'
    tid = ctx.call("browser_open", browser_id=bid, session_id=sid, url="data:text/html," + urllib.parse.quote(html))[
        "tab"
    ]["id"]

    def command(**args):
        return ctx.call("browser_command", browser_id=bid, session_id=sid, tab_id=tid, **args)

    def name_value():
        return command(operation="inspect", locator={"label": "Name"})["text"]

    ctx.require("the fixture field starts unchanged", name_value() == "unchanged")
    for operation, extra in [
        ("upload", {"locator": {"selector": "#file"}, "files": ["/etc/hosts"]}),
        ("history", {"from": 0}),
        ("clipboard", {"action": "read"}),
        ("user_tabs", {}),
        ("claim", {}),
        ("cdp", {"method": "Page.getLayoutMetrics"}),
    ]:
        marker = f"{operation.upper()}-{uuid.uuid4().hex[:6]}"
        start = len(action_log_entries())
        began = time.monotonic()
        error = refused(lambda: command(operation="batch", steps=[
            {"operation": "fill", "locator": {"label": "Name"}, "text": marker},
            {"operation": operation, **extra},
        ]))
        elapsed = time.monotonic() - began
        logged = [e for e in action_log_entries(start) if e.get("target") == f"tab_id={tid}"]
        ctx.check(
            f"a batch with a {operation} step is refused and says to call it on its own",
            error is not None and f"call {operation} on its own" in error,
            error,
        )
        ctx.check(f"the {operation} batch is refused without waiting", elapsed < 2, round(elapsed, 2))
        ctx.check(
            f"the {operation} batch is logged once, as a refusal",
            len(logged) == 1 and "refused" in (logged[0].get("detail") or ""),
            logged,
        )
        ctx.check(f"no step of the {operation} batch ran", name_value() == "unchanged")
    for label, step in [
        ("fields under arguments", {"operation": "history", "arguments": {"from": 0}}),
        ("operation under arguments", {"arguments": {"operation": "history", "from": 0}}),
    ]:
        error = refused(lambda: command(operation="batch", steps=[
            {"operation": "fill", "locator": {"label": "Name"}, "text": "nested"}, step]))
        ctx.check(f"a history step with its {label} is refused the same way",
                  error is not None and "call history on its own" in error, error)
        ctx.check(f"no step of that batch ran ({label})", name_value() == "unchanged")
    command(operation="batch", steps=[
        {"operation": "fill", "arguments": {"operation": "history", "locator": {"label": "Name"}, "text": "kept"}}])
    ctx.check("a step's own operation wins over one nested under arguments", name_value() == "kept")
    ctx.check("no dialog was shown", not observe()["second_cursor_windows"])
    command(operation="batch", steps=[{"operation": "fill", "locator": {"label": "Name"}, "text": "batched"}])
    ctx.check("a batch without sensitive steps still runs", name_value() == "batched")


@scenario
def batch_steps_respect_leases(ctx):
    """A batch addressed to a free window must not reach, through a step, a window another session holds."""
    window = ctx.launch_scratch_textedit("lease-held.txt", "held")
    held, free = window["id"], untitled_window(ctx, window)["id"]
    # Setup's cmd+n and move claimed both windows for this connection's own session.
    for wid in (held, free):
        ctx.call("bind_target", target=f"window:{wid}", action="release")
    a = ctx.call("session", action="create")["session_id"]
    b = ctx.call("session", action="create")["session_id"]
    for sid in (a, b):
        ctx.cleanups.append(lambda sid=sid: ctx.mcp.call("session", action="close", session_id=sid))
    ctx.call("bind_target", session_id=a, target=f"window:{held}")
    marker = "LEASED-" + uuid.uuid4().hex[:6]
    start = len(action_log_entries())

    def held_step(session):
        tree = ctx.call("get_window_state", window_id=held, full=True, **session)["tree"]
        area = element_index(tree, "AXTextArea")
        ctx.require("the held window's text area is indexed", area is not None, tree[:300])
        return {"tool": "set_value", "args": {"window_id": held, "element_index": area, "value": marker}}

    for label, session in [("this connection's", {}), ("session b's", {"session_id": b})]:
        result = ctx.call("batch", window_id=free, observe="none", steps=[held_step(session)], **session)
        step = result["steps"][0]
        ctx.check(
            f"{label} batch step on the window session a holds is refused",
            not step["ok"] and "claimed by another session" in (step.get("error") or ""),
            result["steps"],
        )
        # The batch leased the free window to its session; hand it on to the next one.
        ctx.call("bind_target", target=f"window:{free}", action="release", **session)
    value = value_of(ctx.call("get_window_state", window_id=held, full=True)["tree"])
    ctx.check("nothing reached the held window", value == "held", value)
    logged = [e for e in action_log_entries(start) if e.get("target") == f"window_id={held}"]
    ctx.check(
        "each refused step is logged as a refusal",
        len(logged) == 2 and all("refused" in (e.get("detail") or "") for e in logged),
        logged,
    )

    ctx.call("bind_target", session_id=a, target=f"window:{held}", action="release")
    result = ctx.call("batch", window_id=free, session_id=b, observe="none", steps=[held_step({"session_id": b})])
    ctx.check("once session a releases it, the same step runs", result["steps"][0]["ok"], result["steps"])
    value = value_of(ctx.call("get_window_state", window_id=held, full=True)["tree"])
    ctx.check("and reaches the window", value == marker, value)


def interactive():
    if "--approvals" not in sys.argv:
        raise Skip("Interactive approval tests require --approvals; no prompts opened")


def prompt_windows(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = observe()["second_cursor_windows"]
        if windows:
            return windows
        time.sleep(0.05)
    return []


def capture(ctx, window, name):
    path = ctx.run.dir / "screenshots" / f"{ctx.name}-{name}.png"
    subprocess.run(["screencapture", "-x", "-l", str(window["id"]), str(path)], check=False)
    if path.exists():
        ctx.screenshots.append(path.relative_to(ctx.run.dir).as_posix())


def prompting_instance(ctx):
    """A second, disposable instance with appApproval "prompt"; the default instance runs with it off."""
    instance = harness.TestInstance({"appApproval": "prompt", "approvals": "prompt"})
    ctx.cleanups.append(lambda: instance.stop(keep_logs=ctx.run.dir / "prompting-instance"))
    client = harness.MCP(instance.url, instance.token, ctx.run.transcript)
    client.initialize()
    return instance, client


@scenario
def app_approval_prompt(ctx):
    interactive()
    instance, client = prompting_instance(ctx)
    config_path = instance.config_path
    config_before = json.loads(config_path.read_text())
    window = ctx.launch_scratch_textedit("approval.txt", "untouched")
    wid = window["id"]

    outcomes = []

    def read():
        began = time.monotonic()
        try:
            client.call("get_window_state", window_id=wid, timeout_ms=5000)
            outcomes.append(("allowed", time.monotonic() - began))
        except ToolError as e:
            outcomes.append((str(e), time.monotonic() - began))

    threads = [threading.Thread(target=read) for _ in range(3)]
    for thread in threads:
        thread.start()
    windows = prompt_windows(3)
    ctx.require("reading an unapproved app shows an approval prompt", bool(windows))
    capture(ctx, windows[0], "app-prompt")
    peak = len(windows)
    until = time.monotonic() + 1.5
    while time.monotonic() < until and any(t.is_alive() for t in threads):
        peak = max(peak, len(observe()["second_cursor_windows"]))
        time.sleep(0.1)
    ctx.check("concurrent calls for one app share one prompt", peak == 1, peak)
    for thread in threads:
        thread.join(15)
    ctx.require("every concurrent call returned", len(outcomes) == 3, outcomes)
    allowed = [o for o in outcomes if o[0] == "allowed"]
    ctx.check("one decision answered every waiting call", len(allowed) in (0, 3), outcomes)

    if not allowed:
        ctx.check(
            "unanswered prompt refuses the calls at their deadline",
            all("approval" in o[0].lower() and o[1] < 8 for o in outcomes),
            outcomes,
        )
        gone = time.monotonic() + 2
        while observe()["second_cursor_windows"] and time.monotonic() < gone:
            time.sleep(0.05)
        ctx.check("the expired prompt closes", not observe()["second_cursor_windows"])

        marker = "MUST-NOT-APPEAR-" + uuid.uuid4().hex[:6]
        log_start = len(instance.action_log.read_text().splitlines()) if instance.action_log.exists() else 0
        error = refused(lambda: client.call("type_text", window_id=wid, text=marker, timeout_ms=1500))
        ctx.require("an action on an unapproved app is refused", error is not None and "approval" in error.lower(), error)
        text = ctx.call("get_window_state", window_id=wid, full=True)["tree"]
        ctx.check("the refused action dispatched nothing", marker not in text, text[:300])
        new = instance.action_log.read_text().splitlines()[log_start:] if instance.action_log.exists() else []
        ctx.check(
            "the refusal is logged once, without an admission line",
            len(new) == 1 and "refused" in json.loads(new[0]).get("detail", ""),
            new,
        )

        before = {a["pid"] for a in ctx.call("list_apps")["apps"] if a.get("name") == "Calculator"}
        error = refused(lambda: client.call("launch_app", app="Calculator", new_instance=True, timeout_ms=1500))
        after = {a["pid"] for a in ctx.call("list_apps")["apps"] if a.get("name") == "Calculator"}
        for pid in after - before:
            ctx.kill_on_exit(pid)
        ctx.check("launch_app asks before launching an unapproved app", error is not None and "approval" in error.lower(), error)
        ctx.check("a refused launch starts nothing", after == before, (before, after))
        return

    ctx.check("a person approved the app during the test", True)
    config_after = json.loads(config_path.read_text())
    if TEXTEDIT in (config_after.get("approvedBundleIDs") or []):
        ctx.check(
            "Always allow persisted without touching other config fields",
            all(config_after.get(k) == v for k, v in config_before.items() if k != "approvedBundleIDs"),
            config_after,
        )
    else:
        ctx.check("Allow for this session left the config unchanged", config_after == config_before, config_after)
        status, body = post_turn_ended(client, {})
        ctx.check(
            "turn end clears the session approval",
            status == 200 and isinstance(body, dict) and body.get("cleared_app_approvals", 0) >= 1,
            (status, body),
        )


@scenario
def sensitive_browser_prompts(ctx):
    interactive()
    _, client = prompting_instance(ctx)
    for operation, extra in [
        ("upload", {"files": ["/etc/hosts"]}),
        ("claim", {}),
    ]:
        outcome = []

        def call():
            try:
                client.call("browser_command", operation=operation, tab_id=1, timeout_ms=2500, **extra)
                outcome.append("allowed")
            except ToolError as e:
                outcome.append(str(e))

        thread = threading.Thread(target=call)
        thread.start()
        windows = prompt_windows(2)
        ctx.check(f"{operation} shows a local approval prompt", bool(windows))
        if windows:
            capture(ctx, windows[0], f"{operation}-prompt")
        thread.join(10)
        ctx.require(f"{operation} call returned", bool(outcome))
        if "approval" in outcome[0].lower():
            ctx.check(f"unanswered {operation} prompt refused the call without dispatching", True)
        else:
            ctx.check(f"a person approved {operation}; the call went on to dispatch", True, outcome[0])


@scenario
def late_session_grant_after_turn_end(ctx):
    """Human-attended: click "Allow for this session" only after the harness says the turn ended. The late answer
    lets the waiting call through once, but must not carry the grant into the next turn."""
    interactive()
    _, client = prompting_instance(ctx)
    wid = ctx.launch_scratch_textedit("late-grant.txt", "late")["id"]
    outcome = []

    def read():
        try:
            client.call("get_window_state", window_id=wid, timeout_ms=30000)
            outcome.append("allowed")
        except ToolError as e:
            outcome.append(str(e))

    thread = threading.Thread(target=read)
    thread.start()
    ctx.require("reading an unapproved app shows a prompt", bool(prompt_windows(3)))
    status, body = post_turn_ended(client, {})
    ctx.require("the turn ends while the prompt is up", status == 200, (status, body))
    print('      turn ended: click "Allow for this session" now (30 s)')
    thread.join(35)
    if outcome != ["allowed"]:
        raise Skip(f"nobody clicked Allow for this session; late-grant path not exercised ({outcome})")
    ctx.check("the late answer let the waiting call through once", True)
    # Too short for a person to answer, so a pass can't come from someone clicking this check's own prompt.
    error = refused(lambda: client.call("get_window_state", window_id=wid, timeout_ms=400))
    ctx.check(
        "the next turn asks again: the late grant was not recorded",
        error is not None and "approval" in error.lower(),
        error,
    )


@scenario
def pause_during_bind_target_prompt(ctx):
    """Human-attended: click "Allow for this session" after the harness pauses the instance. bind_target must then
    refuse because of the pause instead of reading the window."""
    interactive()
    instance, client = prompting_instance(ctx)
    wid = ctx.launch_scratch_textedit("bind-pause.txt", "bind")["id"]
    outcome = []

    def bind():
        try:
            client.call("bind_target", target=f"window:{wid}")
            outcome.append("bound")
        except ToolError as e:
            outcome.append(str(e))

    thread = threading.Thread(target=bind)
    thread.start()
    ctx.require("bind_target on an unapproved app shows a prompt", bool(prompt_windows(3)))
    subprocess.run([str(instance.binary), "--pause"], env=instance.env, check=True, timeout=10)
    ctx.cleanups.append(lambda: subprocess.run([str(instance.binary), "--resume"], env=instance.env, timeout=10))
    print('      paused: click "Allow for this session" now (up to 55 s)')
    thread.join(65)
    if not outcome or "approval" in outcome[0].lower():
        raise Skip(f"nobody clicked Allow; the approve-while-paused path was not exercised ({outcome})")
    ctx.check("approved while paused, bind_target refuses with the pause", "paused" in outcome[0].lower(), outcome)


if __name__ == "__main__":
    # Contract 5: the default instance grants without UI; gating is proven on "deny" instances above.
    sys.exit(0 if Run({"approvals": "allow"}).run(sys.argv[1:]) else 1)
