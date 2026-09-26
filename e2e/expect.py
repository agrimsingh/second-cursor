#!/usr/bin/env python3
"""`expect` postconditions on actions, notification-driven settling, and `wait_for`, driven over MCP like a client.

Ways it can fail, and the scenario that covers each:
- A malformed expect (a string that is not JSON, no condition, element_index without value or value without
  element_index, an empty text, an unknown or garbled field, a timeout that is not a number) is ignored, is refused
  only after the action ran, or is refused without naming expect. expect_shapes
- expect sent as a JSON string, the way Gemini through Cursor sends nested objects, is refused or ignored. expect_shapes
- The schema nests an object inside expect, which Gemini would stringify, or wait_for is not read-only. expect_shapes
- verified is reported for a condition that does not hold, or failed for one that does. expect_verified, expect_failed
- The before-check is skipped or runs after the action, so a condition that already held reads as verified instead of
  preexisting. expect_verified, expect_gone
- A condition that never holds makes the call an error, drops the observation, returns well before or well after
  timeout_ms, or timeout_ms is not capped at 10 s. expect_failed, wait_for_timeout
- observe="none" skips the check. expect_verified
- gone waits for the wrong polarity. expect_gone
- element_index + value matches a substring or another element, or an unknown index is only found out after the
  action ran. expect_element_value
- expect on batch is checked before the last step, or waits out its timeout after the batch stopped early.
  expect_on_batch
- The observation is taken before the wait, so it misses the change expect waited for. expect_waits_past_settle
- Settling ends before a change that lands ~150 ms after the action with no notification in between, before a
  keystroke the app handles a moment later, or before the change in an app that posts no notifications at all.
  settle_catches_late_change, silent_app_fallback
- Waiting holds the app's queue, so another session's call on the same app stalls until the wait times out.
  wait_for_concurrent_change
- wait_for wakes only on its 250 ms fallback timer instead of on the app's notification (slow), or never re-checks
  an app that posts nothing (times out). expect_waits_past_settle, silent_app_fallback
- wait_for on a condition that already holds waits anyway, or reports it as anything but verified with waited_ms 0.
  wait_for_timeout
- wait_for by app ignores the window the session observed, does not keep the agent's presence up like a read, or
  drops its caption. wait_for_timeout
- Observers leak: exited apps keep theirs, or the cache grows past 16 apps. observer_bookkeeping

Usage:  SC_E2E_APP=build/SecondCursor.app python3 e2e/expect.py [scenario ...]
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl)
"""
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from run import (ROOT, Run, SCENARIOS, alive, clear_calculator, element_index, refused, scenario, shown, step,
                 value_of)

SCENARIOS.clear()
FIXTURE_BUTTONS = ("Change in 150 ms", "Change in 400 ms", "Silent change")


def delayed_app():
    """e2e/delayed.swift as a minimal .app, rebuilt when the source changes; LaunchServices launches bundles."""
    app = ROOT / ".bin/SCDelayed.app"
    binary = app / "Contents/MacOS/SCDelayed"
    source = ROOT / "delayed.swift"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        shutil.rmtree(app, ignore_errors=True)
        binary.parent.mkdir(parents=True)
        subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)], check=True, capture_output=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "dev.secondcursor.e2e.delayed", "CFBundleName": "SCDelayed",
            "CFBundleExecutable": "SCDelayed", "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "14.0",
        }))
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
    return app


def launch_delayed(ctx):
    """A fresh fixture window on the agent display; returns its id and its buttons' indexes."""
    launched = ctx.call("launch_app", app=str(delayed_app()), new_instance=True, on_agent_display=True)
    pid = launched["app"]["pid"]
    ctx.kill_on_exit(pid)
    wid = ctx.wait_for_window("SCDelayed", pid=pid, title="SC Delayed")["id"]
    tree = ctx.tree(wid)
    buttons = {label: element_index(tree, "AXButton", label) for label in FIXTURE_BUTTONS}
    ctx.require("the fixture's buttons are indexed", None not in buttons.values(), tree)
    return wid, pid, buttons


def textedit(ctx, name, text):
    wid = ctx.launch_scratch_textedit(name, text, on_agent_display=True)["id"]
    area = element_index(ctx.tree(wid), "AXTextArea")
    ctx.require("the text area is indexed", area is not None)
    return wid, area


