#!/usr/bin/env python3
"""Native safety contracts through public MCP, with disposable apps only.

Failure cases are defined in docs/acceptance-native-safety.md. The normal suite stays
on the agent display and preserves the run.py cursor, frontmost-app, and Space
invariants. The foreground scenarios need --takeover: they temporarily make a
disposable fixture frontmost and restore the previous app on cleanup.

Usage: e2e/native_safety.py [scenario ...] [--takeover] [--installed]
       SC_E2E_APP=build/SecondCursor.app e2e/native_safety.py
       --off-space leaves native fixtures on their normal Space instead of moving
       them to the agent display; use from a different/full-screen Space to test
       off-Space execution. All assertions still run.
"""
import json
import pathlib
import plistlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, SCENARIOS, Skip, ToolError, element_frame, element_index, observe, scenario
from focus import FocusProbe, stop_process

# Both imported suites register scenarios; this entry point runs only its own.
SCENARIOS.clear()
OFF_SPACE = "--off-space" in sys.argv


def refusal(ctx, tool, **args):
    """Keep the error in the report so an unrelated refusal cannot pass a check."""
    try:
        result = ctx.call(tool, **args)
    except ToolError as error:
        return str(error)
    raise AssertionError(f"{tool} unexpectedly dispatched: {result}")


def snapshot(ctx, wid, **args):
    return ctx.call("get_window_state", window_id=wid, full=True,
                    include_screenshot=False, **args)


def settled_geometry_snapshot(ctx, wid):
    """Launch and first make-key can finish resizing after AX is already readable."""
    ctx.call("press_key", window_id=wid, keys="escape", observe="none")
    deadline, previous, stable = time.time() + 3, None, 0
    while time.time() < deadline:
        snap = snapshot(ctx, wid)
        frame = snap["window"]["frame"]
        geometry = tuple(frame[key] for key in ("x", "y", "width", "height"))
        stable = stable + 1 if geometry == previous else 1
        if stable >= 5:
            ctx.check("the fixture held identical geometry for five observations before planning", True, geometry)
            return snap
        previous = geometry
        time.sleep(0.1)
    raise AssertionError("TextEdit did not settle to a stable window frame before the layout-only test")


def area_of(ctx, snap):
    area = element_index(snap["tree"], "AXTextArea")
    ctx.require("the disposable document exposes its text area", area is not None,
                snap["tree"][:500])
    return area


def content(ctx, wid, area):
    return ctx.call("inspect_element", window_id=wid, element_index=area,
                    offset=0, length=10000)


def launch_textedit(ctx, filename, text):
    window = ctx.launch_scratch_textedit(filename, text, on_agent_display=not OFF_SPACE)
    if OFF_SPACE:
        ctx.check("off-Space coverage: fixture stays off the agent display",
                  not window.get("on_agent_display"), window)
        ctx.check("off-Space coverage: fixture starts off screen",
                  window.get("is_on_screen") is False, window)
    return window


def check_profile(ctx, profile, label):
    ctx.require(f"{label}: native backend identified", profile.get("backend") == "native_macos", profile)
    ctx.check(f"{label}: OS version disclosed", bool(profile.get("os_version")), profile)
    ctx.check(f"{label}: input monitoring availability is explicit",
              isinstance(profile.get("input_monitoring_available"), bool), profile)
    gestures = profile.get("capabilities", {})
    for name in ("hover", "modifier_click", "modifier_drag"):
        capability = gestures.get(name, {})
        ctx.check(f"{label}: {name} explicitly unsupported", capability.get("supported") is False,
                  capability)
        ctx.check(f"{label}: {name} gives a reason and a usable alternative",
                  bool(capability.get("reason")) and bool(capability.get("alternative")), capability)
    for name in ("pointer", "keyboard", "semantic_actions"):
        ctx.check(f"{label}: {name} support is explicit",
                  isinstance(gestures.get(name, {}).get("supported"), bool), gestures.get(name))


def check_freshness(ctx, shot, started, label):
    freshness = shot.get("freshness", {})
    ctx.check(f"{label}: pixels never claim verified repaint freshness",
              freshness.get("state") == "unverified", freshness)
    captured = freshness.get("captured_at")
    ctx.check(f"{label}: capture timestamp belongs to this request",
              isinstance(captured, (int, float)) and started - 1 <= captured <= time.time() + 1,
              freshness)
    ctx.check(f"{label}: freshness limitation is explained", bool(freshness.get("reason")), freshness)
    ctx.check(f"{label}: on-screen state is explicit",
              isinstance(freshness.get("window_on_screen"), bool), freshness)
    if freshness.get("window_on_screen") is False:
        ctx.check(f"{label}: off-screen capture is explained",
                  bool(re.search(r"off.?screen|space|repaint|stale", freshness.get("reason", ""), re.I)),
                  freshness)


