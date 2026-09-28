#!/usr/bin/env python3
"""End-to-end suite for Second Cursor.

Drives the app over MCP HTTP exactly like a client would, checks results through the tools
themselves, and checks the "never interrupt the user" invariants with an independent observer.
By default every suite runs against a fresh, disposable test instance (see TestInstance) launched
from the installed bundle with its own SC_HOME, port, token and config; the user's running app and
config are never read, written or restarted.

Usage:  e2e/run.py [scenario ...] [--installed] [--browser | --no-browser]
        --installed    target the user's running app and config instead (also SC_E2E_TARGET=installed)
        --browser      always start the isolated test browser (default: only when a selected scenario
                       uses browser tools); --no-browser never starts it
        --approvals    human-attended only: the test instance uses "approvals": "prompt" and may show real
                       approval dialogs (default "allow"; scenarios may override to "deny")
        --on-screen    also run the checks that need a window on the user's screen (a launch that leaves the
                       window where the app puts it, return_window); by default every window stays on the agent display
        SC_E2E_APP=build/SecondCursor.app   launch test instances from another bundle
Output: e2e/artifacts/<run-id>/report.md, results.json, transcript.jsonl, screenshots/, instance/ (app logs)
"""
import atexit
import base64
import datetime
import http.server
import importlib.util
import inspect
import json
import os
import pathlib
import plistlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# `from run import ...` inside scenarios must see this module's rebound globals even when run.py is __main__.
sys.modules.setdefault("run", sys.modules[__name__])

ROOT = pathlib.Path(__file__).resolve().parent
INSTALLED_APP = pathlib.Path.home() / "Applications/SecondCursor.app"
APP_BUNDLE = pathlib.Path(os.environ.get("SC_E2E_APP") or INSTALLED_APP).expanduser().resolve()
APP_BINARY = APP_BUNDLE / "Contents/MacOS/SecondCursor"
# The user's instance. Run rebinds both to the test instance's files, so scenarios can keep using them.
CONFIG_PATH = pathlib.Path.home() / "Library/Application Support/SecondCursor/config.json"
ACTION_LOG = pathlib.Path.home() / "Library/Logs/SecondCursor/actions.jsonl"
# Mirrors SCConfig.defaultBlockedBundleIDs.
DEFAULT_BLOCKED_BUNDLE_IDS = [
    "com.apple.keychainaccess", "com.agilebits.onepassword7", "com.1password.1password",
    "com.apple.systempreferences", "com.apple.preference.security",
]
PROTOCOL_VERSION = "2025-06-18"
ON_SCREEN = "--on-screen" in sys.argv

EXPECTED_TOOLS = {
    "status", "list_apps", "list_windows", "launch_app", "get_window_state", "batch", "click", "drag", "type_text",
    "set_value", "press_key", "scroll", "perform_action", "screenshot", "agent_display",
    "move_to_agent_display", "return_window", "browser_status", "browser_tabs", "browser_open",
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_press_key",
    "browser_scroll", "browser_screenshot", "browser_close",
}
ELECTRON_BUNDLES = [
    "com.tinyspeck.slackmacgap", "com.anthropic.claudefordesktop", "com.hnc.Discord",
    "com.microsoft.VSCode", "notion.id", "com.linear", "com.openai.codex",
]


class ToolError(Exception):
    pass


class Skip(Exception):
    pass


class MCP:
    def __init__(self, url, token, transcript=None, timeout=90):
        self.url, self.token, self.transcript, self.timeout = url, token, transcript, timeout
        self.next_id = 1
        self.lock = threading.Lock()
        self.protocol = PROTOCOL_VERSION

    def raw(self, payload, token="default"):
        token = self.token if token == "default" else token
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode(errors="replace")

    def rpc(self, method, params=None):
        with self.lock:
            msg_id = self.next_id
            self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        started = time.time()
        status, ctype, body = self.raw(payload)
        message = parse_body(ctype, body)
        if self.transcript:
            self.transcript.write(payload, message, status, time.time() - started)
        if status != 200 or message is None:
            raise ToolError(f"HTTP {status}: {body[:300]}")
        if "error" in message:
            raise ToolError(f"JSON-RPC error: {message['error']}")
        return message["result"]

    def initialize(self):
        result = self.rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "second-cursor-e2e", "version": "1"},
        })
        self.protocol = result.get("protocolVersion", PROTOCOL_VERSION)
        return result

    def call(self, tool, **args):
        """Returns (data, images). data is the JSON object in the first text item."""
        result = self.rpc("tools/call", {"name": tool, "arguments": args})
        texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
        images = [c for c in result.get("content", []) if c.get("type") == "image"]
        if result.get("isError"):
            raise ToolError(texts[0] if texts else "tool error")
        data = json.loads(texts[0]) if texts else {}
        return data, images


def parse_body(ctype, body):
    if not body:
        return None
    if "text/event-stream" in ctype:
        events = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        return json.loads(events[-1]) if events else None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


