#!/usr/bin/env python3
"""Browser freshness, truthful outcomes, form settling, and trusted hover over the public MCP tools.

Failure cases are the acceptance contract, written before implementation:
- A checkbox's checked property changes without a DOM mutation, so each click pays the 1.2 s reaction timeout.
  browser_checkbox_settling measures five calls and requires every returned observation to show the right state.
- Treating all input events as settled returns before a 300 ms debounce + 150 ms fetch finishes.
  browser_delayed_search requires results in the typing call's own observation, without a second snapshot.
- A synchronous page handler rejects check/fill/select_option, replaces the target with an unchecked control,
  or a checked radio cannot be unchecked, but the tool claims verification. browser_truthful_outcomes checks
  requested and actual state, detached targets, and secret redaction; browser_repl_uncertain_outcomes requires
  uncertain REPL actions to throw instead of letting subsequent agent work silently continue.
- A same-size reflow or viewport scroll leaves an old snapshot usable for input. browser_freshness_guard attempts
  both ref and coordinate input, checks neither reached the page, then proves a fresh token works.
- Making observation tokens mandatory breaks stable refs or freshly resolved locators in a changing batch.
  browser_live_locator_sequence checks both paths without tokens through actual application changes.
- A browser batch hides dispatch or partial failure behind an absent or successful aggregate outcome.
  browser_live_locator_sequence checks observe=none dispatch and stops a valid/invalid/valid sequence after the
  failed step, requiring a failed aggregate and exactly one additional application-side effect.
- Hover only draws an overlay or fires synthetic events. browser_trusted_hover requires the page to receive a
  trusted mouseenter through extended hover, REPL locator.hover(), and REPL page.mouse.move(). An unsupported
  hover button must be rejected before any mouseenter or click reaches the fixture.

Usage: SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_reliability.py [scenario ...]
The existing harness launches disposable app and browser instances and records focus/cursor/clipboard invariants.
No page JavaScript is injected; fixture handlers are driven through the same public tools an agent uses.
"""
import json
import re
import statistics
import sys
import time

from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario

SCENARIOS.clear()


def open_tab(ctx, base):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    tab = ctx.call("browser_open", url=f"{base}/browser-reliability.html")["tab"]["id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab))
    return tab


def command(ctx, tab, operation, **args):
    return ctx.call("browser_command", tab_id=tab, operation=operation, **args)


def inspect(ctx, tab, selector):
    return command(ctx, tab, "inspect", locator={"selector": selector})


def ref(outline, role, label):
    match = re.search(r'\[(e\d+)\] ' + re.escape(role) + r' "' + re.escape(label) + r'"', outline)
    if not match:
        raise AssertionError(f"missing {role} {label!r} in:\n{outline[:2000]}")
    return match[1]


def error_of(ctx, tool, **args):
    try:
        ctx.call(tool, **args)
    except ToolError as error:
        return str(error)
    return None


def checked_in(outline, label):
    line = next((line for line in outline.splitlines() if f'checkbox "{label}"' in line
                 and not line.lstrip().startswith("-")), None)
    return None if line is None else bool(re.search(r'\bchecked\b', line))


def take_timing(instance, start, tool):
    """Read this call's asynchronously written timing line, without counting log-flush time as call latency."""
    if instance is None:
        return None
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        rows = instance.timings(start)
        matches = [row for row in rows if row.get("tool") == tool]
        if matches:
            return matches[-1]
        time.sleep(0.005)
    return None


def outcome_is(result, state, verification):
    outcome = result.get("outcome") or {}
    return (outcome.get("state") == state and outcome.get("verified") is (state == "verified")
            and outcome.get("verification") == verification and isinstance(outcome.get("reason"), str)
            and bool(outcome["reason"].strip()))


def outcome_matches(result, state, verification, expected, actual):
    outcome = result.get("outcome") or {}
    return (outcome_is(result, state, verification)
            and outcome.get("expected") == expected and outcome.get("actual") == actual)


def repl(ctx, code):
    result = ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {"code": code}})
    text = "\n".join(item["text"] for item in result.get("content", []) if item.get("type") == "text")
    if result.get("isError"):
        raise ToolError(text)
    return text


