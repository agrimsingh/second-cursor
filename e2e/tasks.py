#!/usr/bin/env python3
"""Task lifecycle and takeover regression suite, using disposable runtime sessions and scratch apps.

Failure cases are enumerated before the scenarios in docs/acceptance-tasks.md. This suite proves
wire behavior and actual refused/admitted input; status labels alone do not count as successful control.
No scenario targets the user's installed process. The restart scenario preserves only this suite's SC_HOME.
Use --off-space explicitly to leave native fixtures on their normal, off-screen Space. This preserves all
assertions and records separate off-Space coverage rather than weakening the agent-display release gate.
"""
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run
from run import FixtureServer, MCP, Run, SCENARIOS, Skip, ToolError, element_index, scenario, value_of

SCENARIOS.clear()
OFF_SPACE = "--off-space" in sys.argv


def launch_textedit(ctx, filename, text):
    window = ctx.launch_scratch_textedit(filename, text, on_agent_display=not OFF_SPACE)
    if OFF_SPACE:
        ctx.check("off-Space coverage: fixture stays off the agent display", not window.get("on_agent_display"), window)
        ctx.check("off-Space coverage: fixture starts off screen", window.get("is_on_screen") is False, window)
    return window


def new_task(ctx, title):
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    created = ctx.call("task", action="create", session_id=sid, title=title)["task"]
    return sid, created["id"]


def task(ctx, sid, action="get", **fields):
    return ctx.call("task", action=action, session_id=sid, **fields)["task"]


def refused(ctx, tool, **fields):
    try:
        ctx.call(tool, **fields)
    except ToolError as error:
        return str(error)
    return None


def read(ctx, sid, wid):
    return ctx.call("get_window_state", session_id=sid, window_id=wid, full=True, include_screenshot=False)


def area_of(snapshot):
    area = element_index(snapshot["tree"], "AXTextArea")
    if area is None:
        raise AssertionError("scratch text area missing from fresh observation")
    return area


def change(ctx, sid, wid, area, text):
    return ctx.call("set_value", session_id=sid, window_id=wid, element_index=area, value=text, observe="none")


@scenario
def task_lifecycle(ctx):
    sid, tid = new_task(ctx, "Task lifecycle fixture")
    initial = task(ctx, sid)
    ctx.check("created task has a name and owns the explicit runtime session",
              initial["id"] == tid and initial["title"] == "Task lifecycle fixture" and sid in initial["sessions"], initial)
    updated = task(ctx, sid, "update", status="thinking", title="Waiting for model", client_name="E2E task host")
    ctx.check("host lifecycle and heartbeat are explicit",
              updated["status"] == "thinking" and updated["last_heartbeat_at"] is not None
              and updated["client_name"] == "E2E task host", updated)
    listed = ctx.call("task", action="list", session_id=sid)["tasks"]
    ctx.check("list includes the current named task", any(item["id"] == tid for item in listed), listed)
    finished = task(ctx, sid, "update", status="completed")
    ctx.check("only explicit host completion ends an idle task", finished["status"] == "completed", finished)
    error = refused(ctx, "task", action="resume", session_id=sid)
    ctx.check("completed tasks cannot resume", error is not None, error)
    next_task = task(ctx, sid, "create", title="A new task in the same session")
    ctx.check("explicit create after completion gets a distinct task", next_task["id"] != tid, next_task)