class Transcript:
    def __init__(self, path, run):
        self.file = open(path, "w")
        self.run = run
        self.lock = threading.Lock()

    def write(self, request, response, status, seconds):
        def redact(obj):
            if isinstance(obj, dict):
                if obj.get("type") == "image" and "data" in obj:
                    return {**obj, "data": f"<{len(obj['data'])} base64 chars>"}
                return {k: redact(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [redact(v) for v in obj]
            return obj
        line = json.dumps({
            "scenario": self.run.current, "status": status, "seconds": round(seconds, 3),
            "request": request, "response": redact(response),
        }) + "\n"
        with self.lock:
            self.file.write(line)
            self.file.flush()


def observe():
    binary = ROOT / ".bin/observe"
    source = ROOT / "observe.swift"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        binary.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)], check=True, capture_output=True)
    return json.loads(subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def terminate(pid, grace=15):
    """SIGTERM gives the app time to return hosted windows; SIGKILL if it hangs."""
    for sig, seconds in ((signal.SIGTERM, grace), (signal.SIGKILL, 3)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.time() + seconds
        while alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        if not alive(pid):
            return


def tail(path, lines=20):
    try:
        return "\n".join(pathlib.Path(path).read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def load_start_browser():
    spec = importlib.util.spec_from_file_location("start_browser", ROOT / "start-browser.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestInstance:
    """A disposable Second Cursor instance: its own SC_HOME, a free localhost port, a random token and a
    fresh config, launched from an app bundle through LaunchServices so it keeps the bundle's TCC identity.
    It never reads or writes the user's config, logs or pause flag, and stop() only signals the process
    that was launched with its SC_HOME. The constructor starts it and returns once MCP initialize works.
    Config defaults to "approvals": "allow" and "appApproval": "off" so no approval UI can appear;
    "approvals": "deny" is allowed, "prompt" only with the --approvals flag.

        with TestInstance({"claimIdleSeconds": 2}, browser=True) as app:
            app.mcp.call("status")
            subprocess.run([app.binary, "--pause"], env=app.env)
    """

    def __init__(self, config_overrides=None, app=None, browser=False, timeout=30):
        self.app = pathlib.Path(app or APP_BUNDLE).expanduser().resolve()
        self.binary = self.app / "Contents/MacOS/SecondCursor"
        self.overrides = dict(config_overrides or {})
        self.want_browser, self.timeout = browser, timeout
        self.home = self.pid = self.port = self.token = self.url = self.mcp = self.config = self.browser = None
        self.started = False
        self.start()

    @property
    def config_path(self):
        return self.home / "config.json"

    @property
    def action_log(self):
        return self.home / "logs/actions.jsonl"

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def env(self):
        """Environment for subprocesses (CLI flags, SDK, native host) that must reach this instance."""
        return {**os.environ, "SC_HOME": str(self.home)}

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        if self.started:
            return self
        self.started = True
        # Tests must never show UI: "prompt" (also the app's default for a missing value) opens real approval
        # dialogs, so only a human running with --approvals may ask for it.
        approvals = self.overrides.get("approvals", "allow")
        if approvals not in ("allow", "deny") and "--approvals" not in sys.argv:
            raise RuntimeError(f'approvals={approvals!r} can show approval dialogs; use "allow" or "deny", '
                               "or run with --approvals")
        if not self.binary.exists():
            raise RuntimeError(f"{self.binary} not found; build or install the app first")
        # Short on purpose: the browser socket lives here and Unix socket paths cap at 104 bytes.
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="sc-e2e-", dir="/tmp"))
        atexit.register(self.stop)
        if threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
        self.config = {"port": free_port(), "token": secrets.token_hex(16), "blockedBundleIDs": DEFAULT_BLOCKED_BUNDLE_IDS,
                       "confirmationRequiredTools": [], "appApproval": "off", "approvals": "allow", **self.overrides}
        self.port, self.token = self.config["port"], self.config["token"]
        self.url = f"{self.base_url}/mcp"
        self.mcp = MCP(self.url, self.token, timeout=5)
        self.config_path.write_text(json.dumps(self.config, indent=2))
        logs = self.home / "logs"
        logs.mkdir()
        for name in ("stdout.log", "stderr.log"):
            (logs / name).touch()
        launched = subprocess.run(
            ["open", "-n", "-g", "--env", f"SC_HOME={self.home}", "--stdout", str(logs / "stdout.log"),
             "--stderr", str(logs / "stderr.log"), "-a", str(self.app)],
            capture_output=True, text=True,
        )
        if launched.returncode:
            self._fail(f"open failed: {launched.stderr.strip()}")
        deadline, last = time.time() + self.timeout, None
        while True:
            self.pid = self._pid_file() or self.pid
            if self.pid and not alive(self.pid):
                self._fail(f"pid {self.pid} exited during startup")
            try:
                self.mcp.initialize()
                break
            except (OSError, ToolError) as e:
                last = e
            if time.time() > deadline:
                self._fail(f"no answer to MCP initialize on {self.url} within {self.timeout}s ({last})")
            time.sleep(0.25)
        self.pid = self._pid_file() or self.pid
        self.mcp.timeout = 90
        if self.want_browser:
            try:
                self.start_browser()
            except Exception:
                self.stop()
                raise
        return self

    def start_browser(self, timeout=45):
        """Starts an isolated headless Chrome for Testing whose extension reaches this instance, and waits
        until the instance reports the connection."""
        if self.browser is None:
            self.browser = load_start_browser().launch(sc_home=self.home, app=self.app)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.browser.running():
                raise RuntimeError(f"test browser exited during startup:\n{tail(self.browser.log)}")
            if self.browser_connected():
                return self.browser
            time.sleep(0.5)
        raise RuntimeError(f"extension did not connect to the test instance within {timeout}s:\n{tail(self.browser.log)}")

    def browser_connected(self):
        for tool, key in (("browser_instances", "browsers"), ("browser_status", "connected")):
            try:
                return bool(self.mcp.call(tool)[0].get(key))
            except ToolError:
                continue
        return False

    def stop(self, keep_logs=None):
        """Stops the browser and the app, optionally copies the app's logs to `keep_logs`, deletes SC_HOME.
        Idempotent; also runs at exit."""
        if self.home is None:
            return
        if self.browser:
            try:
                if keep_logs:
                    pathlib.Path(keep_logs).mkdir(parents=True, exist_ok=True)
                    shutil.copy(self.browser.log, pathlib.Path(keep_logs) / "browser.log")
                self.browser.stop()
            except Exception as e:
                print(f"warning: could not stop the test browser: {e}", file=sys.stderr)
            self.browser = None
        pids = set(self._launched_pids())
        pid = self._pid_file() or self.pid
        if pid and alive(pid) and "Contents/MacOS/SecondCursor" in self._command(pid):
            pids.add(pid)
        for pid in pids:
            terminate(pid)
        if keep_logs and (self.home / "logs").exists():
            shutil.copytree(self.home / "logs", keep_logs, dirs_exist_ok=True)
        shutil.rmtree(self.home, ignore_errors=True)
        self.home = None

    def _pid_file(self):
        try:
            return int((self.home / "instance.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def _command(self, pid):
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout

    def _launched_pids(self):
        """App processes started with this SC_HOME, found through their environment, so a bundle that
        ignores SC_HOME (and never writes instance.pid) is still stopped."""
        marker = f" SC_HOME={self.home} "
        listing = subprocess.run(["ps", "-Eww", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [int(line.split(None, 1)[0]) for line in listing.splitlines()
                if "Contents/MacOS/SecondCursor" in line and marker in line + " "]

    def _fail(self, reason):
        logs = "\n".join(f"--- {p.name}:\n{tail(p)}" for p in sorted((self.home / "logs").glob("*.log")) if tail(p))
        hint = "" if self.pid or self._pid_file() else (
            f"\n{self.app} never wrote instance.pid; if it predates SC_HOME support it exits because the user's app "
            "holds the instance lock. Install a current build, or set SC_E2E_APP=build/SecondCursor.app.")
        self.stop()
        raise RuntimeError(f"test instance from {self.app} did not start: {reason}{hint}\n{logs}".rstrip())


class Context:
    def __init__(self, run, name):
        self.run, self.name = run, name
        self.checks = []
        self.cleanups = []
        self.screenshots = []
        # Outside ~/Downloads and other protected folders, so scratch apps never trigger privacy prompts.
        self.scratch = pathlib.Path("/tmp/second-cursor-e2e") / run.dir.name / name
        self.scratch.mkdir(parents=True, exist_ok=True)

    @property
    def mcp(self):
        return self.run.mcp

    def call(self, tool, **args):
        data, images = self.mcp.call(tool, **args)
        for i, image in enumerate(images):
            self.save_image(f"{tool}-{len(self.screenshots) + i}", image)
        return data

    def check(self, label, ok, detail=""):
        self.checks.append({"label": label, "ok": bool(ok), "detail": str(detail)[:500]})
        return bool(ok)

    def require(self, label, ok, detail=""):
        if not self.check(label, ok, detail):
            raise AssertionError(label)

    def save_image(self, stem, image):
        path = self.run.dir / "screenshots" / f"{self.name}-{stem}.png"
        path.write_bytes(base64.b64decode(image["data"]))
        self.screenshots.append(path.relative_to(self.run.dir).as_posix())

    def kill_on_exit(self, pid):
        self.cleanups.append(lambda: os.kill(pid, signal.SIGKILL))

    def wait_for_window(self, app, pid=None, title=None, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self.call("list_windows", app=app)["windows"]:
                if (pid is None or w["pid"] == pid) and (title is None or title in w["title"]):
                    return w
            time.sleep(0.5)
        raise AssertionError(f"no {app} window (pid={pid}, title={title}) within {timeout}s")

    def launch_scratch_textedit(self, filename, text, on_agent_display=True):
        """On the agent display by default: reachable from whatever Space the user is on, and never on their screen."""
        path = self.scratch / filename
        path.write_text(text)
        launched = self.call("launch_app", app="TextEdit", urls=[str(path)], new_instance=True,
                             on_agent_display=on_agent_display)
        pid = launched["app"]["pid"]
        self.kill_on_exit(pid)
        return self.wait_for_window("TextEdit", pid=pid, title=filename)

    def tree(self, window_id, **opts):
        """Always the full tree: scenarios parse indexes from it. Diffs are exercised by tree_diffs."""
        return self.call("get_window_state", window_id=window_id, full=True, **opts)["tree"]


def element_index(tree, role, label=None):
    pattern = rf"\[(\d+)\] {role}" + (rf' "{re.escape(label)}"' if label is not None else "")
    match = re.search(pattern, tree)
    return int(match.group(1)) if match else None


def element_frame(tree, index):
    match = re.search(rf"\[{index}\] .*?\((-?[\d.]+),(-?[\d.]+) ([\d.]+)x([\d.]+)\)", tree)
    return tuple(float(v) for v in match.groups()) if match else None


def inside(frame, bounds):
    return (bounds["x"] <= frame["x"] < bounds["x"] + bounds["width"]
            and bounds["y"] <= frame["y"] < bounds["y"] + bounds["height"])


SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


BROWSER_TOOLS = re.compile(r"""["']browser_\w+["']""")


def uses_browser(fn):
    """Scenarios whose source names a browser_* tool get the isolated test browser started for them."""
    try:
        return bool(BROWSER_TOOLS.search(inspect.getsource(fn)))
    except (OSError, TypeError):
        return False


@scenario
def server_auth(ctx):
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}
    status, _, _ = ctx.mcp.raw(init, token=None)
    ctx.check("request without token is rejected with 401", status == 401, f"got {status}")
    status, _, _ = ctx.mcp.raw(init, token="wrong-token")
    ctx.check("request with wrong token is rejected with 401", status == 401, f"got {status}")
    names = {t["name"] for t in ctx.mcp.rpc("tools/list")["tools"]}
    ctx.check("tools/list exposes the full tool contract", EXPECTED_TOOLS <= names, f"missing {sorted(EXPECTED_TOOLS - names)}")


@scenario
def status(ctx):
    data = ctx.call("status")
    ctx.check("Accessibility permission granted", data["permissions"]["accessibility"], data["permissions"])
    ctx.check("Screen Recording permission granted", data["permissions"]["screen_recording"], data["permissions"])
    ctx.check("not paused", data["paused"] is False, data)


@scenario
def blocked_apps(ctx):
    try:
        ctx.call("launch_app", app="Keychain Access")
        ctx.check("launching a blocked app is refused", False, "launch succeeded")
    except ToolError as e:
        ctx.check("launching a blocked app is refused", re.search(r"block|not allowed", str(e), re.I), e)
    running = [a for a in ctx.call("list_apps")["apps"] if a.get("bundle_id") == "com.apple.keychainaccess" and a.get("is_running")]
    ctx.check("blocked app was not started", not running, running)


@scenario
def textedit_background(ctx):
    if ON_SCREEN:
        window = ctx.launch_scratch_textedit("notes.txt", "alpha", on_agent_display=False)
        ctx.check("new window stays where the app put it", not window.get("on_agent_display"), window)
    if not ON_SCREEN or not window["is_on_screen"]:
        # The editing checks run where every Space can reach them and the user never sees them.
        window = ctx.launch_scratch_textedit("notes-hosted.txt", "alpha")
    wid = window["id"]

    tree = ctx.tree(wid)
    area = element_index(tree, "AXTextArea")
    ctx.require("snapshot exposes the text area with an index", area is not None, tree[:400])
    ctx.check("snapshot shows the file contents", "alpha" in tree, tree[:400])

    ctx.call("set_value", window_id=wid, element_index=area, value="alpha\nbeta")
    ctx.check("set_value replaced the contents", "beta" in ctx.tree(wid, include_screenshot=False))

    area = element_index(ctx.tree(wid, include_screenshot=False), "AXTextArea")
    ctx.call("type_text", window_id=wid, text=" gamma", element_index=area)
    ctx.check("type_text inserted text", "gamma" in ctx.tree(wid, include_screenshot=False))

    pressed = ctx.call("press_key", window_id=wid, keys="cmd+a")
    ctx.check("cmd+a pressed the Select All menu item and the app confirmed it",
              pressed.get("route") == "ax_menu_shortcut" and pressed.get("verified") is True, pressed)
    ctx.call("type_text", window_id=wid, text="replaced")
    tree = ctx.tree(wid)
    ctx.check("cmd+a chord selected all, typing replaced the selection", 'value="replaced"' in tree, tree[:400])

    shot = ctx.call("screenshot", window_id=wid)
    ctx.check("screenshot returned an image", shot.get("width", 0) > 0 and ctx.screenshots, shot)


@scenario
def calculator(ctx):
    launched = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    pid = launched["app"]["pid"]
    ctx.kill_on_exit(pid)
    wid = ctx.wait_for_window("Calculator", pid=pid)["id"]
    tree = ctx.tree(wid, include_screenshot=False)
    for label in ["All Clear", "7", "Multiply", "6", "Equals"]:
        index = element_index(tree, "AXButton", label)
        if index is None and label == "All Clear":
            continue
        ctx.require(f"button {label!r} is indexed", index is not None, tree[:400])
        ctx.call("click", window_id=wid, element_index=index)
    tree = ctx.tree(wid)
    ctx.check("7 x 6 shows 42", re.search(r"\b42\b", tree), tree[:600])


def text_area(ctx, wid):
    tree = ctx.tree(wid, include_screenshot=False)
    area = element_index(tree, "AXTextArea")
    frame = element_frame(tree, area) if area is not None else None
    ctx.require("text area and its frame are reported", frame is not None, tree[:400])
    return area, frame


@scenario
def pixel_click_in_place(ctx):
    window = ctx.launch_scratch_textedit("pixels.txt", "line one\nline two")
    wid = window["id"]
    _, frame = text_area(ctx, wid)
    # The caret starts at the beginning; park it at the end so a click that does nothing cannot pass.
    ctx.call("press_key", window_id=wid, keys="cmd+down")
    ctx.call("click", window_id=wid, x=frame[0] + 3, y=frame[1] + 6)
    ctx.call("type_text", window_id=wid, text="Z")
    tree = ctx.tree(wid)
    ctx.check("pixel click moved the caret to the start of line one", 'value="Zline one' in tree, tree[:400])


@scenario
def pixel_double_click_selects_word(ctx):
    """One click delivered twice (or a double-click whose presses arrive out of step) never selects the word."""
    window = ctx.launch_scratch_textedit("double.txt", "beta gamma\nline two")
    wid = window["id"]
    _, frame = text_area(ctx, wid)
    ctx.call("click", window_id=wid, x=frame[0] + 14, y=frame[1] + 7, count=2)
    ctx.call("type_text", window_id=wid, text="Z")
    tree = ctx.tree(wid)
    ctx.check("a pixel double-click inside \"beta\" selected the word, and typing replaced it",
              'value="Z gamma\\nline two"' in tree, tree[:400])


@scenario
def drag_select(ctx):
    window = ctx.launch_scratch_textedit("drag.txt", "line one\nline two")
    wid = window["id"]
    _, frame = text_area(ctx, wid)
    y = frame[1] + 6
    ctx.call("drag", window_id=wid, from_x=frame[0] + 3, from_y=y, to_x=min(frame[0] + frame[2] - 10, 400), to_y=y)
    ctx.call("type_text", window_id=wid, text="LINE 1")
    tree = ctx.tree(wid)
    ctx.check("drag selected line one and typing replaced it", 'value="LINE 1\\nline two"' in tree, tree[:400])


@scenario
def parallel_apps(ctx):
    notes = ctx.launch_scratch_textedit("parallel.txt", "")
    calc = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    calc_wid = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])["id"]
    area, _ = text_area(ctx, notes["id"])
    calc_tree = ctx.tree(calc_wid, include_screenshot=False)
    buttons = [element_index(calc_tree, "AXButton", label) for label in ["7", "Multiply", "6", "Equals"]]
    ctx.require("calculator buttons are indexed", None not in buttons, calc_tree[:300])

    errors = []

    def write_notes():
        try:
            for word in ["one", " two", " three", " four"]:
                ctx.mcp.call("type_text", window_id=notes["id"], text=word, element_index=area)
        except Exception as e:
            errors.append(f"notes: {e}")

    def compute():
        try:
            for index in buttons:
                ctx.mcp.call("click", window_id=calc_wid, element_index=index)
        except Exception as e:
            errors.append(f"calculator: {e}")

    started = time.time()
    threads = [threading.Thread(target=write_notes), threading.Thread(target=compute)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=60)
    ctx.check(f"both agents finished without errors ({time.time() - started:.1f}s wall time)", not errors, errors)
    ctx.check("TextEdit agent's text arrived in order", 'value="one two three four"' in ctx.tree(notes["id"], include_screenshot=False))
    ctx.check("Calculator agent computed 7 x 6 = 42", re.search(r"\b42\b", ctx.tree(calc_wid)))


def step(tool, **args):
    return {"tool": tool, "args": args}


def value_of(tree, role="AXTextArea"):
    """Reads a full tree or a diff; a diff's '-' lines are state that no longer exists."""
    current = "\n".join(line for line in tree.splitlines() if not line.startswith("-"))
    match = re.search(rf'\] {role} "[^"]*" value="((?:[^"\\]|\\.)*)"', current)
    return json.loads(f'"{match.group(1)}"') if match else None


def clear_calculator(ctx, wid):
    """Calculator restores its last result on launch, and after earlier runs that is 7 x 6 = 42: start from 0 so
    a 42 afterwards proves the new input."""
    tree = ctx.tree(wid, include_screenshot=False)
    clear = element_index(tree, "AXButton", "All Clear")
    if clear is None:
        clear = element_index(tree, "AXButton", "Clear")
    if clear is not None:
        ctx.call("click", window_id=wid, element_index=clear, observe="none")
        tree = ctx.tree(wid, include_screenshot=False)
    ctx.require("Calculator starts from 0", re.search(r'"Edit field" value="0"', tree), tree[:400])
    return tree


@scenario
def batch_calculator(ctx):
    calc = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    wid = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])["id"]
    tree = clear_calculator(ctx, wid)
    labels = ["7", "Multiply", "6", "Equals"]
    indexes = [element_index(tree, "AXButton", label) for label in labels]
    ctx.require("calculator buttons are indexed", None not in indexes, tree[:300])

    calls_before = ctx.run.tool_calls()
    result = ctx.call("batch", window_id=wid, steps=[step("click", element_index=i) for i in indexes])
    ctx.check("the whole sequence took one tool call", ctx.run.tool_calls() - calls_before == 1)
    ctx.check("all four steps succeeded, in order",
              [s["tool"] for s in result["steps"]] == ["click"] * 4 and all(s["ok"] for s in result["steps"]), result["steps"])
    observation = result.get("observation") or {}
    ctx.check("the single returned observation shows 42", re.search(r"\b42\b", observation.get("tree", "")), observation.get("tree", "")[:400])


@scenario
def batch_textedit(ctx):
    window = ctx.launch_scratch_textedit("batch.txt", "alpha")
    wid = window["id"]
    area, _ = text_area(ctx, wid)
    log_before = len(ACTION_LOG.read_text().splitlines()) if ACTION_LOG.exists() else 0
    result = ctx.call("batch", window_id=wid, steps=[
        step("set_value", element_index=area, value="alpha\nbeta"),
        step("type_text", element_index=area, text=" gamma"),
        step("press_key", keys="cmd+a"),
        step("type_text", text="replaced"),
    ])
    ctx.check("all steps succeeded", all(s["ok"] for s in result["steps"]), result["steps"])
    tree = (result.get("observation") or {}).get("tree", "")
    ctx.check('observation ends with value="replaced"', value_of(tree) == "replaced", tree[:400])
    new_lines = [json.loads(l) for l in ACTION_LOG.read_text().splitlines()[log_before:]]
    step_lines = [l for l in new_lines if l["tool"] in {"set_value", "type_text", "press_key"}]
    ctx.check("each step got its own action-log line", len(step_lines) >= 4, [l["tool"] for l in new_lines])


@scenario
def batch_order_and_errors(ctx):
    window = ctx.launch_scratch_textedit("order.txt", "")
    wid = window["id"]
    area, _ = text_area(ctx, wid)

    # Any reordering of these steps yields a different string than "aXYb".
    result = ctx.call("batch", window_id=wid, steps=[
        step("set_value", element_index=area, value=""),
        step("type_text", element_index=area, text="ab"),
        step("press_key", keys="left"),
        step("wait", ms=100),
        step("type_text", text="X"),
        step("type_text", text="Y"),
    ])
    tree = (result.get("observation") or {}).get("tree", "")
    ctx.check("order-sensitive steps produced exactly aXYb", value_of(tree) == "aXYb", tree[:300])

    result = ctx.call("batch", window_id=wid, stop_on_error=True, observe="tree", steps=[
        step("type_text", text="1"),
        step("click", element_index=99999),
        step("type_text", text="2"),
    ])
    oks = [s["ok"] for s in result["steps"]]
    ctx.check("stop_on_error: failure reported and later steps skipped", oks == [True, False, False] and result["steps"][2].get("skipped"), result["steps"])
    ctx.check("stop_on_error: nothing after the failure ran", value_of((result.get("observation") or {}).get("tree", "")) == "aXY1b")

    result = ctx.call("batch", window_id=wid, stop_on_error=False, observe="tree", steps=[
        step("type_text", text="3"),
        step("click", element_index=99999),
        step("type_text", text="4"),
    ])
    ctx.check("stop_on_error=false: continues past the failure", [s["ok"] for s in result["steps"]] == [True, False, True], result["steps"])
    ctx.check("stop_on_error=false: later steps ran", value_of((result.get("observation") or {}).get("tree", "")) == "aXY134b")

    result = ctx.call("batch", window_id=wid, observe="none", steps=[step("wait", ms=10)])
    ctx.check('observe="none" returns no observation', result.get("observation") is None, result)


@scenario
def nested_argument_shapes(ctx):
    """Gemini through Cursor stringifies free-form nested objects; "[object Object]" is what it sent once."""
    window = ctx.launch_scratch_textedit("shapes.txt", "")
    wid = window["id"]
    area, _ = text_area(ctx, wid)
    result = ctx.call("batch", window_id=wid, steps=[
        {"tool": "set_value", "args": json.dumps({"element_index": area, "value": "a"})},
        {"tool": "type_text", "element_index": area, "text": "b"},
        step("type_text", text="c"),
    ])
    ctx.check("a JSON-string args, args beside tool, and an args object all ran",
              [s["ok"] for s in result["steps"]] == [True] * 3, result["steps"])
    tree = (result.get("observation") or {}).get("tree", "")
    ctx.check('the steps ran in order: value="abc"', value_of(tree) == "abc", tree[:300])

    error = refused(ctx, "batch", window_id=wid, steps=[{"tool": "click", "args": "[object Object]"}])
    ctx.check("an unparseable args string is refused by name, not as a missing element_index",
              error and "args must be an object" in error and "[object Object]" in error, error)
    # What Gemini sent when step args were a declared nested object.
    error = refused(ctx, "batch", window_id=wid, steps=[{"tool": "click", "argsFOURTH_args_element_index": 5}])
    ctx.check("a missing argument names the fields the step did not recognize",
              error and "unrecognized fields: argsFOURTH_args_element_index" in error, error)
    items = next(t for t in ctx.mcp.rpc("tools/list")["tools"] if t["name"] == "batch")["inputSchema"]["properties"]["steps"]["items"]
    ctx.check("batch advertises flat steps: tool and the step's own arguments, no nested args",
              {"tool", "element_index", "keys", "text"} <= set(items["properties"]) and "args" not in items["properties"],
              sorted(items["properties"]))
    error = refused(ctx, "browser_command", operation="click", arguments="[object Object]")
    ctx.check("browser_command refuses an unparseable arguments string by name",
              error and "arguments must be an object" in error, error)


def png_pixels(data):
    """Minimal PNG decoder (8-bit RGB/RGBA, non-interlaced), enough to inspect screenshots without PIL."""
    import struct, zlib
    pos, idat = 8, b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, _, color_type = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    bpp = 4 if color_type == 6 else 3
    raw, stride, rows, prev, i = zlib.decompress(idat), width * bpp, [], bytearray(width * bpp), 0
    for _ in range(height):
        kind, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            if kind == 1: line[x] = (line[x] + a) & 255
            elif kind == 2: line[x] = (line[x] + b) & 255
            elif kind == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                line[x] = (line[x] + (a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else b if abs(p - b) <= abs(p - c) else c)) & 255
        rows.append(bytes(line))
        prev = line
    return [(row[x * bpp], row[x * bpp + 1], row[x * bpp + 2]) for row in rows for x in range(width)]


def accent_pixels(pixels):
    return sum(1 for r, g, b in pixels if r > 200 and 40 < g < 130 and b < 70)


def glow_pixels(pixels):
    """Pixels tinted by the agent cursor's orange glow. The arrow itself is near black and white, so it is found by
    its glow, which blends toward orange over light and dark backgrounds alike."""
    return sum(1 for r, g, b in pixels if r > 90 and r - b > 60 and r - g > 25)


@scenario
def tree_diffs(ctx):
    window = ctx.launch_scratch_textedit("diff.txt", "alpha")
    wid = window["id"]
    full = ctx.call("get_window_state", window_id=wid, include_screenshot=False, full=True)
    ctx.check("first full snapshot is not a diff", not full.get("is_diff"), full.get("is_diff"))
    area = element_index(full["tree"], "AXTextArea")
    ctx.require("text area is indexed in the full tree", area is not None, full["tree"][:300])
    base_id = full["snapshot_id"]
    full_len = len(full["tree"])

    acted = ctx.call("set_value", window_id=wid, element_index=area, value="alpha beta gamma")
    diff = acted.get("observation") or {}
    ctx.check("the set_value result itself carries a diff observation", diff.get("is_diff"), acted)
    ctx.check("action result names the window it acted on", acted.get("window_id") == wid, acted)
    ctx.check("diff names the snapshot it is based on", diff.get("diff_base") == base_id, diff.get("diff_base"))
    ctx.check("diff shows the changed value as an added line", re.search(r'^\+.*value="alpha beta gamma"', diff.get("tree", ""), re.M), diff.get("tree", "")[:400])
    ctx.check("diff is much smaller than the full tree", len(diff.get("tree", "")) < full_len, (len(diff.get("tree", "")), full_len))
    after = ctx.call("get_window_state", window_id=wid, include_screenshot=False)
    ctx.check("the action advanced the baseline: the next read reports no changes",
              after.get("is_diff") and "no changes" in after["tree"].lower(), after["tree"][:300])

    # Stable indexes: the index from the first full snapshot still addresses the same element.
    reused = ctx.call("set_value", window_id=wid, element_index=area, value="stable")
    ctx.check("element index stayed stable across snapshots", reused.get("ok"), reused)
    ctx.check("acting on it changed the value", value_of(ctx.tree(wid, include_screenshot=False)) == "stable")

    nochange = ctx.call("get_window_state", window_id=wid, include_screenshot=False)
    ctx.check("a snapshot with no UI change reports no changes", "no changes" in nochange["tree"].lower(), nochange["tree"][:200])

    again_full = ctx.call("get_window_state", window_id=wid, include_screenshot=False, full=True)
    ctx.check("full=true returns the whole tree again", not again_full.get("is_diff") and "AXTextArea" in again_full["tree"])


def shown(tree):
    """What a full tree or a diff says is on screen now: a diff's '-' lines are gone."""
    return "\n".join(line for line in tree.splitlines() if not line.startswith("-"))


def refused(ctx, tool, **args):
    """The error text when the call is refused, else None."""
    try:
        ctx.call(tool, **args)
    except ToolError as e:
        return str(e)
    return None


@scenario
def action_observations(ctx):
    tools = {t["name"]: t["inputSchema"]["properties"] for t in ctx.mcp.rpc("tools/list")["tools"]}
    ctx.check("observe is advertised on native actions only",
              all("observe" in tools[n] for n in ["click", "type_text", "set_value", "press_key", "select_text", "paste"])
              and not any("observe" in tools[n] for n in ["get_window_state", "screenshot", "browser_click"]))

    calc = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    wid = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])["id"]
    first = ctx.call("get_window_state", window_id=wid, full=True)
    buttons = {label: element_index(first["tree"], "AXButton", label) for label in ["7", "8", "9"]}
    ctx.require("digit buttons are indexed", None not in buttons.values(), first["tree"][:400])

    pressed = ctx.call("click", window_id=wid, element_index=buttons["7"])
    seen = pressed.get("observation") or {}
    tree = seen.get("tree", "")
    ctx.check("click returns an observation without a second call", seen.get("snapshot_id"), pressed)
    ctx.check("the observation shows the display reading 7", 'value="7"' in shown(tree), tree[:400])
    ctx.check("it is a diff against the previous read",
              seen.get("is_diff") and seen.get("diff_base") == first["snapshot_id"], {k: seen.get(k) for k in ("is_diff", "diff_base")})
    ctx.check("no screenshot unless asked", "screenshot" not in seen and not ctx.screenshots, list(seen))

    # The action's snapshot is now the window's current observation, so its id admits the next action.
    blind = ctx.call("click", window_id=wid, element_index=buttons["7"], observe="none", observation_id=seen.get("snapshot_id"))
    ctx.check('observe="none" returns no observation key', blind.get("ok") and "observation" not in blind, blind)

    data, images = ctx.mcp.call("click", window_id=wid, element_index=buttons["8"], observe="both",
                                observation_id=seen.get("snapshot_id"))
    for i, image in enumerate(images):
        ctx.save_image(f"observe-both-{i}", image)
    both = data.get("observation") or {}
    ctx.check('observe="both" attaches one image', len(images) == 1 and both.get("screenshot", {}).get("width", 0) > 0,
              (len(images), both.get("screenshot")))
    ctx.check('observe="both" still returns the tree (7, 7, 8 reads 778)', 'value="778"' in shown(both.get("tree", "")),
              both.get("tree", "")[:400])

    stale = refused(ctx, "click", window_id=wid, element_index=buttons["9"], observation_id=first["snapshot_id"])
    ctx.check("an observation_id older than the last action's observation is refused", stale and "stale" in stale.lower(), stale)
    bad = refused(ctx, "click", window_id=wid, element_index=buttons["9"], observe="everything")
    ctx.check("an invalid observe value is refused before acting", bad and "observe" in bad, bad)
    after = ctx.call("get_window_state", window_id=wid, full=True)["tree"]
    ctx.check("the refused calls did not press 9", 'value="778"' in after, after[:400])