def expect_of(result):
    return result.get("expect") or {}


def turn_ended(ctx):
    request = urllib.request.Request(
        ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {ctx.mcp.token}", "Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=15).read()


@scenario
def expect_shapes(ctx):
    tools = {t["name"]: t for t in ctx.mcp.rpc("tools/list")["tools"]}
    takers = ["click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action", "select_text", "paste",
              "batch"]
    ctx.check("expect is advertised on every native action and on batch",
              all("expect" in tools[n]["inputSchema"]["properties"] for n in takers),
              [n for n in takers if "expect" not in tools[n]["inputSchema"]["properties"]])
    schema = tools["click"]["inputSchema"]["properties"]["expect"]
    ctx.check("expect's schema is flat: five scalar fields, nothing nested",
              set(schema.get("properties", {})) == {"text", "element_index", "value", "gone", "timeout_ms"}
              and all(p.get("type") in ("string", "integer", "boolean") for p in schema["properties"].values()), schema)
    wait = tools.get("wait_for") or {}
    ctx.check("wait_for is listed and read-only", wait.get("annotations", {}).get("readOnlyHint") is True, wait)
    ctx.check("wait_for takes a window, the condition fields and a caption",
              {"window_id", "app", "text", "element_index", "value", "gone", "timeout_ms", "caption"}
              <= set(wait.get("inputSchema", {}).get("properties", {})), wait.get("inputSchema"))

    wid, area = textedit(ctx, "shapes.txt", "alpha")
    bad = {
        "an unparseable string": "[object Object]",
        "an empty object": {},
        "element_index without value": {"element_index": area},
        "value without element_index": {"value": "alpha"},
        "an empty text": {"text": ""},
        "a garbled field": {"expectFOURTH_text": "alpha"},
        "a timeout that is not a number": {"text": "alpha", "timeout_ms": "soon"},
    }
    for label, value in bad.items():
        error = refused(ctx, "type_text", window_id=wid, element_index=area, text="Z", expect=value)
        ctx.check(f"{label} is refused, naming expect", error and "expect" in error, error)
    error = refused(ctx, "type_text", window_id=wid, element_index=area, text="Z", expect={"expectFOURTH_text": "a"})
    ctx.check("the refusal names the field it did not recognize", error and "expectFOURTH_text" in error, error)
    error = refused(ctx, "batch", window_id=wid, steps=[step("type_text", element_index=area, text="Z")], expect={})
    ctx.check("batch refuses a malformed expect too", error and "expect" in error, error)
    ctx.check("nothing was typed by the refused calls", value_of(ctx.tree(wid)) == "alpha", value_of(ctx.tree(wid)))

    typed = ctx.call("type_text", window_id=wid, element_index=area, text=" beta", expect=json.dumps({"text": "beta"}))
    ctx.check("expect as a JSON string is accepted and verified", expect_of(typed).get("status") == "verified", typed)

    error = refused(ctx, "wait_for", window_id=wid)
    ctx.check("wait_for without a condition is refused", error and "text" in error, error)
    error = refused(ctx, "wait_for", window_id=wid, element_index=area)
    ctx.check("wait_for with element_index but no value is refused", error and "value" in error, error)


@scenario
def expect_verified(ctx):
    # Typing goes in at the caret, which starts at the beginning of the document.
    wid, area = textedit(ctx, "verified.txt", "hello")
    typed = ctx.call("type_text", window_id=wid, element_index=area, text="world ", expect={"text": "world hello"})
    seen = expect_of(typed)
    ctx.check("typing the expected text reports verified", seen.get("status") == "verified", seen)
    ctx.check("with an integer waited_ms and a detail saying where the text was found",
              isinstance(seen.get("waited_ms"), int) and "world hello" in seen.get("detail", ""), seen)
    ctx.check("the observation still comes back, showing the text",
              "world hello" in shown((typed.get("observation") or {}).get("tree", "")), typed.get("observation"))

    again = ctx.call("type_text", window_id=wid, element_index=area, text="!", expect={"text": "hello"})
    ctx.check("a condition that already held reports preexisting", expect_of(again).get("status") == "preexisting",
              expect_of(again))
    blind = ctx.call("type_text", window_id=wid, element_index=area, text="?", observe="none",
                     expect={"text": "world !?hello"})
    ctx.check('observe="none" still checks expect, and returns no observation',
              expect_of(blind).get("status") == "verified" and "observation" not in blind, blind)