class HumanFixture:
    """Two disposable windows in the same foreground process; --takeover only."""

    counters = ("user_resign_key", "user_resign_active", "user_first_responder_changes")

    def __init__(self, ctx):
        if "--takeover" not in sys.argv:
            raise Skip("takes foreground ownership; run with --takeover when you are away from the Mac")
        self.ctx = ctx
        self.previous = observe()["frontmost"]
        self.state_path = ctx.scratch / "human-state.json"
        self.pid = None
        ctx.cleanups.append(self.stop)

    def read(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def wait(self, predicate, timeout=10):
        deadline, state = time.time() + timeout, None
        while time.time() < deadline:
            state = self.read()
            if state and predicate(state):
                return state
            time.sleep(0.05)
        raise AssertionError(f"human fixture state did not settle: {state}")

    def start(self):
        source = pathlib.Path(__file__).parent / "benchmarks/human_fixture.swift"
        app = self.ctx.scratch / "SCHumanFixture.app"
        executable = app / "Contents/MacOS/human-fixture"
        executable.parent.mkdir(parents=True)
        built = subprocess.run(["xcrun", "swiftc", str(source), "-o", str(executable)],
                               capture_output=True, text=True, timeout=120)
        self.ctx.require("the disposable two-window fixture compiled", built.returncode == 0,
                         (built.stderr or built.stdout)[-1500:])
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "dev.secondcursor.e2e.humanfixture",
            "CFBundleName": "SCHumanFixture",
            "CFBundleExecutable": "human-fixture",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True,
        }))
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)],
                       capture_output=True, check=True, timeout=30)
        self.state_path.unlink(missing_ok=True)
        subprocess.run(["open", "-n", str(app), "--args", "--state", str(self.state_path)],
                       capture_output=True, check=True, timeout=30)
        state = self.wait(lambda item: item.get("ready") and item.get("active")
                          and item.get("user_key") and not item.get("agent_key"))
        self.pid = state["pid"]
        self.ctx.require("the fixture is the foreground process", observe()["frontmost"]["pid"] == self.pid, state)
        # Synthetic activation is not human input. Allow setup and any input that
        # preceded the human-attended run to leave the recent-input window.
        time.sleep(2.2)
        return self.read()

    def unchanged(self, baseline, label):
        time.sleep(0.15)  # The fixture writes independently every 50 ms.
        state = self.read()
        self.ctx.require(f"{label}: the fixture still reports state", state is not None)
        self.ctx.check(f"{label}: the user window remains key and active",
                       state["active"] and state["user_key"] and not state["agent_key"]
                       and observe()["frontmost"]["pid"] == self.pid, state)
        self.ctx.check(f"{label}: no transient user focus loss occurred",
                       all(state[key] == baseline[key] for key in self.counters),
                       {key: (baseline[key], state[key]) for key in self.counters})
        self.ctx.check(f"{label}: neither document changed and the button was not pressed",
                       all(state[key] == baseline[key] for key in
                           ("user_text", "agent_text", "agent_button_presses")), state)

    def stop(self):
        state = self.read()
        if state:
            (self.ctx.run.dir / f"{self.ctx.name}-human-fixture.json").write_text(json.dumps(state, indent=2))
            self.pid = self.pid or state.get("pid")
        if self.pid:
            stop_process(self.pid)
            self.pid = None
        if self.previous.get("bundle_id") and observe()["frontmost"]["pid"] != self.previous["pid"]:
            subprocess.run(["open", "-b", self.previous["bundle_id"]], capture_output=True, timeout=15)
            deadline = time.time() + 5
            while observe()["frontmost"]["pid"] != self.previous["pid"] and time.time() < deadline:
                time.sleep(0.1)


