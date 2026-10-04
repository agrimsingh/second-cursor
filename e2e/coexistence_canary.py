"""Disposable foreground evidence for the explicit --takeover suites.

CoexistenceCanary(ctx).start() takes foreground ownership of two synthetic native
documents. command('focus', window='B') acknowledges a bounded, intentional
window switch; command('start_activity', interval_ms=150) starts deterministic
in-process typing. checkpoint(label) verifies the complete ledger since startup,
never resetting a baseline or forgetting an earlier violation. stop() archives
evidence and restores the app that was foreground before start.

The fixture routes insertText to its CURRENT key-window first responder. It does
not inject global keyboard events, emulate physical input, or type in other apps.
"""
import json
import os
import pathlib
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time

BASELINE = {name: f"Protected canary {name}.\n" for name in ("A", "B")}
TRANSITIONS = {"app_resign_active", "app_become_active", "window_resign_key",
               "window_become_key", "responder_changed", "text_changed"}


def analyze_canary_events(events, commands, state):
    """Independent oracle; accepts ordinary dicts for replay and negative tests.

Expected text derives from the harness command log and specified token format,
not the fixture's reported expected text. Window-switch allowances are exact
transition lists for an acknowledged command, never arbitrary counter deltas.
"""
    errors, expected, observed_counters = [], dict(BASELINE), {}
    issued = {item["id"]: item for item in commands}
    received, acknowledged, typed = set(), set(), set()
    focus_started, focus_completed = set(), set()
    expected_window, boundary, pending_input = "A", None, None
    activity, tick, ready = False, 0, False
    sequence = 0

    def fail(message):
        errors.append(message)

    def ownership(event):
        return (event.get("active") is True and event.get("key_window") == expected_window
                and event.get("responders", {}).get(expected_window) == f"{expected_window}.editor")

    if len(issued) != len(commands):
        fail("The independently recorded command IDs are not unique")
    for event in events:
        sequence += 1
        actual_sequence, kind = event.get("sequence"), event.get("kind")
        detail = event.get("detail", {})
        tag = f"event {actual_sequence} ({kind})"
        if actual_sequence != sequence:
            fail(f"{tag}: missing, duplicated, or reordered ledger sequence; expected {sequence}")
        if not isinstance(event.get("timestamp"), (int, float)) or not isinstance(event.get("uptime"), (int, float)):
            fail(f"{tag}: timestamps are missing")
        if kind == "ready":
            if ready or sequence != 1:
                fail(f"{tag}: startup boundary repeated or not first")
            ready = True
        elif not ready:
            fail(f"{tag}: no initial ready boundary")

        if kind in TRANSITIONS:
            observed_counters[kind] = observed_counters.get(kind, 0) + 1
        if kind == "app_resign_active":
            fail(f"{tag}: foreground application resigned active")

        if kind == "command_received":
            command_id = detail.get("id")
            if command_id not in issued or detail != issued.get(command_id):
                fail(f"{tag}: command differs from the independent issued-command log")
            if command_id in received:
                fail(f"{tag}: command was executed more than once")
            if received and isinstance(command_id, int) and command_id <= max(received):
                fail(f"{tag}: command IDs regressed")
            if isinstance(command_id, int):
                received.add(command_id)
            if detail.get("action") == "start_activity":
                activity = True
            elif detail.get("action") in ("stop_activity", "quit"):
                activity = False
        elif kind == "focus_begin":
            command_id, target = detail.get("id"), detail.get("window")
            command = issued.get(command_id, {})
            if (boundary is not None or command_id not in received
                    or command.get("action") != "focus" or command.get("window") != target
                    or target not in BASELINE or command_id in focus_started):
                fail(f"{tag}: focus boundary has no matching issued focus command")
            focus_started.add(command_id)
            boundary = {"id": command_id, "old": expected_window, "new": target, "transitions": []}
        elif kind == "focus_settled":
            if (boundary is None or detail.get("id") != boundary["id"]
                    or detail.get("window") != boundary["new"]):
                fail(f"{tag}: settled focus does not match its boundary")
            else:
                required = [] if boundary["old"] == boundary["new"] else [
                    ("window_resign_key", boundary["old"]), ("window_become_key", boundary["new"])]
                if boundary["transitions"] != required:
                    fail(f"{tag}: focus transitions {boundary['transitions']} differ from {required}")
                if boundary["new"] in BASELINE:
                    expected_window = boundary["new"]
                focus_completed.add(boundary["id"])
            boundary = None
        elif kind in ("window_resign_key", "window_become_key", "responder_changed"):
            if boundary is None:
                fail(f"{tag}: unrequested focus/responder transition")
            elif kind == "responder_changed":
                # Both editor responders are established before ready. Switching
                # key windows therefore requires no responder replacement.
                fail(f"{tag}: the established editor responder changed during a window switch")
            elif event.get("boundary_id") != boundary["id"]:
                fail(f"{tag}: focus transition has the wrong command boundary")
            else:
                boundary["transitions"].append((kind, detail.get("window")))
        elif kind == "input_intent":
            if pending_input is not None:
                fail(f"{tag}: input overlaps a previous unfinished input")
            if boundary is not None:
                fail(f"{tag}: input ran before the requested window switch settled")
            if "activity_tick" in detail:
                tick += 1
                wanted = f"<{expected_window}:{tick:06d}>"
                if not activity or detail.get("activity_tick") != tick:
                    fail(f"{tag}: activity was disabled or the deterministic tick sequence broke")
            else:
                command_id = detail.get("command_id")
                command = issued.get(command_id, {})
                wanted = command.get("text", "")
                if command.get("action") != "type" or command_id not in received or command_id in typed:
                    fail(f"{tag}: manual input has no unique issued type command")
                typed.add(command_id)
            if detail.get("target") != expected_window or detail.get("text") != wanted:
                fail(f"{tag}: input target/token differs from the independent command model")
            expected[expected_window] += wanted
            pending_input = detail
        elif kind == "input_result":
            result_source = {key: value for key, value in detail.items() if key != "delivered"}
            if pending_input is None or result_source != pending_input:
                fail(f"{tag}: input result has no matching intent")
            if detail.get("delivered") is not True:
                fail(f"{tag}: synthetic foreground input was lost")
            if event.get("documents") != expected:
                fail(f"{tag}: a synthetic insertion changed the wrong content or caret location")
            pending_input = None
        elif kind == "text_changed":
            name = detail.get("window")
            if (name not in expected or detail.get("text") != expected.get(name)
                    or event.get("documents") != expected):
                fail(f"{tag}: protected text was corrupted, even if a later edit repaired it")
        elif kind == "command_ack":
            command_id = detail.get("id")
            if command_id not in received or command_id in acknowledged or detail.get("ok") is not True:
                fail(f"{tag}: command acknowledgement is failed, missing its command, or duplicated")
            if issued.get(command_id, {}).get("action") == "type" and command_id not in typed:
                fail(f"{tag}: type command acknowledged without an input")
            if issued.get(command_id, {}).get("action") == "focus" and (
                    boundary is not None or command_id not in focus_completed):
                fail(f"{tag}: focus command acknowledged without its unique completed transition")
            acknowledged.add(command_id)

        if ready and boundary is None and not ownership(event):
            fail(f"{tag}: foreground ownership or editor responder differs from the commanded window")

    if not ready or boundary is not None or pending_input is not None:
        fail("Ledger ended before startup, a focus command, or a text insertion completed")
    if received != set(issued) or acknowledged != set(issued):
        fail(f"Commands lack execution/acknowledgement: received={sorted(received)}, acknowledged={sorted(acknowledged)}")
    focus_commands = {command_id for command_id, command in issued.items() if command.get("action") == "focus"}
    if focus_started != focus_commands or focus_completed != focus_commands:
        fail("Each issued focus command needs one recorded beginning and completed transition")
    if sequence != state.get("sequence"):
        fail(f"Ledger ended at {sequence}, but state requires {state.get('sequence')}")
    if state.get("documents") != expected:
        fail("Final protected documents differ from independently expected contents")
    if state.get("counters") != observed_counters:
        fail("Final cumulative transition counters do not match the complete ledger")
    if state.get("activity_count") != tick or state.get("activity_running") is not activity:
        fail("Final synthetic activity state differs from the command/tick model")
    if not ownership(state):
        fail("Final foreground ownership is wrong")
    if state.get("write_error"):
        fail(f"Fixture could not preserve evidence: {state['write_error']}")
    return {"ok": not errors, "errors": errors, "event_count": sequence,
            "activity_count": tick, "expected_documents": expected,
            "expected_window": expected_window, "counters": observed_counters,
            "scope": "synthetic in-process first-responder activity; not physical HID or human typing"}


