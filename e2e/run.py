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

    def launch_scratch_textedit(self, filename, text):
        path = self.scratch / filename
        path.write_text(text)
        launched = self.call("launch_app", app="TextEdit", urls=[str(path)], new_instance=True)
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
    window = ctx.launch_scratch_textedit("notes.txt", "alpha")
    ctx.check("new window stays where the app put it", not window.get("on_agent_display"), window)
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
    launched = ctx.call("launch_app", app="Calculator", new_instance=True)
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


def placement(ctx, window):
    where = "on the user's active Space" if window["is_on_screen"] else f"off-Space (spaces {window['space_ids']})"
    ctx.check(f"window left in place, {where}", not window.get("on_agent_display"), window["frame"])


@scenario
def pixel_click_in_place(ctx):
    window = ctx.launch_scratch_textedit("pixels.txt", "line one\nline two")
    wid = window["id"]
    placement(ctx, window)
    _, frame = text_area(ctx, wid)
    ctx.call("click", window_id=wid, x=frame[0] + 3, y=frame[1] + 6)
    ctx.call("type_text", window_id=wid, text="Z")
    tree = ctx.tree(wid)
    ctx.check("pixel click moved the caret to the start of line one", 'value="Zline one' in tree, tree[:400])


@scenario
def drag_select(ctx):
    window = ctx.launch_scratch_textedit("drag.txt", "line one\nline two")
    wid = window["id"]
    placement(ctx, window)
    _, frame = text_area(ctx, wid)
    y = frame[1] + 6
    ctx.call("drag", window_id=wid, from_x=frame[0] + 3, from_y=y, to_x=min(frame[0] + frame[2] - 10, 400), to_y=y)
    ctx.call("type_text", window_id=wid, text="LINE 1")
    tree = ctx.tree(wid)
    ctx.check("drag selected line one and typing replaced it", 'value="LINE 1\\nline two"' in tree, tree[:400])


@scenario
def parallel_apps(ctx):
    notes = ctx.launch_scratch_textedit("parallel.txt", "")
    calc = ctx.call("launch_app", app="Calculator", new_instance=True)
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


@scenario
def batch_calculator(ctx):
    calc = ctx.call("launch_app", app="Calculator", new_instance=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    wid = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])["id"]
    tree = ctx.tree(wid, include_screenshot=False)
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


@scenario
def agent_cursor_visible(ctx):
    window = ctx.launch_scratch_textedit("cursor.txt", "line one\nline two")
    if not window["is_on_screen"]:
        raise Skip("scratch window opened on a Space the user is not viewing; the agent cursor only shows on the current Space")
    wid = window["id"]
    _, frame = text_area(ctx, wid)
    local = (frame[0] + 3, frame[1] + 6)
    # The arrow fades soon after the click; waiting for a post-action observation could miss it.
    ctx.call("click", window_id=wid, x=local[0], y=local[1], observe="none")

    screen_x, screen_y = window["frame"]["x"] + local[0], window["frame"]["y"] + local[1]
    region = ctx.run.dir / "screenshots" / f"{ctx.name}-around-click.png"
    subprocess.run(["screencapture", "-x", "-R", f"{int(screen_x) - 10},{int(screen_y) - 10},48,48", str(region)], check=True)
    ctx.screenshots.append(region.relative_to(ctx.run.dir).as_posix())
    ctx.check("the agent's orange arrow is drawn on screen at the click point", accent_pixels(png_pixels(region.read_bytes())) >= 20)

    shot = ctx.mcp.call("screenshot", window_id=wid)[1][0]
    ctx.check("the agent's own window screenshot does not include its arrow",
              accent_pixels(png_pixels(base64.b64decode(shot["data"]))) < 5)
    ctx.call("type_text", window_id=wid, text="Z")
    ctx.check("the click still landed", value_of(ctx.tree(wid, include_screenshot=False)) == "Zline one\nline two")


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

    calc = ctx.call("launch_app", app="Calculator", new_instance=True)
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


@scenario
def batch_layout_guard(ctx):
    calc = ctx.call("launch_app", app="Calculator", new_instance=True)
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
    display = ctx.call("agent_display", action="create")
    ctx.require("agent display is active", display["is_active"] and display.get("bounds"), display)
    window = ctx.launch_scratch_textedit("hosted.txt", "hosted")
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
        handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(ROOT / "fixtures"))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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

        outline = ctx.call("browser_snapshot", tab_id=tid, include_screenshot=True)["outline"]
        field = re.search(r'\[(\w+)\] textbox "Name"', outline)
        ctx.require("snapshot exposes the Name textbox with a ref", field, outline[:500])
        ctx.call("browser_type", tab_id=tid, ref=field.group(1), text="second cursor", submit=True)
        time.sleep(0.5)
        outline = ctx.call("browser_snapshot", tab_id=tid)["outline"]
        ctx.check("typing + submit reached the page", "Hello, second cursor" in outline, outline[:500])

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
def pause(ctx):
    if not APP_BINARY.exists():
        raise Skip(f"{APP_BINARY} not installed")
    subprocess.run([str(APP_BINARY), "--pause"], check=True, timeout=10)
    try:
        time.sleep(0.5)
        try:
            ctx.call("list_windows")
            ctx.call("launch_app", app="Calculator", new_instance=True)
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
