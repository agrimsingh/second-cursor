#!/usr/bin/env python3
"""Repeatable delayed-effect probes, with independent timing and preserved misses.

SC_E2E_APP=<bundle> SC_E2E_EXTENSION=<dir> python3 e2e/arc_settling.py --takeover --label baseline --repetitions 3

This is a deterministic driver comparison, not a model reasoning benchmark. The
DOM watcher only reads local fixture state; it never dispatches or repairs an
action. DOM-visible means laid out and CSS-visible, not compositor presentation.
Native effect timing is a draw receipt, while returned screenshots are separately
judged for the expected pixels. A missing late effect is retained as a measured
miss; --require-ready upgrades such misses to acceptance failures.
"""
from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse

from run import DevTools, Run, SCENARIOS, element_index, inside, page_socket, png_pixels
from best_in_class import digest, extension_identity

ROOT = pathlib.Path(__file__).resolve().parent
VERSION = "arc-settling/2"
BROWSER_CASES = {
    "immediate": {"expected_effect": True, "max_return_ms": 4000},
    "slow_fetch": {"expected_effect": True, "max_return_ms": 12000},
    "slow_fetch_consumed": {"expected_effect": True, "max_return_ms": 12000},
    "slow_fetch_loading": {"expected_effect": True, "max_return_ms": 12000},
    "streamed_response": {"expected_effect": True, "max_return_ms": 12000},
    "slow_fetch_longpoll": {"expected_effect": True, "max_return_ms": 12000, "longpoll": True},
    **{f"dialog_{ms}": {"expected_effect": True, "max_return_ms": 5000} for ms in (0, 300, 800, 1500)},
    "late_fetch_800": {"expected_effect": True, "max_return_ms": 6000},
    "badge_800_blink": {"expected_effect": True, "max_return_ms": 5000},
    "never_fetch": {"expected_effect": False, "max_return_ms": 13000},
    "never_fetch_screenshot": {"expected_effect": False, "max_return_ms": 13000, "observe": "screenshot"},
}
NATIVE_CASES = {
    **{f"canvas_{ms}": {"delay_ms": ms, "small": False, "blink": False} for ms in (0, 300, 800, 1500)},
    "small_badge_800": {"delay_ms": 800, "small": True, "blink": False},
    "small_badge_800_blink": {"delay_ms": 800, "small": True, "blink": True},
}
VISIBLE_EXPRESSION = """(() => {
  const visible = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none' &&
    Number(getComputedStyle(el).opacity) > 0;
  const result = [document.querySelector('#result'), document.querySelector('#dialog-result')]
    .find(el => visible(el) && el.textContent.startsWith('Effect ready: '));
  const badge = document.querySelector('#badge');
  const badgeReady = visible(badge) && (badge.getAttribute('aria-label') || '').startsWith('Effect ready: ');
  return {visible: !!result || badgeReady,
    text: result?.textContent || (badgeReady ? badge.getAttribute('aria-label') : ''),
    action_count: Number(window.__arcActionCount || 0),
    context: {visibility: document.visibilityState, width: innerWidth, height: innerHeight,
              device_pixel_ratio: devicePixelRatio}};
})()"""


