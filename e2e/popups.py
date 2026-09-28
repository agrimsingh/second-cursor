#!/usr/bin/env python3
"""Tabs and windows an agent tab opens, what the model is told about them, and the agent tab's viewport.

Drives e2e/fixtures/popups.html in the isolated test browser. The user's side is observed past the extension, through
Chrome's own DevTools endpoint: the user's tab (e2e/fixtures/user.html) logs its own visibility changes, and the
extension's service worker is asked for chrome.windows state (which tab each window shows, which window has focus).

Ways this can fail, and the checks that catch them:
- A target=_blank link, window.open, or window.open with window features, clicked by the agent, opens a tab that
  becomes the active tab of the user's window: the user's tab goes hidden and stays hidden.
- The opened tab is not an agent tab: browser_snapshot refuses it, and browser_tabs does not list it.
- The model is never told: no "[system] popup opened" line in the click's result or the next one.
- A popup that closes itself (window.close) leaves the model holding a dead tab id without a word.
- Dialogs and downloads the agent caused are only visible by polling browser_command.
- Agent tabs render at the window's size, so a resize between a screenshot and a click moves the page under the
  model's coordinates. The default is only measured (<scenario>.json): in headless Chrome a background tab keeps its
  size and a tab the user is watching follows the window. The viewport operation must hold its size either way.

Usage:  SC_E2E_APP=build/SecondCursor.app python3 e2e/popups.py [scenario ...]
Output: e2e/artifacts/<run-id>/ as e2e/run.py, plus <scenario>.json with the measurements behind each check.
"""

import json, re, sys, time, urllib.request
from run import Run, SCENARIOS, scenario, FixtureServer, DevTools, ToolError, Skip

SCENARIOS.clear()

EXTENSION_ORIGIN = "chrome-extension://nfjjbnelcophiemlajgbmahaejppblog/"
CHROME_STATE = """chrome.windows.getAll({populate: true}).then((ws) => ws.map((w) => ({
  id: w.id, type: w.type, focused: w.focused, width: w.width, height: w.height,
  tabs: w.tabs.map((t) => ({id: t.id, url: t.url || t.pendingUrl, active: t.active, opener: t.openerTabId ?? null,
                             group: t.groupId}))})))"""


def targets(browser):
    port = int((browser.profile / "DevToolsActivePort").read_text().split()[0])
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        return json.load(response)


def evaluate(ws_url, expression):
    with DevTools(ws_url) as client:
        reply = client.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in reply:
        raise ToolError(reply["exceptionDetails"].get("text", "evaluation failed"))
    return reply["result"].get("value")


def worker(browser):
    return next(t["webSocketDebuggerUrl"] for t in targets(browser)
                if t.get("type") == "service_worker" and t.get("url", "").startswith(EXTENSION_ORIGIN))


def chrome_state(browser):
    return evaluate(worker(browser), CHROME_STATE)


def find(state, fragment):
    """(window, tab) of the tab whose URL contains `fragment`, or None."""
    return next(((w, t) for w in state for t in w["tabs"] if fragment in (t["url"] or "")), None)