@scenario
def browser_checkbox_settling(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, base)
        snapshot = ctx.call("browser_snapshot", tab_id=tab, full=True)
        target = ref(snapshot["outline"], "checkbox", "Plain checkbox")
        ctx.require("the checkbox starts unchecked", checked_in(snapshot["outline"], "Plain checkbox") is False,
                    snapshot["outline"])
        instance = ctx.run.instance
        samples = []
        for i in range(5):
            start = len(instance.timings()) if instance is not None else 0
            began = time.perf_counter()
            result = ctx.call("browser_click", tab_id=tab, ref=target)
            elapsed = (time.perf_counter() - began) * 1000
            timing = take_timing(instance, start, "browser_click")
            samples.append({"client_ms": round(elapsed, 1), "timing": timing})
            observation = result.get("observation") or {}
            ctx.check(f"checkbox click {i + 1} already observes checked={i % 2 == 0}",
                      checked_in(observation.get("outline", ""), "Plain checkbox") is (i % 2 == 0), observation)
            ctx.check(f"checkbox click {i + 1} reports dispatch without claiming verification",
                      outcome_is(result, "dispatched", "none"), result.get("outcome"))
        settled = [sample["timing"]["phases"]["ext.settled"] for sample in samples
                   if sample["timing"] and "ext.settled" in sample["timing"].get("phases", {})]
        clients = [sample["client_ms"] for sample in samples]
        # Prefer the measured settling phase: CPU or overlay rendering elsewhere should not make this flaky.
        if len(settled) == len(samples):
            median = statistics.median(settled)
            ctx.check("native checkbox property events avoid the reaction timeout (median settling <800 ms)",
                      median < 800, {"median_ms": median, "samples_ms": settled})
        else:
            median = statistics.median(clients)
            ctx.check("checkbox clicks return promptly without timing instrumentation (median client <1100 ms)",
                      median < 1100, {"median_ms": median, "samples_ms": clients})
        (ctx.run.dir / "browser-checkbox-settling.json").write_text(json.dumps({
            "samples": samples, "median_client_ms": statistics.median(clients),
            "median_settled_ms": statistics.median(settled) if settled else None,
        }, indent=2) + "\n")


@scenario
def browser_delayed_search(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, base)
        snapshot = ctx.call("browser_snapshot", tab_id=tab, full=True)
        target = ref(snapshot["outline"], "textbox", "Delayed search")
        began = time.perf_counter()
        result = ctx.call("browser_type", tab_id=tab, ref=target, text="reliability")
        elapsed = (time.perf_counter() - began) * 1000
        outline = result.get("observation", {}).get("outline", "")
        ctx.check("typing waits for all three results after a 300 ms debounce and 150 ms fetch",
                  all(f"reliability verified result {i}" in outline for i in (1, 2, 3)), outline)
        ctx.check("the typing call stays bounded while including the delayed reaction", elapsed < 4000,
                  f"{elapsed:.1f} ms")


