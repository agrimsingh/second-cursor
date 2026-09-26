#!/usr/bin/env python3
"""Cursor-presence suite: while an agent task runs, its arrow and menu bar state say so; when it ends, they go.

Opt-in: the arrow is only drawn over windows on the Space the user is viewing, so every scenario makes the
`sc-focus-probe` app frontmost (covering the main screen, as in e2e/focus.py) with a scratch TextEdit behind it,
then reads screen pixels around the arrow. Do not run it while you are using the Mac.

Ways this can fail, each caught below:
- The arrow hides a moment after each action, so the user cannot tell whether a task is still running
  (checked 3 s and 10 s after a click, with no agent calls in between).
- A keyboard action (type_text, press_key, set_value) hides the arrow or moves it away from where the agent last
  pointed; or, as the first action of a task, never shows it at all.
- The arrow outlives the task: still drawn ~1 s after POST /turn-ended, after the user pauses, or forever when the
  host has no turn-end hook (the fallback is 60 s without agent calls).
- One session's turn end removes presence that another session still owns, or closing a session leaves it behind.
- The menu bar item does not follow the same lifecycle ("Agent active: <app>" while working, "Idle" after), or it
  and the `status` tool's `agent_activity` disagree about where the arrow is.
- A pointer action waits longer than the arrow's travel: arrival never fires, so every action sits out the 1.5 s
  timeout; a drag waits for its own animation; a click re-runs the appear animation while the arrow rests on screen;
  or a keyboard action waits at all.
- The arrow shows where the user cannot see its window: over the user's frontmost window covering the target, or
  on another Space; or it stays hidden once the window is uncovered; or a covered action still waits for the arrow;
  or covering the window also hides presence (status and the menu bar must still say the agent is active).
- The caption beside the arrow is missing, stale or wrong: an action's caption never shows or does not reach
  `status`; a read (which has no caption) clears it; an action without a caption leaves the previous one up; a
  batch step's caption is ignored; the caption outlives the turn.

Usage:  e2e/cursor.py [scenario ...]   (same targeting flags as e2e/run.py, e.g. SC_E2E_APP=build/SecondCursor.app)
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, screenshots/ with every pixel region read)
"""
import json
import pathlib
import statistics
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run
from run import ROOT, Run, SCENARIOS, Skip, accent_pixels, glow_pixels, png_pixels, scenario, step
from focus import FocusProbe, text_area

# Importing focus registers its scenarios; this suite runs only its own.
SCENARIOS.clear()

ARROW_MIN = 20
IDLE_TIMEOUT = 60


