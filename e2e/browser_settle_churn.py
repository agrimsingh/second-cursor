#!/usr/bin/env python3
"""How long a browser action waits for the page to settle when the page never stops changing on its own.

Ways settling can fail, written before the implementation; each is the acceptance contract for a case below or a
named risk:

Returning too late (the action's change has rendered, the read still waits):
- A live clock, countdown or ticker mutates text faster than the 300 ms quiet period, so the read waits out its
  whole 2 s cap after every action (settle_churn_checkbox, settle_churn_date_picker: clock every 150 ms).
- A carousel, rotating banner or "updated N s ago" label swaps nodes every second (the fixture's carousel).
- A constant-URL poll starts after the action, so it counts as the action's request; with several pollers or
  analytics batches the network is never quiet for 300 ms (the fixture polls every 500 ms).
- A long poll answers and is reopened after the action; the new request never finishes, so it holds the read until
  the 10 s request deadline (the fixture reopens a 6 s long poll).
- Any request seen pending extends the deadline to 10 s, after which DOM churn alone holds the read that long.
- CSS animations (a spinner, a shimmer) change no DOM and must stay free (the fixture's CSS spinner).
- A click that changes nothing still waits the 1.2 s reaction window (unchanged, and accepted: a request may follow).
- Video, ads and iframes mutate inside other documents, which the main-frame observer does not see (not a cost).

Returning too early (the read misses what the action caused):
- Search results arrive 2-5 s later from a request, behind a spinner (settle_churn_search: 3 s fetch).
- Results arrive later over a channel the network tracker does not wait for (a WebSocket push, a timer) behind a
  spinner, so the spinner's insertion looks like the whole reaction (settle_churn_live_search: 2.5 s timer).
- Ignoring "background" activity ignores the action's own change: the action re-renders a container that also
  churns on its own (a live feed the action filters), or calls the same request a poll calls (a refresh button, one
  GraphQL query polled and fetched with identical body). Only activity seen while the agent was idle may be ignored:
  a DOM change only once the same node changed the same way twice, a request only with identical method, URL and body.
- A long poll that reopens just after the action, before the page was ever seen reopening it, is the action's
  request as far as anything can tell; the first such action on a page may still wait for it.
- A change of the previous action that lands after its read returned (a late toast) is mistaken for background
  churn, so the same change from the next action is ignored.
- The agent acts again within one churn period of the last read (a fast agent, a batch), so there is no idle
  baseline; settling must then fall back to waiting as before rather than guess.
- A permanent spinner or busy region that existed before the action holds every later read until its cap.
- An SPA route change replaces the content without a document load; it is a DOM change like any other.
- A navigation replaces the document and its observer; the new document has no baseline (load path, unchanged).
- A JavaScript dialog blocks the page's script; the action answers before any read (unchanged).
- Lazy images and fonts load after the click; images are not waited for (unchanged).

Ways settling can fail for a fast client (one that acts as soon as it reads, or batches actions), written before the
change that learns churn from every idle window (and a load's own settle) for the document's lifetime, and stops
waiting on a request 5 s old once the action's change has rendered and the page has been still for 1 s:

Returning too late:
- Each idle window (read settled to next input) is shorter than a churn period, so the last window alone never sees
  a clock or carousel change twice; every read waits out its 2 s cap, or 10 s once any request extended the deadline
  (settle_fast_churn_clicks: no think time).
- The page's long poll was opened before its requests were tracked (during browser_open's load), so the first tracked
  instance is the one it reopens during an action, counted as the action's request until it answers or the 10 s cap
  (settle_fast_churn_clicks: the fixture's 6 s long poll).
- The page reopens a channel or fires a request after every input with a new URL each time (a cursor, an event id),
  so no request identity ever repeats and each action waits for it until the 10 s cap (settle_fast_reopened_channel;
  the Navan date click: a request never seen before started at the input and answered 10.0 s later).
- The request outlives the action's change but the page never goes quiet (churn not yet learned), so nothing shows
  the reaction is over; the read falls back to the 10 s cap, never earlier.
- An infinite animation unrelated to loading starts with the action (a pulsing badge, a custom blinking caret) and is
  taken for a spinner, so the request is still waited for until the cap, never earlier.
- EventSource, WebSocket and sendBeacon requests are not tracked at all; keepalive fetches and streamed responses
  are tracked like any fetch, and a stream that never ends is a long poll here (released like one).

Returning too early:
- The action's results come from a slow request behind a spinner without aria-busy or a progressbar role, animated
  only by CSS (settle_fast_slow_result: a 5 s request behind a CSS spinner, while a channel reopens on every input).
- The action shows nothing until its slow request answers (settle_fast_slow_result: 5 s, nothing rendered meanwhile).
- The action's loading state does not animate through CSS or Web Animations (a static "Searching..." text, a GIF or
  SMIL spinner): its request, pending 5 s while the page has been still for 1 s, is taken for the page's own and the
  read returns showing the loading state (a named risk; settling reports outlived_requests, so a client can read
  again).
- Results stream in batches more than 1 s apart over one request with no animated loader; the read returns after
  the first batch that lands 5 s or more into the request.
- The real result needs a request the page starts only after a slow one answers; releasing the slow one returns
  before the second starts (as finishing it 300 ms earlier always could).
- Learned churn is remembered for the document's lifetime: a node that changed the same way twice while the agent
  was idle (rows rendered lazily after a read, a container a load filled twice) never counts again, so an action
  whose only change is to it shows no reaction; a pending request is then never released (no counted reaction), and
  without one the read waits the reaction window.
- A browser_navigate whose shell renders and then waits on a boot request without an animated loader returns after
  5 s with the shell (browser_open tracks no request before its read).

Usage: SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_settle_churn.py [scenario ...] [--rounds N]
       [--think S]   seconds of model think time between reading the page and acting on it (default 1.5; the
                     settle_fast_* scenarios always act at once)
SC_E2E_EXTENSION=DIR measures a saved baseline extension. Writes settle-churn.json with every ext.settled sample.
"""
import json
import os
import re
import statistics
import sys
import time

