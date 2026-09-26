#!/usr/bin/env python3
"""Focus-safety suite: background input must never take focus from the user's frontmost app.

Ways this can fail, each caught below:
- The frontmost app resigns active while a pixel click, drag or cmd shortcut runs (typing, IME and the
  caret are interrupted), even if it is re-activated a moment later.
- The frontmost app's key window resigns key, or its first responder changes.
- Restoring leaves the target app believing it is active, so the user's next keystrokes go to it
  (the probe must still be active + key and still the frontmost process after every action).
- The target never receives the click, drag or scroll once the frontmost app is left alone.
- A fully covered Chrome window drops the pixel click or receives it twice.
- cmd shortcuts (menu key equivalents such as cmd+a) stop working without the frontmost handoff.
- Two agents' handoffs overlap and one agent's click lands with the wrong focus state or is lost.
- The real cursor moves or the active Space changes (checked by the harness invariants per scenario).
- xdotool / Codex key names (Return, BackSpace, Page_Down, Prior, super+a, ...) are rejected or
  map to the wrong key.

The frontmost app is `sc-focus-probe`, built from this package, wrapped in a throwaway .app so
LaunchServices activates it, sized to cover the main screen, and quit at scenario end.

Usage:  e2e/focus.py [scenario ...] [--installed]   (same targeting flags as e2e/run.py)
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, <scenario>-probe.json)
"""
import http.server
import json
import os
import pathlib
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, SCENARIOS, Skip, scenario, element_index, element_frame, observe

SCENARIOS.clear()

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
PROBE_HOME = pathlib.Path("/tmp/second-cursor-e2e/focus-probe")
PROBE_BUNDLE_ID = "dev.secondcursor.focusprobe"
COUNTERS = ("resign_active", "resign_key", "first_responder_changes")
_probe_app = None


def build_probe_app():
    """Unbundled executables may be refused activation, so the probe runs from a minimal .app."""
    global _probe_app
    if _probe_app:
        return _probe_app
    built = subprocess.run(["swift", "build", "--product", "sc-focus-probe"], cwd=REPO,
                           capture_output=True, text=True, timeout=900)
    if built.returncode != 0:
        raise AssertionError("swift build --product sc-focus-probe failed:\n" + (built.stderr or built.stdout)[-1500:])
    bin_dir = subprocess.run(["swift", "build", "--show-bin-path"], cwd=REPO,
                             capture_output=True, text=True, check=True, timeout=120).stdout.strip()
    app = PROBE_HOME / "SCFocusProbe.app"
    shutil.rmtree(app, ignore_errors=True)
    (app / "Contents/MacOS").mkdir(parents=True)
    shutil.copy2(pathlib.Path(bin_dir) / "sc-focus-probe", app / "Contents/MacOS/sc-focus-probe")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": PROBE_BUNDLE_ID,
        "CFBundleName": "SCFocusProbe",
        "CFBundleExecutable": "sc-focus-probe",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    }))
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, timeout=60)
    _probe_app = app
    return app


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_process(pid, grace=5):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    if alive(pid):
        os.kill(pid, signal.SIGKILL)