class CoexistenceCanary:
    def __init__(self, ctx):
        from run import Skip
        if "--takeover" not in sys.argv:
            raise Skip("foreground canary needs --takeover while the user is away from the Mac")
        self.ctx = ctx
        self.state_path = ctx.scratch / "canary-state.json"
        self.command_path = ctx.scratch / "canary-command.json"
        self.events_path = ctx.scratch / "canary-events.jsonl"
        self.commands_path = ctx.scratch / "canary-commands.jsonl"
        self.artifact_prefix = ctx.run.dir / f"{ctx.name}-canary"
        self.commands = []
        self.pid = None
        self.previous = None
        self._lock = threading.RLock()
        ctx.cleanups.append(self.stop)

    def read(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    state = read

    def wait(self, predicate, timeout=10):
        deadline, state = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            state = self.read()
            if state:
                self.pid = state.get("pid", self.pid)
                if state.get("write_error"):
                    raise AssertionError(state["write_error"])
                if predicate(state):
                    return state
            time.sleep(0.025)
        raise AssertionError(f"Canary state did not settle: {state}")

    def start(self):
        from run import observe
        if self.pid is not None:
            raise RuntimeError("Canary has already started")
        self.previous = observe()["frontmost"]
        app = self.ctx.scratch / "SCCoexistenceCanary.app"
        executable = app / "Contents/MacOS/coexistence-canary"
        executable.parent.mkdir(parents=True, exist_ok=True)
        source = pathlib.Path(__file__).parent / "fixtures/coexistence-canary.swift"
        built = subprocess.run(["xcrun", "swiftc", str(source), "-o", str(executable)],
                               capture_output=True, text=True, timeout=120)
        self.ctx.require("the disposable foreground canary compiled", built.returncode == 0,
                         (built.stderr or built.stdout)[-2000:])
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "dev.secondcursor.e2e.coexistencecanary",
            "CFBundleName": "SCCoexistenceCanary", "CFBundleExecutable": "coexistence-canary",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSMinimumSystemVersion": "14.0",
        }))
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)],
                       capture_output=True, check=True, timeout=30)
        for path in (self.state_path, self.command_path, self.events_path, self.commands_path):
            path.unlink(missing_ok=True)
        subprocess.run(["open", "-n", str(app), "--args", "--state", str(self.state_path),
                        "--command", str(self.command_path), "--events", str(self.events_path)],
                       capture_output=True, check=True, timeout=30)
        state = self.wait(lambda item: item.get("ready") and item.get("active") and item.get("key_window") == "A")
        self.ctx.require("the canary owns the foreground process", observe()["frontmost"]["pid"] == self.pid, state)
        return state

    def command(self, action, **fields):
        """Returns the atomic state containing this command's acknowledgement."""
        with self._lock:
            command = {"id": len(self.commands) + 1, "action": action, **fields}
            self.commands.append(command)
            with self.commands_path.open("a") as stream:
                stream.write(json.dumps(command, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary = self.command_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(command, sort_keys=True))
            temporary.replace(self.command_path)
            state = self.wait(lambda item: item.get("last_ack", {}).get("id") == command["id"])
            if not state["last_ack"].get("ok"):
                raise AssertionError(f"Canary command failed: {state['last_ack']}")
            return state

    def _events_through(self, sequence):
        events = []
        with self.events_path.open() as stream:
            for line in stream:
                if len(events) >= sequence:
                    break  # A later activity event can still be being appended.
                events.append(json.loads(line))
        return events

    def checkpoint(self, label):
        from run import observe
        with self._lock:
            state = self.command("mark", label=label)
            try:
                events = self._events_through(state["sequence"])
                result = analyze_canary_events(events, self.commands, state)
            except (OSError, ValueError) as error:
                result = {"ok": False, "errors": [f"Cannot read complete canary evidence: {error}"]}
            result["frontmost"] = observe()["frontmost"]
            if result["frontmost"].get("pid") != self.pid:
                result["ok"] = False
                result["errors"].append("The OS frontmost process is not the canary")
            self.ctx.check(f"{label}: all foreground text and transient ownership events are protected",
                           result["ok"], result)
            self.archive(state)
            return result

    assert_protected = checkpoint

    def archive(self, state=None):
        state = state or self.read()
        if state:
            pathlib.Path(f"{self.artifact_prefix}-state.json").write_text(json.dumps(state, indent=2))
        if self.events_path.exists():
            # Capture exactly the atomic state's ledger prefix. Activity may
            # already have appended more events while this snapshot is saved.
            lines = self.events_path.read_text().splitlines(keepends=True)
            if state:
                lines = lines[:state["sequence"]]
            pathlib.Path(f"{self.artifact_prefix}-events.jsonl").write_text("".join(lines))
        if self.commands_path.exists():
            shutil.copy2(self.commands_path, f"{self.artifact_prefix}-commands.jsonl")

    def stop(self):
        from run import observe
        state = self.read()
        pid = self.pid or (state or {}).get("pid")
        if pid:
            try:
                self.command("stop_activity")
                self.checkpoint("canary cleanup")
            except Exception as error:
                self.ctx.check("canary cleanup retained final evidence", False, str(error))
            finally:
                self.archive()
                try:
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.05)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.pid = None
                # Keep final evidence on disk, but prevent a duplicate cleanup
                # from signalling a PID that the operating system could reuse.
                self.state_path.unlink(missing_ok=True)
        if self.previous and self.previous.get("bundle_id"):
            if observe()["frontmost"]["pid"] != self.previous["pid"]:
                subprocess.run(["open", "-b", self.previous["bundle_id"]], capture_output=True, timeout=15)
                deadline = time.monotonic() + 5
                while observe()["frontmost"]["pid"] != self.previous["pid"] and time.monotonic() < deadline:
                    time.sleep(0.1)
        self.previous = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