class ArcFixtureServer:
    """Owned HTTP fixture. In-memory request history is read only to the judge."""
    def __init__(self):
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                with owner.lock:
                    owner.requests.append({"path": parsed.path, "query": query, "at": time.monotonic()})
                if parsed.path == "/arc-settling.html":
                    body = (ROOT / "fixtures/arc-settling.html").read_bytes()
                    mime = "text/html; charset=utf-8"
                elif parsed.path == "/delay":
                    milliseconds = min(5000, max(0, int(query.get("ms", ["0"])[0])))
                    owner.stopped.wait(milliseconds / 1000)
                    body, mime = b'{"ok":true}', "application/json"
                elif parsed.path == "/longpoll":
                    owner.stopped.wait(30)
                    body, mime = b'{"ok":true}', "application/json"
                elif parsed.path == "/stream":
                    milliseconds = min(5000, max(0, int(query.get("ms", ["4000"])[0])))
                    body = b'{"status":"ready"}'
                    first = body[:10]
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Encoding", "identity")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(first)
                        self.wfile.flush()
                        with owner.lock:
                            owner.requests.append({"path": "/stream", "event": "partial_body_sent",
                                                   "bytes": len(first), "content_length": len(body),
                                                   "at": time.monotonic()})
                        if owner.stopped.wait(milliseconds / 1000):
                            return
                        write_started_at = time.monotonic()
                        self.wfile.write(body[len(first):])
                        self.wfile.flush()
                        with owner.lock:
                            owner.requests.append({"path": "/stream", "event": "complete_body_sent",
                                                   "bytes": len(body), "content_length": len(body),
                                                   "write_started_at": write_started_at, "at": time.monotonic()})
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                else:
                    self.send_error(404)
                    return
                if owner.stopped.is_set():
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def stop(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def wait_longpoll(self, case, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                found = next((r for r in self.requests if r["path"] == "/longpoll"
                              and r["query"].get("case") == [case]), None)
            if found:
                return found
            time.sleep(0.01)
        return None


class PollWatcher:
    """First positive sample is an upper bound; last negative sample is a lower bound."""
    def __init__(self, interval=0.025):
        self.interval = interval
        self.samples = []
        self.error = None
        self.first_visible_at = None
        self.last_absent_at = None
        self.initialized = threading.Event()
        self.finished = threading.Event()
        self.quit = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        raise NotImplementedError("Use a concrete read-only observer")

    def start(self):
        self.thread.start()
        if not self.initialized.wait(5):
            raise RuntimeError("Independent effect watcher did not initialize")
        if self.error:
            raise RuntimeError(self.error)
        return self

    def accept(self, sample, started, ended):
        self.samples.append({"sample_started_at": started, "sample_finished_at": ended, **sample})
        if sample.get("visible") and self.first_visible_at is None:
            self.first_visible_at = ended
            self.finished.set()
        elif not sample.get("visible") and self.first_visible_at is None:
            self.last_absent_at = started
        self.initialized.set()

    def wait(self, timeout=6):
        return self.finished.wait(timeout)

    def stop(self):
        self.quit.set()
        self.thread.join(5)
        if self.thread.is_alive():
            self.error = self.error or "Independent effect watcher did not stop"


class VisibleWatcher(PollWatcher):
    def __init__(self, ws_url, interval=0.025):
        super().__init__(interval)
        self.ws_url = ws_url

    def _run(self):
        try:
            with DevTools(self.ws_url) as page:
                while not self.quit.is_set():
                    started = time.monotonic()
                    response = page.call("Runtime.evaluate", {"expression": VISIBLE_EXPRESSION,
                                                              "returnByValue": True})
                    if "exceptionDetails" in response:
                        raise RuntimeError(str(response["exceptionDetails"]))
                    self.accept(response["result"]["value"], started, time.monotonic())
                    self.quit.wait(self.interval)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.initialized.set()
            self.finished.set()


class NativeReceiptWatcher(PollWatcher):
    def __init__(self, state_path):
        super().__init__()
        self.state_path = state_path

    def _run(self):
        try:
            while not self.quit.is_set():
                started = time.monotonic()
                state = json.loads(self.state_path.read_text())
                self.accept({"visible": state.get("draw_at_uptime") is not None,
                             "action_count": state["actions"]}, started, time.monotonic())
                self.quit.wait(self.interval)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.initialized.set()
            self.finished.set()


def timing_summary(started, returned, watcher):
    lower, upper = watcher.last_absent_at, watcher.first_visible_at
    # A sample interval straddling the return cannot establish which happened first.
    early = True if lower is not None and lower > returned else (
        False if upper is not None and upper <= returned else None)
    return {"return_ms": round((returned - started) * 1000, 2),
            "effect_lower_ms": round(max(0, lower - started) * 1000, 2) if lower is not None else None,
            "effect_upper_ms": round((upper - started) * 1000, 2) if upper is not None else None,
            "returned_early": early, "observer_error": watcher.error,
            "observer_interval_ms": watcher.interval * 1000}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def button_ref(outline, label):
    match = re.search(r'\[(e\d+)\] button "' + re.escape(label) + '"', outline)
    if not match:
        raise AssertionError(f"Missing button {label!r}: {outline[:1000]}")
    return match[1]


def record(ctx, row, args):
    args.rows.append(row)
    write_json(ctx.run.dir / "arc-settling.json", {"version": VERSION, "label": args.label,
               "kind": "deterministic public-tool settling probes", "rows": args.rows,
               "summary": summarize(args.rows), "code_manifest_before": args.code_manifest})
    write_json(ctx.run.dir / f"{ctx.name}-timing.json", row)
    print(f"      {row['case']}: return {row['return_ms']:.0f} ms; "
          f"ready in result={row['ready_in_result']}; early={row['returned_early']}")


def summarize(rows):
    groups = {}
    for case in sorted({row["case"] for row in rows}):
        selected = [row for row in rows if row["case"] == case]
        groups[case] = {"trials": len(selected),
                        "median_return_ms": statistics.median(row["return_ms"] for row in selected),
                        "ready_in_result": sum(row["ready_in_result"] for row in selected),
                        "early_returns": sum(row["returned_early"] is True for row in selected),
                        "timing_boundary_inconclusive": sum(row["returned_early"] is None for row in selected),
                        "observer_errors": sum(bool(row["observer_error"]) for row in selected)}
    return groups


def code_manifest(bundle):
    files = ("arc_settling.py", "fixtures/arc-settling.html", "fixtures/arc-settling.swift",
             "run.py", "observe.swift", "start-browser.py")
    return {"app_bundle": str(bundle), "app_binary_sha256": digest(bundle / "Contents/MacOS/SecondCursor"),
            "extension": extension_identity(), "fixture_and_harness_sha256": {name: digest(ROOT / name) for name in files},
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT.parent,
                                       capture_output=True, text=True, check=True, timeout=10).stdout.strip()}


def browser_trial(ctx, case, args):
    spec = BROWSER_CASES[case]
    ctx.require("the isolated extension is connected", ctx.call("browser_status").get("connected"))
    with ArcFixtureServer() as server:
        url = f"{server.base_url}/arc-settling.html?case={case}"
        tab = ctx.call("browser_open", url=url)["tab"]["id"]
        try:
            initial = ctx.call("browser_snapshot", tab_id=tab, full=True)
            if spec.get("longpoll"):
                ctx.call("browser_click", tab_id=tab,
                         ref=button_ref(initial["outline"], "Start background polling"), observe="none")
                ctx.require("the unrelated longpoll started before the measured action", server.wait_longpoll(case))
                initial = ctx.call("browser_snapshot", tab_id=tab, full=True)
            watcher = VisibleWatcher(page_socket(ctx.run.instance.browser, url)).start()
            ctx.require("the independent observer starts before the effect", watcher.first_visible_at is None,
                        watcher.samples)
            try:
                started = time.monotonic()
                before_images = len(ctx.screenshots)
                result = ctx.call("browser_click", tab_id=tab, ref=button_ref(initial["outline"], "Run action"),
                                  **({"observe": spec["observe"]} if "observe" in spec else {}))
                returned = time.monotonic()
                if spec["expected_effect"]:
                    watcher.wait(6)
                else:
                    time.sleep(0.1)
            finally:
                watcher.stop()
            ready = "Effect ready: " + case in result.get("observation", {}).get("outline", "")
            row = {"surface": "browser", "case": case, "expected_effect": spec["expected_effect"],
                   **timing_summary(started, returned, watcher), "ready_in_result": ready,
                   "effect_observation": "read-only DOM layout and CSS visibility samples",
                   "settling": result.get("observation", {}).get("settling") or result.get("settling"),
                   "samples": watcher.samples, "requests": server.requests}
            if spec.get("observe") == "screenshot":
                row["observe"] = "screenshot"
                row["screenshot_returned"] = len(ctx.screenshots) > before_images
            record(ctx, row, args)
            ctx.check("the independent observer had no errors", watcher.error is None, watcher.error)
            ctx.check("exactly one UI action reached the fixture", watcher.samples[-1]["action_count"] == 1,
                      watcher.samples[-1])
            ctx.check("the call returned within the fixture deadline", row["return_ms"] <= spec["max_return_ms"], row)
            ctx.check("the independent effect matches the scenario contract",
                      (watcher.first_visible_at is not None) == spec["expected_effect"], row)
            if case == "streamed_response":
                partial = next((r for r in server.requests if r.get("event") == "partial_body_sent"), None)
                complete = next((r for r in server.requests if r.get("event") == "complete_body_sent"), None)
                ctx.check("the server delivered only a partial body before the four-second delay",
                          bool(partial and complete and partial["bytes"] < partial["content_length"]
                               and complete["at"] - partial["at"] >= 3.9), {"partial": partial, "complete": complete})
                ctx.check("the independent effect followed delivery of the complete response body",
                          bool(complete and watcher.first_visible_at is not None
                               and watcher.first_visible_at >= complete["write_started_at"]), row)
            if args.require_ready and spec["expected_effect"]:
                ctx.check("the returned observation already includes the effect", ready, row)
            if not spec["expected_effect"]:
                ctx.check("an unfinished request never claims the effect is ready", not ready, row)
            if spec.get("observe") == "screenshot":
                ctx.check("the screenshot-only action returned its requested image", row["screenshot_returned"], result)
            if args.require_settling:
                settling = row["settling"] or {}
                ctx.check("the public MCP result exposes settling metadata", bool(settling), result)
                if not spec["expected_effect"]:
                    ctx.check("the public MCP result explicitly reports the pending-request deadline",
                              settling.get("state") == "timed_out" and settling.get("pending_requests", 0) > 0,
                              settling)
        finally:
            ctx.mcp.call("browser_close", tab_id=tab)


def compile_native():
    source = ROOT / "fixtures/arc-settling.swift"
    binary = ROOT / ".bin/arc-settling-fixture"
    binary.parent.mkdir(exist_ok=True)
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        built = subprocess.run(["xcrun", "swiftc", "-O", str(source), "-o", str(binary)],
                               capture_output=True, text=True, timeout=120)
        if built.returncode:
            raise RuntimeError(built.stderr)
        subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True, capture_output=True)
    return binary