@scenario
def expect_failed(ctx):
    wid, area = textedit(ctx, "failed.txt", "draft")
    started = time.time()
    typed = ctx.call("type_text", window_id=wid, element_index=area, text="x",
                     expect={"text": "never appears", "timeout_ms": 800})
    elapsed = time.time() - started
    seen = expect_of(typed)
    ctx.check("a condition that never holds reports failed, and the call is not an error",
              seen.get("status") == "failed" and typed.get("ok") is True, typed)
    ctx.check("it waited about timeout_ms (800)", 750 <= seen.get("waited_ms", 0) <= 1400, seen)
    ctx.check("detail says what was looked for and not found", "never appears" in seen.get("detail", ""), seen)
    ctx.check("the observation, taken after the wait, shows the typed text",
              value_of((typed.get("observation") or {}).get("tree", "")) == "xdraft", typed.get("observation"))
    ctx.check("the call returned soon after the timeout", elapsed < 3, f"{elapsed:.2f} s")


@scenario
def expect_gone(ctx):
    wid, area = textedit(ctx, "gone.txt", "DRAFT: plan")
    cleared = ctx.call("set_value", window_id=wid, element_index=area, value="plan",
                       expect={"text": "DRAFT", "gone": True})
    ctx.check("gone: removing the text reports verified", expect_of(cleared).get("status") == "verified",
              expect_of(cleared))
    again = ctx.call("set_value", window_id=wid, element_index=area, value="plan v2",
                     expect={"text": "DRAFT", "gone": True})
    ctx.check("gone: text that was already absent reports preexisting",
              expect_of(again).get("status") == "preexisting", expect_of(again))
    back = ctx.call("set_value", window_id=wid, element_index=area, value="DRAFT again",
                    expect={"text": "DRAFT", "gone": True, "timeout_ms": 500})
    seen = expect_of(back)
    ctx.check("gone: text that comes back reports failed, saying where it is",
              seen.get("status") == "failed" and "DRAFT" in seen.get("detail", "") and "AXTextArea" in seen.get("detail", ""),
              seen)


@scenario
def expect_element_value(ctx):
    wid, area = textedit(ctx, "value.txt", "start")
    exact = ctx.call("set_value", window_id=wid, element_index=area, value="exact",
                     expect={"element_index": area, "value": "exact"})
    ctx.check("element_index + value: the exact value reports verified", expect_of(exact).get("status") == "verified",
              expect_of(exact))
    ctx.check("its detail quotes the element's value", '"exact"' in expect_of(exact).get("detail", ""), expect_of(exact))
    longer = ctx.call("type_text", window_id=wid, element_index=area, text="ly",
                      expect={"element_index": area, "value": "exact", "timeout_ms": 500})
    seen = expect_of(longer)
    ctx.check("a value that only contains the expected one reports failed, with the actual value",
              seen.get("status") == "failed" and '"exactly"' in seen.get("detail", ""), seen)
    prefix = ctx.call("set_value", window_id=wid, element_index=area, value="exactly!",
                      expect={"element_index": area, "value": "exactly", "gone": True})
    ctx.check("gone with element_index + value: the value changing away reports verified",
              expect_of(prefix).get("status") == "verified", expect_of(prefix))
    error = refused(ctx, "set_value", window_id=wid, element_index=area, value="unexpected",
                    expect={"element_index": 99999, "value": "x"})
    ctx.check("an element_index the window does not have is refused, naming expect",
              error and "99999" in error and "expect" in error, error)
    ctx.check("and the action did not run", value_of(ctx.tree(wid)) == "exactly!", value_of(ctx.tree(wid)))