@scenario
def orientation_lines(ctx):
    long_run = "x" * 300
    window = ctx.launch_scratch_textedit("orientation.txt", f"hello brave new world\n{long_run}\nend")
    wid = window["id"]
    full = ctx.call("get_window_state", window_id=wid, full=True)
    area = element_index(full["tree"], "AXTextArea")
    ctx.require("text area is indexed", area is not None, full["tree"][:300])

    selected = ctx.call("select_text", window_id=wid, element_index=area, text="brave")
    diff = (selected.get("observation") or {}).get("tree", "")
    ctx.check('the select_text result reports selected text: "brave"', re.search(r'^\+?selected text: "brave"$', diff, re.M), diff[-400:])
    tree = ctx.call("get_window_state", window_id=wid, full=True)["tree"]
    ctx.check("a full read names the focused text area by index", re.search(rf'^focused: \[{area}\] AXTextArea ', tree, re.M), tree[-400:])
    ctx.check("a full read ends with the selected text", tree.splitlines()[-1] == 'selected text: "brave"', tree[-400:])

    moved = ctx.call("select_text", window_id=wid, element_index=area, text="world")
    diff = (moved.get("observation") or {}).get("tree", "")
    ctx.check("a selection change shows up as a -/+ pair",
              re.search(r'^-selected text: "brave"$', diff, re.M) and re.search(r'^\+selected text: "world"$', diff, re.M), diff[:400])
    ctx.check("an unchanged focus is not repeated in the diff", "focused:" not in diff, diff[:400])

    long = ctx.call("select_text", window_id=wid, element_index=area, text=long_run)
    diff = (long.get("observation") or {}).get("tree", "")
    ctx.check("selected text is cut at 200 characters", f'+selected text: "{"x" * 200}…"' in diff, diff[:400])

    caret = ctx.call("select_text", window_id=wid, element_index=area, text="end", selection_type="cursor_after")
    diff = (caret.get("observation") or {}).get("tree", "")
    ctx.check("an empty selection removes the selected text line", "selected text:" not in shown(diff) and "-selected text:" in diff, diff[:400])

    scoped = ctx.call("get_window_state", window_id=wid, scope=area)["tree"]
    ctx.check("scoped reads carry no orientation lines", "focused:" not in scoped and "selected text:" not in scoped, scoped[-300:])


@scenario
def app_target(ctx):
    tools = {t["name"]: t["inputSchema"] for t in ctx.mcp.rpc("tools/list")["tools"]}
    ctx.check("window tools advertise app and no longer require window_id",
              all("app" in tools[n]["properties"] and "window_id" not in tools[n].get("required", [])
                  for n in ["get_window_state", "click", "press_key", "scroll", "screenshot", "batch"]))
    ctx.check("Codex argument aliases stay unadvertised",
              "click_count" not in tools["click"]["properties"] and "key" not in tools["press_key"]["properties"])

    title = f"app-target-{int(time.time())}.txt"
    window = ctx.launch_scratch_textedit(title, "line")
    wid, pid = window["id"], window["pid"]
    windows = ctx.call("list_windows", app="TextEdit")["windows"]
    others = [w for w in windows if w["pid"] != pid]
    front = observe()["frontmost"]
    user_front = any(w["pid"] == front["pid"] for w in others)

    # Several TextEdit processes: the frontmost one wins, else the most recently launched (this scratch one).
    try:
        got, error = ctx.call("get_window_state", app="TextEdit")["window"], None
    except ToolError as e:
        got, error = None, str(e)
    if not user_front:
        ctx.check(f"app=TextEdit resolves to the scratch window ({len(others)} other TextEdit windows)",
                  got and got["id"] == wid, got or error)
    else:
        ctx.check("the user's frontmost TextEdit wins: its main window, or an error listing candidate window ids",
                  (got and got["pid"] == front["pid"])
                  or (error and "window_id" in error and any(str(w["id"]) in error for w in windows)), got or error)
    if not others:
        pressed = ctx.call("press_key", app="TextEdit", keys="right")
        ctx.check("an action addressed by app reports the resolved window_id", pressed.get("window_id") == wid, pressed)

    missing = refused(ctx, "get_window_state", app="NoSuchApp-SecondCursorE2E")
    ctx.check("an app without windows is refused with a launch_app hint", missing and "launch_app" in missing, missing)
    neither = refused(ctx, "press_key", keys="right")
    ctx.check("no window_id and no app is refused", neither and "window_id" in neither, neither)

    # Aliases are exercised on window_id so nothing here can reach a user's TextEdit window.
    tree = ctx.tree(wid)
    area = element_index(tree, "AXTextArea")
    frame = element_frame(tree, area) if area is not None else None
    ctx.require("text area and its frame are reported", frame is not None, tree[:400])
    ctx.call("select_text", window_id=wid, element_index=area, text="line", selection_type="cursor_after", observe="none")
    ctx.call("press_key", window_id=wid, key="Return", observe="none")
    ctx.call("type_text", window_id=wid, text="next", observe="none")
    value = value_of(ctx.tree(wid))
    ctx.check('press_key accepts key="Return"', value == "line\nnext", value)

    ctx.call("click", window_id=wid, x=frame[0] + 12, y=frame[1] + 6, click_count=2, mouse_button="left", observe="none")
    ctx.call("type_text", window_id=wid, text="WORD", observe="none")
    value = value_of(ctx.tree(wid))
    ctx.check("click_count=2 double-clicked (the word was selected and replaced)", value == "WORD\nnext", value)
    bad_button = refused(ctx, "click", window_id=wid, element_index=area, mouse_button="sideways")
    ctx.check("mouse_button is read as button", bad_button and "button" in bad_button, bad_button)
    bad_pages = refused(ctx, "scroll", window_id=wid, direction="down", pages="lots")
    ctx.check("pages is read as the scroll amount", bad_pages and "pages" in bad_pages, bad_pages)
    ctx.check("pages=1 scrolls", ctx.call("scroll", window_id=wid, element_index=area, direction="down", pages=1).get("ok"))


def untitled_window(ctx, window):
    """A second window in the scratch window's own TextEdit process. Each is read once while it is the app's main
    window: on an inactive Space, accessibility only lists the main window until something has touched the others."""
    ctx.call("get_window_state", window_id=window["id"])
    ctx.call("press_key", window_id=window["id"], keys="cmd+n", observe="none")
    untitled = ctx.wait_for_window("TextEdit", pid=window["pid"], title="Untitled")
    ctx.call("get_window_state", window_id=untitled["id"])
    return untitled