@scenario
def task_pause_resume(ctx):
    window = launch_textedit(ctx, "task-pause.txt", "before")
    wid = window["id"]
    sid, tid = new_task(ctx, "Pause and resume fixture")
    area = area_of(read(ctx, sid, wid))
    # Open the second window through the same task so it belongs to the same process and never takes
    # ownership through the harness's default session. This exercises per-step batch target selection.
    ctx.call("press_key", session_id=sid, window_id=wid, keys="cmd+n", observe="none")
    other_window = ctx.wait_for_window("TextEdit", pid=window["pid"], title="Untitled")
    other = other_window["id"]
    ctx.require("both task targets belong to the same scratch process", other_window["pid"] == window["pid"])
    other_area = area_of(read(ctx, sid, other))
    change(ctx, sid, other, other_area, "other before")
    area = area_of(read(ctx, sid, wid))
    change(ctx, sid, wid, area, "ready")
    paused = task(ctx, sid, "pause")
    ctx.check("pause gates a named task", paused["status"] == "paused" and paused["id"] == tid, paused)
    error = refused(ctx, "set_value", session_id=sid, window_id=wid, element_index=area, value="must not land")
    ctx.check("paused input is refused", error is not None, error)
    error = refused(ctx, "task", action="update", session_id=sid, status="running")
    ctx.check("a host heartbeat cannot bypass the user pause", error is not None, error)
    area = area_of(read(ctx, sid, wid))
    resumed = task(ctx, sid, "resume")
    ctx.check("resume requires a fresh target observation", resumed["requires_fresh_observation"], resumed)
    error = refused(ctx, "set_value", session_id=sid, window_id=wid, element_index=area, value="still stale")
    ctx.check("a read while paused does not satisfy resume", error is not None, error)
    area = area_of(read(ctx, sid, wid))
    change(ctx, sid, wid, area, "resumed safely")
    ctx.check("freshly observed target can be changed", value_of(read(ctx, sid, wid)["tree"]) == "resumed safely")
    error = refused(ctx, "set_value", session_id=sid, window_id=other, element_index=other_area, value="wrong target freshness")
    ctx.check("refreshing one target does not authorize another", error is not None, error)
    try:
        stale_batch = ctx.call("batch", session_id=sid, window_id=wid, observe="none", steps=[
            {"tool": "type_text", "window_id": other, "element_index": other_area, "text": " forbidden"},
        ])
        steps = stale_batch.get("steps", [])
        batch_error = steps[0].get("error") if steps and not steps[0].get("ok") else None
    except ToolError as failure:
        batch_error = str(failure)
    ctx.check("batch steps cannot bypass freshness by targeting a second same-process window",
              batch_error is not None and "read target" in batch_error.lower(), batch_error)
    other_snapshot = read(ctx, sid, other)
    ctx.check("the stale batch step did not change the second window", value_of(other_snapshot["tree"]) == "other before")
    other_area = area_of(other_snapshot)
    refreshed_batch = ctx.call("batch", session_id=sid, window_id=wid, observe="none", steps=[
        {"tool": "type_text", "window_id": other, "element_index": other_area, "text": " authorized"},
    ])
    ctx.check("observing the second window allows its batch input", all(step.get("ok") for step in refreshed_batch["steps"]), refreshed_batch)
    ctx.check("the authorized second-window batch reaches the app", "authorized" in value_of(read(ctx, sid, other)["tree"]))
    takeover = task(ctx, sid, "takeover")
    ctx.check("takeover is an explicit needs-user state", takeover["status"] == "needs_user", takeover)
    error = refused(ctx, "set_value", session_id=sid, window_id=wid, element_index=area, value="must not land either")
    ctx.check("takeover refuses new input", error is not None, error)
    task(ctx, sid, "resume")
    area = area_of(read(ctx, sid, wid))
    change(ctx, sid, wid, area, "after takeover")
    settled = task(ctx, sid)
    ctx.check("successful input returns to thinking without claiming task completion",
              settled["status"] == "thinking" and settled["in_flight_count"] == 0, settled)
    ctx.check("only authorized writes reached the scratch document", value_of(read(ctx, sid, wid)["tree"]) == "after takeover")