class FocusProbe:
    """Stands in for the user: the frontmost app, its text field focused, counting every focus loss.

    Create it first in a scenario so its cleanup (quit + re-activate the previous app) runs last."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.previous = observe()["frontmost"]
        self.state_path = ctx.scratch / "probe-state.json"
        self.pid = None
        self.baseline = None
        ctx.cleanups.append(self.stop)

    def read(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def start(self, avoid=None):
        """Covers the main screen, or with `avoid` (a global frame dict), sits small in a screen corner clear of it."""
        app = build_probe_app()
        self.state_path.unlink(missing_ok=True)
        extra = ["--avoid", f"{avoid['x']},{avoid['y']},{avoid['width']},{avoid['height']}"] if avoid else []
        subprocess.run(["open", "-n", str(app), "--args", "--state", str(self.state_path), *extra],
                       check=True, capture_output=True, timeout=30)
        deadline, state = time.time() + 15, None
        while time.time() < deadline:
            state = self.read()
            if state:
                self.pid = state["pid"]
                if state["active"] and state["key"]:
                    break
            time.sleep(0.2)
        self.ctx.require("the probe app became active with a key window", state and state["active"] and state["key"], state)
        time.sleep(0.5)
        self.baseline = self.read()
        front = observe()["frontmost"]
        self.ctx.require("the probe is the frontmost app", front["pid"] == self.pid, front)
        return self.baseline

    def window(self):
        for w in self.ctx.call("list_windows", app="focusprobe")["windows"]:
            if w["pid"] == self.pid:
                return w
        raise AssertionError("probe window not listed")

    def check(self, label):
        """Counters are cumulative, so a resign that was undone a moment later still shows up."""
        time.sleep(0.4)
        state, front = self.read(), observe()["frontmost"]
        problems = [f"{k} +{state[k] - self.baseline[k]}" for k in COUNTERS if state[k] != self.baseline[k]]
        if not state["active"]:
            problems.append("probe no longer active")
        if not state["key"]:
            problems.append("probe window no longer key")
        if front["pid"] != self.pid:
            problems.append(f"frontmost is now {front['name']}")
        self.ctx.check(f"{label}: frontmost app kept active state, key window and first responder",
                       not problems, "; ".join(problems) or state)
        self.baseline = state

    def stop(self):
        if self.pid:
            final = self.read()
            if final:
                (self.ctx.run.dir / f"{self.ctx.name}-probe.json").write_text(json.dumps(final, indent=2))
            stop_process(self.pid)
            self.pid = None
            time.sleep(0.5)
        # The harness requires the frontmost app to be unchanged by the scenario.
        if self.previous.get("bundle_id") and observe()["frontmost"]["pid"] != self.previous["pid"]:
            subprocess.run(["open", "-b", self.previous["bundle_id"]], capture_output=True, timeout=15)
            deadline = time.time() + 5
            while observe()["frontmost"]["pid"] != self.previous["pid"] and time.time() < deadline:
                time.sleep(0.2)


def text_area(ctx, wid):
    tree = ctx.tree(wid, include_screenshot=False)
    area = element_index(tree, "AXTextArea")
    frame = element_frame(tree, area) if area is not None else None
    ctx.require("text area and its frame are reported", frame is not None, tree[:400])
    return area, frame


def text_value(ctx, wid):
    tree = ctx.tree(wid, include_screenshot=False)
    match = re.search(r'\] AXTextArea "[^"]*" value="((?:[^"\\]|\\.)*)"', tree)
    return json.loads(f'"{match.group(1)}"') if match else None


def text_top(ctx, wid):
    """The text view's window-local y; it moves up as the document scrolls down."""
    return text_area(ctx, wid)[1][1]


class FixtureServer:
    def __enter__(self):
        handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(FIXTURES))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *exc):
        self.httpd.shutdown()


def chrome_for_testing():
    home = pathlib.Path.home()
    found = sorted(home.glob(".agent-browser/browsers/*/Google Chrome for Testing.app"))
    found += sorted(home.glob("Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app"))
    return found[0] if found else None


def covers(outer, inner):
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
            and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
            and inner["y"] + inner["height"] <= outer["y"] + outer["height"])


