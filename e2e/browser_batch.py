#!/usr/bin/env python3
"""batch with tab_id: browser steps run in one call on an agent tab, and the tab is read once after the last.

Ways this can fail, and the scenarios that catch them:
- Steps run out of order, or a step acts before the previous one's effect lands (inbox_batch: the inbox backend
  records label, star and archive for the open message, in that order).
- The observation is read before the last step lands, or is the whole page instead of a diff (inbox_batch: a diff that
  shows the summary after the archive).
- A ref from an earlier read stops working once its element is hidden and shown again, so one batch cannot work
  through several messages (inbox_batch: a second batch opens two more messages and labels them with the toolbar refs
  read before the first archive hid the toolbar).
- A step skips a lone click's checks, so a covered target is clicked anyway; a failure does not stop the batch or does
  not say which step failed (covered_step_stops: the second of three steps targets a button under a toast; it fails
  naming the toast, the third is skipped, and the page counts no click on either button).
- stop_on_error=false stops anyway, or observe="none" still reads (covered_step_stops).
- The action log hides what ran: one line for the batch and one per step that ran, each naming the tab, and none for a
  skipped step (inbox_batch, covered_step_stops).
- A step that navigates is not waited for: the batch's read shows the page it left, whether the step navigated at
  once (a link) or its handler navigated 200 ms later (batch_navigation).
- The step after a link acts on the page the link left instead of failing on its stale ref (batch_navigation).
- A malformed step, a native target (element_index), window_id beside tab_id, or expect lets the steps before it run,
  or is ignored (bad_steps_run_nothing: each is refused and the backend records nothing).
- A tab the session did not open is acted on (bad_steps_run_nothing: an unknown tab id fails its step).

Usage:  e2e/browser_batch.py [scenario ...]   (same targeting flags as e2e/run.py, e.g. SC_E2E_APP=build/SecondCursor.app)
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl)
"""
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run
from jev_triage import InboxServer
from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario

SCENARIOS.clear()


def ref(outline, pattern):
    match = re.search(r"\[(e\d+)\] " + pattern, outline)
    if not match:
        raise AssertionError(f"no element matching {pattern!r} in:\n{outline[:1500]}")
    return match[1]


def error_of(ctx, tool, **args):
    try:
        ctx.call(tool, **args)
    except ToolError as e:
        return str(e)
    return None


