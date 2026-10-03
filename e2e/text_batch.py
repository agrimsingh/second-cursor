#!/usr/bin/env python3
"""Native select_text batching through public MCP and disposable TextEdit windows.

Acceptance cases were written first in docs/acceptance-text-batch.md.
Usage: SC_E2E_APP=build/SecondCursor.app python3 e2e/text_batch.py [scenario ...]
"""
import pathlib
import plistlib
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from native_safety import OFF_SPACE, area_of, content, launch_textedit, refusal, snapshot
from run import Run, SCENARIOS, scenario

SCENARIOS.clear()


def fixture(ctx, name, text, **args):
    wid = launch_textedit(ctx, name, text)["id"]
    snap = snapshot(ctx, wid, **args)
    return wid, area_of(ctx, snap), snap


def selection(area, text, **args):
    return {"tool": "select_text", "element_index": area, "text": text, **args}


def insertion(area, text):
    return {"tool": "type_text", "element_index": area, "text": text}


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


@scenario
def batch_text_schema(ctx):
    tools = ctx.mcp.rpc("tools/list")["tools"]
    batch = next(tool for tool in tools if tool["name"] == "batch")
    fields = batch["inputSchema"]["properties"]["steps"]["items"]["properties"]
    ctx.check("the public batch schema advertises native text selection",
              "select_text" in fields["tool"]["enum"], fields["tool"])
    ctx.check("selection arguments are available as flat step fields",
              {"element_index", "text", "prefix", "suffix", "selection_type"} <= fields.keys(), list(fields))
    ctx.check("all supported selection modes are discoverable",
              set(fields.get("selection_type", {}).get("enum", [])) == {"text", "cursor_before", "cursor_after"},
              fields.get("selection_type"))


@scenario
def batch_text_unicode(ctx):
    original = "😀 cafe\u0301 first repeat, second repeat! tail"
    expected = "😀 cafe\u0301 first repeat, second 🧮42! tail"
    wid, area, snap = fixture(ctx, "batch-selection-unicode.txt", original)
    calls = ctx.run.tool_calls()
    result = ctx.call("batch", window_id=wid, observation_id=snap["snapshot_id"], steps=[
        selection(area, "repeat", prefix="second ", suffix="!"), insertion(area, "🧮42"),
    ], expect={"element_index": area, "value": expected, "timeout_ms": 1000})
    ctx.check("selection, insertion, verification and final observation use one call",
              ctx.run.tool_calls() - calls == 1)
    steps = result.get("steps", [])
    ctx.check("both semantic steps run in order with verified readback",
              [step.get("tool") for step in steps] == ["select_text", "type_text"]
              and [step.get("route") for step in steps] == ["ax_selection", "ax_selected_text"]
              and all(step.get("ok") and step.get("verified") is True and not step.get("uncertain") for step in steps), steps)
    ctx.check("the batch expectation verifies the exact document after insertion",
              result.get("expect", {}).get("status") == "verified", result.get("expect"))
    ctx.check("the single final observation includes the replacement",
              "🧮42" in (result.get("observation") or {}).get("tree", ""), result.get("observation"))
    actual = content(ctx, wid, area)
    ctx.check("only the contextual occurrence changed, including Unicode prefix and suffix",
              actual["text"] == expected, actual)
    ctx.check("AX caret uses UTF-16 units and is collapsed after the replacement",
              actual.get("selection") == {"offset": utf16_length("😀 cafe\u0301 first repeat, second 🧮42"), "length": 0}, actual)


@scenario
def batch_text_carets(ctx):
    wid, area, _ = fixture(ctx, "batch-selection-carets.txt", "😀middle終")
    result = ctx.call("batch", window_id=wid, observe="none", steps=[
        {"tool": "select_text", "args": {"element_index": area, "text": "middle", "selection_type": "cursor_before"}},
        insertion(area, "before "),
        selection(area, "middle", selection_type="cursor_after"), insertion(area, " after"),
        selection(area, "middle"), insertion(area, "middle"),
    ], expect={"element_index": area, "value": "😀before middle after終", "timeout_ms": 1000})
    ctx.check("flat and legacy nested selections both execute", all(step.get("ok") for step in result["steps"]), result)
    ctx.check("observe=none still evaluates the exact final expectation",
              result.get("observation") is None and result.get("expect", {}).get("status") == "verified", result)
    actual = content(ctx, wid, area)
    ctx.check("caret insertions preserve the match, and identical replacement does not duplicate it",
              actual["text"] == "😀before middle after終", actual)
    ctx.check("identical replacement collapses the UTF-16 selection once",
              actual.get("selection") == {"offset": utf16_length("😀before middle"), "length": 0}, actual)