@scenario
def native_capabilities(ctx):
    """Clients can choose a route without guessing what the current Mac supports."""
    status = ctx.call("status")
    check_profile(ctx, status.get("native_capabilities", {}), "status")
    capabilities = ctx.call("capabilities")
    check_profile(ctx, capabilities.get("native", {}), "capabilities tool")
    ctx.check("the agent display is explicitly shared with this login session",
              capabilities.get("agent_display_is_isolated") is False, capabilities)
    ctx.check("unimplemented isolated execution is not advertised",
              capabilities.get("isolated_backend_available") is False, capabilities)

    wid = launch_textedit(ctx, "capabilities.txt", "Capture freshness fixture")["id"]
    started = time.time()
    snap = ctx.call("get_window_state", window_id=wid, full=True, include_screenshot=True)
    check_profile(ctx, snap.get("capabilities", {}), "window snapshot")
    ctx.require("window snapshot captured an image", "screenshot" in snap and bool(ctx.screenshots), snap)
    check_freshness(ctx, snap["screenshot"], started, "window snapshot")
    started = time.time()
    shot = ctx.call("screenshot", window_id=wid)
    ctx.require("standalone screenshot captured pixels", shot.get("width", 0) > 0, shot)
    check_freshness(ctx, shot, started, "screenshot tool")
    changed = shot.get("freshness", {}).get("changed_since_previous_capture")
    ctx.check("pixel comparison remains separate from repaint verification",
              isinstance(changed, bool) and shot.get("freshness", {}).get("state") == "unverified", shot)


@scenario
def native_semantic_edits(ctx):
    """Stable elements survive our own value/selection changes without a reread."""
    wid = launch_textedit(ctx, "semantic-edits.txt", "alpha beta gamma")["id"]
    snap = snapshot(ctx, wid)
    area = area_of(ctx, snap)
    target = dict(window_id=wid, element_index=area, observation_id=snap["snapshot_id"], observe="none")
    ctx.call("select_text", text="beta", **target)
    ctx.call("type_text", text="delta", **target)
    first = content(ctx, wid, area)
    ctx.require("typing replaced only the selected occurrence", first["text"] == "alpha delta gamma", first)

    # Replacing selected text with the same text is still a successful insertion.
    # Treating an unchanged value as failure would retry by keyboard after AX has
    # collapsed the selection, duplicating the word at the caret.
    ctx.call("select_text", text="delta", **target)
    identical = ctx.call("type_text", text="delta", **target)
    unchanged = content(ctx, wid, area)
    ctx.require("identical replacement does not fall through and duplicate text",
                unchanged["text"] == "alpha delta gamma", unchanged)
    ctx.check("identical replacement reports its verified semantic result",
              identical.get("route") == "ax_selected_text" and identical.get("verified") is True, identical)
    ctx.check("identical replacement collapses selection exactly after the replacement",
              unchanged.get("selection") == {"offset": len("alpha delta"), "length": 0}, unchanged)

    # In TextEdit the document becomes Edited after the first action. That unrelated
    # title-bar change must not invalidate the original, still-identical text area.
    ctx.call("set_value", value="first repeat then second repeat", **target)
    ctx.call("select_text", text="repeat", prefix="second ", **target)
    ctx.call("type_text", text="replacement", **target)
    final = content(ctx, wid, area)
    ctx.require("the original element and observation support sequential semantic editing",
                final["text"] == "first repeat then second replacement", final)
    ctx.check("selection remains bounded by the resulting document",
              final.get("selection", {}).get("offset", -1) == len(final["text"]), final)


@scenario
def native_stale_coordinates(ctx):
    """Opening Find changes the target layout while its outer size stays fixed."""
    wid = launch_textedit(ctx, "stale-layout.txt", "alpha beta gamma")["id"]
    snap = settled_geometry_snapshot(ctx, wid)
    area = area_of(ctx, snap)
    frame = element_frame(snap["tree"], area)
    ctx.require("the original text area has coordinates", frame is not None, snap["tree"])
    x, y, _, _ = frame
    before = content(ctx, wid, area)["text"]
    ctx.call("press_key", window_id=wid, keys="cmd+f", observe="none")
    time.sleep(0.4)
    error = refusal(ctx, "click", window_id=wid, x=x + 12, y=y + 12,
                    observation_id=snap["snapshot_id"], observe="none")
    ctx.check("same-size layout change refuses stale coordinates before dispatch",
              bool(re.search(r"layout|stale|changed.*observ|observ.*again", error, re.I)), error)
    after = snapshot(ctx, wid)
    old_size = (snap["window"]["frame"]["width"], snap["window"]["frame"]["height"])
    new_size = (after["window"]["frame"]["width"], after["window"]["frame"]["height"])
    ctx.check("the fixture changed layout without resizing the window", old_size == new_size,
              (old_size, new_size))
    ctx.check("Find is visible in the fresh observation",
              bool(re.search(r"Find|Search|search", after["tree"])), after["tree"][:1000])
    current_area = area_of(ctx, after)
    ctx.check("refused coordinates left the document intact", content(ctx, wid, current_area)["text"] == before)


