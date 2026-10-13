#!/usr/bin/env python3
"""Whether browser calls keep working once a page's renderer stops answering: after the agent closes a tab whose page
ran a long poll, after a page's server stops answering it, and when a page's own script never yields.

What happens (measured on the isolated Chrome for Testing before the fix): closing a tab whose page loops
`for (;;) await fetch(...).catch(() => {})` aborts the poll, and every fetch the dying document starts then fails at
once, so the loop never leaves the microtask queue and the renderer's main thread spins forever. Chrome without the
extension does the same. Chrome then puts the next new tab of that site into that renderer, once: the tab never
commits its navigation and never answers CDP. browser_open waited 20 s for the load, then forever on Page.enable,
and every later call without a tab (browser_tabs, browser_open) queued behind it in the extension and the app.

Ways the fix can fail, written before the implementation; each is a check below or a named risk:

Still hanging, or recovering too slowly:
- The replacement for a stranded tab lands in the same hung renderer, so the open fails a second time
  (open_after_longpoll_close: the open returns the page within 12 s).
- Closing the stranded tab hands the hung renderer back to the next open, so every open pays the detection time
  (open_after_longpoll_close: a second open within 5 s).
- Detection only after the 20 s load wait, so the open takes 25 s or more (every open within 12 s).
- A navigation of an existing tab into the site of the hung renderer strands the same way, and the agent holds that
  tab's id, so it cannot be swapped for a new tab. Measured: that tab never loads the site again (navigating it again,
  discarding it, detaching first all stay stuck), so browser_navigate must say so within 12 s and name browser_open,
  later calls on the tab must fail as fast instead of waiting forever (which would also queue browser_close behind
  them), and the tab must still close (navigate_after_longpoll_close).
- The stranded tab is left behind: stuck loading in the user's window, or still listed by browser_tabs
  (every scenario: each page target answers, and browser_tabs lists only the agent's live tabs).
- The renderer stops answering after the tab was attached (a runaway script once the page loads), so a later step
  of the open (settling, the snapshot) waits forever and still holds every call without a tab; it must answer by its
  deadline and leave later calls free (hung_page_open_releases_browser).
- browser_tabs waits behind a stuck or slow open in the extension's or the app's per-browser lane (every scenario
  times browser_tabs right after the open).
- The page's server stops answering (requests held open forever) or refuses connections, before and after the tab is
  closed (open_after_server_stops_answering, open_after_server_refuses).
- The navigation sequence of the original report: a long-poll page, a second page, back to the long poll, about:blank,
  close (open_after_longpoll_navigations).

Recovering when nothing was wrong:
- A page whose server answers late (8 s) is taken for a hung renderer and replaced, requesting the URL twice (a GET
  with side effects repeated) and taking longer (slow_server_open_is_not_replaced: one request, open within 8 s plus
  the usual load).
- A renderer busy for a few seconds (a heavy page sharing the new tab's process, a slow machine starting a process)
  is declared hung and its tab replaced. Not exercised; the bound is 5 s of no answer at all.
- Attaching the debugger before the first document commits changes what the load wait and settling see, so normal
  opens get slower or settle early (regression suites: browser_settle_churn, browser_reliability, run.py browser).
- A JavaScript dialog blocks the renderer and leaves the click that opened it pending until it is answered; an agent
  that thinks for more than 10 s would find the tab declared hung and could not answer the dialog
  (dialog_answered_after_twelve_seconds). Found while implementing the detector.
- A page legitimately busy for more than 10 s (a long synchronous task, window.print()) is declared hung, and calls
  on it fail until it commits a new document, even after it recovers. Not exercised.
- Not fixed: the hung renderer spins a CPU core until Chrome exits, with or without the extension; an extension
  cannot end another renderer's script or process. Each scenario kills the ones it made in the test browser.

Usage: SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_longpoll_close.py [scenario ...]
Writes longpoll-close.json with every timed call.
"""
import http.server
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request

from run import ROOT, SCENARIOS, DevTools, Run, Skip, ToolError, scenario

SCENARIOS.clear()
RESULTS = {}
OPEN_BOUND_S = 12
TABS_BOUND_S = 3
CALL_TIMEOUT_MS = 20000