def open_tab(ctx, url):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("no browser connected")
    opened = ctx.call("browser_open", url=url)
    tab_id = opened["tab"]["id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab_id))
    return tab_id


def log_mark():
    return len(run.ACTION_LOG.read_text().splitlines()) if run.ACTION_LOG.exists() else 0


def logged(mark):
    lines = run.ACTION_LOG.read_text().splitlines()[mark:]
    return [(entry["tool"], entry.get("target")) for entry in map(json.loads, lines)]


def until(predicate, seconds=3.0):
    deadline = time.time() + seconds
    while not predicate() and time.time() < deadline:
        time.sleep(0.05)
    return predicate()


def click(target):
    return {"tool": "browser_click", "ref": target}


@scenario
def inbox_batch(ctx):
    with InboxServer() as backend:
        tab = open_tab(ctx, f"{backend.base}/inbox.html")
        until(lambda: backend.loads() >= 1)
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        rows = {name: ref(outline, f'button "{name} —') for name in ("Priya Raman", "Tom Becker", "Crypto Profits Daily")}
        pane = ctx.call("browser_click", tab_id=tab, ref=rows["Priya Raman"])["observation"]["outline"]
        billing, bug, spam = (ref(pane, f'button "Label: {label}"') for label in ("Billing", "Bug", "Spam"))
        star, archive = ref(pane, 'button "Star"'), ref(pane, 'button "Archive"')

        mark = log_mark()
        result = ctx.call("batch", tab_id=tab, steps=[click(billing), click(star), {"tool": "click", "ref": archive}])
        ctx.check("every step ran", [s["ok"] for s in result["steps"]] == [True] * 3 and result["completed"] == 3
                  and not result["stopped_early"], result["steps"])
        changes = lambda: [(e["type"], e.get("value")) for e in backend.state()["events"]
                           if e["id"] == "m01" and e["type"] != "open"]
        until(lambda: len(changes()) >= 3)
        ctx.check("the backend got label, star and archive for the open message, in that order",
                  changes() == [("label", "Billing"), ("star", True), ("archive", None)], changes())
        observation = result.get("observation") or {}
        ctx.check("the observation is a diff read after the last step: one message archived",
                  observation.get("is_diff") is True and "1 archived" in observation.get("outline", ""), observation)
        ctx.check("the action log has the batch, then each step, all naming the tab",
                  logged(mark) == [("batch", f"tab_id={tab}")] + [("browser_click", f"tab_id={tab}")] * 3,
                  logged(mark))

        result = ctx.call("batch", tab_id=tab, steps=[
            click(rows["Tom Becker"]), click(bug), click(rows["Crypto Profits Daily"]), click(spam), click(archive),
        ])
        ctx.check("refs read before the toolbar was hidden work once it is shown again",
                  all(s["ok"] for s in result["steps"]), result["steps"])
        until(lambda: backend.state()["messages"]["m03"]["archived"])
        messages = backend.state()["messages"]
        ctx.check("the second batch labeled both messages and archived the second",
                  messages["m02"] == {"label": "Bug", "starred": False, "archived": False}
                  and messages["m03"] == {"label": "Spam", "starred": False, "archived": True}, messages)
        ctx.check("its observation shows two archived", "2 archived" in (result.get("observation") or {}).get("outline", ""),
                  result.get("observation"))
        ctx.check("nothing reached the backend except from the page", not backend.bypass(), backend.bypass())


@scenario
def covered_step_stops(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, f"{base}/covered.html")
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        show, spam, glass = (ref(outline, f'button "{name}"') for name in ("Show toast", "Label: Spam", "Under glass"))
        status = lambda: re.search(r"clicks=\d+ glassed=\d+", ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"])[0]

        mark = log_mark()
        result = ctx.call("batch", tab_id=tab, steps=[click(show), click(spam), click(glass)])
        steps = result["steps"]
        ctx.check("the covered step fails and the one after it is skipped",
                  [s["ok"] for s in steps] == [True, False, False] and steps[2].get("skipped")
                  and result["stopped_early"], steps)
        ctx.check("the failure names step 1 and what covers its target",
                  steps[1]["index"] == 1 and 'covered by div "Message archived' in steps[1].get("error", ""), steps[1])
        ctx.check("neither the covered button nor the skipped one was clicked", status() == "clicks=0 glassed=0", status())
        ctx.check("the action log has the batch and the two steps that ran, not the skipped one",
                  logged(mark) == [("batch", f"tab_id={tab}")] + [("browser_click", f"tab_id={tab}")] * 2,
                  logged(mark))

        result = ctx.call("batch", tab_id=tab, stop_on_error=False, steps=[click(spam), click(glass)])
        ctx.check("stop_on_error=false runs the step after a failure",
                  [s["ok"] for s in result["steps"]] == [False, True] and status() == "clicks=0 glassed=1",
                  (result["steps"], status()))

        result = ctx.call("batch", tab_id=tab, observe="none", steps=[{"tool": "wait", "ms": 10}])
        ctx.check('observe="none" returns no observation', result.get("observation") is None, result)


@scenario
def batch_navigation(ctx):
    with FixtureServer() as base:
        start = f"{base}/navigate.html"
        tab = open_tab(ctx, start)

        def batch(*names):
            ctx.call("browser_navigate", tab_id=tab, url=start, observe="none")
            outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
            return ctx.call("batch", tab_id=tab, steps=[click(ref(outline, rf'\w+ "{name}"')) for name in names])

        def arrived(result):
            observation = result.get("observation") or {}
            return "arrived" in observation.get("outline", "") and observation.get("tab", {}).get("url", "").endswith("/next.html")

        result = batch("Plain link")
        ctx.check("a batch whose step follows a link reads the page it opened", arrived(result), result)
        result = batch("Go in 200 ms")
        ctx.check("a batch whose step navigates 200 ms later reads the new page", arrived(result), result)
        result = batch("Plain link", "Go in 200 ms")
        steps = result["steps"]
        ctx.check("the step after a link fails on its ref from the page the link left",
                  [s["ok"] for s in steps] == [True, False] and "no longer on the page" in steps[1].get("error", ""),
                  steps)


@scenario
def bad_steps_run_nothing(ctx):
    with InboxServer() as backend:
        tab = open_tab(ctx, f"{backend.base}/inbox.html")
        until(lambda: backend.loads() >= 1)
        row = ref(ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"], 'button "Priya Raman —')
        mark = log_mark()

        cases = {
            "a native target": ([click(row), {"tool": "click", "element_index": 3}], {}, "element_index"),
            "a tool a tab batch does not take": ([click(row), {"tool": "set_value", "value": "x"}], {}, "tool must be one of"),
            "a step missing what it needs": ([click(row), {"tool": "browser_type"}], {}, "Step 1 (browser_type): text is required"),
            "window_id beside tab_id": ([click(row)], {"window_id": 1}, "not both"),
            "expect": ([click(row)], {"expect": {"element": "x"}}, "expect"),
        }
        for name, (steps, extra, needle) in cases.items():
            error = error_of(ctx, "batch", tab_id=tab, steps=steps, **extra)
            ctx.check(f"{name} is refused before anything runs", error is not None and needle in error, error)
        ctx.check("no step ran: the backend recorded nothing", backend.state()["events"] == [], backend.state()["events"])
        ctx.check("no step was logged", all(tool == "batch" for tool, _ in logged(mark)), logged(mark))

        result = ctx.call("batch", tab_id=999999, steps=[click(row)])
        ctx.check("a tab the session did not open fails its step",
                  result["steps"][0]["ok"] is False and "999999" in result["steps"][0].get("error", ""), result["steps"])


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