@scenario
def app_target_pins_observed_window(ctx):
    """Actions and element reads addressed by app name go to the window this session last observed, not to whichever
    window is the app's main window when the call arrives."""
    first = ctx.launch_scratch_textedit(f"pin-{int(time.time())}.txt", "one")
    pid = first["pid"]
    windows = {first["id"]: "one", untitled_window(ctx, first)["id"]: ""}
    one, two = windows
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))

    # Refused before anything is sent, so these can never reach a user's own TextEdit window.
    for tool, extra in [("press_key", {"keys": "right"}), ("click", {"x": 5, "y": 5}), ("inspect_element", {"element_index": 1})]:
        error = refused(ctx, tool, app="TextEdit", session_id=sid, **extra)
        ctx.check(f"{tool} by app before this session observed the app is refused",
                  error and "observe the app first" in error, error)

    for wid in (one, two, one):
        tree = ctx.call("get_window_state", window_id=wid, session_id=sid, full=True)["tree"]
        area = element_index(tree, "AXTextArea")
        ctx.require("text area is indexed", area is not None, tree[:300])
        pressed = ctx.call("press_key", app="TextEdit", session_id=sid, keys="right", observe="none")
        ctx.check(f"press_key by app acts on the window just observed ({wid})", pressed.get("window_id") == wid, pressed)
        clicked = ctx.call("click", app="TextEdit", session_id=sid, element_index=area, observe="none")
        ctx.check(f"click by app with an element index acts on that window ({wid})", clicked.get("window_id") == wid, clicked)
        text = ctx.call("inspect_element", app="TextEdit", session_id=sid, element_index=area)["text"]
        ctx.check(f"inspect_element by app reads that window's element ({wid})", text == windows[wid], text)
        scoped = ctx.call("get_window_state", app="TextEdit", session_id=sid, scope=area)["window"]
        ctx.check(f"a scoped read by app stays on that window ({wid})", scoped["id"] == wid, scoped)

    request = urllib.request.Request(
        ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=json.dumps({"session_id": sid}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {ctx.mcp.token}"})
    urllib.request.urlopen(request, timeout=30).read()
    error = refused(ctx, "press_key", app="TextEdit", session_id=sid, keys="right")
    ctx.check("after the turn ends the app must be observed again", error and "observe the app first" in error, error)

    ctx.call("get_window_state", window_id=two, session_id=sid)
    os.kill(pid, signal.SIGKILL)
    deadline = time.time() + 10
    while any(w["pid"] == pid for w in ctx.call("list_windows", app="TextEdit")["windows"]) and time.time() < deadline:
        time.sleep(0.3)
    error = refused(ctx, "press_key", app="TextEdit", session_id=sid, keys="right")
    ctx.check("once the observed window is gone, acting by app is refused and says so",
              error and "gone" in error and str(two) in error, error)


@scenario
def paste_restores_clipboard(ctx):
    wid = ctx.launch_scratch_textedit("paste.txt", "")["id"]
    text_area(ctx, wid)
    before = subprocess.run(["pbpaste"], capture_output=True).stdout
    pasted = ctx.call("paste", window_id=wid, text="pasted text", format="text")
    after = subprocess.run(["pbpaste"], capture_output=True).stdout
    ctx.check("paste reports the app confirmed the menu press", pasted.get("verified") is True, pasted)
    ctx.check("the user's clipboard is back once the paste is confirmed", before == after)
    ctx.check("the text arrived", value_of(ctx.tree(wid)) == "pasted text")


@scenario
def structured_opt_in(ctx):
    window = ctx.launch_scratch_textedit("structured.txt", "structured")
    wid = window["id"]
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    first = (ctx.call("press_key", window_id=wid, session_id=sid, keys="right").get("observation") or {})
    ctx.check("an action with no earlier read returns the full tree", first.get("tree") and not first.get("is_diff"),
              {k: first.get(k) for k in ("is_diff", "snapshot_id")})
    ctx.check("action observations never carry elements", "elements" not in first, list(first))

    plain = ctx.call("get_window_state", window_id=wid, full=True)
    ctx.check("a full read without structured has no elements", "elements" not in plain, list(plain))
    rich = ctx.call("get_window_state", window_id=wid, full=True, structured=True)
    area = element_index(rich["tree"], "AXTextArea")
    ctx.check("structured=true adds the elements array", any(e["index"] == area for e in rich.get("elements", [])), list(rich))
    diff = ctx.call("get_window_state", window_id=wid, structured=True)
    ctx.check("a diff never carries elements, even with structured=true", diff.get("is_diff") and "elements" not in diff, list(diff))
    scoped = ctx.call("get_window_state", window_id=wid, scope=area, structured=True)
    ctx.check("a scoped read honors structured=true", any(e["index"] == area for e in scoped.get("elements", [])), list(scoped))
    bare = ctx.call("get_window_state", window_id=wid, scope=area)
    ctx.check("a scoped read without structured has no elements", "elements" not in bare, list(bare))


@scenario
def scope_reads(ctx):
    window = ctx.launch_scratch_textedit("scope.txt", "scoped")
    wid = window["id"]
    full = ctx.call("get_window_state", window_id=wid, include_screenshot=False, full=True)
    ctx.check("full tree includes the window control buttons", full["tree"].count("AXButton") >= 2, full["tree"][:300])
    area = element_index(full["tree"], "AXTextArea")
    ctx.require("text area is indexed", area is not None, full["tree"][:300])

    scoped = ctx.call("get_window_state", window_id=wid, include_screenshot=False, scope=area)
    ctx.check("scoped read is rooted at the requested element", "AXTextArea" in scoped["tree"], scoped["tree"][:300])
    ctx.check("scoped read excludes elements outside the subtree", "AXButton" not in scoped["tree"], scoped["tree"][:300])
    ctx.check("scoped read is not a diff", not scoped.get("is_diff"), scoped)
    ctx.check("the scoped element index still acts", ctx.call("set_value", window_id=wid, element_index=area, value="ok").get("ok"))


def agent_origin(ctx, dx=40, dy=60):
    """A point on the agent display, as "x,y" for a fixture's --origin: a window opened there never shows on the user's
    screen and is reachable from whatever Space they are on."""
    bounds = ctx.call("agent_display", action="create")["bounds"]
    return f"{int(bounds['x'] + dx)},{int(bounds['y'] + dy)}"


def links_document(ctx, name, count=200, on_agent_display=True):
    """A TextEdit window whose text area holds `count` links: that many indexed elements, all of which move on a scroll."""
    path = ctx.scratch / name
    path.write_text("<html><body>" + "".join(
        f'<p>Paragraph {i} <a href="https://example.com/{i}">link {i}</a> trailing text</p>' for i in range(count))
        + "</body></html>")
    launched = ctx.call("launch_app", app="TextEdit", urls=[str(path)], new_instance=True,
                        on_agent_display=on_agent_display)
    ctx.kill_on_exit(launched["app"]["pid"])
    return ctx.wait_for_window("TextEdit", pid=launched["app"]["pid"], title=name)


MOVED_LINE = re.compile(r"^~ (\d+) elements moved \(scroll or resize\); frames above are current", re.M)


@scenario
def scroll_moved_diff(ctx):
    """A scroll shifts every frame; the observation must stay a compact diff instead of falling back to the full tree."""
    wid = links_document(ctx, "scroll.html")["id"]
    full = ctx.call("get_window_state", window_id=wid, full=True)
    link = element_index(full["tree"], "AXLink", "link 0")
    ctx.require("the 200 links are indexed", link is not None and full["element_count"] > 200, full["tree"][:300])
    before = element_frame(full["tree"], link)

    seen = ctx.call("scroll", window_id=wid, direction="down", amount=5).get("observation") or {}
    tree = seen.get("tree", "")
    moved = MOVED_LINE.search(tree)
    ctx.check("the scroll's observation is a diff, not the full tree", seen.get("is_diff"),
              {k: seen.get(k) for k in ("is_diff", "element_count")})
    ctx.check("one ~ line counts the elements that only moved", moved and int(moved.group(1)) >= 100, tree[-400:])
    ctx.check("moved links are not resent as -/+ lines", not re.search(r"^[-+].*AXLink", tree, re.M), tree[:400])
    ctx.check("a frames-only change is just the ~ line", moved and len(tree.splitlines()) == 1, tree[:400])
    ctx.check("the diff is under 5% of the full tree", len(tree) < len(full["tree"]) / 20, (len(tree), len(full["tree"])))

    after = ctx.call("get_window_state", window_id=wid)
    ctx.check("the scroll advanced the baseline: the next read reports no changes",
              after.get("is_diff") and after["tree"] == "(no changes)", after["tree"][:300])
    now = ctx.call("get_window_state", window_id=wid, full=True)["tree"]
    ctx.check("link 0 kept its index across the scroll", element_index(now, "AXLink", "link 0") == link, now[:300])
    moved_to = element_frame(now, link)
    ctx.check("a full read reports the moved frame", moved_to and before and moved_to[1] < before[1], (before, moved_to))

    bold = element_index(now, "AXCheckBox", "bold")
    ctx.require("the bold checkbox is indexed", bold is not None, now[-600:])
    result = ctx.call("batch", window_id=wid, steps=[step("scroll", direction="up", amount=5), step("click", element_index=bold)])
    tree = (result.get("observation") or {}).get("tree", "")
    changed = re.search(rf'^\+\s*\[{bold}\] AXCheckBox "bold" value="1" \(\d+,\d+ \d+x\d+\)', tree, re.M)
    ctx.check("a content change next to a scroll is printed with its current frame", changed, tree[:400])
    ctx.check("and the moved elements are still summed up on the ~ line, last", MOVED_LINE.search(tree.splitlines()[-1] if tree else ""), tree[-400:])


@scenario
def element_limit(ctx):
    wid = links_document(ctx, "limit.html")["id"]
    whole = ctx.call("get_window_state", window_id=wid, full=True)
    ctx.require("the whole window fits the default limit of 400", not whole["truncated"] and whole["element_count"] > 200,
                {k: whole[k] for k in ("truncated", "element_count")})
    ctx.check("an untruncated outline has no truncation footer", "truncated at" not in whole["tree"], whole["tree"][-300:])

    cut = ctx.call("get_window_state", window_id=wid, full=True, max_elements=60)
    footer = cut["tree"].splitlines()[-1]
    ctx.check("max_elements=60 lists exactly 60 elements", cut["element_count"] == 60 and cut["truncated"],
              {k: cut[k] for k in ("truncated", "element_count")})
    ctx.check("the outline ends with a truncation footer", footer.startswith("… truncated at 60 elements; not shown: "), footer)
    ctx.check('the footer names the omitted "Formatting" group', re.search(r'"Formatting" \(\d+ elements\)', footer), footer)
    ctx.check("the footer names the text area whose links were cut", re.search(r"\[\d+\] AXTextArea \(1\d\d elements\)", footer), footer)
    ctx.check("the footer says how to see the rest", "max_elements" in footer and "scope" in footer, footer)

    low = ctx.call("get_window_state", window_id=wid, full=True, max_elements=5)
    ctx.check("max_elements is clamped to at least 50", low["element_count"] == 50, low["element_count"])
    sticky = ctx.call("get_window_state", window_id=wid, full=True)
    ctx.check("later reads of the window keep the last max_elements", sticky["element_count"] == 50, sticky["element_count"])

    scoped = ctx.call("get_window_state", window_id=wid, scope="Formatting")
    lines = scoped["tree"].splitlines()
    ctx.check("scope takes a container label", len(lines) > 1 and lines[1].startswith('AXGroup "Formatting"'), lines[:3])
    bold = element_index(scoped["tree"], "AXCheckBox", "bold")
    ctx.check("the labelled scope holds the group's controls at full depth",
              bold is not None and element_index(scoped["tree"], "AXButton", "font size") is not None, scoped["tree"][:600])
    ctx.check("the labelled scope excludes the rest of the window", "AXLink" not in scoped["tree"], scoped["tree"][:300])
    ctx.check("an element found through a labelled scope can be acted on",
              bold is not None and ctx.call("click", window_id=wid, element_index=bold, observe="none").get("ok"))
    folded = ctx.call("get_window_state", window_id=wid, scope="formatting")["tree"]
    ctx.check("label matching falls back to case-insensitive", 'AXGroup "Formatting"' in folded, folded[:200])
    unknown = refused(ctx, "get_window_state", window_id=wid, scope="No Such Panel")
    ctx.check("an unknown label is refused with candidate labels", unknown and '"Formatting"' in unknown, unknown)

    everything = ctx.call("get_window_state", window_id=wid, full=True, max_elements=3000)
    ctx.check("max_elements=3000 lists the whole window again",
              not everything["truncated"] and everything["element_count"] == whole["element_count"],
              {k: everything[k] for k in ("truncated", "element_count")})


def png_size(image):
    import struct
    return struct.unpack(">II", base64.b64decode(image["data"])[16:24])


def wide_document(ctx, name, lines):
    """TextEdit sizes an RTF document's window from \\vieww/\\viewh (twips), so this opens a window wider than 1024 points
    without any resize permission."""
    path = ctx.scratch / name
    path.write_text("{\\rtf1\\ansi{\\fonttbl\\f0\\fswiss Helvetica;}\\vieww26000\\viewh14000\\viewkind0\\pard\\f0\\fs36 "
                    + "\\\n".join(lines) + "}")
    launched = ctx.call("launch_app", app="TextEdit", urls=[str(path)], new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(launched["app"]["pid"])
    return ctx.wait_for_window("TextEdit", pid=launched["app"]["pid"], title=name)


def screenshot_shape(ctx, label, shot, image, window):
    w, h = png_size(image)
    ctx.check(f"{label}: long edge is at most 1024 px", max(w, h) <= 1024, (w, h))
    ctx.check(f"{label}: width/height are the PNG's", (w, h) == (shot.get("width"), shot.get("height")), (shot, (w, h)))
    ctx.check(f"{label}: scale is image pixels per window point",
              abs(shot.get("scale", 0) - w / window["frame"]["width"]) < 0.01, (shot.get("scale"), w, window["frame"]))
    hint = shot.get("coordinates", "")
    divisor = re.search(r"divide by ([\d.]+) for window points", hint)
    ctx.check(f"{label}: says how to use its pixels", 'coordinate_space: "screenshot"' in hint and divisor
              and abs(float(divisor.group(1)) - shot.get("scale", 0)) < 0.001, hint)


@scenario
def screenshot_scale(ctx):
    lines = [f"row {i:02d}" for i in range(40)]
    window = wide_document(ctx, "wide.rtf", lines)
    wid = window["id"]
    if window["frame"]["width"] <= 1024:
        raise Skip(f"the display could not fit a window wider than 1024 points ({window['frame']})")

    shot, images = ctx.mcp.call("screenshot", window_id=wid)
    ctx.save_image("screenshot", images[0])
    screenshot_shape(ctx, "screenshot", shot, images[0], window)
    ctx.check("a window wider than 1024 points is scaled down", shot["scale"] < 0.8, shot)

    state, images = ctx.mcp.call("get_window_state", window_id=wid, full=True, include_screenshot=True)
    ctx.require("get_window_state attached one image", len(images) == 1 and "screenshot" in state, list(state))
    screenshot_shape(ctx, "get_window_state", state["screenshot"], images[0], window)
    ctx.check("a successful capture carries no screenshot_error", "screenshot_error" not in state, list(state))
    tree = state["tree"]
    area = element_index(tree, "AXTextArea")
    view = element_frame(tree, element_index(tree, "AXScrollArea"))
    ctx.require("text and scroll areas are indexed with frames", area is not None and view, tree[:400])

    data, images = ctx.mcp.call("click", window_id=wid, element_index=area, observe="both")
    seen = data.get("observation") or {}
    ctx.check("observe=both carries no observation_error", "observation_error" not in data, data.get("observation_error"))
    if images and "screenshot" in seen:
        screenshot_shape(ctx, "observation", seen["screenshot"], images[0], window)
    else:
        ctx.check("observe=both attached a screenshot", False, list(seen))

    # Far right of the visible middle: the caret lands at the end of whichever row is there, so a click scaled wrongly
    # ends up on a different row.
    point = (view[0] + view[2] - 60, view[1] + view[3] / 2)
    original = "\n".join(lines)

    def mark(click_args, batch=False):
        reset = step("set_value", element_index=area, value=original)
        if batch:
            ctx.call("batch", window_id=wid, observe="none",
                     steps=[reset, step("click", **click_args), step("type_text", text="Z")])
        else:
            ctx.call("batch", window_id=wid, observe="none", steps=[reset])
            ctx.call("click", window_id=wid, observe="none", **click_args)
            ctx.call("type_text", window_id=wid, text="Z", observe="none")
        return value_of(ctx.tree(wid))

    scale = state["screenshot"]["scale"]
    pixels = {"x": point[0] * scale, "y": point[1] * scale, "coordinate_space": "screenshot"}
    by_points = mark({"x": point[0], "y": point[1]})
    ctx.check("the window-point click put the caret mid-document", by_points and "Z\n" in by_points
              and not by_points.startswith("Z") and not by_points.endswith("Z"), by_points)
    ctx.check("the same spot in screenshot pixels lands on the same row", mark(pixels) == by_points, by_points)
    ctx.check("batch steps take coordinate_space too", mark(pixels, batch=True) == by_points, by_points)

    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    unseen = refused(ctx, "click", window_id=wid, session_id=sid, x=10, y=10, coordinate_space="screenshot")
    ctx.check("screenshot coordinates without a screenshot of the window are refused", unseen and "screenshot" in unseen, unseen)
    bad = refused(ctx, "click", window_id=wid, x=10, y=10, coordinate_space="pixels")
    ctx.check("an unknown coordinate_space is refused", bad and "coordinate_space" in bad, bad)


@scenario
def perception_docs(ctx):
    tools = {t["name"]: t for t in ctx.mcp.rpc("tools/list")["tools"]}
    props = {name: tool["inputSchema"]["properties"] for name, tool in tools.items()}
    ctx.check("get_window_state takes max_elements", "max_elements" in props["get_window_state"], list(props["get_window_state"]))
    scope = props["get_window_state"]["scope"]
    ctx.check("scope accepts an index or a label", {"integer", "string"} <= {t.get("type") for t in scope.get("anyOf", [])}, scope)
    ctx.check("pixel tools take coordinate_space",
              all(props[n].get("coordinate_space", {}).get("enum") == ["window", "screenshot"] for n in ["click", "drag", "scroll"]))
    ctx.check("screenshot explains the 1024 px limit and coordinate_space",
              "1024" in tools["screenshot"]["description"] and "coordinate_space" in tools["screenshot"]["description"])
    ctx.check("get_window_state explains the ~ moved line", "~" in tools["get_window_state"]["description"])
    ctx.check("press_key says what verified means", "verified" in tools["press_key"]["description"]
              and "effect" in tools["press_key"]["description"], tools["press_key"]["description"])


@scenario
def batch_layout_guard(ctx):
    calc = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    window = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])
    wid = window["id"]
    start_w = window["frame"]["width"]

    # cmd+2 switches Calculator to Scientific, which resizes the window; the following pixel click
    # would land on stale coordinates, so the guard should stop the batch before it runs.
    result = ctx.call("batch", window_id=wid, stop_on_layout_change=True, steps=[
        step("press_key", keys="cmd+2"),
        step("wait", ms=400),
        step("click", x=40, y=40),
    ])
    tools = [(s["tool"], s["ok"], s.get("skipped"), s.get("error")) for s in result["steps"]]
    ctx.check("the mode switch ran", result["steps"][0]["ok"], tools)
    ctx.check("the pixel click after the resize was stopped", not result["steps"][2]["ok"], tools)
    ctx.check("it was stopped for a layout change, not attempted", "layout" in (result["steps"][2].get("error") or "").lower(), tools)
    changed = ctx.call("list_windows", app="Calculator")["windows"]
    now = next((w for w in changed if w["id"] == wid), None) or changed[0]
    ctx.check("the window really did resize", now["frame"]["width"] != start_w, (start_w, now["frame"]["width"]))
    ctx.call("batch", window_id=now["id"], steps=[step("press_key", keys="cmd+1")], observe="none")


@scenario
def agent_display_optional(ctx):
    if not ON_SCREEN:
        raise Skip("moves a window on and off the user's screen; run with --on-screen to include it")
    display = ctx.call("agent_display", action="create")
    ctx.require("agent display is active", display["is_active"] and display.get("bounds"), display)
    window = ctx.launch_scratch_textedit("hosted.txt", "hosted", on_agent_display=False)
    wid = window["id"]
    placed = ctx.call("move_to_agent_display", window_id=wid)
    ctx.check("opt-in move put the window inside the agent display", inside(placed["agent_frame"], display["bounds"]), placed)
    ctx.call("return_window", window_id=wid)
    returned = next(w for w in ctx.call("list_windows", app="TextEdit")["windows"] if w["id"] == wid)
    ctx.check("return_window moved it back off the agent display", not inside(returned["frame"], display["bounds"]), returned["frame"])


@scenario
def off_space_read(ctx):
    active = ctx.run.baseline["active_space"]
    candidates = [w for w in ctx.call("list_windows")["windows"]
                  if not w["is_on_screen"] and not w.get("is_minimized") and w["space_ids"] and active not in w["space_ids"]
                  and w["title"]]
    if not candidates:
        raise Skip("no titled window on an inactive Space right now")
    for w in candidates[:3]:
        tree = ctx.tree(w["id"], include_screenshot=False)
        if ctx.check(f"read AX tree of off-Space window {w['app']!r}", len(re.findall(r"\[\d+\]", tree)) >= 3, tree[:300]):
            return


def fixture_app(name, bundle_id, **info):
    """e2e/delayed.swift as a minimal .app, rebuilt when the source changes; LaunchServices launches bundles. `info`
    joins its Info.plist."""
    app = ROOT / f".bin/{name}.app"
    binary = app / f"Contents/MacOS/{name}"
    source = ROOT / "delayed.swift"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        shutil.rmtree(app, ignore_errors=True)
        binary.parent.mkdir(parents=True)
        subprocess.run(["swiftc", "-O", str(source), "-o", str(binary)], check=True, capture_output=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleName": name, "CFBundleExecutable": name,
            "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "14.0", **info,
        }))
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
    return app