class Server:
    """Serves e2e/fixtures (?delay_ms=N answers N ms late, at most 10 s) and logs each request path.
    stop_answering() stops accepting and holds every open request, like a backend that hangs; refuse() drops open
    requests unanswered and closes the socket, so new connections are refused."""

    def __init__(self):
        self.requests, self.mode = [], "answering"
        server = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(ROOT / "fixtures"), **kwargs)

            def log_message(self, *args):
                pass

            def do_GET(self):
                server.requests.append(self.path)
                delay = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("delay_ms", ["0"])[0]
                until = time.monotonic() + min(int(delay), 10000) / 1000
                while time.monotonic() < until or server.mode == "holding":
                    if server.mode == "refusing":
                        return
                    time.sleep(0.05)
                if server.mode == "refusing":
                    return
                super().do_GET()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        # Chrome drops the requests of a closed tab; those broken pipes are expected.
        self.httpd.handle_error = lambda *args: None
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path, host="127.0.0.1"):
        return f"http://{host}:{self.port}/{path}"

    def stop_answering(self):
        self.mode = "holding"
        self.httpd.shutdown()

    def refuse(self):
        self.mode = "refusing"
        self.httpd.shutdown()
        self.httpd.server_close()

    def close(self):
        if self.mode == "answering":
            self.httpd.shutdown()
        if self.mode != "refusing":
            self.mode = "refusing"
            self.httpd.server_close()


def devtools_port(ctx):
    instance = ctx.run.instance
    if instance is None or instance.browser is None:
        raise Skip("needs the isolated test browser (not --installed)")
    return int((instance.browser.profile / "DevToolsActivePort").read_text().split()[0])


