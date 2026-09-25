#!/usr/bin/env python3
"""End-to-end suite for Second Cursor.

Drives the running app over MCP HTTP exactly like a client would, checks results through the
tools themselves, and checks the "never interrupt the user" invariants with an independent observer.

Usage:  e2e/run.py [scenario ...]        (default: every scenario, in order)
Output: e2e/artifacts/<run-id>/report.md, results.json, transcript.jsonl, screenshots/
"""
import base64
import datetime
import http.server
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = pathlib.Path.home() / "Library/Application Support/SecondCursor/config.json"
ACTION_LOG = pathlib.Path.home() / "Library/Logs/SecondCursor/actions.jsonl"
APP_BINARY = pathlib.Path.home() / "Applications/SecondCursor.app/Contents/MacOS/SecondCursor"
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
    def __init__(self, url, token, transcript):
        self.url, self.token, self.transcript = url, token, transcript
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
            with urllib.request.urlopen(req, timeout=90) as resp:
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
        return self.call("get_window_state", window_id=window_id, **opts)["tree"]


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

    ctx.call("press_key", window_id=wid, keys="cmd+a")
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
    match = re.search(rf'\] {role} "[^"]*" value="((?:[^"\\]|\\.)*)"', tree)
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
    def __init__(self):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = ROOT / "artifacts" / stamp
        (self.dir / "screenshots").mkdir(parents=True)
        self.current = "setup"
        self.transcript = Transcript(self.dir / "transcript.jsonl", self)
        config = json.loads(CONFIG_PATH.read_text())
        self.mcp = MCP(f"http://127.0.0.1:{config['port']}/mcp", config["token"], self.transcript)
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
        self.mcp.initialize()
        for fn in SCENARIOS:
            if names and fn.__name__ not in names:
                continue
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
        self.write_report()
        return all(r["status"] != "FAIL" for r in self.results)

    def write_report(self):
        (self.dir / "results.json").write_text(json.dumps({"baseline": self.baseline, "results": self.results}, indent=2))
        lines = [f"# Second Cursor E2E — {self.dir.name}", "",
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