def magenta_pixels(path):
    return sum(r > 180 and b > 180 and g < 90 for r, g, b in png_pixels(path.read_bytes()))


def native_trial(ctx, case, args):
    spec = NATIVE_CASES[case]
    display = ctx.call("agent_display", action="create")
    bounds = display["bounds"]
    state_path = ctx.scratch / "native-receipt.json"
    command = [str(compile_native()), "--state", str(state_path), "--delay-ms", str(spec["delay_ms"]),
               "--display-id", str(display["display_id"])]
    command += [flag for key, flag in (("small", "--small"), ("blink", "--blink")) if spec[key]]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctx.cleanups.append(lambda: process.terminate() if process.poll() is None else None)
    deadline = time.monotonic() + 18
    while not state_path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.025)
    ctx.require("the native drawing fixture started", state_path.exists())
    state = json.loads(state_path.read_text())
    ctx.require("WindowServer confirmed the fixture's placement before measuring",
                state.get("placement_ready") and not state.get("setup_error"), state)
    wid = state["window"]
    initial = ctx.call("get_window_state", window_id=wid, full=True, include_screenshot=True)
    ctx.require("the fixture is on the agent display", inside(initial["window"]["frame"], bounds), initial["window"])
    target = element_index(initial["tree"], "AXButton", "Run action")
    ctx.require("the native trigger is grounded in accessibility", target is not None, initial["tree"])
    ctx.require("the baseline screenshot has no completion pixels",
                bool(ctx.screenshots) and magenta_pixels(ctx.run.dir / ctx.screenshots[-1]) == 0)
    tools = ctx.mcp.rpc("tools/list")["tools"]
    schema = next(t for t in tools if t["name"] == "get_window_state")["inputSchema"]["properties"]
    visual = args.visual_settle != "off" and "visual_settle" in schema
    if args.visual_settle == "on":
        ctx.require("the candidate advertises visual_settle", visual)
    watcher = NativeReceiptWatcher(state_path).start()
    try:
        started = time.monotonic()
        ctx.call("perform_action", window_id=wid, element_index=target, action="AXPress", observe="none")
        action_returned = time.monotonic()
        before_images = len(ctx.screenshots)
        result = ctx.call("get_window_state", window_id=wid, include_screenshot=True, full=True,
                          **({"visual_settle": True} if visual else {}))
        returned = time.monotonic()
        ctx.require("the measured read returned a screenshot", len(ctx.screenshots) > before_images, result)
        pixels = magenta_pixels(ctx.run.dir / ctx.screenshots[-1])
        watcher.wait(5)
    finally:
        watcher.stop()
    row = {"surface": "native", "case": case, **spec, **timing_summary(started, returned, watcher),
           "action_return_ms": round((action_returned - started) * 1000, 2),
           "ready_in_result": pixels >= 4, "completion_pixels": pixels,
           "visual_settle_requested": visual, "settling": result.get("visual_settle"),
           "effect_observation": "fixture draw receipt; returned screenshot separately judged for magenta pixels",
           "samples": watcher.samples, "native_receipt": json.loads(state_path.read_text())}
    record(ctx, row, args)
    ctx.check("the independent receipt watcher had no errors", watcher.error is None, watcher.error)
    ctx.check("exactly one native action was dispatched", row["native_receipt"]["actions"] == 1, row)
    ctx.check("the canvas eventually drew its new state", watcher.first_visible_at is not None, row)
    ctx.check("the action and read stayed bounded", row["return_ms"] < 6500, row)
    ctx.check("the custom drawing did not become AX text", "Effect ready" not in result["tree"], result["tree"])
    if args.require_ready:
        ctx.check("the returned screenshot already contains the meaningful visual change", row["ready_in_result"], row)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--takeover", action="store_true")
    result.add_argument("--label", default="candidate")
    result.add_argument("--repetitions", type=int, default=1)
    group = result.add_mutually_exclusive_group()
    group.add_argument("--browser-only", action="store_true")
    group.add_argument("--native-only", action="store_true")
    result.add_argument("--cases", nargs="*", help="Exact case names, not generated scenario names")
    result.add_argument("--visual-settle", choices=("auto", "on", "off"), default="auto")
    result.add_argument("--require-ready", action="store_true", help="Fail on early/incomplete returned observations")
    result.add_argument("--require-settling", action="store_true", help="Require candidate browser settling metadata")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.takeover:
        raise SystemExit("Pass --takeover only during an authorized quiet desktop window")
    if not 1 <= args.repetitions <= 20:
        raise SystemExit("--repetitions must be between 1 and 20")
    available = {**({} if args.native_only else BROWSER_CASES), **({} if args.browser_only else NATIVE_CASES)}
    selected = args.cases or list(available)
    if set(selected) - set(available):
        raise SystemExit("Unknown case(s): " + ", ".join(sorted(set(selected) - set(available))))
    args.rows = []
    SCENARIOS.clear()
    for repetition in range(args.repetitions):
        for case in selected:
            trial = browser_trial if case in BROWSER_CASES else native_trial
            def scenario(ctx, case=case, trial=trial):
                trial(ctx, case, args)
            scenario.__name__ = f"arc_settling_{case}_{repetition + 1}"
            SCENARIOS.append(scenario)
    run = Run(browser=any(case in BROWSER_CASES for case in selected), installed=False, headed_browser=True)
    bundle = run.instance.app
    args.code_manifest = code_manifest(bundle)
    run.notes += ["Arc-inspired deterministic settling probes; no model decisions.",
                  "Misses are measurements unless --require-ready is passed. Invariants are always required.",
                  "Effect timing is interval-bounded; DOM visibility is not physical compositor presentation."]
    ok = run.run([])
    artifact = run.dir / "arc-settling.json"
    if artifact.exists():
        payload = json.loads(artifact.read_text())
        payload["results"] = run.results
        payload["browser_runtime"] = run.browser_runtime
        payload["target"] = run.target
        payload["code_manifest_after"] = code_manifest(bundle)
        payload["code_changed_during_run"] = payload["code_manifest_before"] != payload["code_manifest_after"]
        ok = ok and not payload["code_changed_during_run"]
        write_json(artifact, payload)
    return 0 if ok and run.results and all(row["status"] == "PASS" for row in run.results) else 1


if __name__ == "__main__":
    sys.exit(main())