@scenario
def focus_native_actions(ctx):
    probe = FocusProbe(ctx)
    wid = ctx.launch_scratch_textedit("focus.txt", "line one\nline two")["id"]
    probe.start()
    area, (x, y, w, h) = text_area(ctx, wid)

    # TextEdit opens with the caret at 0, so each typed marker proves where the preceding input landed.
    result = ctx.call("click", window_id=wid, element_index=area)
    probe.check("element click")
    ctx.check("element click on the text area used the pixel path", result.get("route") == "skylight_pointer", result)
    ctx.call("type_text", window_id=wid, text="E")
    value = text_value(ctx, wid)
    ctx.check("element click (text area center) put the caret at the end", value == "line one\nline twoE", value)

    ctx.call("click", window_id=wid, x=x + 3, y=y + 6)
    probe.check("pixel click")
    ctx.call("type_text", window_id=wid, text="Z")
    value = text_value(ctx, wid)
    ctx.check("pixel click moved the caret to the start of line one", value == "Zline one\nline twoE", value)

    ctx.call("press_key", window_id=wid, keys="cmd+a")
    probe.check("press_key cmd+a")
    ctx.call("type_text", window_id=wid, text="replaced")
    probe.check("type_text")
    value = text_value(ctx, wid)
    ctx.check("cmd+a selected all, typing replaced it", value == "replaced", value)

    ctx.call("set_value", window_id=wid, element_index=area, value="line one\nline two")
    ctx.call("drag", window_id=wid, from_x=x + 3, from_y=y + 6, to_x=min(x + w - 10, 400), to_y=y + 6)
    probe.check("drag")
    ctx.call("type_text", window_id=wid, text="LINE 1")
    value = text_value(ctx, wid)
    ctx.check("drag selected line one and typing replaced it", value == "LINE 1\nline two", value)

    area, _ = text_area(ctx, wid)
    ctx.call("set_value", window_id=wid, element_index=area, value="\n".join(f"row {i}" for i in range(1, 301)))
    before = text_top(ctx, wid)
    direction = "down" if before >= y - 5 else "up"
    ctx.call("scroll", window_id=wid, direction=direction, amount=10)
    probe.check("scroll")
    after = text_top(ctx, wid)
    moved = after < before - 20 if direction == "down" else after > before + 20
    ctx.check(f"scroll {direction} moved the covered document", moved, (before, after))