def turn_ended(ctx, session=None):
    """POST /turn-ended on the instance under test, exactly like `SecondCursor --turn-ended` does."""
    url = ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended"
    body = json.dumps({"session_id": session} if session else {}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {ctx.mcp.token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def activity(ctx):
    return ctx.call("status").get("agent_activity") or {}


def capture(ctx, stem, x, y, width, height):
    path = ctx.run.dir / "screenshots" / f"{ctx.name}-{stem}.png"
    done = subprocess.run(["screencapture", "-x", "-R", f"{int(x)},{int(y)},{int(width)},{int(height)}", str(path)],
                          capture_output=True, timeout=15)
    if done.returncode or not path.exists():
        return None
    ctx.screenshots.append(path.relative_to(ctx.run.dir).as_posix())
    return png_pixels(path.read_bytes())


def arrow_pixels(ctx, point, stem):
    """Glow pixels in a 48 pt square around a global point; the arrow's tip sits at the point it aims at."""
    pixels = capture(ctx, stem, point[0] - 10, point[1] - 10, 48, 48)
    ctx.require("screen region captured (Screen Recording permission for the runner)", pixels is not None, stem)
    return glow_pixels(pixels)


def pill_region(ctx, point, stem):
    """Where the caption pill sits: from 20 to 46 pt below the tip, starting 30 pt right of it (clear of the arrow's
    glow)."""
    return capture(ctx, stem, point[0] + 30, point[1] + 18, 220, 30)


def changed(before, after):
    return sum(1 for a, b in zip(before, after) if sum(abs(x - y) for x, y in zip(a, b)) > 60)


def inconclusive(ctx, label, detail):
    ctx.checks.append({"label": label, "ok": True, "inconclusive": True, "detail": detail})


def status_item(ctx):
    """The instance's own menu bar item, read through Accessibility, or None when that is not possible."""
    pid = ctx.run.instance.pid if ctx.run.instance else None
    if not pid:
        return None
    binary, source = ROOT / ".bin/status_item", ROOT / "status_item.swift"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        binary.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=300)
    state = json.loads(subprocess.run([str(binary), str(pid)], check=True, capture_output=True, text=True,
                                      timeout=15).stdout)
    if not state["trusted"]:
        return None
    return next((item for item in state["items"] if item["description"] == "Second Cursor"), None)


def check_menu(ctx, expected, when):
    """`expected` is "Agent active: <app>" or "Idle"; returns the item so callers can read its pixels."""
    label = f"menu bar item says “{expected}” {when}"
    item = status_item(ctx)
    if item is None:
        inconclusive(ctx, label, "status item not readable through Accessibility (--installed, or the runner "
                                 "lacks Accessibility); the status tool's agent_activity is checked instead")
        return None
    ctx.check(label, expected in item["help"], item["help"])
    return item


def check_menu_tint(ctx, active_pixels, idle_pixels):
    label = "menu bar icon is orange while the agent is active and not after"
    if active_pixels is None or idle_pixels is None or len(set(idle_pixels)) <= 1:
        inconclusive(ctx, label, "the status item is not visibly drawn (hidden by the notch or an auto-hiding menu bar)")
        return
    active, idle = accent_pixels(active_pixels), accent_pixels(idle_pixels)
    ctx.check(label, active >= 8 and idle < 3, f"active={active} idle={idle}")


def item_pixels(ctx, item, stem):
    if item is None:
        return None
    frame = item["frame"]
    if frame["width"] < 4 or frame["height"] < 4:
        return None
    return capture(ctx, stem, frame["x"], frame["y"], frame["width"], frame["height"])


def start(ctx, filename="cursor.txt", covered=False):
    """A scratch TextEdit on the current Space in the background: beside the frontmost probe, so the user can see
    it, or with `covered`, behind a probe that covers the main screen. Returns the probe, the window, its text area's
    index, and a point near the text area's top-left corner as (window-local, global)."""
    probe = FocusProbe(ctx)
    window = ctx.launch_scratch_textedit(filename, "line one\nline two")
    if not window["is_on_screen"]:
        raise Skip("scratch window opened on a Space the user is not viewing; the arrow only shows on the current Space")
    ctx.cleanups.append(lambda: turn_ended(ctx))
    probe.start(avoid=None if covered else window["frame"])
    area, frame = text_area(ctx, window["id"])
    local = (frame[0] + 3, frame[1] + 6)
    return probe, window, area, frame, local, to_global(window, local)


def to_global(window, local):
    return (window["frame"]["x"] + local[0], window["frame"]["y"] + local[1])


def near(arrow, point, tolerance=2):
    return bool(arrow) and abs(arrow["x"] - point[0]) <= tolerance and abs(arrow["y"] - point[1]) <= tolerance


def wait_until(start_time, seconds):
    time.sleep(max(0.0, start_time + seconds - time.time()))


def visible_or_skip(ctx, what="the click point"):
    """The probe stays clear of the scratch window, but one of the user's own windows may still cover it."""
    time.sleep(0.3)
    if activity(ctx).get("arrow_visible") is not True:
        raise Skip(f"{what} is covered by another app's window, so the arrow is rightly hidden there")


@scenario
def cursor_stays_for_the_task(ctx):
    _, window, _, _, local, point = start(ctx)
    wid = window["id"]
    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none")
    clicked = time.time()
    visible_or_skip(ctx)

    state = activity(ctx)
    ctx.check("status: agent active in TextEdit right after the click", state.get("active") is True
              and state.get("app") == "TextEdit", state)
    ctx.check("status: the arrow rests on the click point", near(state.get("arrow"), point), (state, point))
    item = check_menu(ctx, "Agent active: TextEdit", "while the agent works")
    active_icon = item_pixels(ctx, item, "menu-active")

    for seconds in (3, 10):
        wait_until(clicked, seconds)
        count = arrow_pixels(ctx, point, f"after-{seconds}s")
        ctx.check(f"the arrow is still drawn at the click point {seconds} s after the action", count >= ARROW_MIN, count)
    ctx.check("status still active 10 s later, with no agent calls in between", activity(ctx).get("active") is True)

    ended = turn_ended(ctx)
    ctx.check("POST /turn-ended answered ok", ended.get("ok") is True, ended)
    time.sleep(1.0)
    count = arrow_pixels(ctx, point, "after-turn-end")
    ctx.check("the arrow is gone within 1 s of /turn-ended", count < 5, count)
    state = activity(ctx)
    ctx.check("status: idle, no arrow, after the turn ended", state.get("active") is False and not state.get("arrow"),
              state)
    item = check_menu(ctx, "Idle", "after the turn ended")
    check_menu_tint(ctx, active_icon, item_pixels(ctx, item, "menu-idle"))


@scenario
def cursor_stays_through_keyboard_actions(ctx):
    _, window, area, _, local, point = start(ctx, "keyboard.txt")
    wid = window["id"]
    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none")
    visible_or_skip(ctx)
    ctx.call("type_text", window_id=wid, text="Z", observe="none")
    ctx.call("press_key", window_id=wid, keys="right", observe="none")
    ctx.call("set_value", window_id=wid, element_index=area, value="line one\nline two", observe="none")
    typed = time.time()

    state = activity(ctx)
    ctx.check("status: keyboard actions left the arrow where the click put it", near(state.get("arrow"), point),
              (state, point))
    wait_until(typed, 3)
    count = arrow_pixels(ctx, point, "after-keyboard")
    ctx.check("the arrow is still drawn at the click point 3 s after keyboard actions", count >= ARROW_MIN, count)


@scenario
def cursor_appears_for_keyboard_first(ctx):
    _, window, _, _, _, _ = start(ctx, "keyboard-first.txt")
    wid = window["id"]
    turn_ended(ctx)
    ctx.call("get_window_state", window_id=wid, full=True)
    state = activity(ctx)
    ctx.check("status: a read alone marks the agent active", state.get("active") is True, state)
    ctx.require("baseline: no arrow before the first pointer or keyboard action", not state.get("arrow"), state)

    ctx.call("type_text", window_id=wid, text="Z", observe="none")
    time.sleep(0.5)
    arrow = activity(ctx).get("arrow")
    frame = window["frame"]
    inside = bool(arrow) and frame["x"] <= arrow["x"] < frame["x"] + frame["width"] \
        and frame["y"] <= arrow["y"] < frame["y"] + frame["height"]
    ctx.require("a first keyboard action shows the arrow inside the target window", inside, (arrow, frame))
    visible_or_skip(ctx, "the focused element")
    count = arrow_pixels(ctx, (arrow["x"], arrow["y"]), "keyboard-first")
    ctx.check("the arrow is drawn where status says it is", count >= ARROW_MIN, count)
    time.sleep(3)
    count = arrow_pixels(ctx, (arrow["x"], arrow["y"]), "keyboard-first-3s")
    ctx.check("and it is still there 3 s later", count >= ARROW_MIN, count)


@scenario
def cursor_ends_on_pause(ctx):
    _, window, _, _, local, point = start(ctx, "pause.txt")
    ctx.call("click", window_id=window["id"], x=local[0], y=local[1], observe="none")
    visible_or_skip(ctx)
    ctx.require("the arrow is drawn after the click", arrow_pixels(ctx, point, "before-pause") >= ARROW_MIN)

    subprocess.run([str(run.APP_BINARY), "--pause"], check=True, capture_output=True, timeout=10)
    ctx.cleanups.append(lambda: subprocess.run([str(run.APP_BINARY), "--resume"], capture_output=True, timeout=10))
    time.sleep(1.5)
    count = arrow_pixels(ctx, point, "after-pause")
    ctx.check("pausing removes the arrow within 1.5 s", count < 5, count)
    state = activity(ctx)
    ctx.check("status: idle while paused", state.get("active") is False and not state.get("arrow"), state)
    check_menu(ctx, "Idle", "while paused")

    subprocess.run([str(run.APP_BINARY), "--resume"], check=True, capture_output=True, timeout=10)
    time.sleep(1.0)
    ctx.check("resuming does not bring the arrow back before the next agent call",
              arrow_pixels(ctx, point, "after-resume") < 5 and activity(ctx).get("active") is False)


@scenario
def cursor_idle_timeout(ctx):
    _, window, _, _, local, point = start(ctx, "idle.txt")
    ctx.call("click", window_id=window["id"], x=local[0], y=local[1], observe="none")
    clicked = time.time()
    visible_or_skip(ctx)

    # Only `status` from here on: it is not a window or app call, so it does not count as agent activity.
    wait_until(clicked, IDLE_TIMEOUT - 15)
    count = arrow_pixels(ctx, point, f"after-{IDLE_TIMEOUT - 15}s")
    ctx.check(f"the arrow is still drawn {IDLE_TIMEOUT - 15} s after the last call", count >= ARROW_MIN, count)
    ctx.check("status still active", activity(ctx).get("active") is True)

    went_idle = None
    while time.time() < clicked + IDLE_TIMEOUT + 10:
        if activity(ctx).get("active") is False:
            went_idle = time.time() - clicked
            break
        time.sleep(0.5)
    ctx.check(f"without a turn-end hook, presence ends about {IDLE_TIMEOUT} s after the last call",
              went_idle is not None and IDLE_TIMEOUT - 5 <= went_idle <= IDLE_TIMEOUT + 5, went_idle)
    time.sleep(1.0)
    count = arrow_pixels(ctx, point, "after-idle-timeout")
    ctx.check("and the arrow is gone", count < 5, count)
    check_menu(ctx, "Idle", "after the idle timeout")


@scenario
def cursor_turn_end_is_per_session(ctx):
    _, window, _, _, local, point = start(ctx, "sessions.txt")
    wid = window["id"]
    turn_ended(ctx)
    session = ctx.call("session", action="create")["session_id"]
    closed = []
    ctx.cleanups.append(lambda: closed or ctx.mcp.call("session", action="close", session_id=session))

    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none", session_id=session)
    visible_or_skip(ctx)
    turn_ended(ctx, "legacy")
    time.sleep(1.0)
    count = arrow_pixels(ctx, point, "after-other-turn-end")
    ctx.check("another session's turn end leaves the arrow alone", count >= ARROW_MIN, count)
    ctx.check("status: still active", activity(ctx).get("active") is True)

    turn_ended(ctx, session)
    time.sleep(1.0)
    count = arrow_pixels(ctx, point, "after-own-turn-end")
    ctx.check("the owning session's turn end removes it within 1 s", count < 5, count)

    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none", session_id=session)
    ctx.require("the arrow is back after the session's next click", arrow_pixels(ctx, point, "next-turn") >= ARROW_MIN)
    ctx.call("session", action="close", session_id=session)
    closed.append(True)
    time.sleep(1.0)
    count = arrow_pixels(ctx, point, "after-session-close")
    ctx.check("closing the session removes it within 1 s", count < 5, count)
    ctx.check("status: idle after the session closed", activity(ctx).get("active") is False)


@scenario
def cursor_arrival_timing(ctx):
    _, window, _, frame, local, _ = start(ctx, "latency.txt")
    wid = window["id"]
    far = (frame[0] + min(frame[2], window["frame"]["width"] - frame[0]) - 30,
           frame[1] + min(frame[3], window["frame"]["height"] - frame[1]) - 30)

    def timed(tool, **args):
        started = time.time()
        ctx.call(tool, window_id=wid, observe="none", **args)
        return time.time() - started

    appearing = timed("click", x=local[0], y=local[1])
    visible_or_skip(ctx)
    ctx.call("click", window_id=wid, x=far[0], y=far[1], observe="none")
    visible_or_skip(ctx, "the far corner of the text area")
    resting = [timed("click", x=p[0], y=p[1]) for p in (far, local, far, local, far, local)]
    drag = timed("drag", from_x=local[0], from_y=local[1], to_x=far[0], to_y=far[1])
    typing = timed("type_text", text="Z")
    numbers = f"appearing={appearing:.3f}s resting={[round(s, 3) for s in resting]} drag={drag:.3f}s type={typing:.3f}s"
    print(f"      {numbers}")
    # The pointer path itself sleeps ~0.26 s per click and ~0.49 s per drag. A click then waits for the arrow to
    # land: at most 0.5 s for the long far<->local arcs, 0.18 s to appear. The drag starts where the arrow rests.
    ctx.check("a click waits for the arrow to land, not for the 1.5 s timeout (median < 0.85 s, max < 1.2 s)",
              statistics.median(resting) < 0.85 and max(resting) < 1.2, numbers)
    ctx.check("the first click waits only for the appear animation (< 0.6 s)", appearing < 0.6, numbers)
    ctx.check("a drag does not wait for its own drag animation (< 0.8 s)", drag < 0.8, numbers)
    ctx.check("a keyboard action is not held up by the arrow (< 0.5 s)", typing < 0.5, numbers)


@scenario
def cursor_hidden_when_covered(ctx):
    probe, window, _, _, local, point = start(ctx, "covered.txt", covered=True)
    wid = window["id"]
    started = time.time()
    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none", caption="Working out of sight")
    took = time.time() - started
    time.sleep(0.6)

    state = activity(ctx)
    ctx.check("status: active, arrow position known, and not visible while covered",
              state.get("active") is True and near(state.get("arrow"), point) and state.get("arrow_visible") is False,
              state)
    ctx.check("nothing is drawn over the covering window", arrow_pixels(ctx, point, "covered") < 5)
    ctx.check("a click on a covered window does not wait for the arrow (< 0.45 s)", took < 0.45, f"{took:.3f}s")
    check_menu(ctx, "Agent active: TextEdit", "while the target is covered")
    ctx.call("type_text", window_id=wid, text="Z", observe="none")
    ctx.check("the agent keeps working on the covered window", "Zline one" in ctx.tree(wid, include_screenshot=False))

    # Quitting the probe re-activates the app that was frontmost before; its windows may cover TextEdit again.
    probe.stop()
    time.sleep(1.2)
    state = activity(ctx)
    if state.get("arrow_visible") is not True:
        inconclusive(ctx, "uncovering the window brings the arrow back", f"still covered by the user's windows: {state}")
        return
    count = arrow_pixels(ctx, point, "uncovered")
    ctx.check("uncovered: the arrow is drawn at its last point without a new action", count >= ARROW_MIN, count)


@scenario
def cursor_caption(ctx):
    _, window, _, _, local, point = start(ctx, "caption.txt")
    wid = window["id"]
    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none")
    visible_or_skip(ctx)
    time.sleep(0.8)
    bare = pill_region(ctx, point, "no-caption")
    ctx.require("pill region captured", bare is not None)
    ctx.check("status: no caption after an action without one", activity(ctx).get("caption") is None, activity(ctx))

    text = "Checking the caption pill"
    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none", caption=text)
    time.sleep(0.8)
    shown = pill_region(ctx, point, "caption")
    ctx.check("status reports the caption", activity(ctx).get("caption") == text, activity(ctx))
    ctx.check("the pill is drawn beside the arrow", changed(bare, shown) > 400, changed(bare, shown))

    ctx.call("get_window_state", window_id=wid, full=True)
    ctx.check("a read keeps the caption", activity(ctx).get("caption") == text, activity(ctx))

    ctx.call("batch", window_id=wid, observe="none", steps=[
        step("press_key", keys="right", caption="Stepping right"),
        step("press_key", keys="left", caption="Stepping back"),
    ])
    ctx.check("a batch step's caption replaces the batch's", activity(ctx).get("caption") == "Stepping back", activity(ctx))

    ctx.call("press_key", window_id=wid, keys="right", observe="none", caption="  Moving\nright  ")
    ctx.check("captions are trimmed to one line", activity(ctx).get("caption") == "Moving right", activity(ctx))

    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none")
    time.sleep(0.6)
    cleared = pill_region(ctx, point, "cleared")
    ctx.check("an action without a caption clears it", activity(ctx).get("caption") is None, activity(ctx))
    ctx.check("and the pill is gone", changed(bare, cleared) < 60, changed(bare, cleared))

    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none", caption=text)
    turn_ended(ctx)
    time.sleep(1.0)
    ctx.check("the turn end takes the caption with it", activity(ctx).get("caption") is None, activity(ctx))
    ended = pill_region(ctx, point, "after-turn-end")
    ctx.check("and nothing is drawn where the pill was", changed(bare, ended) < 60, changed(bare, ended))


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