@scenario
def browser_truthful_outcomes(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, base)
        ctx.call("browser_snapshot", tab_id=tab, full=True)
        cases = [
            ("accepted checkbox", "check", "#plain-check", {"checked": True}, "verified", "checked", True, True),
            ("reverted checkbox", "check", "#reverting-check", {"checked": True}, "uncertain", "checked", True, False),
            ("selected radio uncheck", "check", "#selected-radio", {"checked": False}, "uncertain", "checked", False, True),
            ("accepted fill", "fill", "#plain-value", {"text": "accepted"}, "verified", "value", "accepted", "accepted"),
            ("reverted fill", "fill", "#reverting-value", {"text": "discarded"}, "uncertain", "value", "discarded", "locked"),
            ("accepted option", "select_option", "#plain-choice", {"value": "change"}, "verified", "selected_values", ["change"], ["change"]),
            ("reverted option", "select_option", "#reverting-choice", {"value": "change"}, "uncertain", "selected_values", ["change"], ["keep"]),
        ]
        for label, operation, selector, args, state, verification, expected, actual in cases:
            result = command(ctx, tab, operation, locator={"selector": selector}, **args)
            ctx.check(f"{label} reports {state} with the requested and resulting state",
                      outcome_matches(result, state, verification, expected, actual), result)
            observed = inspect(ctx, tab, selector)
            observed_state = observed.get("checked") if verification == "checked" else observed.get("text")
            wanted = actual[0] if verification == "selected_values" else actual
            ctx.check(f"{label}'s reported actual state agrees with a separate public read", observed_state == wanted, observed)
        result = command(ctx, tab, "check", locator={"selector": "#replacing-check"}, checked=True)
        ctx.check("checking an element detached by its handler cannot claim verified",
                  outcome_is(result, "uncertain", "checked") and result.get("outcome", {}).get("expected") is True, result)
        observed = inspect(ctx, tab, "#replacing-check")
        ctx.check("a fresh read sees the unchecked replacement rather than the detached target",
                  observed.get("checked") is False, observed)
        secret = "fixture-secret-should-not-appear"
        result = command(ctx, tab, "fill", locator={"selector": "#secret-value"}, text=secret)
        ctx.check("verification does not expose a secret through expected or actual values",
                  secret not in json.dumps(result)
                  and outcome_matches(result, "verified", "value", "[redacted]", "[redacted]"), result)


@scenario
def browser_repl_uncertain_outcomes(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    with FixtureServer() as base:
        repl(ctx, "const outcomePage = await context.newPage(); "
             f"await outcomePage.goto({json.dumps(base + '/browser-reliability.html')})")
        ctx.cleanups.append(lambda: ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {
            "code": "await outcomePage.close()"}}))
        cases = [
            ("reverted checkbox", "#reverting-check", "check()", "isChecked()", "false"),
            ("selected radio uncheck", "#selected-radio", "uncheck()", "isChecked()", "true"),
            ("reverted fill", "#reverting-value", "fill('discarded')", "inputValue()", "locked"),
            ("reverted option", "#reverting-choice", "selectOption('change')", "inputValue()", "keep"),
            ("detached checkbox", "#replacing-check", "check()", "isChecked()", "false"),
        ]
        for label, selector, action, read, expected in cases:
            locator = f"outcomePage.locator({json.dumps(selector)})"
            error = None
            try:
                repl(ctx, f"await {locator}.{action}")
            except ToolError as failure:
                error = str(failure)
            ctx.check(f"REPL {label} fails visibly when its postcondition is uncertain",
                      bool(error) and "not verified" in error, error)
            actual = repl(ctx, f"await {locator}.{read}")
            ctx.check(f"REPL {label} keeps working and freshly reads the actual live value", actual == expected, actual)