@scenario
def batch_text_validation(ctx):
    wid, area, _ = fixture(ctx, "batch-selection-validation.txt", "unchanged target")
    malformed = [
        {"tool": "select_text", "element_index": area},
        selection(area, ""), selection(area, "target", selection_type="everything"),
        {"tool": "select_text", "text": "target"},
        selection("not-an-index", "target"), selection(area, "target", prefix={"invalid": "context"}),
        selection(area, "target", suffix=["invalid", "context"]), selection(area, "target", selection_type=3),
    ]
    for index, bad in enumerate(malformed):
        before = content(ctx, wid, area)
        error = refusal(ctx, "batch", window_id=wid, observe="none", steps=[
            {"tool": "set_value", "element_index": area, "value": "must never run"}, bad,
        ])
        ctx.check(f"malformed selection {index} produces an argument error", bool(error), error)
        after = content(ctx, wid, area)
        ctx.check(f"malformed selection {index} is rejected before the earlier mutation",
                  after["text"] == before["text"] and after.get("selection") == before.get("selection"), after)


@scenario
def batch_text_refusals(ctx):
    wid, area, _ = fixture(ctx, "batch-selection-refusals.txt", "first repeat second repeat")
    for text, matches in [("repeat", 2), ("absent", 0)]:
        before = content(ctx, wid, area)
        result = ctx.call("batch", window_id=wid, observe="none", steps=[
            selection(area, text), insertion(area, "must never appear"),
        ])
        steps = result["steps"]
        ctx.check(f"a {matches}-match selection fails and skips typing",
                  len(steps) == 2 and not steps[0].get("ok")
                  and f"matched {matches} times" in steps[0].get("error", "")
                  and steps[1].get("skipped") is True, steps)
        after = content(ctx, wid, area)
        ctx.check(f"a {matches}-match refusal leaves text and selected range unchanged",
                  after["text"] == before["text"] and after.get("selection") == before.get("selection"), after)


@scenario
def batch_text_overlap(ctx):
    for index, (original, text, prefix, expected) in enumerate([
        ("aaa", "aa", "a", "aX"), ("😀😀😀", "😀😀", "😀", "😀X"),
    ]):
        wid, area, _ = fixture(ctx, f"batch-selection-overlap-{index}.txt", original)
        result = ctx.call("batch", window_id=wid, observe="none", steps=[
            selection(area, text), insertion(area, "must never appear"),
        ])
        ctx.check(f"overlap {index}: both possible occurrences count as ambiguity",
                  not result["steps"][0].get("ok") and "matched 2 times" in result["steps"][0].get("error", "")
                  and result["steps"][1].get("skipped") is True, result)
        ctx.check(f"overlap {index}: refusal leaves original text intact", content(ctx, wid, area)["text"] == original)
        result = ctx.call("batch", window_id=wid, observe="none", steps=[
            selection(area, text, prefix=prefix), insertion(area, "X"),
        ], expect={"element_index": area, "value": expected, "timeout_ms": 1000})
        actual = content(ctx, wid, area)
        ctx.check(f"overlap {index}: prefix selects the second occurrence exactly",
                  all(step.get("ok") for step in result["steps"]) and actual["text"] == expected, result)
        ctx.check(f"overlap {index}: final caret is on a valid UTF-16 boundary",
                  actual.get("selection") == {"offset": utf16_length(expected), "length": 0}, actual)


@scenario
def batch_text_stale(ctx):
    wid, area, _ = fixture(ctx, "batch-selection-stale.txt", "untouched target")
    before = content(ctx, wid, area)
    error = refusal(ctx, "batch", window_id=wid, observation_id="obsolete-selection-observation", observe="none", steps=[
        selection(area, "target"), insertion(area, "must never appear"),
    ])
    ctx.check("an obsolete observation is refused before selection", "stale observation" in error.lower(), error)
    snapshot(ctx, wid)
    result = ctx.call("batch", window_id=wid, observe="none", steps=[
        selection(999999, "target"), insertion(area, "must never appear"),
    ])
    ctx.check("a nonexistent selected element fails and skips insertion",
              not result["steps"][0].get("ok") and result["steps"][1].get("skipped") is True, result)
    after = content(ctx, wid, area)
    ctx.check("stale targets never changed the document or selected range",
              after["text"] == before["text"] and after.get("selection") == before.get("selection"), after)