from run import FixtureServer, Run, SCENARIOS, Skip, scenario

SCENARIOS.clear()
ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 3
THINK = float(sys.argv[sys.argv.index("--think") + 1]) if "--think" in sys.argv else 1.5
RESULTS = {}
BASE = None


def open_tab(ctx, query=""):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    # One server and no browser_close for the whole run: after a tab that ran a long poll is closed, or its server
    # stops answering it, the next browser_open never finishes loading (before this change too). Blanking the tab
    # stops its polls, so finished scenarios do not hold Chrome's six connections to the server.
    tab = ctx.call("browser_open", url=f"{BASE}/settle-churn.html{query}")["tab"]["id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("browser_navigate", tab_id=tab, url="about:blank"))
    return tab


def ref(outline, role, label):
    match = re.search(r'\[(e\d+)\] ' + re.escape(role) + r' "' + re.escape(label) + r'"', outline)
    if not match:
        raise AssertionError(f"missing {role} {label!r} in:\n{outline[:2000]}")
    return match[1]


def timed_click(ctx, tab, target, label, think=None):
    """Clicks after a think pause, returning the observation's outline and recording the call's ext.settled."""
    instance = ctx.run.instance
    time.sleep(THINK if think is None else think)
    start = len(instance.timings()) if instance else 0
    began = time.perf_counter()
    result = ctx.call("browser_click", tab_id=tab, ref=target)
    client = round((time.perf_counter() - began) * 1000, 1)
    row = None
    deadline = time.monotonic() + 1
    while instance and row is None and time.monotonic() < deadline:
        row = next((r for r in reversed(instance.timings(start)) if r.get("tool") == "browser_click"), None)
        time.sleep(0.01)
    phases = (row or {}).get("phases", {})
    sample = {"client_ms": client, "settled_ms": phases.get("ext.settled"),
              "phases": {k: v for k, v in phases.items() if k.startswith("ext.settled")},
              "settling": (result.get("observation") or {}).get("settling")}
    RESULTS.setdefault(label, []).append(sample)
    return (result.get("observation") or {}).get("outline", ""), sample


def median_settled(label):
    values = [s["settled_ms"] for s in RESULTS.get(label, []) if s["settled_ms"] is not None]
    return statistics.median(values) if values else None


def check_median(ctx, label, low, high):
    median = median_settled(label)
    samples = [s["settled_ms"] for s in RESULTS.get(label, [])]
    ctx.check(f"{label}: median ext.settled {median} ms within [{low}, {high}) ms",
              median is not None and low <= median < high, samples)


@scenario
def settle_static_click(ctx):
    tab = open_tab(ctx, "?calm")
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    target = ref(outline, "button", "Count")
    for i in range(1, ROUNDS + 1):
        observed, _ = timed_click(ctx, tab, target, "static click")
        ctx.check(f"static click {i} observes Count: {i}", f"Count: {i}" in observed, observed[-400:])
    check_median(ctx, "static click", 0, 700)


@scenario
def settle_churn_date_picker(ctx):
    tab = open_tab(ctx)
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    field = ref(outline, "button", "Departure date")
    for i in range(ROUNDS):
        opened, _ = timed_click(ctx, tab, field, "churn: open date picker")
        ctx.require(f"round {i + 1}: the date grid opened", 'button "Oct 14"' in opened, opened[-800:])
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        day = 14 + i
        observed, _ = timed_click(ctx, tab, ref(outline, "button", f"Oct {day}"), "churn: click a date")
        ctx.check(f"round {i + 1}: the date click's own observation shows Departure: Oct {day}",
                  f"Departure: Oct {day}" in observed, observed[-800:])
    check_median(ctx, "churn: click a date", 0, 1000)


@scenario
def settle_churn_checkbox(ctx):
    tab = open_tab(ctx)
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    target = ref(outline, "checkbox", "In policy only")
    for i in range(ROUNDS):
        observed, _ = timed_click(ctx, tab, target, "churn: in-policy checkbox")
        checked = i % 2 == 0
        line = next((l for l in observed.splitlines() if 'checkbox "In policy only"' in l
                     and not l.lstrip().startswith("-")), "")
        ctx.check(f"click {i + 1}: the observation shows the checkbox checked={checked}",
                  bool(re.search(r"\bchecked\b", line)) is checked, observed[-800:])
    check_median(ctx, "churn: in-policy checkbox", 0, 1000)


@scenario
def settle_churn_search(ctx):
    tab = open_tab(ctx)
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    target = ref(outline, "button", "Search")
    for i in range(1, ROUNDS + 1):
        observed, _ = timed_click(ctx, tab, target, "churn: search, results after a 3 s request")
        ctx.check(f"search {i}: its own observation has the results, not the spinner",
                  f"Fare 1 for search {i}" in observed and "Loading results" not in observed, observed[-800:])
    check_median(ctx, "churn: search, results after a 3 s request", 2900, 4500)


@scenario
def settle_churn_live_search(ctx):
    tab = open_tab(ctx)
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    target = ref(outline, "button", "Search live fares")
    for i in range(1, ROUNDS + 1):
        observed, _ = timed_click(ctx, tab, target, "churn: live search, results pushed after 2.5 s")
        ctx.check(f"live search {i}: its own observation has the results, not the spinner",
                  f"Live fare 1 for search {i}" in observed and "Loading live fares" not in observed,
                  observed[-800:])
    check_median(ctx, "churn: live search, results pushed after 2.5 s", 2400, 4000)


def date_rounds(ctx, tab, prefix, rounds):
    """Opens the date picker, reads it and picks a date, then toggles the checkbox, all without think time."""
    for i in range(rounds):
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        opened, _ = timed_click(ctx, tab, ref(outline, "button", "Departure date"), f"{prefix}: open date picker", 0)
        ctx.require(f"{prefix} round {i + 1}: the date grid opened", 'button "Oct 14"' in opened, opened[-800:])
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        day = 14 + i % 12
        observed, _ = timed_click(ctx, tab, ref(outline, "button", f"Oct {day}"), f"{prefix}: click a date", 0)
        ctx.check(f"{prefix} round {i + 1}: the date click's own observation shows Departure: Oct {day}",
                  f"Departure: Oct {day}" in observed, observed[-800:])
        observed, _ = timed_click(ctx, tab, ref(outline, "checkbox", "In policy only"), f"{prefix}: checkbox", 0)
        line = next((l for l in observed.splitlines() if 'checkbox "In policy only"' in l
                     and not l.lstrip().startswith("-")), "")
        ctx.check(f"{prefix} round {i + 1}: the observation shows the checkbox checked={i % 2 == 0}",
                  bool(re.search(r"\bchecked\b", line)) is (i % 2 == 0), observed[-800:])


@scenario
def settle_fast_churn_clicks(ctx):
    tab = open_tab(ctx)
    date_rounds(ctx, tab, "fast churn", ROUNDS * 2)
    for label in ("open date picker", "click a date", "checkbox"):
        check_median(ctx, f"fast churn: {label}", 0, 1000)


@scenario
def settle_fast_reopened_channel(ctx):
    tab = open_tab(ctx, "?reopen")
    date_rounds(ctx, tab, "fast reopen", ROUNDS)
    for label in ("open date picker", "click a date", "checkbox"):
        check_median(ctx, f"fast reopen: {label}", 0, 6000)


@scenario
def settle_fast_slow_result(ctx):
    tab = open_tab(ctx, "?reopen")
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    spinner, silent = ref(outline, "button", "Search, CSS spinner"), ref(outline, "button", "Search, no indicator")
    for i in range(1, ROUNDS + 1):
        observed, _ = timed_click(ctx, tab, spinner, "fast: results after 5 s behind a CSS spinner", 0)
        ctx.check(f"spinner search {i}: its own observation has the results, not the spinner",
                  f"Spun fare 1 for search {i}" in observed and "Finding fares" not in observed, observed[-800:])
        observed, _ = timed_click(ctx, tab, silent, "fast: results after 5 s, nothing shown meanwhile", 0)
        ctx.check(f"silent search {i}: its own observation has the results",
                  f"Silent fare 1 for search {i}" in observed, observed[-800:])
    check_median(ctx, "fast: results after 5 s behind a CSS spinner", 4900, 6500)
    check_median(ctx, "fast: results after 5 s, nothing shown meanwhile", 4900, 6500)


if __name__ == "__main__":
    run = Run()
    server = FixtureServer()
    BASE = server.__enter__()
    try:
        ok = run.run([a for a in sys.argv[1:] if not re.fullmatch(r"[\d.]+", a)])
    finally:
        server.__exit__(None, None, None)
        (run.dir / "settle-churn.json").write_text(json.dumps({
            "extension": os.environ.get("SC_E2E_EXTENSION") or "extension", "rounds": ROUNDS, "think_s": THINK,
            "median_settled_ms": {label: median_settled(label) for label in RESULTS}, "samples": RESULTS,
        }, indent=2) + "\n")
    sys.exit(0 if ok else 1)