@scenario
def focus_concurrent_handoffs(ctx):
    probe = FocusProbe(ctx)
    windows = [ctx.launch_scratch_textedit(f"concurrent-{n}.txt", "line one\nline two") for n in (1, 2)]
    probe.start()
    targets = {}
    for window in windows:
        _, (x, y, w, h) = text_area(ctx, window["id"])
        targets[window["id"]] = ((x + 3, y + 6), (x + w / 2, min(y + h, window["frame"]["height"]) - 20))
    errors = []

    def agent(wid):
        start, end = targets[wid]
        try:
            for _ in range(3):
                ctx.mcp.call("click", window_id=wid, x=end[0], y=end[1])
                ctx.mcp.call("type_text", window_id=wid, text="Z")
                ctx.mcp.call("click", window_id=wid, x=start[0], y=start[1])
                ctx.mcp.call("type_text", window_id=wid, text="A")
        except Exception as e:
            errors.append(f"{wid}: {e}")

    threads = [threading.Thread(target=agent, args=(wid,)) for wid in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    ctx.check("both agents finished without errors", not errors, errors)
    probe.check("two agents clicking different apps at once")
    for wid in targets:
        value = text_value(ctx, wid)
        ctx.check(f"every click in window {wid} landed where aimed", value == "AAAline one\nline twoZZZ", value)


@scenario
def focus_chrome_occluded(ctx):
    app = chrome_for_testing()
    if not app:
        raise Skip("no Chrome for Testing under ~/.agent-browser/browsers or ~/Library/Caches/ms-playwright")
    probe = FocusProbe(ctx)
    profile = pathlib.Path(tempfile.mkdtemp(prefix="sc-focus-chrome-", dir="/tmp"))
    # Runs after Chrome is stopped; its helper processes need a moment to release the profile.
    ctx.cleanups.append(lambda: (time.sleep(1), shutil.rmtree(profile, ignore_errors=True)))
    token = f"focus-{uuid.uuid4().hex[:8]}"
    with FixtureServer() as base:
        # Window managers may resize the probe, so Chrome is placed inside wherever the probe really is.
        probe.start()
        cover = probe.window()["frame"]
        width, height = min(900, int(cover["width"]) - 80), min(640, int(cover["height"]) - 80)
        subprocess.run([
            "open", "-n", "-g", "-a", str(app), "--args",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--use-mock-keychain",
            f"--window-position={int(cover['x']) + 40},{int(cover['y']) + 40}", f"--window-size={width},{height}",
            f"{base}/focus/click.html?token={token}",
        ], check=True, capture_output=True, timeout=30)
        window = ctx.wait_for_window("Chrome for Testing", title=f"{token} clicks=0", timeout=30)
        wid, chrome_pid = window["id"], window["pid"]
        ctx.cleanups.append(lambda: stop_process(chrome_pid))
        time.sleep(1.5)
        state = probe.read()
        ctx.require("launching Chrome in the background left the probe active and key",
                    state and state["active"] and state["key"], state)
        probe.baseline = state
        cover = probe.window()
        window = ctx.wait_for_window("Chrome for Testing", pid=chrome_pid, title=token)

        frame, deadline = None, time.time() + 15
        while frame is None and time.time() < deadline:
            tree = ctx.tree(wid, include_screenshot=False)
            button = element_index(tree, "AXButton", "Click me")
            frame = element_frame(tree, button) if button is not None else None
            if frame is None:
                time.sleep(0.5)
        ctx.require("the fixture button is in Chrome's tree with a frame", frame is not None, tree[:600])

        bx, by, bw, bh = frame
        # Chrome's minimum window width can exceed a squeezed probe, so require only the click point to be covered.
        point = {"x": window["frame"]["x"] + bx + bw / 2, "y": window["frame"]["y"] + by + bh / 2, "width": 0, "height": 0}
        ctx.require("the probe window covers the click point", covers(cover["frame"], point), (cover["frame"], point))
        result = ctx.call("click", window_id=wid, x=bx + bw / 2, y=by + bh / 2)
        ctx.check("the click went through the SkyLight pointer path", result.get("route") == "skylight_pointer", result)
        probe.check("pixel click into covered Chrome")

        def title():
            return next((w["title"] for w in ctx.call("list_windows", app="Chrome for Testing")["windows"] if w["id"] == wid), "")

        deadline = time.time() + 5
        while f"{token} clicks=0" in title() and time.time() < deadline:
            time.sleep(0.25)
        # A second delivery (e.g. both posting channels firing) would arrive shortly after the first.
        time.sleep(1.5)
        final = title()
        ctx.check("covered Chrome received exactly one DOM click", final.startswith(f"{token} clicks=1"), final)
        probe.check("after Chrome handled the click")


@scenario
def press_key_aliases(ctx):
    wid = ctx.launch_scratch_textedit("aliases.txt", "old text")["id"]

    ctx.call("press_key", window_id=wid, keys="super+a")
    ctx.call("type_text", window_id=wid, text="xy")
    value = text_value(ctx, wid)
    ctx.check("super+a selected all (typing replaced it)", value == "xy", value)

    ctx.call("press_key", window_id=wid, keys="Return")
    value = text_value(ctx, wid)
    ctx.check("Return inserted a newline", value == "xy\n", value)

    ctx.call("press_key", window_id=wid, keys="BackSpace")
    value = text_value(ctx, wid)
    ctx.check("BackSpace deleted it (delete/backspace keep the macOS meaning)", value == "xy", value)

    area, _ = text_area(ctx, wid)
    ctx.call("set_value", window_id=wid, element_index=area, value="\n".join(f"row {i}" for i in range(1, 301)))
    ctx.call("press_key", window_id=wid, keys="Home")
    top = text_top(ctx, wid)
    ctx.call("press_key", window_id=wid, keys="Page_Down")
    paged = text_top(ctx, wid)
    ctx.check("Page_Down scrolled the document down", paged < top - 20, (top, paged))
    ctx.call("press_key", window_id=wid, keys="Prior")
    back = text_top(ctx, wid)
    ctx.check("Prior (xdotool Page_Up) scrolled back to the top", abs(back - top) <= 2, (top, back))
    ctx.call("press_key", window_id=wid, keys="Next")
    nexted = text_top(ctx, wid)
    ctx.check("Next (xdotool Page_Down) scrolled down", nexted < top - 20, (top, nexted))
    ctx.call("press_key", window_id=wid, keys="End")
    end = text_top(ctx, wid)
    ctx.check("End scrolled to the end of the document", end < nexted - 20, (nexted, end))


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