@scenario
def browser_freshness_guard(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, base)
        # Capturing an unchanged page must not invalidate the previous DOM token merely because the agent's cursor
        # was concealed. Keep the guard enabled here so any capture-induced revision drift is visible.
        clicks = 0
        for capture in ("browser_screenshot", "browser_command screenshot"):
            before_capture = ctx.call("browser_snapshot", tab_id=tab, full=True)
            if capture == "browser_screenshot":
                ctx.call("browser_screenshot", tab_id=tab)
            else:
                command(ctx, tab, "screenshot")
            ctx.call("browser_click", tab_id=tab,
                     ref=ref(before_capture["outline"], "button", "Guarded target"),
                     observation_id=before_capture["snapshot_id"], observe="none")
            clicks += 1
            ctx.check(f"{capture} of the unchanged page preserves grounded input authority",
                      inspect(ctx, tab, "#guarded-status")["text"] == f"Target clicks: {clicks}")
        for cause, trigger in (("DOM reflow", "#change-layout"), ("viewport scrolling", "#scroll-viewport")):
            began_ms = time.time() * 1000
            snapshot = ctx.call("browser_snapshot", tab_id=tab, full=True)
            ended_ms = time.time() * 1000
            freshness = snapshot.get("freshness") or {}
            captured = freshness.get("captured_at_ms")
            age = freshness.get("age_ms")
            ctx.require(f"{cause}: snapshot carries an opaque token and current revision",
                        bool(snapshot.get("snapshot_id")) and isinstance(freshness.get("revision"), str)
                        and bool(freshness["revision"]) and freshness.get("status") == "current", snapshot)
            ctx.check(f"{cause}: freshness reports capture time and nonnegative age",
                      isinstance(captured, (int, float)) and began_ms - 1000 <= captured <= ended_ms + 1000
                      and isinstance(age, (int, float)) and 0 <= age < 5000, freshness)
            target = ref(snapshot["outline"], "button", "Guarded target")
            bounds = inspect(ctx, tab, "#guarded-target")["bounds"]
            point = {"x": bounds["x"] + bounds["width"] / 2, "y": bounds["y"] + bounds["height"] / 2}
            # This is an ordinary UI action, and deliberately does not replace the saved snapshot.
            command(ctx, tab, "click", locator={"selector": trigger})
            for label, target_args in (("ref", {"ref": target}), ("coordinates", point)):
                error = error_of(ctx, "browser_click", tab_id=tab, observation_id=snapshot["snapshot_id"],
                                 observe="none", **target_args)
                ctx.check(f"{cause}: stale {label} input is refused", bool(error) and "Stale browser observation" in error,
                          error)
            state = inspect(ctx, tab, "#guarded-status")["text"]
            ctx.check(f"{cause}: neither stale input reached the page", state == f"Target clicks: {clicks}", state)
            fresh = ctx.call("browser_snapshot", tab_id=tab, full=True)
            ctx.check(f"{cause}: the revision changes even without navigation or window resizing",
                      fresh.get("freshness", {}).get("revision") != freshness["revision"], fresh.get("freshness"))
            ctx.call("browser_click", tab_id=tab, ref=ref(fresh["outline"], "button", "Guarded target"),
                     observation_id=fresh["snapshot_id"], observe="none")
            clicks += 1
            state = inspect(ctx, tab, "#guarded-status")["text"]
            ctx.check(f"{cause}: a refreshed observation permits the intended action", state == f"Target clicks: {clicks}", state)
        obsolete = ctx.call("browser_snapshot", tab_id=tab, full=True)["snapshot_id"]
        command(ctx, tab, "click", locator={"selector": "#change-layout"})
        read = command(ctx, tab, "inspect", locator={"selector": "#guarded-status"}, observation_id=obsolete)
        ctx.check("a stale input token does not block a fresh read needed for recovery", read["text"] == f"Target clicks: {clicks}")
        closed = ctx.call("browser_close", tab_id=tab, observation_id=obsolete)
        ctx.check("a stale DOM token does not block closing the owned tab", closed.get("ok") is True, closed)
        ctx.cleanups.pop()  # This scenario explicitly verified its own tab cleanup.