@scenario
def launch_on_hidden_space(ctx):
    """Behind a full-screen app, a new window of an app with no remembered frame opens on a desktop Space nobody is
    viewing. macOS hides it from Accessibility and refuses to let another process move it, so launch_app must say so
    at once (not after a futile search), and window tools must name the reason instead of "call list_windows again".
    The move once the user shows that desktop needs a real Space switch, which only a person can make."""
    if not observe()["main_space_full_screen"]:
        raise Skip("the main display is not showing a full-screen app, so a new window opens where it can be moved")
    display = ctx.call("agent_display", action="create")["bounds"]
    app = fixture_app("SCPlainPlacement", "dev.secondcursor.e2e.plain", SCPlainPlacement=True)
    started = time.time()
    launched = ctx.call("launch_app", app=str(app), new_instance=True, on_agent_display=True)
    took = time.time() - started
    ctx.kill_on_exit(launched["app"]["pid"])
    window = next(iter(launched.get("windows") or []), None)
    ctx.require("launch_app reports the new window", window, launched)
    if inside(window["frame"], display):
        raise Skip("the window reached the agent display after all (the full-screen app left in the meantime)")
    note = launched.get("note") or ""
    ctx.check("the note says the window is on a desktop Space the user is not viewing",
              "desktop Space the user is not viewing" in note, note)
    ctx.check("launch_app answers without a futile Accessibility search (< 4 s)", took < 4, f"{took:.1f}s")
    ctx.check("the window stays where the user cannot see it", not window["is_on_screen"], window)
    error = refused(ctx, "get_window_state", window_id=window["id"], include_screenshot=False)
    ctx.check("reading it names the hidden Space rather than asking to call list_windows again",
              error and "desktop Space the user is not viewing" in error and "list_windows" not in error, error)


@scenario
def electron_read(ctx):
    windows = [w for w in ctx.call("list_windows")["windows"] if w.get("bundle_id") in ELECTRON_BUNDLES and w["title"]]
    if not windows:
        raise Skip("no Electron app window open")
    w = windows[0]
    tree = ctx.tree(w["id"])
    ctx.check(f"Electron app {w['app']!r} exposes web content", len(re.findall(r"\[\d+\]", tree)) >= 10, tree[:400])


class FixtureServer:
    def __enter__(self):
        directory = str(ROOT / "fixtures")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=directory, **kwargs)

            def do_GET(self):
                # ?delay_ms=N answers N ms late, like a slow backend request.
                delay = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("delay_ms")
                if delay:
                    time.sleep(min(int(delay[0]), 10000) / 1000)
                super().do_GET()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *exc):
        self.httpd.shutdown()