def devtools_get(ctx, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{devtools_port(ctx)}{path}", timeout=5) as response:
        return json.load(response)


def renderer_cpu(ctx):
    with DevTools(devtools_get(ctx, "/json/version")["webSocketDebuggerUrl"]) as browser:
        infos = browser.call("SystemInfo.getProcessInfo")["processInfo"]
    return {p["id"]: p["cpuTime"] for p in infos if p["type"] == "renderer"}


def spinning(ctx):
    """Renderers of the test browser that used most of a core over the last second."""
    before = renderer_cpu(ctx)
    time.sleep(1)
    after = renderer_cpu(ctx)
    return sorted(pid for pid in after if after[pid] - before.get(pid, after[pid]) > 0.5)


def kill_spinning(ctx):
    for pid in spinning(ctx):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def unresponsive_pages(ctx, skip=()):
    """URLs of the test browser's tabs whose renderer does not answer a trivial evaluation within 2 s."""
    stuck = []
    for target in devtools_get(ctx, "/json/list"):
        if target.get("type") != "page" or any(s in target.get("url", "") for s in skip):
            continue
        try:
            page = DevTools(target["webSocketDebuggerUrl"])
            with page:
                page.sock.settimeout(2)
                page.call("Runtime.evaluate", {"expression": "1"})
        except Exception:
            stuck.append(target.get("url") or "(no url)")
    return stuck


def timed(ctx, step, tool, **args):
    """Calls with a 20 s call deadline; returns (data or None, seconds) and records the call."""
    began = time.monotonic()
    data, error = None, None
    try:
        data = ctx.call(tool, timeout_ms=CALL_TIMEOUT_MS, **args)
    except ToolError as e:
        error = str(e)[:300]
    seconds = round(time.monotonic() - began, 2)
    RESULTS.setdefault(ctx.name, []).append({"step": step, "tool": tool, "seconds": seconds, "ok": error is None,
                                              **({"error": error} if error else {})})
    return data, seconds


def setup(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    devtools_port(ctx)
    opened = []

    def cleanup():
        for tab in opened:
            try:
                ctx.mcp.call("browser_close", tab_id=tab, timeout_ms=5000)
            except ToolError:
                pass
        kill_spinning(ctx)

    ctx.cleanups.append(cleanup)
    return opened


def open_tab(ctx, opened, step, url):
    data, seconds = timed(ctx, step, "browser_open", url=url)
    tab = (data or {}).get("tab", {}).get("id")
    if tab is not None:
        opened.append(tab)
    return data, seconds, tab


def server(ctx):
    s = Server()
    ctx.cleanups.append(s.close)
    return s


def check_open(ctx, label, data, seconds, text, bound=OPEN_BOUND_S):
    outline = ((data or {}).get("observation") or {}).get("outline", "")
    ctx.check(f"{label} returns the page within {bound} s (took {seconds} s)",
              data is not None and seconds < bound and text in outline,
              RESULTS[ctx.name][-1].get("error") or outline[-300:])


def check_browser_free(ctx, opened, skip=()):
    data, seconds = timed(ctx, "list tabs", "browser_tabs")
    listed = sorted(t["id"] for t in (data or {}).get("tabs", []))
    ctx.check(f"browser_tabs answers within {TABS_BOUND_S} s (took {seconds} s) and lists only the agent's open tabs",
              data is not None and seconds < TABS_BOUND_S and listed == sorted(opened),
              {"listed": listed, "opened": opened, "error": RESULTS[ctx.name][-1].get("error")})
    stuck = unresponsive_pages(ctx, skip)
    ctx.check("every tab in the browser answers (none left stranded)", not stuck, stuck)


def close_tab(ctx, opened, tab):
    timed(ctx, "close", "browser_close", tab_id=tab)
    if tab in opened:
        opened.remove(tab)


@scenario
def open_after_longpoll_close(ctx):
    opened = setup(ctx)
    s = server(ctx)
    _, _, poll = open_tab(ctx, opened, "open long poll", s.url("longpoll.html"))
    ctx.require("the long-poll page opened", poll is not None)
    close_tab(ctx, opened, poll)
    hung = spinning(ctx)
    ctx.check("closing the long-poll tab left its renderer spinning (the trigger)", hung, hung)
    data, seconds, _ = open_tab(ctx, opened, "open form after close", s.url("form.html"))
    check_open(ctx, "browser_open of a same-site page after the close", data, seconds, "Hello, stranger")
    check_browser_free(ctx, opened)
    data, seconds, _ = open_tab(ctx, opened, "second open", s.url("next.html"))
    check_open(ctx, "the next browser_open", data, seconds, "next", bound=5)


@scenario
def open_after_longpoll_navigations(ctx):
    opened = setup(ctx)
    s = server(ctx)
    _, _, tab = open_tab(ctx, opened, "open long poll", s.url("longpoll.html"))
    ctx.require("the long-poll page opened", tab is not None)
    for step, url in (("navigate to form", s.url("form.html")), ("navigate back to long poll", s.url("longpoll.html")),
                      ("navigate to about:blank", "about:blank")):
        data, _ = timed(ctx, step, "browser_navigate", tab_id=tab, url=url)
        ctx.check(f"{step} answers", data is not None, RESULTS[ctx.name][-1].get("error"))
    close_tab(ctx, opened, tab)
    data, seconds, _ = open_tab(ctx, opened, "open form after close", s.url("form.html"))
    check_open(ctx, "browser_open after the reported sequence", data, seconds, "Hello, stranger")
    check_browser_free(ctx, opened)


@scenario
def navigate_after_longpoll_close(ctx):
    opened = setup(ctx)
    s = server(ctx)
    _, _, tab = open_tab(ctx, opened, "open another site", s.url("next.html", host="localhost"))
    ctx.require("a tab on another site opened", tab is not None)
    _, _, poll = open_tab(ctx, opened, "open long poll", s.url("longpoll.html"))
    ctx.require("the long-poll page opened", poll is not None)
    close_tab(ctx, opened, poll)
    hung = spinning(ctx)
    ctx.check("closing the long-poll tab left its renderer spinning (the trigger)", hung, hung)
    data, seconds = timed(ctx, "navigate into the site", "browser_navigate", tab_id=tab, url=s.url("form.html"))
    error = RESULTS[ctx.name][-1].get("error") or ""
    ctx.check(f"browser_navigate into the long poll's site says within {OPEN_BOUND_S} s that the tab is not responding "
              f"and names browser_open (took {seconds} s)",
              seconds < OPEN_BOUND_S and (data is not None or ("not responding" in error and "browser_open" in error)),
              error or "navigated")
    _, seconds = timed(ctx, "read the stranded tab", "browser_snapshot", tab_id=tab)
    ctx.check(f"a later call on that tab answers within 7 s (took {seconds} s)", seconds < 7,
              RESULTS[ctx.name][-1].get("error") or "answered")
    _, seconds = timed(ctx, "close the stranded tab", "browser_close", tab_id=tab)
    ctx.check(f"the tab still closes within {TABS_BOUND_S} s (took {seconds} s)",
              RESULTS[ctx.name][-1]["ok"] and seconds < TABS_BOUND_S, RESULTS[ctx.name][-1].get("error"))
    opened.remove(tab)
    data, seconds, _ = open_tab(ctx, opened, "open the page instead", s.url("form.html"))
    check_open(ctx, "browser_open of that page", data, seconds, "Hello, stranger")
    check_browser_free(ctx, opened)


def server_stops(ctx, stop):
    opened = setup(ctx)
    gone, live = server(ctx), server(ctx)
    _, _, poll = open_tab(ctx, opened, "open long poll", gone.url("longpoll.html?hold_ms=2000"))
    ctx.require("the long-poll page opened", poll is not None)
    stop(gone)
    time.sleep(3)
    data, seconds, _ = open_tab(ctx, opened, "open while the server is gone", live.url("form.html"))
    check_open(ctx, "browser_open of another server while the page's server is gone", data, seconds, "Hello, stranger")
    check_browser_free(ctx, opened)
    close_tab(ctx, opened, poll)
    RESULTS[ctx.name].append({"step": "renderers spinning after the close", "pids": spinning(ctx)})
    data, seconds, _ = open_tab(ctx, opened, "open after closing the page", live.url("next.html"))
    check_open(ctx, "browser_open after closing that page", data, seconds, "next")
    check_browser_free(ctx, opened)


@scenario
def open_after_server_stops_answering(ctx):
    server_stops(ctx, Server.stop_answering)


@scenario
def open_after_server_refuses(ctx):
    server_stops(ctx, Server.refuse)


@scenario
def slow_server_open_is_not_replaced(ctx):
    opened = setup(ctx)
    s = server(ctx)
    path = "form.html?delay_ms=8000"
    data, seconds, _ = open_tab(ctx, opened, "open a page served 8 s late", s.url(path))
    check_open(ctx, "browser_open of a page served 8 s late", data, seconds, "Hello, stranger", bound=8 + 4)
    requests = [r for r in s.requests if r == "/" + path]
    ctx.check("the late page was requested once (its tab was not replaced)", len(requests) == 1, s.requests)
    check_browser_free(ctx, opened)


@scenario
def hung_page_open_releases_browser(ctx):
    opened = setup(ctx)
    s = server(ctx)
    data, seconds, _ = open_tab(ctx, opened, "open a page that hangs its renderer", s.url("hung.html"))
    ctx.check(f"browser_open of a page that stops answering returns by its deadline (took {seconds} s)",
              seconds < CALL_TIMEOUT_MS / 1000 + 3, RESULTS[ctx.name][-1].get("error") or "returned")
    if data is None:
        listed, _ = timed(ctx, "list tabs for the hung page", "browser_tabs")
        opened += [t["id"] for t in (listed or {}).get("tabs", []) if "hung.html" in t.get("url", "")]
    data, seconds, _ = open_tab(ctx, opened, "open form after the hung page", s.url("form.html"))
    check_open(ctx, "the next browser_open", data, seconds, "Hello, stranger")
    check_browser_free(ctx, opened, skip=("hung.html",))


@scenario
def dialog_answered_after_twelve_seconds(ctx):
    opened = setup(ctx)
    s = server(ctx)
    _, _, tab = open_tab(ctx, opened, "open the dialog page", s.url("popups.html"))
    ctx.require("the page opened", tab is not None)
    outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
    ask = re.search(r'\[(e\d+)\] button "Ask"', outline)
    ctx.require("the page has its Ask button", ask, outline[-500:])
    clicked, seconds = timed(ctx, "click Ask", "browser_click", tab_id=tab, ref=ask[1])
    ctx.check(f"the click returns with the dialog open (took {seconds} s)",
              clicked is not None and "dialog" in json.dumps(clicked), clicked)
    time.sleep(12)
    answered, seconds = timed(ctx, "answer after 12 s", "browser_command", tab_id=tab, operation="dialog", accept=True)
    ctx.check(f"the dialog is still answerable after 12 s (took {seconds} s)", answered is not None,
              RESULTS[ctx.name][-1].get("error"))
    read, _ = timed(ctx, "read the answer", "browser_snapshot", tab_id=tab)
    ctx.check("the page shows the answer", "confirmed" in (read or {}).get("outline", ""),
              RESULTS[ctx.name][-1].get("error") or (read or {}).get("outline", "")[-300:])
    check_browser_free(ctx, opened)


if __name__ == "__main__":
    run = Run(browser=True)
    try:
        ok = run.run(sys.argv[1:])
    finally:
        (run.dir / "longpoll-close.json").write_text(json.dumps({"bounds_s": {"open": OPEN_BOUND_S, "tabs": TABS_BOUND_S},
                                                                 "calls": RESULTS}, indent=2) + "\n")
    sys.exit(0 if ok else 1)