@scenario
def batch_text_unconfirmed(ctx):
    source = pathlib.Path(__file__).parent / "selection_fixture.swift"
    app = ctx.scratch / "SCSelectionFixture.app"
    binary = app / "Contents/MacOS/selection-fixture"
    binary.parent.mkdir(parents=True)
    subprocess.run(["xcrun", "swiftc", str(source), "-o", str(binary)],
                   capture_output=True, check=True, timeout=120)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "dev.secondcursor.e2e.selectionfixture",
        "CFBundleName": "SCSelectionFixture", "CFBundleExecutable": "selection-fixture",
        "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSMinimumSystemVersion": "14.0",
    }))
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=True, timeout=30)
    launched = ctx.call("launch_app", app=str(app), new_instance=True, on_agent_display=not OFF_SPACE)
    pid = launched["app"]["pid"]
    ctx.kill_on_exit(pid)
    window = ctx.wait_for_window("SCSelectionFixture", pid=pid, title="SC Unconfirmed Selection")
    wid = window["id"]
    if OFF_SPACE:
        ctx.check("off-Space faulty selection fixture stays off the agent display and off screen",
                  not window.get("on_agent_display") and window.get("is_on_screen") is False, window)
    area = area_of(ctx, snapshot(ctx, wid))
    before = content(ctx, wid, area)
    result = ctx.call("batch", window_id=wid, observe="none", steps=[
        selection(area, "target"), insertion(area, "must never appear"),
    ])
    steps = result["steps"]
    ctx.check("acknowledged selection with mismatched readback is explicitly uncertain",
              steps[0].get("ok") is True and steps[0].get("route") == "ax_selection"
              and steps[0].get("verified") is False and steps[0].get("uncertain") is True, steps)
    ctx.check("uncertain selection stops the default batch before typing",
              result.get("stopped_early") is True and steps[1].get("skipped") is True, result)
    after = content(ctx, wid, area)
    ctx.check("the unconfirmed selection never triggers insertion at the previous caret",
              after["text"] == before["text"] and after.get("selection") == before.get("selection"), after)


@scenario
def batch_text_controls(ctx):
    for control in ("pause", "takeover", "cancel"):
        sid = ctx.call("session", action="create")["session_id"]
        ctx.cleanups.append(lambda sid=sid: ctx.mcp.call("session", action="close", session_id=sid))
        ctx.call("task", action="create", session_id=sid, title=f"Text batch {control} fixture")
        wid, area, _ = fixture(ctx, f"batch-selection-{control}.txt", "untouched target", session_id=sid)
        before = ctx.call("inspect_element", session_id=sid, window_id=wid, element_index=area, offset=0, length=10000)
        outcome = {}

        def worker():
            try:
                outcome["output"] = ctx.call("batch", session_id=sid, window_id=wid, observe="none", steps=[
                    {"tool": "wait", "ms": 1800}, selection(area, "target"), insertion(area, "must never appear"),
                ])
            except Exception as error:
                outcome["error"] = str(error)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = ctx.call("task", action="get", session_id=sid)["task"]
            if task["in_flight_count"] > 0:
                break
            time.sleep(0.02)
        ctx.require(f"{control}: the batch is in flight before control changes", task["in_flight_count"] > 0, task)
        controlled = ctx.call("task", action=control, session_id=sid)["task"]
        wanted = {"pause": "paused", "takeover": "needs_user", "cancel": "cancelled"}[control]
        ctx.check(f"{control}: the requested control state is authoritative", controlled["status"] == wanted, controlled)
        thread.join(timeout=10)
        ctx.require(f"{control}: the batch worker settles", not thread.is_alive(), outcome)
        output = outcome.get("output") or {}
        steps = output.get("steps") or []
        errors = " ".join([outcome.get("error", ""), *(step.get("error", "") for step in steps)])
        ctx.check(f"{control}: the batch was refused for control rather than an unrelated error",
                  bool(re.search(r"paus|cancel|takeover|interrupted|awaiting.*user|needs.user|control.*changed", errors, re.I)), outcome)
        ctx.check(f"{control}: no selection or insertion step was successful",
                  bool(outcome.get("error")) or (len(steps) == 3 and all(not step.get("ok") for step in steps[1:])), outcome)
        settled = ctx.call("task", action="get", session_id=sid)["task"]
        ctx.check(f"{control}: in-flight accounting drains without changing the control state",
                  settled["in_flight_count"] == 0 and settled["status"] == wanted, settled)
        # Task control deliberately invalidates its cached observations, including
        # read-only inspect_element indexes. Reobserve after the worker drains;
        # this is inspection while paused/cancelled, not authority to resume input.
        area = area_of(ctx, snapshot(ctx, wid, session_id=sid))
        after = ctx.call("inspect_element", session_id=sid, window_id=wid, element_index=area, offset=0, length=10000)
        ctx.check(f"{control}: queued selection and insertion never land",
                  after["text"] == before["text"] and after.get("selection") == before.get("selection"),
                  {"after": after, "outcome": outcome})


if __name__ == "__main__":
    run = Run(browser=False)
    sys.exit(0 if run.run(set(sys.argv[1:])) else 1)
