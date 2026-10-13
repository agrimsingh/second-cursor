#!/usr/bin/env python3
"""What a client that only reads tool results is told when it polls a tab, re-reads after an action, or comes back
with state from an earlier conversation, driven over MCP like Grok Bot's Navan run (94 calls, 40 back-to-back
full=true browser_snapshot polls while fares loaded, 20 reads right after a click that had returned the page, and
~25 s lost to an interrupted task and a tab from before the extension reloaded).

Ways it can fail, written down before the code:

Polling an unchanged page (hints_unchanged_reads, hints_poll_replay):
 1. A full=true read of a page that has not changed since the client's last read resends the whole outline every
    time, with no word that nothing changed, so each poll costs the client the page again.
 2. The unchanged answer does not say how to wait in one call, or names a tool, operation or argument that does not
    exist or does not work when followed literally: the browser_command wait it names must return once the expected
    text is on the page.
 3. The read that finds the page changed (the fares arriving) is answered as unchanged, or shortened, so the client
    misses the results: it must return the whole outline with them (full=true) or a diff with them (default).
 4. A short "(no changes)" answer replaces a full=true read when the client does not hold that whole outline: right
    after an action whose observation was only a diff, after a browser_repl read of the tab in between (the model saw
    only what the script printed), after the turn ended, more than 30 s after the whole outline was last sent, after
    an observe=none action, or for structured=true (code wants the elements array). Or it replaces the first or second
    unchanged read in a row: full=true promises the whole outline, and a script that reads twice to parse it
    (find.py host_parameters does) or a model that has not yet seen the hint must still get it.
 5. The short answer breaks diff semantics: diff_base does not name the read whose content it repeats, the next
    default read is not a correct diff from it, or its snapshot_id cannot ground an action (observation_id refused
    as stale).
 6. A default (full=false) unchanged read loses its "(no changes)" outline, or the hint wording find.py's
    call_guidance relies on ("actions return this diff").
 7. The hint claims no action happened when an observe=none action ran in between.
 8. Another session's reads change this session's answers. Not reachable: a tab belongs to one session, and another
    session's browser_snapshot of it is refused.

Reading right after an action (hints_unchanged_reads, hints_compaction_guards):
 9. A read right after an action that already returned the page, finding it unchanged, does not point at that
    action's observation; or a read that finds a later change (the page moved on after the action) is told it was
    redundant.
10. full=true right after an action whose observation was a diff is answered "(no changes)": the client asked for
    the whole page and holds only a diff of it.

State from an earlier conversation (hints_stale_tab, hints_user_controls_still_refuse, hints_interrupted_restart):
11. A tab id that is no longer an agent tab (closed, or from before the extension reloaded) is refused with no way
    forward: the refusal must name browser_open as the one call that gets the page.
12. After the app restarts, a reused session's interrupted task refuses browser_open, which touches no earlier
    target and sends no input to a page the user could be using; it must open the page and continue the task.
13. browser_open continues a task the user paused, took over or cancelled: those must still refuse it, and their
    refusals must not offer browser_open as a way around them.
14. Continuing the task through browser_open lets an earlier target take input without a fresh read, or leaves the
    new tab needing an extra read before its first click (its own observation must count under the new control
    generation), or leaves the extension's session paused so that click is refused there.
15. Other input while interrupted (browser_navigate on an old tab, launch_app) goes through, or is refused without
    naming browser_open and task resume, or tells the client to reconcile an operation_id although nothing was
    dispatched.

Usage:  SC_E2E_APP=$PWD/build/SecondCursor.app python3 e2e/client_hints.py [scenario ...]
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, poll-replay.json)
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

import run as harness
from run import MCP, FixtureServer, Run, SCENARIOS, Skip, ToolError, refused, scenario

SCENARIOS.clear()


def new_session(ctx, title=None):
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    if title:
        ctx.call("task", action="create", session_id=sid, title=title)
    return sid


def require_browser(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")


def ref(outline, role, label):
    match = re.search(r'\[(e\d+)\] ' + re.escape(role) + r' "' + re.escape(label) + r'"', outline)
    if not match:
        raise AssertionError(f"missing {role} {label!r} in:\n{outline[:2000]}")
    return match[1]


def open_search(ctx, base, sid, after_ms):
    opened = ctx.call("browser_open", url=f"{base}/late-search.html?after_ms={after_ms}", session_id=sid)
    tab = opened["tab"]["id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab, session_id=sid))
    return tab, opened


def snapshot(ctx, sid, tab, **args):
    return ctx.call("browser_snapshot", tab_id=tab, session_id=sid, **args)


def wait_arguments(hint):
    """The arguments object the hint tells the client to pass to browser_command operation=wait."""
    start = hint.find("arguments=")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(hint[start + len("arguments="):])[0]
    except ValueError:
        return None


def end_turn(ctx, sid):
    request = urllib.request.Request(
        ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=json.dumps({"session_id": sid}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {ctx.mcp.token}"})
    urllib.request.urlopen(request, timeout=30).read()


@scenario
def hints_unchanged_reads(ctx):
    """Failure modes 1-3, 5, 9 and 10 on a fare search whose results land 7 s after the click."""
    require_browser(ctx)
    sid = new_session(ctx)
    with FixtureServer() as base:
        tab, opened = open_search(ctx, base, sid, 7000)
        clicked = ctx.call("browser_click", tab_id=tab, ref=ref(opened["observation"]["outline"], "button", "Search"),
                           session_id=sid)
        observation = clicked["observation"]
        ctx.require("the click's observation shows the search started, as a diff",
                    observation.get("is_diff") and "Searching for flights" in observation["outline"], observation)

        after = snapshot(ctx, sid, tab, full=True)
        hint = after.get("hint", "")
        ctx.check("full=true right after a diff observation still returns the whole outline (10)",
                  not after.get("is_diff") and 'button "Search"' in after["outline"], after)
        ctx.check("and says the click's observation already had this page, by its snapshot id (9)",
                  "browser_click" in hint and observation["snapshot_id"] in hint and "actions return this diff" in hint,
                  hint)

        second = snapshot(ctx, sid, tab, full=True)
        ctx.check("the second unchanged full=true read in a row is still whole (4)",
                  not second.get("is_diff") and 'button "Search"' in second["outline"]
                  and after["snapshot_id"] in second.get("hint", ""), second)
        poll = snapshot(ctx, sid, tab, full=True)
        hint = poll.get("hint", "")
        ctx.check("the third answers (no changes) instead of the whole outline (1)",
                  poll.get("outline") == "(no changes)" and poll.get("is_diff") is True, poll)
        ctx.check("its diff_base is the read whose whole outline it repeats (5)",
                  poll.get("diff_base") == second["snapshot_id"], poll)
        ctx.check("it costs under 1 KB, whatever the page's size: tab, freshness, settling and the hint (1)",
                  len(json.dumps(poll)) < 1000, (len(json.dumps(poll)), len(json.dumps(after))))
        ctx.check("it names the one-call wait: browser_command, operation=wait, this tab_id and the arguments (2)",
                  "browser_command" in hint and "operation=wait" in hint and f"tab_id={tab}" in hint
                  and wait_arguments(hint) is not None, hint)

        default = snapshot(ctx, sid, tab)
        ctx.check("a default unchanged poll stays (no changes), diffed from the short answer (5, 6)",
                  default.get("outline") == "(no changes)" and default.get("diff_base") == poll["snapshot_id"], default)
        ctx.check("and says no action happened since that read (7)",
                  poll["snapshot_id"] in default.get("hint", "") and "operation=wait" in default.get("hint", ""),
                  default.get("hint"))

        arguments = wait_arguments(hint) or {}
        arguments.setdefault("locator", {})["text"] = "flights found"
        began = time.monotonic()
        try:
            waited = ctx.call("browser_command", operation="wait", tab_id=tab, arguments=arguments, session_id=sid)
        except ToolError as error:
            waited = {"error": str(error)}
        ctx.check("the wait the hint names, followed literally, returns once the fares land (2)",
                  "error" not in waited and time.monotonic() - began < 9.5, (waited, round(time.monotonic() - began, 2)))

        landed = snapshot(ctx, sid, tab, full=True)
        ctx.check("the read after the fares land returns them in the whole outline, without an unchanged hint (3)",
                  "3 flights found (search 1)" in landed["outline"] and "$212 United (search 1)" in landed["outline"]
                  and not landed.get("is_diff") and "Unchanged" not in landed.get("hint", ""), landed)

        snapshot(ctx, sid, tab, full=True)
        snapshot(ctx, sid, tab, full=True)
        again = snapshot(ctx, sid, tab, full=True)
        ctx.require("the third unchanged read in a row after that change is short again", again.get("outline") == "(no changes)",
                    again)
        try:
            grounded = ctx.call("browser_click", tab_id=tab, ref=ref(landed["outline"], "textbox", "From"),
                                observation_id=again["snapshot_id"], session_id=sid)
        except ToolError as error:
            grounded = {"error": str(error)}
        ctx.check("the short answer's snapshot_id grounds an action (observation_id is not stale) (5)",
                  "error" not in grounded, grounded)


@scenario
def hints_compaction_guards(ctx):
    """Failure modes 4, 6, 7 and 9: the short answer is only given when the client holds that whole outline."""
    require_browser(ctx)
    sid = new_session(ctx)
    with FixtureServer() as base:
        tab, opened = open_search(ctx, base, sid, 600000)
        outline = opened["observation"]["outline"]

        first = snapshot(ctx, sid, tab, full=True)
        ctx.check("a full read right after browser_open is whole and points at browser_open's result (4, 9)",
                  'button "Search"' in first.get("outline", "") and "browser_open" in first.get("hint", ""), first)

        def shortening(label):
            """Reads until the answers are short, so that the next read shows whether `label` reset them."""
            reads = [snapshot(ctx, sid, tab, full=True) for _ in range(3)]
            ctx.require(f"before {label}, unchanged full reads in a row turn short",
                        reads[-1].get("outline") == "(no changes)", [r.get("outline", "")[:40] for r in reads])

        def whole(read, label):
            ctx.check(f"after {label}, the full read is whole (4)",
                      read.get("outline") != "(no changes)" and 'button "Search"' in read.get("outline", ""), read)

        shortening("a structured read")
        structured = snapshot(ctx, sid, tab, full=True, structured=True)
        ctx.check("structured=true is never shortened: code wants the elements (4)",
                  structured.get("outline") != "(no changes)" and structured.get("elements"), list(structured))

        shortening("an observe=none action")
        ctx.call("browser_scroll", tab_id=tab, direction="down", amount=1, observe="none", session_id=sid)
        blind = snapshot(ctx, sid, tab, full=True)
        whole(blind, "an observe=none action")
        ctx.check("and does not claim no action happened (7)", "no action" not in blind.get("hint", ""),
                  blind.get("hint"))

        sys.path.insert(0, str(harness.ROOT))
        from repl import repl
        shortening("a browser_repl read")
        ok, text, _, _ = repl(ctx, "const outline = await page.snapshot(); outline.length", tab_id=tab, session_id=sid)
        ctx.require("browser_repl reads the tab", ok, text)
        whole(snapshot(ctx, sid, tab, full=True), "a browser_repl read in between")

        shortening("the turn's end")
        end_turn(ctx, sid)
        whole(snapshot(ctx, sid, tab, full=True), "the turn ended")

        clicked = ctx.call("browser_click", tab_id=tab, ref=ref(outline, "textbox", "To"), session_id=sid)
        default = snapshot(ctx, sid, tab)
        ctx.check("a default read right after an action stays (no changes) with call_guidance's wording (6, 9)",
                  default.get("outline") == "(no changes)" and "actions return this diff" in default.get("hint", "")
                  and "browser_click" in default.get("hint", "")
                  and clicked["observation"]["snapshot_id"] in default.get("hint", ""), default)

        shortening("30 s pass")
        time.sleep(31)
        whole(snapshot(ctx, sid, tab, full=True), "more than 30 s since the whole outline was sent")


@scenario
def hints_stale_tab(ctx):
    """Failure mode 11."""
    require_browser(ctx)
    sid = new_session(ctx)
    with FixtureServer() as base:
        tab = ctx.call("browser_open", url=f"{base}/form.html", session_id=sid)["tab"]["id"]
        ctx.call("browser_close", tab_id=tab, session_id=sid)
        for tool, args in (("browser_snapshot", {}), ("browser_click", {"x": 10, "y": 10}),
                           ("browser_navigate", {"url": f"{base}/form.html"})):
            error = refused(ctx, tool, tab_id=tab, session_id=sid, **args)
            ctx.check(f"{tool} on a tab that is no longer an agent tab names browser_open as the way forward",
                      error is not None and "browser_open" in error.replace("opened with browser_open", ""), error)


@scenario
def hints_user_controls_still_refuse(ctx):
    """Failure mode 13."""
    require_browser(ctx)
    with FixtureServer() as base:
        url = f"{base}/form.html"
        sid = new_session(ctx, "User control fixture")
        ctx.call("browser_open", url=url, session_id=sid)
        for action, status in (("pause", "paused"), ("takeover", "needs_user")):
            ctx.call("task", action=action, session_id=sid)
            error = refused(ctx, "browser_open", url=url, session_id=sid)
            ctx.check(f"after the user's {action}, browser_open is refused",
                      error is not None and status in error, error)
            ctx.check(f"and the {action} refusal does not offer browser_open as a way around it",
                      error is None or "browser_open" not in error, error)
            current = ctx.call("task", action="get", session_id=sid)["task"]
            ctx.check(f"the refused browser_open leaves the task {status}", current["status"] == status, current)
            ctx.call("task", action="resume", session_id=sid)
        ctx.call("task", action="cancel", session_id=sid)
        error = refused(ctx, "browser_open", url=url, session_id=sid)
        ctx.check("after cancel, browser_open is refused", error is not None, error)


def restart(ctx):
    """Restarts only this suite's disposable app process, keeping its SC_HOME, and waits for the browser."""
    instance = ctx.run.instance
    old_pid = instance.pid
    ctx.require("restart is scoped to the disposable app process",
                old_pid and f"SC_HOME={instance.home}" in subprocess.run(
                    ["ps", "-Eww", "-p", str(old_pid), "-o", "command="], capture_output=True, text=True).stdout)
    os.kill(old_pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while harness.alive(old_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    ctx.require("the disposable app stopped", not harness.alive(old_pid))
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
    deadline = time.monotonic() + 45
    while not ctx.call("browser_status").get("connected"):
        if time.monotonic() >= deadline:
            raise AssertionError("the isolated browser did not reconnect after the restart")
        time.sleep(0.5)


@scenario
def hints_interrupted_restart(ctx):
    """Failure modes 12, 14 and 15, across a restart of the disposable app with a reused session."""
    require_browser(ctx)
    if ctx.run.instance is None:
        raise Skip("restart only operates on a disposable SC_HOME")
    with FixtureServer() as base:
        sid = new_session(ctx, "Interrupted by a restart")
        old = ctx.call("browser_open", url=f"{base}/form.html", session_id=sid)
        old_tab = old["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=old_tab, session_id=sid))
        restart(ctx)
        task = ctx.call("task", action="get", session_id=sid)["task"]
        ctx.require("the restart left the reused session's task interrupted", task["status"] == "interrupted", task)

        for tool, args in (("browser_navigate", {"tab_id": old_tab, "url": f"{base}/next.html"}),
                           ("launch_app", {"app": "TextEdit", "urls": [str(ctx.scratch / "never.txt")]})):
            error = refused(ctx, tool, session_id=sid, **args)
            ctx.check(f"{tool} is refused while interrupted (15)", error is not None and "interrupted" in error, error)
            ctx.check(f"and its refusal names browser_open and task resume, with no operation to reconcile (15)",
                      error is not None and "browser_open" in error and "resume" in error and "operation_id=" not in error,
                      error)

        try:
            opened = ctx.call("browser_open", url=f"{base}/form.html", session_id=sid)
        except ToolError as error:
            opened = {"error": str(error)}
        ctx.check("browser_open is not refused: it opens the page (12)",
                  "error" not in opened and 'button "Submit"' in opened.get("observation", {}).get("outline", ""), opened)
        if "error" in opened:
            return
        tab = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab, session_id=sid))
        task = ctx.call("task", action="get", session_id=sid)["task"]
        ctx.check("and continues the task, saying so in its result (12)",
                  task["status"] not in ("interrupted", "paused", "needs_user")
                  and opened.get("resumed_task") == task["id"], (task, opened.get("resumed_task")))

        outline = opened["observation"]["outline"]
        try:
            typed = ctx.call("browser_type", tab_id=tab, ref=ref(outline, "textbox", "Name"), text="Ada", session_id=sid)
            clicked = ctx.call("browser_click", tab_id=tab, ref=ref(outline, "button", "Submit"), session_id=sid)
            landed = "Hello, Ada" in clicked["observation"]["outline"]
        except ToolError as error:
            typed, landed = str(error), False
        ctx.check("the new tab takes input with no extra read, in the app and in the extension (14)", landed, typed)

        stale = refused(ctx, "browser_click", tab_id=old_tab, ref=ref(old["observation"]["outline"], "button", "Submit"),
                        session_id=sid)
        ctx.check("a tab from before the restart still needs a fresh read before input (14)",
                  stale is not None and "read target" in stale, stale)
        fresh = snapshot(ctx, sid, old_tab, full=True)
        clicked = ctx.call("browser_click", tab_id=old_tab, ref=ref(fresh["outline"], "button", "Submit"),
                           session_id=sid)
        ctx.check("and takes input once read", "outcome" in clicked, clicked)


@scenario
def hints_poll_replay(ctx):
    """The Navan waste replayed: click Search, then poll browser_snapshot full=true every 0.5 s like Grok Bot did."""
    require_browser(ctx)
    sid = new_session(ctx)
    polls = []
    with FixtureServer() as base:
        tab, opened = open_search(ctx, base, sid, 8000)
        clicked_at = time.monotonic()
        ctx.call("browser_click", tab_id=tab, ref=ref(opened["observation"]["outline"], "button", "Search"),
                 session_id=sid)
        while time.monotonic() - clicked_at < 20:
            began = time.monotonic()
            read = snapshot(ctx, sid, tab, full=True)
            polls.append({"at_s": round(began - clicked_at, 2), "ms": round((time.monotonic() - began) * 1000, 1),
                          "chars": len(json.dumps(read)), "outline_chars": len(read.get("outline", "")),
                          "is_diff": read.get("is_diff"), "outline_head": read.get("outline", "")[:80],
                          "hint": read.get("hint")})
            if "flights found" in read.get("outline", ""):
                break
            time.sleep(0.5)
    (ctx.run.dir / "poll-replay.json").write_text(json.dumps(polls, indent=2) + "\n")
    # The first two polls are whole (the first follows the click's diff); the last is the one that finds the fares.
    waiting = polls[2:-1]
    ctx.check(f"the fares arrived in a whole outline after {len(polls)} polls (3)",
              bool(polls) and not polls[-1]["is_diff"] and polls[-1]["outline_chars"] > 200, polls[-1:])
    ctx.check("every poll while nothing changed after the second was short (1)",
              bool(waiting) and all(p["outline_head"] == "(no changes)" for p in waiting),
              [p["outline_head"] for p in polls])
    ctx.check("and every poll before the fares named the one-call wait (2)",
              len(polls) > 1 and all("operation=wait" in (p["hint"] or "") for p in polls[:-1]),
              [p["hint"] for p in polls[:3]])
    print(f"poll replay: {len(polls)} polls, {sum(p['chars'] for p in polls)} characters in all; "
          f"the whole outline is {polls[-1]['outline_chars'] if polls else 0} characters", flush=True)


if __name__ == "__main__":
    sys.exit(0 if Run().run(sys.argv[1:]) else 1)
