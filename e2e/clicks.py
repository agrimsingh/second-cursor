#!/usr/bin/env python3
"""Pointer delivery: every pixel click, double-click and drag must act exactly once, whatever the view under it.

Pixel input reaches an app through two channels (SkyLight and the public pid path). AppKit controls act once on the
pair, but a bare custom surface (a canvas, a DAW panel, a game view: nothing accessible under the point) acts on each
copy, so a toggle flips back. The fixture (sc-focus-probe --clicks) opens on the agent display, so this suite never
depends on the Space the user is viewing and never shows on their screen.

Ways this can fail, covered by click_delivery and scroll_delivery:
- A custom view that accepts the first click into an inactive window gets two mouse-downs per click.
- A plain custom view (the AppKit default, which does not accept the first click) gets two, or none.
- A standard button is pressed twice, or not at all.
- A text view's caret does not move to the click point (the channel it needs was dropped).
- A double-click reaches a custom view as more or fewer than two presses.
- A drag on a custom surface starts twice.
- A scroll step reaches a custom view or a standard scroll view as two wheel events (or none), so content moves
  twice as far as asked; or the standard scroll view does not move.

Usage:  e2e/clicks.py [scenario ...]   (same targeting flags as e2e/run.py, e.g. SC_E2E_APP=build/SecondCursor.app)
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl)
"""
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, SCENARIOS, agent_origin, element_index, scenario
from ocr import probe_binary, require_on_agent_display

# Importing ocr registers its scenarios; this suite runs only its own.
SCENARIOS.clear()


class ClickFixture:
    def __init__(self, ctx):
        self.state_path = ctx.scratch / "clicks-state.json"
        self.process = subprocess.Popen([str(probe_binary()), "--clicks", "--state", str(self.state_path),
                                         "--origin", agent_origin(ctx)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ctx.cleanups.append(self.stop)
        deadline = time.time() + 15
        while self.read() is None and time.time() < deadline:
            time.sleep(0.2)
        state = self.read()
        ctx.require("the click fixture started and wrote its state", state is not None)
        self.wid = state["window"]
        require_on_agent_display(ctx, self.wid, "the click fixture")

    def read(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def center(self, target):
        t = self.read()["targets"][target]
        return t["x"] + t["width"] / 2, t["y"] + t["height"] / 2

    def settled(self, key, before, expect):
        deadline = time.time() + 1.5
        while self.read()[key] - before < expect and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        return self.read()[key] - before

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()


@scenario
def click_delivery(ctx):
    fx = ClickFixture(ctx)
    for target in ("first_mouse", "plain", "button"):
        before = fx.read()[target]
        x, y = fx.center(target)
        ctx.call("click", window_id=fx.wid, x=x, y=y, observe="none")
        got = fx.settled(target, before, 1)
        ctx.check(f"{target}: one pixel click acted exactly once", got == 1, got)

    area = element_index(ctx.tree(fx.wid), "AXTextArea")
    ctx.require("the text view is indexed", area is not None)
    ctx.call("set_value", window_id=fx.wid, element_index=area, value="alpha beta gamma", observe="none")
    time.sleep(0.3)
    t = fx.read()["targets"]["text"]
    ctx.call("click", window_id=fx.wid, x=t["x"] + 70, y=t["y"] + 14, observe="none")
    time.sleep(0.5)
    caret = fx.read()["text_caret"]
    ctx.check("a pixel click moved the text view's caret into \"beta\" (offsets 6-10)", 6 <= caret <= 10, caret)

    before = fx.read()["plain"]
    x, y = fx.center("plain")
    ctx.call("click", window_id=fx.wid, x=x, y=y, count=2, observe="none")
    got = fx.settled("plain", before, 2)
    ctx.check("a double-click reached the custom view as exactly two presses", got == 2, got)

    before = fx.read()["first_mouse"]
    x, y = fx.center("first_mouse")
    ctx.call("drag", window_id=fx.wid, from_x=x - 40, from_y=y, to_x=x + 40, to_y=y, observe="none")
    got = fx.settled("first_mouse", before, 1)
    ctx.check("a drag on the custom view started exactly once", got == 1, got)


@scenario
def scroll_delivery(ctx):
    fx = ClickFixture(ctx)
    for target in ("plain", "list"):
        before = fx.read()[f"{target}_wheel"]
        x, y = fx.center(target)
        ctx.call("scroll", window_id=fx.wid, x=x, y=y, direction="down", amount=2, observe="none")
        got = fx.settled(f"{target}_wheel", before, 2)
        ctx.check(f"{target}: two scroll steps arrived as exactly two wheel events", got == 2, got)
    ctx.check("the standard scroll view moved its content", fx.read()["list_offset"] > 0, fx.read()["list_offset"])


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