def wait_until(read, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        value = read()
        if value or time.time() > deadline:
            return value
        time.sleep(0.1)


def user_tab(browser, url):
    """Turns the test browser's first tab, the one a person would be looking at, into e2e/fixtures/user.html."""
    page = next(t for t in targets(browser) if t.get("type") == "page" and t.get("url") in ("about:blank", url))
    with DevTools(page["webSocketDebuggerUrl"]) as client:
        client.call("Page.navigate", {"url": url})
    ws = wait_until(lambda: next((t["webSocketDebuggerUrl"] for t in targets(browser)
                                  if t.get("type") == "page" and t.get("url") == url), None))
    wait_until(lambda: evaluate(ws, "typeof seen === 'object' && document.readyState === 'complete'"))
    return ws


def hidden_ms(seen, since):
    """How long the user's tab was hidden in the visibility log after entry `since`; None if it never was."""
    total, hid = 0.0, None
    for state, at in seen[since:]:
        if state == "hidden" and hid is None:
            hid = at
        elif state == "visible" and hid is not None:
            total, hid = total + at - hid, None
    if hid is not None:
        return float("inf")
    return total if any(s == "hidden" for s, _ in seen[since:]) else None


def events_of(result):
    return [line for line in result.get("events", []) if isinstance(line, str)]


def save(ctx, measurements):
    (ctx.run.dir / f"{ctx.name}.json").write_text(json.dumps(measurements, indent=2, default=str))


def ref_of(outline, role, name):
    match = re.search(rf'\[(\w+)\] {role} "{re.escape(name)}"', outline)
    return match and match.group(1)


def close_tab(ctx, browser, tab_id):
    try:
        ctx.mcp.call("browser_close", tab_id=tab_id)
    except ToolError:
        evaluate(worker(browser), f"chrome.tabs.remove({tab_id}).catch(() => {{}})")


@scenario
def browser_popups(ctx):
    app = ctx.run.instance
    if app is None or app.browser is None:
        raise Skip("needs the isolated test browser")
    browser = app.browser
    measurements = {}
    with FixtureServer() as base:
        user = user_tab(browser, f"{base}/user.html")
        opened = ctx.call("browser_open", url=f"{base}/popups.html")
        tid = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = opened["observation"]["outline"]
        state = chrome_state(browser)
        agent_window, _ = find(state, "popups.html")
        _, mine = find(state, "user.html")
        ctx.require("before any popup, the user's tab is the active one in the window the agent tab shares",
                    mine["active"] and evaluate(user, "document.visibilityState") == "visible", state)
        measurements["initial"] = state

        for label, role, name, fragment in [
            ("target=_blank link", "link", "Open in new tab", "via=blank"),
            ("window.open", "button", "Open with window.open", "via=open"),
            ("window.open with window features", "button", "Open popup window", "via=features"),
        ]:
            since = evaluate(user, "seen.length")
            result = ctx.call("browser_click", tab_id=tid, ref=ref_of(outline, role, name))
            found = wait_until(lambda: find(chrome_state(browser), fragment))
            ctx.require(f"{label}: Chrome opened it", found, chrome_state(browser))
            time.sleep(1.0)
            state = chrome_state(browser)
            window, tab = find(state, fragment)
            seen = evaluate(user, "seen")
            listed = [t["id"] for t in ctx.call("browser_tabs")["tabs"]]
            try:
                read = ctx.call("browser_snapshot", tab_id=tab["id"])
                drivable, error = "Next page" in read["outline"], None
            except ToolError as e:
                read, drivable, error = {}, False, str(e)
            told = [line for line in events_of(result) + events_of(read) if "popup opened" in line]
            flash = hidden_ms(seen, since)
            measurements[label] = {
                "click_result": result, "tab": tab, "window": {k: window[k] for k in ("id", "type", "focused")},
                "same_window_as_agent_tab": window["id"] == agent_window["id"], "user_tab_visibility": seen[since:],
                "user_tab_hidden_ms": flash, "listed_by_browser_tabs": tab["id"] in listed,
                "snapshot_error": error, "events": {"click": events_of(result), "snapshot": events_of(read)},
                "windows": state,
            }
            ctx.check(f"{label}: the user's tab keeps the view (hidden for at most 500 ms)",
                      flash is None or flash <= 500, f"hidden {flash} ms; {seen[since:]}")
            ctx.check(f"{label}: the opened tab is not the active tab of its window, or it has a window of its own",
                      not tab["active"] or window["id"] != agent_window["id"], tab)
            ctx.check(f"{label}: browser_tabs lists it", tab["id"] in listed, listed)
            ctx.check(f"{label}: the agent can read it", drivable, error)
            ctx.check(f"{label}: the model is told, with its tab id",
                      any(f"tab {tab['id']}" in line for line in told), told)
            if window["type"] == "normal":
                ctx.check(f"{label}: it joins the agent tab's group", tab["group"] != -1 and tab["group"] ==
                          next(t for t in window["tabs"] if t["id"] == tid)["group"], window["tabs"])
            close_tab(ctx, browser, tab["id"])

        result = ctx.call("browser_click", tab_id=tid, ref=ref_of(outline, "button", "Open self-closing popup"))
        found = wait_until(lambda: find(chrome_state(browser), "popup-close.html"))
        ctx.require("the self-closing popup opened", found)
        closer = found[1]["id"]
        gone = wait_until(lambda: not find(chrome_state(browser), "popup-close.html"), timeout=8)
        after = ctx.call("browser_snapshot", tab_id=tid, full=False)
        lines = events_of(result) + events_of(after)
        measurements["self-closing popup"] = {"tab": found[1], "closed": bool(gone), "events": lines}
        ctx.check("the popup closed itself", gone)
        ctx.check("the model is told it closed", any(f"tab {closer}" in l and "closed" in l for l in lines), lines)

        result = ctx.call("browser_click", tab_id=tid, ref=ref_of(outline, "button", "Ask"))
        answered = ctx.call("browser_command", tab_id=tid, operation="dialog", accept=True)
        read = ctx.call("browser_snapshot", tab_id=tid)
        lines = events_of(result) + events_of(answered) + events_of(read)
        measurements["dialog"] = {"click_result": result, "answer_result": answered, "events": lines}
        ctx.check("the dialog was answered", "confirmed" in read["outline"], read["outline"][-300:])
        ctx.check("the model is told a dialog opened", any("dialog opened" in l and "Proceed?" in l for l in lines),
                  lines)
        ctx.check("and that it was answered", any("dialog closed" in l for l in lines), lines)

        result = ctx.call("browser_click", tab_id=tid, ref=ref_of(outline, "link", "Download a copy"))
        lines = events_of(result)
        deadline = time.time() + 8
        while not any("download finished" in l for l in lines) and time.time() < deadline:
            time.sleep(0.3)
            lines += events_of(ctx.call("browser_snapshot", tab_id=tid, full=False))
        downloads = ctx.call("browser_command", tab_id=tid, operation="downloads")["downloads"]
        measurements["download"] = {"events": lines, "downloads": downloads}
        ctx.check("the download happened", downloads, downloads)
        ctx.check("the model is told it started", any("download started" in l and "popups-copy.html" in l
                                                      for l in lines), lines)
        ctx.check("and that it finished", any("download finished" in l for l in lines), lines)

        # Past the 5 s a click keeps its page activated, a person switches to the agent tab and opens the link.
        time.sleep(5.5)
        state = chrome_state(browser)
        evaluate(worker(browser), f"chrome.tabs.update({tid}, {{active: true}})")
        page = next(t["webSocketDebuggerUrl"] for t in targets(browser) if t.get("url") == f"{base}/popups.html")
        with DevTools(page) as person:
            box = person.call("Runtime.evaluate", {
                "expression": "(({x, y, width, height}) => ({x, y, width, height}))"
                              "(document.querySelector('a[target=_blank]').getBoundingClientRect())",
                "returnByValue": True})["result"]["value"]
            for kind in ("mousePressed", "mouseReleased"):
                person.call("Input.dispatchMouseEvent", {"type": kind, "x": box["x"] + box["width"] / 2,
                                                         "y": box["y"] + box["height"] / 2, "button": "left",
                                                         "clickCount": 1})
        found = wait_until(lambda: find(chrome_state(browser), "via=blank"))
        ctx.require("the person's click opened the link", found)
        time.sleep(1.0)
        _, theirs = find(chrome_state(browser), "via=blank")
        read = ctx.call("browser_snapshot", tab_id=tid, full=False)
        listed = [t["id"] for t in ctx.call("browser_tabs")["tabs"]]
        measurements["opened by the person"] = {"tab": theirs, "events": events_of(read), "listed": listed}
        ctx.check("a tab the person opens from an agent tab stays in front", theirs["active"], theirs)
        ctx.check("and stays theirs, not an agent tab", theirs["id"] not in listed, listed)
        ctx.check("and the model is not told about it", not any("popup opened" in l for l in events_of(read)),
                  events_of(read))
        evaluate(worker(browser), f"chrome.tabs.remove({theirs['id']})")
        _, mine = find(state, "user.html")
        evaluate(worker(browser), f"chrome.tabs.update({mine['id']}, {{active: true}})")
    save(ctx, measurements)


RAF_RATE = """new Promise((done) => { let n = 0; const t0 = performance.now();
  const f = () => { n++; performance.now() - t0 < 1000 ? requestAnimationFrame(f) : done(n); };
  requestAnimationFrame(f); setTimeout(() => done(n), 1500); })"""
SIZE = re.compile(r"inner (\d+)x(\d+) outer (\d+)x(\d+)")


@scenario
def browser_fixed_viewport(ctx):
    """A person resizes the window the agent tab lives in, once while looking at another tab and once while watching
    the agent tab; each time the agent clicks where its screenshot from before the resize showed the corner button."""
    app = ctx.run.instance
    if app is None or app.browser is None:
        raise Skip("needs the isolated test browser")
    browser = app.browser
    measurements = {}
    with FixtureServer() as base:
        for mode in ("default", "viewport operation 1440x900"):
            marker = f"mode={len(measurements)}"
            tid = ctx.call("browser_open", url=f"{base}/popups.html?{marker}")["tab"]["id"]
            ctx.cleanups.append(lambda tid=tid: ctx.mcp.call("browser_close", tab_id=tid))
            if mode != "default":
                ctx.call("browser_command", tab_id=tid, operation="viewport", width=1440, height=900)
            window, _ = find(chrome_state(browser), marker)
            viewed = next(t["id"] for t in window["tabs"] if t["active"])
            page = next(t["webSocketDebuggerUrl"] for t in targets(browser) if marker in t["url"])
            results, hits = {}, 0
            for case, shrink, watching in (("background", (320, 180), False), ("watching", (200, 120), True)):
                shot = ctx.call("browser_screenshot", tab_id=tid)
                size = SIZE.search(ctx.call("browser_snapshot", tab_id=tid)["outline"]).groups()
                bounds = ctx.call("browser_command", tab_id=tid, operation="inspect",
                                  locator={"selector": "#corner"})["bounds"]
                point = (bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
                if watching:
                    evaluate(worker(browser), f"chrome.tabs.update({tid}, {{active: true}})")
                evaluate(worker(browser), f"chrome.windows.update({window['id']}, "
                                          f"{{width: {window['width'] - shrink[0]}, height: {window['height'] - shrink[1]}}})")
                time.sleep(0.5)
                after = ctx.call("browser_screenshot", tab_id=tid)
                after_size = SIZE.search(ctx.call("browser_snapshot", tab_id=tid)["outline"]).groups()
                ctx.call("browser_click", tab_id=tid, x=point[0], y=point[1])
                hit = f"hit {hits + 1}" in ctx.call("browser_snapshot", tab_id=tid)["outline"]
                hits += hit
                results[case] = {
                    "window": [window["width"], window["height"]],
                    "window_resized_to": [window["width"] - shrink[0], window["height"] - shrink[1]],
                    "screenshot_before": [shot["width"], shot["height"]],
                    "screenshot_after": [after["width"], after["height"]],
                    "inner_outer_before": size, "inner_outer_after": after_size,
                    "corner_center_before": point, "click_after_resize_hit": hit,
                }
                if watching:
                    results[case]["raf_per_second_watched"] = evaluate(page, RAF_RATE)
                    evaluate(worker(browser), f"chrome.tabs.update({viewed}, {{active: true}})")
                else:
                    results[case]["raf_per_second_background"] = evaluate(page, RAF_RATE)
                evaluate(worker(browser), f"chrome.windows.update({window['id']}, "
                                          f"{{width: {window['width']}, height: {window['height']}}})")
                time.sleep(0.3)
                if mode != "default":
                    ctx.check(f"{mode}, {case}: the screenshot keeps its size when the user resizes the window",
                              results[case]["screenshot_before"] == results[case]["screenshot_after"], results[case])
                    ctx.check(f"{mode}, {case}: a click at coordinates read before the resize lands", hit,
                              results[case])
            results["screen_and_dpr"] = evaluate(page, "[screen.width, screen.height, devicePixelRatio]")
            measurements[mode] = results
            ctx.mcp.call("browser_close", tab_id=tid)
    save(ctx, measurements)


if __name__ == "__main__":
    run = Run()
    run.run(sys.argv[1:])
    sys.exit(1 if any(x["status"] == "FAIL" for x in run.results) else 0)