@scenario
def native_stale_resize(ctx):
    """Calculator's scientific mode provides a deterministic public-MCP resize."""
    launched = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=not OFF_SPACE)
    pid = launched["app"]["pid"]
    ctx.kill_on_exit(pid)
    window = ctx.wait_for_window("Calculator", pid=pid)
    wid = window["id"]
    if OFF_SPACE:
        ctx.check("off-Space coverage: Calculator stays off the agent display",
                  not window.get("on_agent_display"), window)
        ctx.check("off-Space coverage: Calculator starts off screen",
                  window.get("is_on_screen") is False, window)
    # Persisted Calculator preferences may start scientific, so establish basic first.
    ctx.call("press_key", window_id=wid, keys="cmd+1", observe="none")
    snap = snapshot(ctx, wid)
    ctx.call("press_key", window_id=wid, keys="cmd+2", observe="none")
    time.sleep(0.4)
    windows = ctx.call("list_windows", app="Calculator")["windows"]
    current = next(w for w in windows if w["id"] == wid)
    old_size = (snap["window"]["frame"]["width"], snap["window"]["frame"]["height"])
    new_size = (current["frame"]["width"], current["frame"]["height"])
    ctx.require("the disposable Calculator actually resized", old_size != new_size, (old_size, new_size))
    error = refusal(ctx, "click", window_id=wid, x=40, y=80,
                    observation_id=snap["snapshot_id"], observe="none")
    ctx.check("resized window refuses old coordinates before dispatch",
              bool(re.search(r"resiz|layout|stale|observ.*again", error, re.I)), error)


@scenario
def native_modifier_refusal(ctx):
    """Unsupported modifier gestures must not quietly become unmodified input."""
    wid = launch_textedit(ctx, "modifiers.txt", "alpha beta gamma")["id"]
    snap = snapshot(ctx, wid)
    area = area_of(ctx, snap)
    frame = element_frame(snap["tree"], area)
    ctx.require("the fixture text area has coordinates", frame is not None, snap["tree"])
    x, y, _, _ = frame
    ctx.call("select_text", window_id=wid, element_index=area, text="beta", observe="none")
    before = content(ctx, wid, area)
    gestures = [
        ("click", dict(element_index=area, modifiers=["shift"])),
        ("click", dict(x=x + 5, y=y + 8, modifiers=["cmd"])),
        ("drag", dict(from_x=x + 5, from_y=y + 8, to_x=x + 110, to_y=y + 8, modifiers=["shift"])),
    ]
    for tool, args in gestures:
        error = refusal(ctx, tool, window_id=wid, observe="none", **args)
        ctx.check(f"{tool} modifier refusal identifies the unsupported gesture",
                  "modifier" in error.lower() and bool(re.search(r"unsupported|not support|cannot|can't", error, re.I)),
                  error)
        ctx.check(f"{tool} modifier refusal suggests a supported route",
                  bool(re.search(r"semantic|select_text|browser", error, re.I)), error)
        after = content(ctx, wid, area)
        ctx.check(f"{tool} refusal leaves text and selected range unchanged",
                  after["text"] == before["text"] and after.get("selection") == before.get("selection"),
                  {"before": before, "after": after})