@scenario
def browser(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    with FixtureServer() as base:
        tab = ctx.call("browser_open", url=f"{base}/form.html")["tab"]
        tid = tab["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        ctx.check("opened tab is an agent tab", tab["is_agent_tab"], tab)

        outline = ctx.call("browser_snapshot", tab_id=tid, full=True, include_screenshot=True)["outline"]
        field = re.search(r'\[(\w+)\] textbox "Name"', outline)
        ctx.require("snapshot exposes the Name textbox with a ref", field, outline[:500])
        greeting = ctx.call("browser_command", tab_id=tid, operation="inspect", locator={"text": "Hello, ", "exact": False})
        ctx.check("a text locator matches the page's text, not the source of its script", greeting["text"] == "Hello, stranger",
                  greeting["text"])
        submit = ctx.call("browser_command", tab_id=tid, operation="inspect", locator={"role": "button", "name": "Submit"})
        ctx.check("inspect returns a button's text, not its empty value", submit["text"] == "Submit", submit["text"])
        typed = ctx.call("browser_type", tab_id=tid, ref=field.group(1), text="second cursor", submit=True)
        outline = typed["observation"]["outline"]
        ctx.check("typing + submit reached the page, and the action's observation shows it",
                  "Hello, second cursor" in outline, outline[:500])

        # Models write batch steps in the compact schema's shape, with each step's fields under arguments.
        ctx.call("browser_command", tab_id=tid, operation="batch", steps=[
            {"operation": "fill", "arguments": {"locator": {"label": "Name"}, "text": "nested"}},
            {"operation": "click", "arguments": json.dumps({"locator": {"role": "button", "name": "Submit"}})},
        ])
        outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        ctx.check("batch steps with their fields under arguments, as an object or a JSON string, both ran",
                  "Hello, nested" in outline, outline[:500])
        error = refused(ctx, "browser_command", tab_id=tid, operation="batch",
                        steps=[{"operation": "click", "arguments": "[object Object]"}])
        ctx.check("a batch step's unparseable arguments string is refused by step and name",
                  error and "Batch step 1: arguments must be an object" in error, error)

        link = re.search(r'\[(\w+)\] link "Next page"', outline)
        ctx.require("snapshot exposes the link with a ref", link, outline[:500])
        ctx.call("browser_click", tab_id=tid, ref=link.group(1))
        time.sleep(0.5)
        tabs = {t["id"]: t for t in ctx.call("browser_tabs")["tabs"]}
        ctx.check("click navigated the agent tab", tabs.get(tid, {}).get("url", "").endswith("/next.html"), tabs.get(tid))
        ctx.check("only agent tabs are listed", all(t["is_agent_tab"] for t in tabs.values()), tabs)

        shot = ctx.call("browser_screenshot", tab_id=tid)
        ctx.check("background tab screenshot returned an image", shot.get("width", 0) > 0, shot)


@scenario
def browser_settled_reads(ctx):
    """Browser reads a model can act on at once, taken without giving the page a reason to suspect automation.

    Ways this can fail, each checked against e2e/fixtures/late.html:
    - browser_open returns before the page renders what it fetches after load, so the model reads only the page's
      frame and must read again (the results arrive from a request that answers 1.2 s after load).
    - A navigation keeps waiting for the page to react after its network has gone idle, so every load of a page that
      fetches nothing more costs the whole post-load window (1.5 s), or the read after it waits out the rest of the
      reaction window. Checked with e2e/fixtures/form.html.
    - A click that makes the page fetch and render ("Load more", a filter) returns an observation of the old page.
    - Reads carry the per-element JSON list without structured=true, multiplying the payload about tenfold.
    - The cursor stays off the tab until the first action; Codex shows it as soon as the agent works in a tab.
    - Ordinary work switches on the DevTools Runtime domain, which the page can detect through its console. Only
      reading console logs may switch it on. The fixture's probe must then detect it; if it cannot, the check is
      inconclusive rather than passed, since a probe that never fires proves nothing.
    """
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    with FixtureServer() as base:
        started = time.time()
        opened = ctx.call("browser_open", url=f"{base}/late.html")
        took = time.time() - started
        tid = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = opened["observation"]["outline"]
        ctx.check("browser_open waited for the results the page renders after load", "Fancy Feast can" in outline,
                  outline[:400])
        ctx.check("and not much longer than the page needed (< 5 s)", took < 5, f"{took:.1f}s")
        ctx.check("its observation has no per-element list", "elements" not in opened["observation"],
                  list(opened["observation"]))

        def command(operation, **args):
            return ctx.call("browser_command", tab_id=tid, operation=operation, **args)

        viewport = command("cdp", method="Page.getLayoutMetrics")["result"]["cssLayoutViewport"]
        expected = (viewport["clientWidth"] * 0.58, viewport["clientHeight"] * 0.55)
        state = poll(lambda: overlay_in(command("cdp", method="DOM.getDocument",
                                                params={"depth": -1, "pierce": True})["result"]["root"]),
                     lambda s: bool(s and s["point"]))
        ctx.check("the cursor is on the tab as soon as it opened, at 58% x 55% of the viewport, before any action",
                  bool(state and state["point"]) and abs(state["point"][0] - expected[0]) < 2
                  and abs(state["point"][1] - expected[1]) < 2, (state, expected))

        more = re.search(r'\[(\w+)\] button "Load more"', outline)
        ctx.require("the Load more button has a ref", more, outline[:400])
        read = ctx.call("browser_click", tab_id=tid, ref=more.group(1))["observation"]
        ctx.check("the click's observation waits for what the click made the page fetch",
                  "Second batch ready" in read["outline"], read["outline"][-300:])
        ctx.check("it has no per-element list", "elements" not in read, list(read))
        rich = ctx.call("browser_snapshot", tab_id=tid, full=True, structured=True)
        ctx.check("structured=true adds it", bool(rich.get("elements")), list(rich))
        ctx.check("the page saw no DevTools Runtime client while the agent opened, read and clicked",
                  "automation: not detected" in rich["outline"], re.findall(r"automation: \w+( \w+)?", rich["outline"]))

        command("logs")
        time.sleep(0.6)
        after = ctx.call("browser_snapshot", tab_id=tid)["outline"]
        label = "the probe works: reading console logs switches Runtime on, and the page notices"
        if "automation: detected" in after:
            ctx.check(label, True)
        else:
            ctx.checks.append({"label": label, "ok": True, "inconclusive": True,
                               "detail": "this Chrome does not expose Runtime.enable to the page's console; the "
                                         "not-detected check above proves nothing here"})

        started = time.time()
        ctx.call("browser_navigate", tab_id=tid, url=f"{base}/form.html")
        took = time.time() - started
        ctx.check("a navigation to a page that fetches nothing after load returns once its network is idle (< 1 s, "
                  "not the 1.5 s post-load window)", took < 1.0, f"{took:.2f}s")
        started = time.time()
        ctx.call("browser_snapshot", tab_id=tid)
        took = time.time() - started
        ctx.check("so does a read right after it (< 0.5 s, not the rest of the 1.2 s reaction window)", took < 0.5,
                  f"{took:.2f}s")


@scenario
def browser_compact_outline(ctx):
    """The outline a model reads for a page: every control and visible text, in as few tokens as possible.

    Ways this can fail, each checked against e2e/fixtures/cards.html:
    - A link made of several texts (a listing card: title, price, rating, delivery) is spread over one line per
      text and icon, or loses a text; its line must hold all of them, and a price's "$" stays on its number.
    - Icon alt text ("star-icon") or unnamed images take lines; empty placeholder list items take lines.
    - A link whose visible text is split into pieces ("[", "1", "]") reads as a list instead of "[1]".
    - An icon-only button loses its label.
    """
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    with FixtureServer() as base:
        opened = ctx.call("browser_open", url=f"{base}/cards.html")
        tid = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = opened["observation"]["outline"]
        for card in ("Blue kettle 1.7L · $24.90 · 4.8 · Arrives tomorrow",
                     "Two-slot toaster · €31.00 · 4.5 · Arrives in 3 days",
                     "Burr coffee grinder · $89.00 · 4.9 · Arrives in 2 days"):
            ctx.check(f"one line holds the whole card: {card}",
                      re.search(r'^\s*\[e\d+\] link "' + re.escape(card) + '"$', outline, re.M), outline)
        ctx.check("icon alt text takes no line or space", "star-icon" not in outline and "photo" not in outline, outline)
        ctx.check("unnamed images take no lines", not re.search(r"^\s*image$", outline, re.M), outline)
        ctx.check("empty placeholder list items take no lines",
                  len(re.findall(r"^\s*listitem$", outline, re.M)) == 3, outline)
        ctx.check('a citation built from "[", "1" and "]" reads "[1]"',
                  re.search(r'^\s*\[e\d+\] link "\[1\]"$', outline, re.M), outline)
        ctx.check("an icon-only button keeps its label",
                  re.search(r'^\s*\[e\d+\] button "Close dialog"$', outline, re.M), outline)
        ctx.check("the whole page fits in a few lines", len(outline.splitlines()) <= 14, outline)


@scenario
def browser_snapshot_hints(ctx):
    """What the accessibility tree leaves out of the outline, added from the DOM and from frames in other processes.

    Ways this can fail, each checked against e2e/fixtures/hints.html and hints-frame.html:
    - An element made clickable only by a listener (a card), an onclick attribute, or a framework's root listener plus
      a pointer cursor (a row) gets no ref, or every child that inherits its pointer cursor gets one too.
    - A scroll panel gets no ref, or a container whose content fits is marked scrollable.
    - A cross-origin iframe, which Chrome runs in another process, reads as an empty iframe line; its refs act on the
      tab's own process, where the same backend ids name other nodes; a click lands at the frame-local point instead
      of the frame's place in the tab; typed text or Enter never reaches the frame's field.
    - A password, card number, expiry or security code shows its value on the field's line (a field's visible text
      repeats its value), in the structured elements, in browser_command dom_snapshot or inspect, or as a count of
      bullets; an ordinary field loses its value.
    - Content in open or closed shadow roots is missing (Chrome's tree covers both; this guards it).
    - A 1x1 tracker iframe or a hidden one has its document read into the outline.
    """
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    secrets = ["hunter2", "4242", "737", "12/34", "5555555555554444", "•", "typed-Secret-77"]
    with FixtureServer() as base:
        tid = ctx.call("browser_open", url=f"{base}/hints.html")["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))

        browser = ctx.run.instance and ctx.run.instance.browser
        if browser:
            port = int((browser.profile / "DevToolsActivePort").read_text().split()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                targets = json.load(response)
            ctx.require("Chrome runs the localhost frame in its own process (an iframe target)",
                        any(t.get("type") == "iframe" and "localhost" in t.get("url", "") for t in targets),
                        [(t.get("type"), t.get("url")) for t in targets])

        def read():
            return ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]

        # The page, then the same-origin copy of the frame (in the tab's process), then the localhost copy.
        def part(outline, which):
            page, rest = outline.split('iframe "Local coupon widget"', 1)
            return dict(zip(("page", "local", "isolated"), [page, *rest.split('iframe "Coupon widget"', 1)]))[which]

        def ref(pattern, outline, which="page"):
            match = re.search(r"^\s*\[(\w+)\] " + pattern, part(outline, which), re.M)
            ctx.require(f"a ref for {pattern} ({which})", match, outline)
            return match.group(1)

        def clicks(pattern, outline, status, which="page"):
            ctx.call("browser_click", tab_id=tid, ref=ref(pattern, outline, which))
            after = poll(read, lambda o: status in part(o, which))
            ctx.check(f"clicking {pattern} ({which}) by its ref reached the page: {status}", status in part(after, which),
                      part(after, which)[:600])

        outline = ctx.call("browser_snapshot", tab_id=tid, full=True, include_screenshot=True)["outline"]
        (ctx.run.dir / "hints-outline.txt").write_text(outline + "\n")
        ctx.check("children that inherit a card's pointer cursor get no line of their own",
                  "Mountain cabin" not in re.sub(r".*clickable.*", "", outline) and 'clickable "Plain link"' not in outline,
                  outline)
        ctx.check("only the overflowing panel is scrollable", outline.count("scrollable") == 1, outline)
        ctx.check("the panel lists its content below its line",
                  re.search(r'^( *)\[e\d+\] scrollable "Activity log"\n\1  text "Log line 1 ', outline, re.M), outline)
        ref(r'button "Closed shadow action"', outline)
        ctx.check("the localhost frame's content sits under its iframe line, with frame refs",
                  re.search(r'^( *)iframe "Coupon widget"\n(\1  .*\n)*\1  +\[f\d+e\d+\] button "Apply coupon"', outline, re.M),
                  outline)
        ctx.check("the same-origin frame's content is there too, with plain refs",
                  re.search(r'^\s*\[e\d+\] button "Apply coupon"', part(outline, "local"), re.M)
                  and not re.search(r"\[f\d+", part(outline, "local")), outline)
        for field in ("Password", "Card number", "Expiry", "Security code", "Card no."):
            ctx.check(f"{field}: its value is redacted", f'textbox "{field}" value="[redacted]"' in outline, outline)
        ctx.check("an ordinary field keeps its value, printed once",
                  'textbox "Email" value="ada@example.com"' in outline, outline)
        ctx.check("a 1x1 iframe and a hidden one are not read", outline.count('button "Apply coupon"') == 2, outline)

        def command(operation, **args):
            return ctx.call("browser_command", tab_id=tid, operation=operation, **args)

        def values(elements):
            return [v for e in elements for v in [e.get("value"), *values(e.get("children", []))] if v]

        def leaks():
            """Secrets reachable through each read. dom_snapshot and inspect carry coordinates, so only their values
            are searched."""
            reads = {
                "outline": read(),
                "structured elements": json.dumps(
                    ctx.call("browser_snapshot", tab_id=tid, full=True, structured=True)["elements"], ensure_ascii=False),
                "dom_snapshot": json.dumps(values(command("dom_snapshot")["elements"]), ensure_ascii=False),
                "inspect": json.dumps([[r["text"], r["attributes"].get("value")] for r in (
                    command("inspect", locator={"selector": s}) for s in ("#password", "#cc", "#exp", "#cvc", "#cc2"))]),
            }
            return {name: [s for s in secrets if s in text] for name, text in reads.items() if any(s in text for s in secrets)}

        leaked = leaks()
        ctx.check("no secret value, nor a password's length in bullets, is in the outline, the structured elements, "
                  "dom_snapshot or inspect", not leaked, leaked)
        ctx.check("an ordinary field keeps its value in dom_snapshot and inspect",
                  "ada@example.com" in values(command("dom_snapshot")["elements"])
                  and command("inspect", locator={"selector": "#email"})["text"] == "ada@example.com")

        clicks(r'clickable "Open project Alpha"', outline, "Opened project Alpha")
        clicks(r'clickable "Mountain cabin · \$120 / night"', outline, "Picked Mountain cabin")
        clicks(r'clickable "Delegated row two"', outline, "Row two chosen")
        clicks(r'clickable "Shadow card"', outline, "Shadow card clicked")
        clicks(r'button "Shadow action"', outline, "Shadow button clicked")
        # browser_scroll takes no ref; a click into the panel makes it the target of keyboard scrolling.
        ctx.call("browser_click", tab_id=tid, ref=ref(r'scrollable "Activity log"', outline))
        ctx.call("browser_press_key", tab_id=tid, keys="end")
        after = poll(read, lambda o: "Panel scrolled" in o)
        ctx.check("a click on the panel's ref, then End, scrolls the panel", "Panel scrolled" in after, after[:300])

        clicks(r'clickable "Frame chip"', outline, "Chip clicked", "local")
        clicks(r'clickable "Frame chip"', outline, "Chip clicked", "isolated")
        ctx.call("browser_type", tab_id=tid, ref=ref(r'textbox "Coupon"', outline, "isolated"), text="SAVE10", submit=True)
        after = poll(read, lambda o: "Applied SAVE10" in part(o, "isolated"))
        ctx.check("typing into the localhost frame's field and pressing Enter reached it",
                  "Applied SAVE10" in part(after, "isolated"), after)
        clicks(r'clickable "Frame chip"', outline, "Chip clicked", "isolated")
        clicks(r'button "Apply coupon"', outline, "Applied SAVE10", "isolated")

        typed = ctx.call("browser_type", tab_id=tid, ref=ref(r'textbox "Password"', outline), text="typed-Secret-77")
        after = read()
        leaked = leaks()
        ctx.check("a typed password stays redacted, in the typing's observation and every read",
                  not leaked and "typed-Secret-77" not in json.dumps(typed)
                  and 'textbox "Password" value="[redacted]"' in after, (leaked, typed))
        error = refused(ctx, "browser_click", tab_id=tid, ref="f99e5")
        ctx.check("a ref into an unknown frame is refused, not looked up in the tab's own process",
                  error and "no longer on the page" in error, error)


@scenario
def browser_covered_clicks(ctx):
    """A click on a ref or locator that something covers, checked against e2e/fixtures/covered.html.

    Ways this can fail:
    - The click lands on what covers the target (an Undo toast over a button), and reports ok.
    - It fails without saying what covers the target, or fails at once for a toast that clears within a second.
    - The agent's own cursor, resting on the target after the last click, or a layer with pointer-events: none
      counts as covering, so ordinary clicks fail; or a scrolled page is hit-tested at the wrong place.
    - Something in the page over a cross-origin frame goes unnoticed, since the frame's process cannot see it.
    - A locator click, or a click inside a batch, skips the check.
    - The check makes every click slow (latency per click is saved to <run>/covered-clicks.json).
    """
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    with FixtureServer() as base:
        opened = ctx.call("browser_open", url=f"{base}/covered.html")
        tid = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = opened["observation"]["outline"]

        def ref(pattern):
            match = re.search(r"\[(\w+)\] " + pattern, outline)
            ctx.require(f"a ref for {pattern}", match, outline)
            return match.group(1)

        def status():
            return re.search(r"clicks=\d+ glassed=\d+", ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]).group(0)

        def timed(tool, **args):
            started = time.time()
            try:
                ctx.call(tool, tab_id=tid, **args)
                return time.time() - started, None
            except ToolError as e:
                return time.time() - started, str(e)

        target, spam = ref(r'button "Label: Spam"'), {"role": "button", "name": "Label: Spam"}
        took, error = timed("browser_click", ref=target)
        ctx.check("an uncovered click goes through", not error and status() == "clicks=1 glassed=0", error)
        latency = {"ref": [], "locator": []}
        for _ in range(9):
            latency["ref"].append(timed("browser_click", ref=target, observe="none")[0])
            latency["locator"].append(timed("browser_command", operation="click", locator=spam)[0])
        ctx.check("the agent's cursor, resting on the target after each click, never counts as covering it",
                  status() == "clicks=19 glassed=0", status())
        took, error = timed("browser_click", ref=ref(r'button "Under glass"'))
        ctx.check("a layer with pointer-events: none over a button does not count as covering it",
                  not error and status() == "clicks=19 glassed=1", error)
        ctx.call("browser_click", tab_id=tid, ref=ref(r'button "Show toast"'))
        outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        for label, tool, args in (("ref", "browser_click", {"ref": target}),
                                  ("locator", "browser_command", {"operation": "click", "locator": spam})):
            took, error = timed(tool, **args)
            ctx.check(f"a covered {label} click fails after waiting about 2 s, naming what covers it",
                      error and 'covered by div "Message archived Undo Dismiss"' in error and 1.8 < took < 4,
                      (round(took, 2), error))
        error = refused(ctx, "browser_command", tab_id=tid, operation="batch",
                        steps=[{"operation": "click", "locator": spam}])
        ctx.check("so does a click in a batch", error and "covered by" in error, error)
        ctx.check("none of them reached the button", status() == "clicks=19 glassed=1", status())

        ctx.call("browser_click", tab_id=tid, ref=ref(r'button "Dismiss"'))
        ctx.call("browser_click", tab_id=tid, ref=ref(r'button "Show brief toast"'))
        took, error = timed("browser_click", ref=target)
        ctx.check("a click waits for a toast that clears within a second, then goes through",
                  not error and status() == "clicks=20 glassed=1", (round(took, 2), error))

        chip = re.search(r"\[(f\d+e\d+)\] clickable \"Frame chip\"", outline)
        ctx.require("the cross-origin frame's chip has a frame ref", chip, outline)
        ctx.call("browser_click", tab_id=tid, ref=ref(r'button "Show curtain"'))
        took, error = timed("browser_click", ref=chip.group(1))
        ctx.check("a frame ref click fails when the page covers the frame, naming the cover",
                  error and "covered by div" in error and "Hide curtain" in error, error)
        outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        ctx.call("browser_click", tab_id=tid, ref=ref(r'button "Hide curtain"'))
        took, error = timed("browser_click", ref=chip.group(1))
        after = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        ctx.check("and goes through once the cover is gone", not error and "Chip clicked" in after, error or after)
        took, error = timed("browser_click", ref=ref(r'button "Far below"'))
        ctx.check("a button the click scrolls into view is hit-tested where it now is, and goes through",
                  not error and "Far clicked" in ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"], error)

        medians = {k: round(sorted(v)[len(v) // 2] * 1000) for k, v in latency.items()}
        (ctx.run.dir / "covered-clicks.json").write_text(json.dumps({"median_ms": medians, "samples_s": latency}, indent=2))
        ctx.check(f"uncovered clicks: median browser_click {medians['ref']} ms (observe none; includes its 300 ms wait "
                  f"for a navigation to start), browser_command click {medians['locator']} ms", True, latency)


OVERLAY_ID = "second-cursor-overlay"


def warm_pixels(png, around=None, half=30):
    """Pixels tinted Cursor orange (the agent cursor's glow) in a PNG, or in a 2*half square `around` a point."""
    import struct
    width, height = struct.unpack(">II", png[16:24])
    pixels = png_pixels(png)
    xs, ys = range(width), range(height)
    if around:
        cx, cy = int(around[0]), int(around[1])
        xs, ys = range(max(0, cx - half), min(width, cx + half)), range(max(0, cy - half), min(height, cy + half))
    return sum(1 for y in ys for x in xs for r, g, b in [pixels[y * width + x]]
               if r > 180 and r - b > 60 and r - g > 25)


def dark_pixels(png, x0, y0, x1, y1):
    import struct
    width = struct.unpack(">I", png[16:20])[0]
    pixels = png_pixels(png)
    return sum(1 for y in range(y0, y1) for x in range(x0, x1) if max(pixels[y * width + x]) < 60)


class DevTools:
    """A page's own DevTools socket, which sees the tab the way a person does: past the extension and the concealment
    it applies to the agent's captures. A minimal WebSocket client; the standard library has none.

        with DevTools(page_socket(browser, url)) as page:
            page.call("Page.captureScreenshot", {"format": "png"})
    """

    def __init__(self, ws_url):
        import urllib.parse
        self.url = urllib.parse.urlparse(ws_url)
        self.next_id, self.buffer = 0, b""

    def __enter__(self):
        self.sock = socket.create_connection((self.url.hostname, self.url.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {self.url.path} HTTP/1.1\r\nHost: {self.url.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            self.read(len(self.buffer) + 1, keep=True)
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace")[:200])
        return self

    def __exit__(self, *exc):
        self.sock.close()

    def read(self, n, keep=False):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools socket closed")
            self.buffer += chunk
        if keep:
            return None
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def call(self, method, params=None):
        import struct
        self.next_id += 1
        payload = json.dumps({"id": self.next_id, "method": method, "params": params or {}}).encode()
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        message = b""
        while True:
            first, second = self.read(2)
            n = second & 0x7F
            if n == 126:
                n = struct.unpack(">H", self.read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self.read(8))[0]
            data = self.read(n)
            if (first & 0x0F) in (0, 1):
                message += data
            if first & 0x80 and message:
                reply, message = json.loads(message), b""
                if reply.get("id") == self.next_id:
                    if "error" in reply:
                        raise ToolError(f"{method}: {reply['error']}")
                    return reply["result"]


def page_socket(browser, url):
    port = int((browser.profile / "DevToolsActivePort").read_text().split()[0])
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.load(response)
    return next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page" and t.get("url") == url)


def overlay_in(document):
    """The agent cursor's host in a DOM.getDocument(depth=-1, pierce=true) tree: its parent, attributes, the text in
    its shadow root, and the point its cursor's tip sits on. None when the page has no cursor."""
    stack = [(document, None)]
    while stack:
        node, parent = stack.pop()
        if node.get("localName") == OVERLAY_ID:
            break
        stack += [(child, node) for child in node.get("children", []) + node.get("shadowRoots", [])]
    else:
        return None

    def attributes(n):
        return dict(zip(n.get("attributes", [])[::2], n.get("attributes", [])[1::2]))

    shadow = (node.get("shadowRoots") or [{}])[0]
    text, point, pill, inner = [], None, None, [shadow]
    while inner:
        n = inner.pop()
        if n.get("nodeType") == 3:
            text.append(n.get("nodeValue", ""))
        classes = attributes(n).get("class", "").split()
        if "cursor" in classes:
            move = re.search(r"translate3d\((-?[\d.]+)px, (-?[\d.]+)px", attributes(n).get("style", ""))
            point = move and (float(move.group(1)), float(move.group(2)))
        if "pill" in classes:
            pill = n.get("nodeId")
        inner += reversed(n.get("children", []))
    host = attributes(node)
    return {"parent": parent and parent.get("localName"), "aria_hidden": host.get("aria-hidden"),
            "shadow": shadow.get("shadowRootType"), "text": "".join(text), "point": point, "pill": pill}


def poll(read, ok, timeout=2.0):
    """Reads until `ok(value)` or the timeout; returns the last value read."""
    deadline = time.time() + timeout
    while True:
        value = read()
        if ok(value) or time.time() > deadline:
            return value
        time.sleep(0.15)


@scenario
def browser_cursor(ctx):
    """The agent's cursor drawn inside agent tabs by the extension (extension/cursor.js).

    Ways it can fail, and what covers each:
    - It never shows: the isolated world, the closed shadow root, or the program fails (CSP, no debugger, a thrown
      error), or the caption command is refused. Covered: host and caption read back with DOM.getDocument(pierce),
      and a capture over the page's own DevTools socket must show the orange glow and the dark caption pill.
    - It shows in the wrong place: tip offset from the hotspot, wrong point for a ref click, a typed-into field, a
      scroll, a locator click, or a fill. Covered: the tip's translate is compared with each action's point. (It is
      on the tab from browser_open, at 58% x 55%, so the first click travels from there; browser_settled_reads
      checks that.)
    - A keyboard action in a fresh tab shows nothing, or not at 58% x 55% of the viewport. Covered.
    - The caption is missing, not fully typed out, not replaced by the next caption, or survives a caption-less
      action. Covered.
    - The agent sees its own cursor: browser_screenshot or browser_command screenshot right after a click (while it
      is still animating) contains it; or the host or caption shows in browser_snapshot, dom_snapshot, locator
      matching, or text/HTML exports; or the host is not aria-hidden, its shadow root is open, or it hangs off
      <body> where "body *" walks list it. Covered.
    - It interferes with the page: swallows or doubles a click, moves focus on a caption update, or throws into the
      page's console log (which browser_command logs reports). Covered with a click counter, dom_snapshot focus,
      and logs.
    - Navigation wipes it and it never comes back, or comes back without its caption or somewhere else. Covered.
    - It outlives the turn (still in the page ~1 s after POST /turn-ended), or the next turn's action does not bring
      it back. Covered.
    - Pointer input does not wait for the cursor to arrive (the click lands before the person sees the cursor get
      there), waits until the spring settles instead of its first arrival, waits the whole 1.5 s cap, or waits when
      the cursor does not travel. Covered by timing browser_command clicks, which return right after input.
    - It waits in a tab nobody can see. Not exercised: headless Chrome reports every tab "visible", even behind a
      new foreground tab, after Target.activateTarget on another tab, or with its window minimized.
    - Not exercised: the 60 s idle fade (a minute of waiting), prefers-reduced-motion, cleanup on tab close, a newer
      move cutting an older wait short (actions on one tab run one at a time).
    """
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    browser = getattr(ctx.run.instance, "browser", None)
    with FixtureServer() as base:
        url = f"{base}/cursor.html"
        opened = ctx.call("browser_open", url=url)
        tid = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))

        def command(operation, tab=tid, **args):
            return ctx.call("browser_command", tab_id=tab, operation=operation, **args)

        def overlay(tab=tid):
            document = command("cdp", tab, method="DOM.getDocument", params={"depth": -1, "pierce": True})
            return overlay_in(document["result"]["root"])

        def at(state, x, y, caption=None):
            return bool(state and state["point"]) and abs(state["point"][0] - x) < 1.5 \
                and abs(state["point"][1] - y) < 1.5 and (caption is None or state["text"] == caption)

        def bounds(selector, tab=tid):
            return command("inspect", tab, locator={"selector": selector})["bounds"]

        def center(b):
            return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

        # The fixture has nothing orange, so any orange pixel is the cursor, wherever its animation has got to.
        def own_capture(label, tab, extended=False):
            if extended:
                image = ctx.mcp.call("browser_command", tab_id=tab, operation="screenshot")[1][0]
            else:
                image = ctx.mcp.call("browser_screenshot", tab_id=tab)[1][0]
            ctx.save_image(label, image)
            count = warm_pixels(base64.b64decode(image["data"]))
            ctx.check(f"the agent's own {'browser_command screenshot' if extended else 'browser_screenshot'}, right "
                      "after the click, has no trace of its cursor", count <= 3, f"{count} orange pixels")

        outline = opened["observation"]["outline"]
        button = re.search(r'\[(\w+)\] button "Count clicks"', outline)
        field = re.search(r'\[(\w+)\] textbox "Name"', outline)
        ctx.require("the fixture exposes its button and field", button and field, outline[:400])

        clicked = ctx.call("browser_click", tab_id=tid, x=160, y=100, caption="Opening the menu")
        own_capture("own-screenshot-after-click", tid)
        state = poll(overlay, lambda s: at(s, 160, 100, "Opening the menu"))
        ctx.require("after a captioned click the host is in the page with the caption in its shadow root",
                    state and state["text"] == "Opening the menu", state)
        ctx.check("the cursor appeared with its tip on the click point", at(state, 160, 100), state)
        ctx.check("the host hangs off <html>, not <body>", state["parent"] == "html", state)
        ctx.check("the host is aria-hidden", state["aria_hidden"] == "true", state)
        ctx.check("its shadow root is closed", state["shadow"] == "closed", state)

        if browser:
            clip = {"x": 100, "y": 40, "width": 320, "height": 120, "scale": 1}
            with DevTools(page_socket(browser, url)) as page:
                seen = poll(lambda: base64.b64decode(page.call("Page.captureScreenshot",
                                                               {"format": "png", "clip": clip})["data"]),
                            lambda png: warm_pixels(png, (60, 60)) >= 40, timeout=3)
                pill = overlay_in(page.call("DOM.getDocument", {"depth": -1, "pierce": True})["root"])["pill"]
                handle = page.call("DOM.resolveNode", {"nodeId": pill})["object"]["objectId"]
                # Fractional widths: text-overflow cuts the caption short when it overflows by even 0.1 px, which
                # the integer scrollWidth and clientWidth round away.
                widths = page.call("Runtime.callFunctionOn", {
                    "objectId": handle, "returnByValue": True,
                    "functionDeclaration": "function () { const s = getComputedStyle(this); return ["
                                           "this.querySelector('.text').getBoundingClientRect().width, "
                                           "this.getBoundingClientRect().width - parseFloat(s.paddingLeft) "
                                           "- parseFloat(s.paddingRight)]; }",
                })["result"]["value"]
            path = ctx.run.dir / "screenshots" / f"{ctx.name}-as-a-person-sees-it.png"
            path.write_bytes(seen)
            ctx.screenshots.append(path.relative_to(ctx.run.dir).as_posix())
            ctx.check("a person looking at the tab sees the cursor's orange glow at the click point",
                      warm_pixels(seen, (60, 60)) >= 40, f"{warm_pixels(seen, (60, 60))} orange pixels")
            # The pill starts 14 px right of and 20 px below the tip.
            dark = dark_pixels(seen, 78, 82, 134, 102)
            ctx.check("and the dark caption pill beside it", dark >= 400, f"{dark} of 1120 pixels dark")
            ctx.check("the pill shows the whole caption, not cut off with an ellipsis", widths[0] <= widths[1],
                      f"text width, pill content width = {widths}")

        snapshot = json.dumps([clicked, ctx.call("browser_snapshot", tab_id=tid, full=True, structured=True)])
        ctx.check("the click's observation and browser_snapshot (outline and elements) never show the caption or the host",
                  "Opening the menu" not in snapshot and OVERLAY_ID not in snapshot, snapshot[:300])
        dom = json.dumps(command("dom_snapshot"))
        ctx.check("dom_snapshot does not list the host or its caption",
                  "Opening the menu" not in dom and OVERLAY_ID not in dom, dom[:300])
        for locator in ({"selector": f"#{OVERLAY_ID}"}, {"text": "Opening the menu"}):
            error = refused(ctx, "browser_command", tab_id=tid, operation="inspect", locator=locator)
            ctx.check(f"locator {locator} matches nothing", error and "matched 0" in error, error)
        for fmt in ("text", "html"):
            content = command("export", format=fmt)["content"]
            ctx.check(f"a {fmt} export has neither the caption nor the host",
                      "Opening the menu" not in content and OVERLAY_ID not in content and "Count clicks" in content,
                      content[:300])
        ctx.check("the page got exactly one click",
                  command("inspect", locator={"selector": "#count"})["text"] == "clicks=1")

        ctx.call("browser_type", tab_id=tid, ref=field.group(1), text="ada")
        box = bounds("#name")
        typed_at = (box["x"] + min(24, box["width"] / 2), box["y"] + box["height"] / 2)
        ctx.call("browser_press_key", tab_id=tid, keys="End", caption="Checking the field")
        state = poll(overlay, lambda s: at(s, *typed_at, "Checking the field"))
        ctx.check("typing into a field points near its left edge; the next caption replaces the old one",
                  at(state, *typed_at, "Checking the field"), (state, typed_at))
        focused = [e for e in command("dom_snapshot")["elements"] if e.get("focused")]
        ctx.check("caption updates leave focus in the field the agent typed into",
                  len(focused) == 1 and focused[0]["role"] == "textbox" and focused[0]["value"] == "ada", focused)

        ctx.call("browser_click", tab_id=tid, ref=button.group(1))
        pressed = center(bounds("#target"))
        state = poll(overlay, lambda s: at(s, *pressed, ""), timeout=1.5)
        ctx.check("a ref click points at the element's center, and without a caption clears it",
                  at(state, *pressed, ""), (state, pressed))
        ctx.check("the page got exactly one more click",
                  command("inspect", locator={"selector": "#count"})["text"] == "clicks=2")

        link = center(bounds("#next"))
        ctx.call("browser_click", tab_id=tid, x=link[0], y=link[1], caption="Following the link")
        state = poll(overlay, lambda s: at(s, *link, "Following the link"))
        ctx.check("a link click loads a new document, and with no further action it has the cursor back on the "
                  "link with its caption", at(state, *link, "Following the link"), state)
        ctx.call("browser_navigate", tab_id=tid, url=f"{url}?page=2", caption="Opening page two")
        state = poll(overlay, lambda s: at(s, *link, "Opening page two"))
        ctx.check("after browser_navigate the cursor is back where it was, with the new caption",
                  at(state, *link, "Opening page two"), state)
        ctx.call("browser_click", tab_id=tid, x=500, y=400)
        state = poll(overlay, lambda s: at(s, 500, 400, ""))
        ctx.check("a click on the new page moves it to the new point", at(state, 500, 400, ""), state)
        ctx.check("a click on empty page reached no button",
                  command("inspect", locator={"selector": "#count"})["text"] == "clicks=0")

        logs = command("logs")["logs"]
        ctx.check("the overlay wrote nothing to the page's console log", not logs, logs[:3])

        tid2 = ctx.call("browser_open", url=f"{url}?page=3")["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid2))
        viewport = command("cdp", tid2, method="Page.getLayoutMetrics")["result"]["cssLayoutViewport"]
        width, height = viewport["clientWidth"], viewport["clientHeight"]
        ctx.call("browser_press_key", tab_id=tid2, keys="End", caption="Thinking it over")
        state = poll(lambda: overlay(tid2), lambda s: at(s, width * 0.58, height * 0.55, "Thinking it over"))
        ctx.check("a keyboard action in a fresh tab shows the cursor at 58% x 55% of the viewport",
                  at(state, width * 0.58, height * 0.55, "Thinking it over"), (state, viewport))
        ctx.call("browser_scroll", tab_id=tid2, direction="down")
        state = poll(lambda: overlay(tid2), lambda s: at(s, width / 2, height / 2, ""))
        ctx.check("a scroll points at the viewport center", at(state, width / 2, height / 2, ""), state)

        pressed = center(bounds("#target", tid2))
        command("click", tid2, locator={"selector": "#target"}, caption="Pressing by locator")
        own_capture("own-extended-screenshot-after-click", tid2, extended=True)
        state = poll(lambda: overlay(tid2), lambda s: at(s, *pressed, "Pressing by locator"))
        ctx.check("a locator click points at the element's center with its caption",
                  at(state, *pressed, "Pressing by locator"), (state, pressed))
        command("fill", tid2, locator={"selector": "#name"}, text="grace")
        filled = center(bounds("#name", tid2))
        state = poll(lambda: overlay(tid2), lambda s: at(s, *filled, ""))
        ctx.check("a fill points at the field's center", at(state, *filled, ""), (state, filled))

        # browser_command click returns right after its mouse events, so its time is the wait plus a round trip.
        import math

        def timed_click(x, y):
            started = time.time()
            command("click", tid2, x=x, y=y)
            return time.time() - started

        timed_click(80, 80)
        time.sleep(0.8)
        still = sorted(timed_click(80, 80) for _ in range(3))[1]
        ctx.check("a click with no travel does not wait for the cursor", still < 0.1, f"{still:.3f} s")
        far = (650, 380)
        distance = ((far[0] - 80) ** 2 + (far[1] - 80) ** 2) ** 0.5
        # The spring's step response first reaches the target at (pi - acos(z)) / (w * sqrt(1 - z^2)), w = 2 pi / r.
        response, damping = min(max(0.28 + distance / 4000, 0.28), 0.5), 0.86
        arrives = (math.pi - math.acos(damping)) / (2 * math.pi / response * (1 - damping ** 2) ** 0.5)
        travel = timed_click(*far)
        ctx.check(f"a click {distance:.0f} px away waits for the cursor's first arrival (~{arrives * 1000:.0f} ms)",
                  travel - still >= arrives - 0.05, f"{travel:.3f} s vs {still:.3f} s with no travel")
        ctx.check("and well under the 1.5 s cap", travel < 1.0, f"{travel:.3f} s")
        state = overlay(tid2)
        ctx.check("it was dispatched at the cursor's target", at(state, *far), state)

        request = urllib.request.Request(
            ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {ctx.mcp.token}", "Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=15).read()
        gone = [poll(lambda t=t: overlay(t), lambda s: s is None, timeout=1.0) for t in (tid, tid2)]
        ctx.check("both tabs' cursors are gone within ~1 s of POST /turn-ended", gone == [None, None], gone)
        ctx.call("browser_click", tab_id=tid, x=300, y=300, caption="Back to work")
        state = poll(overlay, lambda s: at(s, 300, 300, "Back to work"))
        ctx.check("the next turn's first action brings it back at its target", at(state, 300, 300, "Back to work"),
                  state)


@scenario
def pause(ctx):
    if not APP_BINARY.exists():
        raise Skip(f"{APP_BINARY} not installed")
    subprocess.run([str(APP_BINARY), "--pause"], check=True, timeout=10)
    try:
        time.sleep(0.5)
        try:
            ctx.call("list_windows")
            ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
            ctx.check("actions are refused while paused", False, "launch succeeded while paused")
        except ToolError as e:
            ctx.check("actions are refused while paused", "paused" in str(e).lower(), e)
    finally:
        subprocess.run([str(APP_BINARY), "--resume"], check=True, timeout=10)
    time.sleep(0.5)
    ctx.check("status reports resumed", ctx.call("status")["paused"] is False)


@scenario
def action_log(ctx):
    lines = ACTION_LOG.read_text().splitlines() if ACTION_LOG.exists() else []
    new = lines[ctx.run.action_log_start:]
    ctx.check("every action tool call was logged", len(new) >= ctx.run.action_calls(), f"{len(new)} log lines for {ctx.run.action_calls()} action calls")
    ctx.check("log lines are JSON with tool and time", all({"tool", "time"} <= json.loads(l).keys() for l in new[-20:]), new[-1:] if new else "")


@scenario
def teardown(ctx):
    ctx.call("agent_display", action="teardown")
    status = ctx.call("agent_display", action="status")
    ctx.check("agent display is gone", not status["is_active"], status)
    ctx.check("display count is back to the baseline", len(observe()["displays"]) == len(ctx.run.baseline["displays"]))


ACTION_TOOLS = {"launch_app", "batch", "click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action",
                "move_to_agent_display", "return_window", "browser_open", "browser_navigate", "browser_click",
                "browser_type", "browser_press_key", "browser_scroll", "browser_close"}


class Run:
    def __init__(self, config_overrides=None, browser=None, installed=None):
        """Runs against a fresh TestInstance (merged with `config_overrides`) unless `installed`
        (default: --installed or SC_E2E_TARGET=installed) targets the user's running app.
        `browser`: True always starts the isolated test browser, False never, None (default, or the
        --browser / --no-browser flags) starts it when a selected scenario uses browser tools."""
        if installed is None:
            installed = "--installed" in sys.argv or os.environ.get("SC_E2E_TARGET") == "installed"
        if browser is None and {"--browser", "--no-browser"} & set(sys.argv):
            browser = "--browser" in sys.argv
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = ROOT / "artifacts" / stamp
        (self.dir / "screenshots").mkdir(parents=True)
        self.current = "setup"
        self.transcript = Transcript(self.dir / "transcript.jsonl", self)
        self.browser, self.instance, self.notes = browser, None, []
        self.saved_sc_home = os.environ.get("SC_HOME")
        if installed:
            if config_overrides:
                raise SystemExit(f"config overrides {config_overrides} need a test instance; drop --installed")
            os.environ.pop("SC_HOME", None)
            config = json.loads(CONFIG_PATH.read_text())
            url, token = f"http://127.0.0.1:{config['port']}/mcp", config["token"]
            self.target = f"installed app (config {CONFIG_PATH})"
        else:
            if "--approvals" in sys.argv:
                config_overrides = {"approvals": "prompt", **(config_overrides or {})}
            self.instance = TestInstance(config_overrides)
            # Scenarios read these module globals and run APP_BINARY --pause/--resume without an env,
            # so point both at the test instance.
            globals().update(CONFIG_PATH=self.instance.config_path, ACTION_LOG=self.instance.action_log)
            os.environ["SC_HOME"] = str(self.instance.home)
            url, token = self.instance.url, self.instance.token
            self.target = f"test instance of {self.instance.app} (SC_HOME={self.instance.home}, pid {self.instance.pid})"
        print(f"Target: {self.target}")
        self.mcp = MCP(url, token, self.transcript)
        self.baseline = observe()
        self.action_log_start = len(ACTION_LOG.read_text().splitlines()) if ACTION_LOG.exists() else 0
        self.results = []

    def tool_calls(self):
        return sum(1 for line in (self.dir / "transcript.jsonl").read_text().splitlines()
                   if json.loads(line)["request"].get("method") == "tools/call")

    def action_calls(self):
        count = 0
        for line in (self.dir / "transcript.jsonl").read_text().splitlines():
            entry = json.loads(line)
            req = entry["request"]
            if req.get("method") == "tools/call" and req["params"]["name"] in ACTION_TOOLS:
                count += 1
        return count

    def invariants(self, before, after, seconds):
        """A change only counts as a failure when the user provided no input during the scenario;
        otherwise it is recorded as inconclusive, since the user may have caused it."""
        user_input = after["seconds_since_user_input"] < seconds
        user_mouse = after["seconds_since_user_mouse"] < seconds

        def check(label, changed, user_active, detail):
            return {"label": label, "ok": not changed or user_active, "inconclusive": changed and user_active, "detail": detail}

        return [
            check("frontmost app unchanged", before["frontmost"]["pid"] != after["frontmost"]["pid"], user_input,
                  f"{before['frontmost']['name']} -> {after['frontmost']['name']}"),
            check("active Space unchanged", before["active_space"] != after["active_space"], user_input,
                  f"{before['active_space']} -> {after['active_space']}"),
            check("cursor untouched", before["cursor"] != after["cursor"], user_mouse,
                  f"{before['cursor']} -> {after['cursor']}"),
        ]

    def run(self, names):
        """One-shot: runs the selected scenarios (flags in `names` are ignored), writes the report and
        stops the test instance."""
        names = {n for n in names if not n.startswith("--")}
        selected = [fn for fn in SCENARIOS if not names or fn.__name__ in names]
        try:
            self.mcp.initialize()
            self.start_browser_if_needed(selected)
            for fn in selected:
                self.run_scenario(fn)
        finally:
            self.write_report()
            self.close()
        return all(r["status"] != "FAIL" for r in self.results)

    def run_scenario(self, fn):
        self.current = fn.__name__
        ctx = Context(self, fn.__name__)
        before, started = observe(), time.time()
        status, error = "PASS", None
        try:
            fn(ctx)
        except Skip as e:
            status, error = "SKIP", str(e)
        except Exception as e:
            status, error = "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        finally:
            for cleanup in reversed(ctx.cleanups):
                try:
                    cleanup()
                except Exception:
                    pass
        seconds = time.time() - started
        invariants = self.invariants(before, observe(), seconds)
        if status == "PASS" and not all(c["ok"] for c in ctx.checks + invariants):
            status = "FAIL"
        self.results.append({"scenario": fn.__name__, "status": status, "seconds": round(seconds, 1), "error": error,
                             "checks": ctx.checks, "invariants": invariants, "screenshots": ctx.screenshots})
        print(f"{status:4}  {fn.__name__}  ({seconds:.1f}s){'  ' + error.splitlines()[0] if error else ''}")

    def start_browser_if_needed(self, selected):
        wanted = self.browser if self.browser is not None else any(uses_browser(fn) for fn in selected)
        if not wanted or self.instance is None:
            return
        try:
            self.instance.start_browser()
            self.notes.append(f"Isolated test browser connected (Chrome pid {self.instance.browser.pid}).")
        except Exception as e:
            if self.browser:
                raise
            self.notes.append(f"Test browser unavailable; browser scenarios will skip or fail: {e}")
        print(self.notes[-1])

    def close(self):
        if self.instance is None:
            return
        self.instance.stop(keep_logs=self.dir / "instance")
        self.instance = None
        if self.saved_sc_home is None:
            os.environ.pop("SC_HOME", None)
        else:
            os.environ["SC_HOME"] = self.saved_sc_home

    def write_report(self):
        (self.dir / "results.json").write_text(json.dumps(
            {"target": self.target, "notes": self.notes, "baseline": self.baseline, "results": self.results}, indent=2))
        lines = [f"# Second Cursor E2E — {self.dir.name}", "",
                 f"Target: {self.target}", "", *[f"- {note}" for note in self.notes], *([""] if self.notes else []),
                 f"Frontmost at start: {self.baseline['frontmost']['name']} · active Space {self.baseline['active_space']} · "
                 f"{len(self.baseline['displays'])} display(s)", "",
                 "| Scenario | Result | Time |", "| --- | --- | --- |"]
        lines += [f"| {r['scenario']} | {r['status']} | {r['seconds']}s |" for r in self.results]
        for r in self.results:
            lines += ["", f"## {r['scenario']} — {r['status']}", ""]
            if r["error"]:
                lines += ["```", r["error"].strip(), "```", ""]
            for c in r["checks"] + r["invariants"]:
                if c.get("inconclusive"):
                    lines.append(f"- ⚠️ {c['label']} — inconclusive, user input during scenario: `{c['detail']}`")
                else:
                    lines.append(f"- {'✅' if c['ok'] else '❌'} {c['label']}" + (f" — `{c['detail']}`" if c["detail"] and not c["ok"] else ""))
            lines += [f"", *[f"![{p}]({p})" for p in r["screenshots"]]]
        (self.dir / "report.md").write_text("\n".join(lines) + "\n")
        print(f"\nArtifact: {self.dir / 'report.md'}")


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