@scenario
def task_inflight_takeover(ctx):
    wid = launch_textedit(ctx, "task-inflight.txt", "untouched")["id"]
    sid, _ = new_task(ctx, "In-flight takeover fixture")
    area = area_of(read(ctx, sid, wid))
    result = {}

    def worker():
        try:
            result["output"] = ctx.call("batch", session_id=sid, window_id=wid, observe="none", steps=[
                {"tool": "wait", "ms": 1800},
                {"tool": "set_value", "element_index": area, "value": "late input must not land"},
            ])
        except Exception as error:
            result["error"] = str(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and task(ctx, sid)["in_flight_count"] == 0:
        time.sleep(0.02)
    running = task(ctx, sid)
    ctx.require("batch is tracked while input is queued", running["in_flight_count"] > 0, running)
    takeover = task(ctx, sid, "takeover")
    ctx.check("takeover stops admission while the worker is still draining",
              takeover["status"] == "needs_user" and takeover["in_flight_count"] > 0, takeover)
    error = refused(ctx, "task", action="resume", session_id=sid)
    ctx.check("resume refuses while the old operation is in flight", error is not None, error)
    thread.join(timeout=10)
    ctx.require("the interrupted worker settles", not thread.is_alive(), result)
    ctx.check("takeover prevents the queued mutation from landing", value_of(read(ctx, sid, wid)["tree"]) == "untouched", result)
    settled = task(ctx, sid)
    ctx.check("in-flight accounting drains before resume", settled["in_flight_count"] == 0, settled)
    task(ctx, sid, "resume")
    area = area_of(read(ctx, sid, wid))
    change(ctx, sid, wid, area, "new operation")
    ctx.check("fresh input works after the old worker is reconciled", value_of(read(ctx, sid, wid)["tree"]) == "new operation")


@scenario
def task_cancel(ctx):
    wid = launch_textedit(ctx, "task-cancel.txt", "unchanged")["id"]
    sid, _ = new_task(ctx, "Cancellation fixture")
    area = area_of(read(ctx, sid, wid))
    cancelled = task(ctx, sid, "cancel")
    ctx.check("cancellation is terminal", cancelled["status"] == "cancelled", cancelled)
    for action in ("resume", "update"):
        fields = {"status": "running"} if action == "update" else {}
        error = refused(ctx, "task", action=action, session_id=sid, **fields)
        ctx.check(f"{action} cannot revive cancelled work", error is not None, error)
    error = refused(ctx, "set_value", session_id=sid, window_id=wid, element_index=area, value="cancelled input")
    ctx.check("cancelled work cannot mutate", error is not None, error)
    ctx.check("the cancelled write never reached the app", value_of(read(ctx, sid, wid)["tree"]) == "unchanged")


@scenario
def task_browser_cancel_recreate(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("isolated browser is not connected")
    sid, _ = new_task(ctx, "Browser cancelled task")
    with FixtureServer() as base:
        ctx.call("browser_open", session_id=sid, url=f"{base}/form.html")
        cancelled = task(ctx, sid, "cancel")
        created = task(ctx, sid, "create", title="Browser replacement task")
        ctx.check("new task advances the browser session control generation",
                  created["control_generation"] > cancelled["control_generation"], (cancelled, created))
        opened = ctx.call("browser_open", session_id=sid, url=f"{base}/form.html")
        tab = opened["tab"]["id"]
        outline = ctx.call("browser_snapshot", session_id=sid, tab_id=tab, full=True)["outline"]
        field = re.search(r'\[(e\d+)\] textbox "Name"', outline)
        submit = re.search(r'\[(e\d+)\] button "Submit"', outline)
        ctx.require("fresh replacement task reads the form", field and submit, outline)
        ctx.call("browser_type", session_id=sid, tab_id=tab, ref=field[1], text="Resumed task", clear=True)
        ctx.call("browser_click", session_id=sid, tab_id=tab, ref=submit[1])
        result = ctx.call("browser_snapshot", session_id=sid, tab_id=tab, full=True)
        ctx.check("cancel then create does not leave browser input paused",
                  "Hello, Resumed task" in result["outline"], result["outline"])


@scenario
def task_browser_control_failure(ctx):
    instance = ctx.run.instance
    if instance is None or instance.browser is None or not ctx.call("browser_status").get("connected"):
        raise Skip("control failure scenario requires the isolated test browser")
    sid, _ = new_task(ctx, "Browser acknowledgement failure fixture")
    ctx.call("browser_open", session_id=sid, url="about:blank")
    task(ctx, sid, "pause")
    instance.browser.stop()
    instance.browser = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and ctx.call("browser_status").get("connected"):
            time.sleep(0.05)
        ctx.require("the isolated bridge disconnected", not ctx.call("browser_status").get("connected"))
        resume_error = refused(ctx, "task", action="resume", session_id=sid)
        ctx.check("a disconnected browser refuses the resume request", resume_error is not None, resume_error)
        failed = task(ctx, sid)
        ctx.check("failed browser control acknowledgement leaves a visible needs-user state",
                  failed["status"] == "needs_user" and bool(failed.get("last_control_error")), failed)
        error = refused(ctx, "browser_open", session_id=sid, url="about:blank")
        ctx.check("a failed resume cannot admit new browser input",
                  error is not None, error)
        still_gated = task(ctx, sid)
        ctx.check("the refused browser request preserves the control-failure gate",
                  still_gated["status"] == "needs_user" and bool(still_gated.get("last_control_error")), still_gated)
    finally:
        instance.start_browser()


@scenario
def task_heartbeat(ctx):
    sid, tid = new_task(ctx, "Long model reasoning fixture")
    initial = task(ctx, sid, "update", status="thinking")
    # Cross the former 60-second activity timeout without blocking the test operator's terminal feedback.
    print("Task heartbeat: waiting across the previous 60-second idle expiry", flush=True)
    time.sleep(31)
    print("Task heartbeat: task is still intentionally thinking", flush=True)
    time.sleep(31)
    quiet = task(ctx, sid)
    ctx.check("no input for over a minute does not finish or hide task state",
              quiet["id"] == tid and quiet["status"] == "thinking"
              and quiet["last_heartbeat_at"] == initial["last_heartbeat_at"], quiet)
    beat = task(ctx, sid, "update", status="thinking")
    ctx.check("host heartbeat advances independently of tool activity",
              beat["last_heartbeat_at"] != initial["last_heartbeat_at"], beat)


@scenario
def task_restart_recovery(ctx):
    instance = ctx.run.instance
    if instance is None:
        raise Skip("restart recovery only operates on a disposable SC_HOME")
    sid, tid = new_task(ctx, "Restart recovery fixture")
    task(ctx, sid, "update", status="thinking")
    journal = instance.home / "tasks.json"
    ctx.require("task metadata is atomically persisted", journal.exists(), journal)
    saved = json.loads(journal.read_text())
    record = next(item for item in saved["tasks"] if item["id"] == tid)
    ctx.check("journal records metadata without command or screenshot payloads",
              record["title"] == "Restart recovery fixture" and not
              ({"arguments", "prompt", "screenshots", "url", "text"} & set(record)), record)
    old_pid = instance.pid
    ctx.require("restart is scoped to the disposable app process",
                old_pid and f"SC_HOME={instance.home}" in subprocess.run(
                    ["ps", "-Eww", "-p", str(old_pid), "-o", "command="], capture_output=True, text=True).stdout)
    os.kill(old_pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while run.alive(old_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    ctx.require("the disposable app stopped", not run.alive(old_pid))
    subprocess.run(["open", "-n", "-g", "--env", f"SC_HOME={instance.home}", "-a", str(instance.app)], check=True)
    replacement = MCP(instance.url, instance.token, ctx.run.transcript, timeout=3)
    deadline = time.monotonic() + 20
    while True:
        try:
            replacement.initialize()
            break
        except (OSError, ToolError):
            if time.monotonic() >= deadline:
                raise AssertionError("disposable runtime did not restart")
            time.sleep(0.2)
    instance.pid = instance._pid_file()
    replacement.timeout = 90
    ctx.run.mcp = replacement
    instance.mcp = replacement
    retained = ctx.call("task", action="get", session_id=sid)["task"]
    ctx.check("a persisted transport session recovers the interruption gate instead of creating a new task",
              retained["id"] == tid and retained["status"] == "interrupted", retained)
    marker = ctx.scratch / "restart-must-not-launch.txt"
    marker.write_text("no input before explicit recovery")
    try:
        unexpected = ctx.call("launch_app", session_id=sid, app="TextEdit", urls=[str(marker)],
                              new_instance=True, on_agent_display=True)
        if unexpected.get("app", {}).get("pid"):
            ctx.kill_on_exit(unexpected["app"]["pid"])
        rejected = None
    except ToolError as error:
        rejected = str(error)
    ctx.check("reconnected interrupted sessions cannot mutate before explicit resume",
              rejected is not None and "interrupted" in rejected.lower(), rejected)
    recovered = next(item for item in json.loads(journal.read_text())["tasks"] if item["id"] == tid)
    ctx.check("restart preserves task identity and marks unfinished work interrupted",
              recovered["id"] == tid and recovered["status"] == "interrupted" and recovered["inFlightCount"] == 0,
              recovered)
    rebound = ctx.call("task", action="bind", task_id=tid)["task"]
    ctx.check("a fresh live session explicitly rebinds the interrupted task", rebound["id"] == tid, rebound)
    resumed = ctx.call("task", action="resume", task_id=tid)["task"]
    ctx.check("explicit recovery resumes the task without claiming completion", resumed["status"] == "thinking", resumed)


if __name__ == "__main__":
    if "--installed" in sys.argv or os.environ.get("SC_E2E_TARGET") == "installed":
        raise SystemExit("tasks.py requires a disposable runtime; remove --installed / SC_E2E_TARGET=installed")
    suite = Run(installed=False)
    if OFF_SPACE:
        suite.notes.append("Off-Space coverage: native fixtures stayed on their normal Space with off-screen state asserted; "
                           "all selected scenario assertions ran unchanged. This is separate from the agent-display release gate.")
    sys.exit(0 if suite.run(sys.argv[1:]) else 1)