@scenario
def native_same_process_refusal(ctx):
    """Semantic edits preserve the user's key window; physical routes must refuse."""
    fixture = HumanFixture(ctx)
    profile = ctx.call("status").get("native_capabilities", {})
    ctx.require("input monitoring availability is disclosed",
                isinstance(profile.get("input_monitoring_available"), bool), profile)
    if not profile["input_monitoring_available"]:
        raise Skip("input monitoring is unavailable, so conservative foreground refusal applies; "
                   "native_foreground_refusal checks that fallback instead")
    initial = fixture.start()
    wid, user_wid = initial["agent_window"], initial["user_window"]
    snap, user_snap = snapshot(ctx, wid), snapshot(ctx, user_wid)
    ctx.require("both windows belong to the disposable foreground process",
                wid != user_wid and snap["window"]["pid"] == user_snap["window"]["pid"] == fixture.pid,
                {"agent": snap["window"], "user": user_snap["window"]})
    fields = [element_index(snap["tree"], role) for role in ("AXTextArea", "AXTextField")]
    field = next((index for index in fields if index is not None), None)
    ctx.require("the agent window exposes its editable text", field is not None, snap["tree"])
    marker = "AX edit stayed in the agent window"
    edited = ctx.call("set_value", window_id=wid, element_index=field, value=marker, observe="none")
    ctx.require("a semantic AX edit succeeds without a key-window handoff",
                edited.get("route") == "ax_value" and edited.get("verified") is True, edited)
    baseline = fixture.wait(lambda state: state["agent_text"] == marker)
    ctx.check("semantic editing leaves the user's document and key window intact",
              baseline["user_text"] == initial["user_text"] and baseline["active"]
              and baseline["user_key"] and not baseline["agent_key"], baseline)
    ctx.check("semantic editing caused no transient user focus loss",
              all(baseline[key] == initial[key] for key in fixture.counters),
              {key: (initial[key], baseline[key]) for key in fixture.counters})

    # Refresh after our semantic edit so input refusal must identify the key-window
    # conflict, rather than accidentally passing through a stale-layout refusal.
    snapshot(ctx, wid)
    button = baseline["agent_button_frame"]
    x, y = button["x"] + button["width"] / 2, button["y"] + button["height"] / 2
    text = baseline["agent_text_frame"]
    for tool, args in (
        ("press_key", {"keys": "x"}),
        ("press_key", {"keys": "cmd+a"}),
        ("click", {"x": x, "y": y}),
        ("drag", {"from_x": text["x"] + 6, "from_y": text["y"] + 8,
                  "to_x": text["x"] + min(120, text["width"] - 6), "to_y": text["y"] + 8}),
    ):
        error = refusal(ctx, tool, window_id=wid, observe="none", **args)
        ctx.check(f"{tool} refuses because the user owns another key window",
                  bool(re.search(r"another key window|change their key window|key.window.*user", error, re.I)), error)
        fixture.unchanged(baseline, tool)


@scenario
def native_foreground_refusal(ctx):
    """Verify the conservative fallback only when real input cannot be monitored.

    With monitoring available an idle foreground target is allowed. Launching or
    activating the probe is synthetic activity, so it cannot honestly establish
    recent human input. The separate native_same_process_refusal scenario proves
    key-window arbitration with a two-window fixture.
    """
    probe = FocusProbe(ctx)  # Skips before launching anything unless --takeover is supplied.
    profile = ctx.call("status").get("native_capabilities", {})
    ctx.require("input monitoring availability is disclosed",
                isinstance(profile.get("input_monitoring_available"), bool), profile)
    if profile["input_monitoring_available"]:
        raise Skip("monitoring is available, so idle foreground input is allowed; "
                   "synthetic probe activation cannot prove recent human activity; "
                   "native_same_process_refusal checks key-window ownership separately")
    probe.start()
    wid = probe.window()["id"]
    snap = snapshot(ctx, wid)
    field = element_index(snap["tree"], "AXTextField")
    ctx.require("the foreground probe exposes its text field", field is not None, snap["tree"])
    before = content(ctx, wid, field)
    for tool, args in (
        ("type_text", dict(element_index=field, text="UNEXPECTED")),
        ("set_value", dict(element_index=field, value="UNEXPECTED")),
        ("click", dict(element_index=field)),
    ):
        error = refusal(ctx, tool, window_id=wid, observe="none", **args)
        ctx.check(f"{tool} protects the foreground window when input monitoring is unavailable",
                  bool(re.search(r"foreground|human|user|frontmost", error, re.I)), error)
    after = content(ctx, wid, field)
    ctx.check("refused mutations leave the foreground field unchanged", before["text"] == after["text"], after)
    probe.check("foreground mutations refused")


if __name__ == "__main__":
    run = Run(browser=False)
    if OFF_SPACE:
        run.notes.append("Off-Space coverage: native fixtures were left on their normal Space, "
                         "with off-screen state asserted; all selected scenario assertions ran unchanged.")
    # Run.run ignores all --flags when selecting names, including --off-space.
    sys.exit(0 if run.run(set(sys.argv[1:])) else 1)