@scenario
def browser_live_locator_sequence(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, base)
        snapshot = ctx.call("browser_snapshot", tab_id=tab, full=True)
        target = ref(snapshot["outline"], "button", "Advance batch")
        result = ctx.call("batch", tab_id=tab, observe="none", steps=[{"tool": "browser_click", "ref": target}] * 3)
        ctx.check("stable refs remain usable through changing batch steps without an observation token",
                  result.get("completed") == 3 and all(step.get("ok") for step in result.get("steps", [])), result)
        ctx.check("a successful observe=none tab batch reports dispatched without claiming verification",
                  result.get("outcome", {}).get("state") == "dispatched"
                  and result.get("outcome", {}).get("verified") is False
                  and result.get("observation") is None, result)
        ctx.check("all three ref batch clicks reached the page",
                  inspect(ctx, tab, "#batch-status")["text"] == "Batch advances: 3")
        command(ctx, tab, "batch", steps=[
            {"operation": "click", "arguments": {"locator": {"selector": "#batch-next"}}},
            {"operation": "click", "arguments": {"locator": {"selector": "#change-layout"}}},
            {"operation": "click", "arguments": {"locator": {"selector": "#batch-next"}}},
        ])
        ctx.check("fresh locator resolution survives a DOM change in the middle of a batch",
                  inspect(ctx, tab, "#batch-status")["text"] == "Batch advances: 5"
                  and inspect(ctx, tab, "#layout-status")["text"] == "Layout changes: 1")
        result = ctx.call("batch", tab_id=tab, observe="none", steps=[
            {"tool": "browser_click", "ref": target},
            {"tool": "browser_click", "ref": "e999999999"},
            {"tool": "browser_click", "ref": target},
        ])
        steps = result.get("steps", [])
        ctx.check("a partially failed tab batch reports failed without claiming verification",
                  result.get("outcome", {}).get("state") == "failed"
                  and result.get("outcome", {}).get("verified") is False, result)
        ctx.check("the failed batch preserves its successful, failed, and skipped step results",
                  len(steps) == 3 and [step.get("ok") for step in steps] == [True, False, False]
                  and bool(steps[1].get("error")) and steps[2].get("skipped") is True
                  and result.get("completed") == 1 and result.get("stopped_early") is True, result)
        state = inspect(ctx, tab, "#batch-status")["text"]
        ctx.check("only the first action in the partially failed batch reached the page",
                  state == "Batch advances: 6", state)


@scenario
def browser_trusted_hover(ctx):
    with FixtureServer() as base:
        tab = open_tab(ctx, base)
        command(ctx, tab, "hover", x=5, y=5)
        before = inspect(ctx, tab, "#hover-status")["text"]
        ctx.require("the pointer starts outside the hover target", before == "Hover: outside", before)
        error = error_of(ctx, "browser_command", tab_id=tab, operation="hover",
                         locator={"selector": "#hover-target"}, button="right")
        ctx.check("hover rejects unsupported mouse-button arguments instead of ignoring them",
                  bool(error) and "button is supported only by click" in error, error)
        ctx.check("the refused hover sends no mouseenter or click to the page",
                  inspect(ctx, tab, "#hover-status")["text"] == before
                  and inspect(ctx, tab, "#guarded-status")["text"] == "Target clicks: 0")
        result = ctx.call("browser_command", tab_id=tab, operation="hover", locator={"selector": "#hover-target"})
        state = inspect(ctx, tab, "#hover-status")["text"]
        ctx.check("extended hover delivers a trusted mouseenter to the actual page", state == "Hover: inside; trusted: true", state)
        ctx.check("hover reports dispatched, without claiming a semantic postcondition",
                  outcome_is(result, "dispatched", "none"), result)
        text = repl(ctx, f"const reliabilityPage = await context.newPage(); "
                    f"await reliabilityPage.goto({json.dumps(base + '/browser-reliability.html')}); "
                    "await reliabilityPage.getByRole('button', { name: 'Hover target', exact: true }).hover(); "
                    "await reliabilityPage.locator('#hover-status').textContent()")
        ctx.cleanups.append(lambda: ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {
            "code": "await reliabilityPage.close()"}}))
        ctx.check("REPL locator.hover delivers a trusted mouseenter", text == "Hover: inside; trusted: true", text)
        text = repl(ctx, "await reliabilityPage.mouse.move(5, 5); "
                    "await reliabilityPage.locator('#hover-status').textContent()")
        ctx.check("REPL mouse.move leaves the hovered element", text == "Hover: outside", text)
        text = repl(ctx, "const hoverBounds = await reliabilityPage.locator('#hover-target').boundingBox(); "
                    "await reliabilityPage.mouse.move(hoverBounds.x + hoverBounds.width / 2, hoverBounds.y + hoverBounds.height / 2); "
                    "await reliabilityPage.locator('#hover-status').textContent()")
        ctx.check("REPL mouse.move enters the target at grounded viewport coordinates", text == "Hover: inside; trusted: true", text)


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