@scenario
def expect_on_batch(ctx):
    wid, area = textedit(ctx, "batch-expect.txt", "start")
    result = ctx.call("batch", window_id=wid, expect={"element_index": area, "value": "ab"}, steps=[
        step("set_value", element_index=area, value=""),
        step("type_text", element_index=area, text="a"),
        step("type_text", element_index=area, text="b"),
    ])
    seen = expect_of(result)
    ctx.check("batch: the value after the last step reports verified", seen.get("status") == "verified", seen)
    ctx.check("batch: the observation shows the final value",
              value_of((result.get("observation") or {}).get("tree", "")) == "ab", result.get("observation"))

    result = ctx.call("batch", window_id=wid, expect={"text": "abcd", "timeout_ms": 5000}, steps=[
        step("type_text", text="c"),
        step("click", element_index=99999),
        step("type_text", text="d"),
    ])
    seen = expect_of(result)
    ctx.check("a batch that stopped early reports failed", result.get("stopped_early") and seen.get("status") == "failed",
              (result.get("steps"), seen))
    ctx.check("and is checked once instead of waiting out its 5 s timeout", seen.get("waited_ms", 99999) < 1000, seen)


@scenario
def expect_waits_past_settle(ctx):
    wid, _, buttons = launch_delayed(ctx)
    clicked = ctx.call("click", window_id=wid, element_index=buttons["Change in 400 ms"],
                       expect={"text": "state: changed 1"})
    seen = expect_of(clicked)
    ctx.check("expect waits for a change that lands 400 ms after the click", seen.get("status") == "verified", seen)
    ctx.check("waited_ms reflects the delay", 300 <= seen.get("waited_ms", 0) <= 1500, seen)
    tree = (clicked.get("observation") or {}).get("tree", "")
    ctx.check("the observation is taken after the wait, so it shows the change", "state: changed 1" in shown(tree), tree)

    ctx.call("click", window_id=wid, element_index=buttons["Change in 150 ms"], observe="none")
    waited = ctx.call("wait_for", window_id=wid, text="state: changed 2", timeout_ms=3000)
    ctx.check("wait_for sees a change that lands ~150 ms after the click", waited.get("status") == "verified", waited)
    # Its fallback timer re-checks at 0 and 250 ms, so waking before 250 ms can only come from the notification.
    ctx.check("it woke on the app's notification, before its 250 ms fallback re-check",
              waited.get("waited_ms", 999) < 240, waited.get("waited_ms"))
    ctx.check("wait_for returns the window's diff, showing the change",
              "state: changed 2" in shown((waited.get("observation") or {}).get("tree", "")), waited.get("observation"))


@scenario
def settle_catches_late_change(ctx):
    wid, _, buttons = launch_delayed(ctx)
    for n in range(1, 4):
        clicked = ctx.call("click", window_id=wid, element_index=buttons["Change in 150 ms"])
        tree = (clicked.get("observation") or {}).get("tree", "")
        ctx.check(f"click {n}: the observation includes the change that landed ~150 ms after the click",
                  f'+  AXStaticText value="state: changed {n}"' in tree, tree)

    calc = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    calc_wid = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])["id"]
    clear_calculator(ctx, calc_wid)
    for digits in ("7", "78", "789"):
        pressed = ctx.call("press_key", window_id=calc_wid, keys=digits[-1])
        tree = (pressed.get("observation") or {}).get("tree", "")
        ctx.check(f"press_key {digits[-1]}: the keystroke, handled after the call returned, shows as {digits}",
                  f'"Edit field" value="{digits}"' in shown(tree), (pressed.get("route"), tree))


@scenario
def silent_app_fallback(ctx):
    """A fresh fixture has posted no notification yet, and its silent text never posts one."""
    wid, _, buttons = launch_delayed(ctx)
    clicked = ctx.call("click", window_id=wid, element_index=buttons["Silent change"])
    tree = (clicked.get("observation") or {}).get("tree", "")
    ctx.check("an app that posts nothing still has its change in the observation", "silent: changed 1" in shown(tree), tree)

    ctx.call("click", window_id=wid, element_index=buttons["Silent change"], observe="none")
    waited = ctx.call("wait_for", window_id=wid, text="silent: changed 2", timeout_ms=3000)
    ctx.check("wait_for re-checks an app that posts nothing and sees its change",
              waited.get("status") == "verified", waited)
    ctx.check("found by the 250 ms re-check (no notification came), well before the timeout",
              200 <= waited.get("waited_ms", 0) < 1000, waited.get("waited_ms"))


