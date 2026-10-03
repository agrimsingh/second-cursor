#!/usr/bin/env python3
"""Rendered task supervision checks against a disposable app instance.

Run with --ui to explicitly allow showing/activating this instance's Tasks and Setup windows.
No permission, client installation, browser installation, or user app buttons are pressed.
Task controls are pressed in the actual native panel and checked through the independent task API.
The ordinary engine suite owns background/coexistence checks; this suite intentionally activates its own
panels, restores the previous foreground process, and checks that the pointer and active Space did not move.

Failure cases: docs/acceptance-task-ui.md and docs/acceptance-setup-ui.md.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run
from run import Run, SCENARIOS, Skip, scenario

SCENARIOS.clear()


def require_ui(ctx):
    if "--ui" not in sys.argv:
        raise Skip("opens the disposable instance's native panel; add --ui to include rendered checks")
    if ctx.run.instance is None:
        raise Skip("panel tests require a disposable SC_HOME")
    if run.observe()["main_space_full_screen"]:
        raise Skip("panel presentation is tested on a desktop Space to avoid switching the user's Space")


def new_task(ctx, title, status="thinking"):
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    task = ctx.call("task", action="create", session_id=sid, title=title)["task"]
    ctx.call("task", action="update", session_id=sid, status=status, client_name="Panel fixture")
    return sid, task["id"]


def state(ctx, sid):
    return ctx.call("task", action="get", session_id=sid)["task"]


def panel(ctx, title):
    deadline = time.monotonic() + 10
    detail = None
    while time.monotonic() < deadline:
        code, detail = external_tool("window", ctx.run.instance.pid, title)
        if code == 0:
            wid = detail["window_id"]
            if not hasattr(ctx, "panel_titles"):
                ctx.panel_titles = {}
            ctx.panel_titles[wid] = title
            return wid
        time.sleep(0.1)
    raise AssertionError(f"test instance never showed {title}: {detail}")


def show_tasks(ctx):
    require_ui(ctx)
    previous = run.observe()["frontmost"]
    ctx.cleanups.append(lambda: restore_frontmost(ctx, previous))
    binary = ctx.run.instance.app / "Contents/MacOS/SecondCursor"
    env = dict(os.environ, SC_HOME=str(ctx.run.instance.home))
    result = subprocess.run([str(binary), "--show-tasks"], env=env, capture_output=True, text=True, timeout=15)
    ctx.require("CLI asks the running test instance to show tasks", result.returncode == 0, result.stderr)
    return panel(ctx, "Second Cursor Tasks")


def external_tool(command, pid, title=None, label=None):
    source = run.ROOT / "task_panel.swift"
    binary = run.ROOT / ".bin/task_panel"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        binary.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)],
                       capture_output=True, check=True, timeout=120)
    argv = [str(binary), command, str(pid)]
    if title is not None:
        argv.append(title)
    if label is not None:
        argv.append(label)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"external panel helper returned no JSON: {result.stderr or result.stdout}")
    return result.returncode, data


def external_ax(ctx, wid, command="inspect", label=None):
    code, data = external_tool(command, ctx.run.instance.pid, ctx.panel_titles[wid], label)
    ctx.require("external AX inspector can operate the test panel", code == 0, data)
    return data


def restore_frontmost(ctx, previous):
    # Address only the already-running process; bundle activation could launch a closed app or select
    # another instance. Record cleanup failures because the general harness catches cleanup exceptions.
    try:
        current = run.observe()["frontmost"]
        if current["pid"] != previous["pid"]:
            code, detail = external_tool("activate", previous["pid"])
            ctx.check("previous foreground process accepts restoration", code == 0, detail)
            deadline = time.monotonic() + 5
            while current["pid"] != previous["pid"] and time.monotonic() < deadline:
                time.sleep(0.1)
                current = run.observe()["frontmost"]
        ctx.check("test panel restores the previous foreground process", current["pid"] == previous["pid"],
                  {"before": previous, "after": current})
    except Exception as error:
        ctx.check("test panel restores the previous foreground process", False, str(error))


def screenshot(ctx, wid):
    code, window = external_tool("window", ctx.run.instance.pid, ctx.panel_titles[wid])
    ctx.require("external screenshot still addresses the exact test panel",
                code == 0 and window.get("window_id") == wid, window)
    path = ctx.run.dir / "screenshots" / f"{ctx.name}-panel-{len(ctx.screenshots)}.png"
    result = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(wid), str(path)],
                            capture_output=True, text=True, timeout=15)
    ctx.require("external capture saved the test panel as a PNG",
                result.returncode == 0 and path.is_file() and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
                result.stderr)
    ctx.screenshots.append(path.relative_to(ctx.run.dir).as_posix())


def tree(ctx, wid):
    nodes = external_ax(ctx, wid)["elements"]
    return "\n".join(node["role"] + " " + node["label"] for node in nodes)


def press(ctx, wid, title):
    result = external_ax(ctx, wid, "press", title)
    ctx.require(f"native {title} button accepted its accessibility press", result.get("ok"), result)


def select_task(ctx, wid, title):
    external_ax(ctx, wid, "select", title)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        content = tree(ctx, wid)
        if "Task details for " + title in content:
            return content
        time.sleep(0.1)
    raise AssertionError("selected task detail did not appear")


def wait_status(ctx, sid, expected):
    deadline = time.monotonic() + 3
    current = state(ctx, sid)
    while current["status"] != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        current = state(ctx, sid)
    ctx.require(f"panel action changes runtime state to {expected}", current["status"] == expected, current)
    return current


@scenario
def task_panel_controls(ctx):
    require_ui(ctx)
    first, _ = new_task(ctx, "Panel task alpha")
    second, _ = new_task(ctx, "Panel task beta")
    wid = show_tasks(ctx)
    content = select_task(ctx, wid, "Panel task alpha")
    ctx.check("two named tasks are visible independently", "Panel task alpha" in content and "Panel task beta" in content)
    press(ctx, wid, "Pause")
    wait_status(ctx, first, "paused")
    ctx.check("pausing alpha leaves beta thinking", state(ctx, second)["status"] == "thinking")
    press(ctx, wid, "Resume task")
    wait_status(ctx, first, "thinking")
    press(ctx, wid, "Take over")
    wait_status(ctx, first, "needs_user")
    content = tree(ctx, wid)
    ctx.check("takeover explains that user changes precede resume", "Make your changes" in content, content)
    screenshot(ctx, wid)
    press(ctx, wid, "Resume task")
    wait_status(ctx, first, "thinking")
    press(ctx, wid, "Cancel task")
    wait_status(ctx, first, "cancelled")
    ctx.check("cancelling alpha leaves beta thinking", state(ctx, second)["status"] == "thinking")
    content = tree(ctx, wid)
    ctx.check("cancelled detail stays available", "Cancelled" in content and "Last verified result" in content, content)
    screenshot(ctx, wid)


@scenario
def task_panel_history(ctx):
    require_ui(ctx)
    completed, _ = new_task(ctx, "Panel completed fixture", "completed")
    failed, _ = new_task(ctx, "Panel failed fixture", "failed")
    wid = show_tasks(ctx)
    content = select_task(ctx, wid, "Panel completed fixture")
    ctx.check("completed and failed tasks remain in the default list",
              "Panel completed fixture" in content and "Panel failed fixture" in content, content)
    ctx.check("unverified work has no manufactured successful result", "No verified result yet." in content, content)
    ctx.check("finished task has no resume control", "AXButton Resume task" not in content, content)
    screenshot(ctx, wid)
    content = select_task(ctx, wid, "Panel failed fixture")
    ctx.check("failed task explains its final state", "This task failed." in content, content)
    ctx.check("rendering history does not revive finished tasks",
              state(ctx, completed)["status"] == "completed" and state(ctx, failed)["status"] == "failed")


@scenario
def setup_panel_checks(ctx):
    require_ui(ctx)
    wid = show_tasks(ctx)
    press(ctx, wid, "Setup…")
    setup = panel(ctx, "Second Cursor Setup")
    content = tree(ctx, setup)
    ctx.check("setup exposes permissions, client configuration, and browser connection",
              all(word in content for word in ["Accessibility", "Screen Recording", "Connect an agent", "Connect Chrome"]), content)
    press(ctx, setup, "Check connection")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        content = tree(ctx, setup)
        if "Connection check passed" in content or "Connection verified" in content:
            break
        time.sleep(0.2)
    ctx.check("read-only setup check shows a successful real result",
              "Connection check passed" in content or "Connection verified" in content, content)
    screenshot(ctx, setup)


if __name__ == "__main__":
    sys.exit(0 if Run(config_overrides={"approvals": "allow", "appApproval": "off"},
                     installed=False, browser=False).run(set(sys.argv[1:])) else 1)