@scenario
def wait_for_concurrent_change(ctx):
    wid, _ = textedit(ctx, "wait.txt", "pending")
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    area = element_index(ctx.call("get_window_state", window_id=wid, session_id=sid, full=True)["tree"], "AXTextArea")
    other = {}

    def change_later():
        time.sleep(0.7)
        started = time.time()
        try:
            other["result"] = ctx.mcp.call("set_value", window_id=wid, element_index=area, value="done", session_id=sid)[0]
        except Exception as e:
            other["error"] = str(e)
        other["seconds"] = time.time() - started

    thread = threading.Thread(target=change_later)
    thread.start()
    waited = ctx.call("wait_for", window_id=wid, text="done", timeout_ms=5000)
    thread.join(15)
    ctx.check("the other session's set_value ran while the wait was on", other.get("result", {}).get("ok"), other)
    ctx.check("and was not held up behind the wait", other.get("seconds", 99) < 2, other.get("seconds"))
    ctx.check("wait_for reports verified", waited.get("status") == "verified", waited)
    ctx.check("waited_ms is above the ~700 ms it took the change to come, and well below the 5 s timeout",
              600 < waited.get("waited_ms", 0) < 2500, waited.get("waited_ms"))
    ctx.check("its observation shows the new text", value_of((waited.get("observation") or {}).get("tree", "")) == "done",
              waited.get("observation"))


@scenario
def wait_for_timeout(ctx):
    wid, area = textedit(ctx, "timeout.txt", "present")
    started = time.time()
    waited = ctx.call("wait_for", window_id=wid, text="absent", timeout_ms=800)
    elapsed = time.time() - started
    ctx.check("a condition that never holds reports failed, not an error", waited.get("status") == "failed", waited)
    ctx.check("after about timeout_ms (800)", 750 <= waited.get("waited_ms", 0) <= 1400 and elapsed < 3,
              (waited.get("waited_ms"), elapsed))
    ctx.check("with the observation and what was looked for",
              (waited.get("observation") or {}).get("snapshot_id") and "absent" in waited.get("detail", ""), waited)

    held = ctx.call("wait_for", window_id=wid, text="present")
    ctx.check("a condition that already holds is verified with waited_ms 0",
              held.get("status") == "verified" and held.get("waited_ms") == 0, held)
    gone = ctx.call("wait_for", window_id=wid, text="absent", gone=True)
    ctx.check("gone on text that is not there is verified at once",
              gone.get("status") == "verified" and gone.get("waited_ms") == 0, gone)

    turn_ended(ctx)
    area = element_index(ctx.tree(wid), "AXTextArea")
    by_app = ctx.call("wait_for", app="TextEdit", element_index=area, value="present", caption="Waiting for the text")
    ctx.check("wait_for by app with element_index uses the window this session observed",
              by_app.get("window_id") == wid and by_app.get("status") == "verified", by_app)
    activity = ctx.call("status")["agent_activity"]
    ctx.check("it keeps the agent's presence up like a read, with its caption",
              activity.get("active") and activity.get("app") == "TextEdit" and activity.get("caption") == "Waiting for the text",
              activity)

    started = time.time()
    capped = ctx.call("wait_for", window_id=wid, text="absent", timeout_ms=15000)
    ctx.check("timeout_ms is capped at 10 s", 9500 <= capped.get("waited_ms", 0) <= 11000 and time.time() - started < 13,
              capped.get("waited_ms"))


@scenario
def observer_bookkeeping(ctx):
    def observed():
        return ctx.call("status")["runtime"]["observed_apps"]

    base = observed()
    wid, _ = textedit(ctx, "observed.txt", "watch me")
    pid = next(w["pid"] for w in ctx.call("list_windows", app="TextEdit")["windows"] if w["id"] == wid)
    ctx.check("reading an app starts observing it", observed() == base + 1, (base, observed()))
    os.kill(pid, signal.SIGKILL)
    deadline = time.time() + 5
    while time.time() < deadline and alive(pid):
        time.sleep(0.1)
    ctx.check("an app that exits is no longer observed", observed() == base, (base, observed()))

    pids = []
    for _ in range(17):
        launched_wid, launched_pid, _ = launch_delayed(ctx)
        pids.append(launched_pid)
    count = observed()
    ctx.check("observers are capped at 16 apps, least recently used evicted", count == 16, count)
    for launched_pid in pids:
        os.kill(launched_pid, signal.SIGKILL)
    time.sleep(0.5)
    ctx.check("and all of them go once their apps exit", observed() <= base, (base, observed()))


if __name__ == "__main__":
    sys.exit(0 if Run().run(sys.argv[1:]) else 1)
