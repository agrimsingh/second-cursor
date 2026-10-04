"""Task-scoped public MCP clients for the native-tools benchmark track.

This is an engineering guard around a trusted model runner, not an OS sandbox.
The model receives engine-native schemas and every MCP content block. Host-only
setup/cleanup uses public tools too; it never invokes evaluator endpoints.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import pathlib
import queue
import re
import signal
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid

from benchmarks.workspace_fixtures import WORKSPACE_TASKS

PROTOCOL = "2025-03-26"
TASKS = {"native_notes", "browser_work_order", "canvas_route", "transient_dialog", *WORKSPACE_TASKS}


class EngineError(RuntimeError):
    pass


class UnsupportedEngine(EngineError):
    pass


class ScopeError(EngineError):
    """A solver-facing refusal authored by the local fixture scope validator.

    Messages must describe the public tool contract, never interpolate driver
    diagnostics, configuration, credentials or arbitrary model argument values.
    """


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def payloads(result):
    """Extract structured values for host bookkeeping; never replace model content."""
    if isinstance(result.get("structuredContent"), (dict, list)):
        yield result["structuredContent"]
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                value = json.loads(item["text"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, (dict, list)):
                yield value


def records(result):
    for value in payloads(result):
        yield from _objects(value)


def _unique(result, key, kind):
    values = {row[key] for row in records(result) if type(row.get(key)) is kind}
    if len(values) != 1:
        raise EngineError(f"Driver did not return one unambiguous {key}")
    return values.pop()


def _outcome_ok(data):
    """Inspect outcome fields, not arbitrary values inside an observed page/tree."""
    if not isinstance(data, dict):
        return True
    if (data.get("status") == "refused" or data.get("ok") is False
            or data.get("skipped") is True or data.get("uncertain") is True
            or data.get("stopped_early") is True):
        return False
    outcome, expectation = data.get("outcome"), data.get("expect")
    if isinstance(outcome, dict) and outcome.get("state") in ("failed", "uncertain"):
        return False
    if isinstance(expectation, dict) and expectation.get("status") in ("failed", "timeout"):
        return False
    # A batch can complete its transport successfully while only its first few
    # actions succeed. Preserve that distinction even with older driver replies
    # that lack the aggregate outcome annotation.
    return all(_outcome_ok(step) for key in ("steps", "results")
               if isinstance(data.get(key), list) for step in data[key])


def _ok(result):
    """Whether public tool outcome evidence is free of failure/uncertainty.

    This is not proof that the user's task succeeded. It does not rewrite the
    MCP response or prevent the model from observing and recovering from it.
    """
    if not isinstance(result, dict) or result.get("isError", False):
        return False
    return all(_outcome_ok(data) for data in payloads(result))


def _refusal_diagnostic(result):
    """Retain public refusal identifiers without echoing arbitrary private diagnostics."""
    data = result.get("structuredContent", {})
    refusal = data.get("refusal", {}) if isinstance(data, dict) else {}
    if not isinstance(refusal, dict):
        return ""
    detail = refusal.get("detail", {})
    values = [refusal.get("code"), detail.get("reason") if isinstance(detail, dict) else None]
    identifiers = [value for value in values if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value)]
    return " (" + ": ".join(identifiers) + ")" if identifiers else ""


def process_identity(pid):
    """Host-only process metadata for cleanup; never passed to the solver."""
    completed = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
                               capture_output=True, text=True, timeout=3)
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    # macOS ps reports an absent PID with status 1 and no output. Other
    # failures are not evidence that the owned process stopped.
    if completed.returncode == 1 and not completed.stdout.strip() and not completed.stderr.strip():
        return None
    raise EngineError("Process identity observation failed")


def verify_harness_browser(request):
    """Read-only attestation of the exact browser process supplied by the harness."""
    targets = request.get("targets", {})
    expected = request.get("browser_identity")
    if expected is None and not any(key in targets for key in ("browser_pid", "browser_window_id")):
        return {}
    if (not isinstance(expected, dict) or set(expected) != {"binary", "version", "sha256"}
            or not all(isinstance(expected.get(key), str) and expected[key] for key in expected)
            or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])):
        raise EngineError("Harness browser requires a complete pinned binary identity")
    pid, wid = targets.get("browser_pid"), targets.get("browser_window_id")
    if type(pid) is not int or pid <= 0 or type(wid) is not int or wid <= 0:
        raise EngineError("Pinned browser requires exact harness-provided PID and window")
    if request.get("browser_visibility") != "headed":
        raise EngineError("Matched browser benchmark requires a headed harness browser")
    birth = process_identity(pid)
    if birth is None:
        raise EngineError("Harness browser process is absent")
    observed = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="],
                              capture_output=True, text=True, timeout=3)
    if observed.returncode or not observed.stdout.strip():
        raise EngineError("Cannot inspect harness browser executable")
    observed_path = pathlib.Path(observed.stdout.strip())
    binary = observed_path.resolve()
    if (not observed_path.is_absolute() or not pathlib.Path(expected["binary"]).is_absolute()
            or binary != pathlib.Path(expected["binary"]).resolve()):
        raise EngineError("Harness browser process executable differs from the pinned browser")
    try:
        digest = hashlib.sha256()
        with binary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineError("Cannot inspect harness browser binary identity") from exc
    actual = {"binary": str(binary), "version": version.stdout.strip(), "sha256": digest.hexdigest()}
    if version.returncode or actual != expected:
        raise EngineError("Harness browser binary identity differs from the pinned browser")
    if process_identity(pid) != birth:
        raise EngineError("Harness browser process changed during identity verification")
    return {"browser_identity": actual, "browser_identity_verified": True,
            "browser_visibility": "headed", "harness_browser": {
                "pid": pid, "window_id": wid, "process_identity": birth,
                "identity_method": "ps-lstart-comm", "ownership_method": "harness",
                "cleanup_owner": "harness"}}


class MCPClient:
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.next_id = 0
        self.protocol = PROTOCOL
        self.server_info = {}

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                         "clientInfo": {"name": "second-cursor-benchmark", "version": "1"}})
        self.protocol = result.get("protocolVersion", PROTOCOL)
        self.server_info = result.get("serverInfo", {})
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        tools, cursor, seen = [], None, set()
        while True:
            page = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(page.get("tools"), list):
                raise EngineError("MCP tools/list returned no tool array")
            tools.extend(page["tools"])
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
            if cursor in seen:
                raise EngineError("MCP tools/list repeated its cursor")
            seen.add(cursor)

    def call(self, name, args):
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        if not isinstance(result, dict) or not isinstance(result.get("content", []), list):
            raise EngineError("MCP tool returned malformed content")
        return result

    def _message(self, method, params, notification=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            self.next_id += 1
            payload["id"] = self.next_id
        return payload

    @staticmethod
    def _result(message, expected):
        if not isinstance(message, dict) or message.get("id") != expected:
            raise EngineError("MCP response id does not match request")
        if "error" in message:
            # Server diagnostics can echo private config; keep credentials out of telemetry.
            code = message.get("error", {}).get("code", "unknown")
            raise EngineError(f"MCP request failed (code {code})")
        if "result" not in message:
            raise EngineError("MCP response has no result")
        return message["result"]


class HTTPMCP(MCPClient):
    """One persistent streamable-HTTP session. No launch fallback or retries."""
    def __init__(self, url, token, timeout=60):
        super().__init__(timeout)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/mcp" or parsed.query or parsed.fragment or parsed.username:
            raise EngineError("Benchmark MCP URL must be a local /mcp endpoint")
        if not isinstance(token, str) or not token:
            raise EngineError("Fixture MCP bearer token is missing")
        self.host, self.port, self.path = parsed.hostname, parsed.port, parsed.path
        self._token, self.session_id = token, None
        self.connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        self.poisoned = False

    def _send(self, method, message=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self._token, "MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            self.connection.request(method, self.path, None if message is None else json.dumps(message), headers)
            response = self.connection.getresponse()
            if message and message.get("method") == "initialize":
                self.session_id = response.getheader("Mcp-Session-Id")
            body = response.read()
            if not 200 <= response.status < 300:
                raise EngineError(f"MCP HTTP request failed (status {response.status}); it was not retried")
            if not body:
                return None
            if "text/event-stream" in response.getheader("Content-Type", ""):
                # MCP can put notifications before its JSON-RPC response.
                messages = []
                for event in body.decode().replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        messages.append(json.loads(data))
                return next((item for item in reversed(messages) if item.get("id") == message.get("id")), None)
            return json.loads(body)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.poisoned = True
            self.connection.close()
            raise EngineError("MCP transport failed; delivery may be uncertain and no action was retried") from exc

    def rpc(self, method, params=None):
        if self.poisoned:
            raise EngineError("MCP transport is uncertain; refusing further actions")
        payload = self._message(method, params)
        result = self._result(self._send("POST", payload), payload["id"])
        if method == "initialize" and not self.session_id:
            raise EngineError("MCP server did not issue a session id")
        return result

    def notify(self, method, params=None):
        self._send("POST", self._message(method, params, notification=True))

    def close(self):
        try:
            if self.session_id:
                # A new connection may be needed after a failed request; DELETE is
                # cancellation/cleanup, never a retry of the uncertain input.
                if self.poisoned:
                    self.connection = http.client.HTTPConnection(self.host, self.port, timeout=min(5, self.timeout))
                self._send("DELETE")
        finally:
            self.connection.close()
            self.session_id = None


class StdioMCP(MCPClient):
    """Newline-framed public MCP subprocess, retaining its app-daemon identity."""
    def __init__(self, command, timeout=60, env=None):
        super().__init__(timeout)
        self.command, self.env = command, env
        self.process = None
        self.incoming = queue.Queue()
        self.poisoned = False

    def start(self):
        if self.process:
            return
        # stderr is not model input, and may contain private daemon configuration.
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                        start_new_session=True, env=self.env)
        process = self.process
        def reader():
            try:
                for line in process.stdout:
                    if line.strip():
                        try:
                            self.incoming.put(json.loads(line))
                        except ValueError:
                            self.incoming.put(EngineError("MCP stdout was not JSON-RPC"))
            finally:
                self.incoming.put(EngineError("MCP subprocess exited before responding"))
        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()

    def _write(self, payload):
        self.start()
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            self.poisoned = True
            raise EngineError("MCP subprocess input closed; action was not retried") from exc

    def rpc(self, method, params=None):
        if self.poisoned:
            raise EngineError("MCP transport is uncertain; refusing further actions")
        payload = self._message(method, params)
        self._write(payload)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.incoming.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                self.poisoned = True
                raise EngineError("MCP request timed out; delivery is uncertain and no action was retried") from exc
            if isinstance(message, Exception):
                self.poisoned = True
                raise message
            if "id" not in message:  # server notifications
                continue
            if "method" in message:
                # No sampling, elicitation or host resource access is available.
                self._write({"jsonrpc": "2.0", "id": message["id"],
                             "error": {"code": -32601, "message": "Client capability unavailable"}})
                continue
            return self._result(message, payload["id"])

    def notify(self, method, params=None):
        self._write(self._message(method, params, notification=True))

    def close(self):
        if not self.process:
            return
        process, self.process = self.process, None
        try:
            process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        finally:
            # The MCP proxy may exit before its helper children. Its private
            # process group belongs to this adapter even after the leader exits.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.reader.join(timeout=.15)
            # A helper may ignore SIGTERM after the proxy exits, or keep stdout
            # open forever. Kill the owned group before closing the pipe; closing
            # a buffered pipe while the reader holds it can otherwise deadlock.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.reader.join(timeout=1)
            process.stdout.close()


SC_NATIVE = {"get_window_state", "find_elements", "inspect_element", "screenshot", "wait_for",
             "click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action", "select_text", "batch", "act"}
SC_BROWSER = {"browser_open", "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
              "browser_press_key", "browser_scroll", "browser_screenshot", "browser_close", "browser_command", "batch", "act"}
CUA_NATIVE = {"get_window_state", "click", "double_click", "right_click", "drag", "scroll", "type_text",
              "press_key", "hotkey", "set_value", "zoom", "verify_state", "parse_visual_regions"}
CUA_BROWSER = {"get_browser_state", "browser_navigate", "browser_click", "browser_type", "browser_pointer", "browser_dialog"}
CUA_WEB_NATIVE = {"get_window_state", "click", "type_text", "set_value", "press_key", "hotkey"}
BROWSER_OPERATIONS = {"capabilities", "dom_snapshot", "inspect", "click", "hover", "fill", "type", "paste", "select_text", "check",
                      "select_option", "perform_action", "press_key", "scroll", "drag", "wait", "dialog", "screenshot"}
BROWSER_ARGUMENTS = {"locator", "ref", "text", "value", "values", "checked", "selection_type", "prefix", "suffix",
                     "keys", "direction", "amount", "x", "y", "from_x", "from_y", "to_x", "to_y", "button", "count",
                     "delta_x", "delta_y", "timeout_ms", "state", "accept", "prompt_text", "full", "max_elements",
                     "offset", "length", "format", "submit", "caption", "modifiers", "action", "full_page", "clip"}
HIDDEN_FIELDS = {"session", "session_id", "operation_id", "observation_id", "app", "target",
                 "screenshot_out_file", "debug_image_out", "browser_id", "include_menu_bar"}
SAFE_KEYS = {"return", "enter", "tab", "escape", "esc", "up", "down", "left", "right", "space", "delete",
             "backspace", "home", "end", "pageup", "pagedown", "cmd+a", "command+a", "ctrl+a",
             "cmd+left", "cmd+right", "cmd+up", "cmd+down", "shift+left", "shift+right", "shift+up", "shift+down",
             "cmd+shift+left", "cmd+shift+right", "cmd+shift+up", "cmd+shift+down"}
KEY_ALIASES = {"arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right"}
MODIFIER_ALIASES = {"command": "cmd", "super": "cmd", "control": "ctrl"}
KEY_SCOPE_DESCRIPTION = (
    " Fixture keyboard scope: letters/digits, Return/Enter, Tab, Escape/Esc, Space, Delete/Backspace, "
    "Home/End, PageUp/PageDown, Up/Down/Left/Right (also ArrowUp/ArrowDown/ArrowLeft/ArrowRight); "
    "Cmd+A or Ctrl+A; arrows with Cmd, Shift, or Cmd+Shift. Names are case-insensitive; "
    "Command/Super mean Cmd and Control means Ctrl. Clipboard, browser chrome, file and app shortcuts are unavailable."
)


class ScopedEngine:
    def __init__(self, name, request, client, emit=None, clock_start=None, pinned_identity=None):
        if request.get("task_id") not in TASKS:
            raise UnsupportedEngine("Unknown benchmark task")
        if request.get("track", "host-background") != "host-background":
            raise UnsupportedEngine("Only the host-background track is supported")
        self.name, self.request, self.client = name, copy.deepcopy(request), client
        self.emit, self.clock_start = emit or (lambda event: None), clock_start or time.monotonic()
        self.native = request["task_id"] == "native_notes"
        self.targets = request.get("targets", {})
        self.windows = {self.targets[key] for key in ("calculator_window_id", "notes_window_id") if type(self.targets.get(key)) is int}
        if self.native and len(self.windows) != 2:
            raise ScopeError("Native task requires exactly two distinct fixture windows")
        self.url = self.targets.get("url")
        if not self.native:
            parsed = urllib.parse.urlsplit(self.url or "")
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.path != "/fixture.html" or parsed.username or parsed.fragment:
                raise ScopeError("Browser task requires the local fixture.html URL")
        self.owned_tabs, self.window_pids = set(), {}
        self.owned_browser_pid = None
        self.owned_browser_identity = None
        self.element_tokens, self.snapshot_ids, self.capture_ids = {}, {}, {}
        self.cua_target = self.cua_tab = None
        self.session = "benchmark-" + uuid.uuid4().hex
        self.context = {"engine": name, "permitted_targets": copy.deepcopy(self.targets)}
        self.initial_observations, self.cleanup_errors = [], []
        self.identity, self.catalog, self.allowed = {}, {}, {}
        self.pinned_identity = pinned_identity or {}
        self.started = self.closed = False

    def _invoke(self, name, args, phase="model"):
        start, ok, transport_status = (time.monotonic() - self.clock_start) * 1000, False, "error"
        try:
            result = self.client.call(name, args)
            transport_status = "completed"
            ok = _ok(result)
            return result
        finally:
            self.emit({"type": "tool", "name": name, "phase": phase, "start_ms": start,
                       "end_ms": (time.monotonic() - self.clock_start) * 1000, "ok": ok,
                       "transport_status": transport_status, "retry": False})

    def _host(self, name, args, phase="setup"):
        if name not in self.catalog:
            raise UnsupportedEngine(f"Driver does not advertise required public tool {name}")
        args = copy.deepcopy(args)
        if self.name == "cua-driver" and "session" in self.catalog[name].get("inputSchema", {}).get("properties", {}):
            args["session"] = self.session
        result = self._invoke(name, args, phase)
        if not _ok(result):
            raise EngineError(f"Driver refused {name} during {phase}" + _refusal_diagnostic(result))
        return result

    def start(self):
        if self.started:
            return self
        browser_identity = verify_harness_browser(self.request) if not self.native else {}
        self.client.initialize()
        self.started = True
        self.catalog = {tool["name"]: tool for tool in self.client.list_tools()}
        self.identity = {"engine": self.name, "server": self.client.server_info, "protocol": self.client.protocol,
                         "interface_track": "native-tools", "scope_policy": "fixture-only-v1",
                         **self.pinned_identity, **browser_identity}
        if self.name == "cua-driver":
            expected = self.pinned_identity.get("semantic_version")
            actual = self.client.server_info.get("version")
            if expected and isinstance(actual, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", actual):
                if expected != actual:
                    raise EngineError("Cua MCP daemon version differs from the pinned CLI version")
                self.identity["daemon_version_consistency"] = "verified"
            else:
                self.identity["daemon_version_consistency"] = "unavailable"
            self._start_cua()
        elif "harness_browser" in self.identity:
            self._start_sc_browser()
        names = (SC_NATIVE if self.native else SC_BROWSER) if self.name == "second-cursor" else (CUA_NATIVE if self.native else CUA_BROWSER | CUA_WEB_NATIVE)
        if self.name == "second-cursor" and "harness_browser" in self.identity:
            names = names - {"browser_open"}  # The matched headed profile uses its visible initial tab.
        self.allowed = {name: self._schema(self.catalog[name]) for name in names if name in self.catalog}
        if not self.allowed:
            raise UnsupportedEngine("Driver advertises none of the task's allowed tools")
        return self

    def _start_sc_browser(self):
        # The fixture SC instance is connected only to the harness's disposable
        # profile. Discovery stays host-only; claim precisely its sole blank tab.
        observed = self._host("browser_command", {"operation": "user_tabs"})
        tabs = {}
        for value in payloads(observed):
            if not isinstance(value, dict) or not isinstance(value.get("tabs"), list):
                continue
            for tab in value["tabs"]:
                if not isinstance(tab, dict) or type(tab.get("id")) is not int:
                    raise EngineError("Fixture browser returned a malformed tab identity")
                if tab["id"] in tabs and tabs[tab["id"]] != tab:
                    raise EngineError("Fixture browser returned inconsistent tab identities")
                tabs[tab["id"]] = tab
        if len(tabs) != 1 or next(iter(tabs.values())).get("url") != "about:blank":
            raise EngineError("Harness browser requires exactly one initial blank tab")
        tid = next(iter(tabs))
        claimed = self._host("browser_command", {"operation": "claim", "tab_id": tid})
        if not any(row.get("tab", {}).get("id") == tid and row["tab"].get("url") == "about:blank"
                   for row in records(claimed) if isinstance(row.get("tab"), dict)):
            raise EngineError("Driver did not confirm the exact fixture tab claim")
        self.owned_tabs.add(tid)
        self.context["browser"] = {"tab_id": tid, "url": self.url,
                                   "pid": self.targets["browser_pid"], "window_id": self.targets["browser_window_id"]}
        self.identity["harness_browser"]["tab_binding"] = "single_initial_blank_tab_claim"
        self.initial_observations.append(claimed)

    def _start_cua(self):
        if self.native:
            for prefix in ("calculator", "notes"):
                pid = self.targets.get(prefix + "_pid")
                if type(pid) is not int or pid <= 0:
                    raise UnsupportedEngine("Cua native fixture requires exact harness-provided PIDs")
                self.window_pids[self.targets[prefix + "_window_id"]] = pid
            return
        if "harness_browser" in self.identity:
            # The harness owns this newly created disposable browser. Public
            # binding may refuse it; never replace it with a different browser.
            pid, wid = self.targets["browser_pid"], self.targets["browser_window_id"]
            prepared = self._host("browser_prepare", {"pid": pid, "window_id": wid,
                                                       "strategy": {"kind": "existing_profile"}})
            attachment = prepared.get("structuredContent", {})
            if (not isinstance(attachment, dict) or attachment.get("action") != "attached_existing_profile"
                    or attachment.get("prepared") is not True or attachment.get("prepared_pid") != pid):
                raise EngineError("Cua did not confirm attachment to the exact harness browser")
            self.identity["harness_browser"]["attachment"] = "existing_profile"
        else:
            pid, wid = self._launch_cua_browser()
        self.windows = {wid}
        self.window_pids[wid] = pid
        bound = self._host("get_browser_state", {"pid": pid, "window_id": wid})
        self.cua_target = _unique(bound, "target_id", str)
        self.cua_tab = _unique(bound, "tab_id", str)
        self.context["browser"] = {"target_id": self.cua_target, "tab_id": self.cua_tab, "url": self.url,
                                   "pid": pid, "window_id": wid,
                                   "native_actions": "Only control tokens observed under this window's AXWebArea or attached AXSheet/AXDialog are authorized; browser chrome, unscoped native keys, and native pixel actions are unavailable."}
        self.initial_observations.append(bound)

    def _launch_cua_browser(self):
        # This inventory stays in the host; it is never included in model context.
        before = self._host("list_apps", {})
        before_pids = {row["pid"] for row in records(before) if type(row.get("pid")) is int and row["pid"] > 0}
        if not before_pids:
            raise EngineError("Cannot establish browser process ownership before setup")
        prepared = self._host("browser_prepare", {"allow_launch": True, "profile": {"mode": "isolated_new"}})
        preparation = prepared.get("structuredContent", {})
        ownership = preparation.get("endpoint_ownership", {})
        pid = preparation.get("prepared_pid")
        if (preparation.get("action") != "launched_isolated_browser" or preparation.get("prepared") is not True
                or type(pid) is not int or ownership.get("owner_pid") != pid
                or ownership.get("method") != "spawned_by_driver"):
            raise EngineError("Cua did not return verified isolated browser ownership metadata")
        if pid <= 0 or pid in before_pids:
            raise EngineError("Cua did not prove a newly owned isolated browser process")
        identity = process_identity(pid)
        if identity is None:
            raise EngineError("New Cua browser process identity could not be verified")
        self.owned_browser_pid = pid
        self.owned_browser_identity = identity
        self.identity["owned_browser"] = {"pid": pid, "process_identity": identity,
                                          "identity_method": "ps-lstart-comm", "ownership_method": "spawned_by_driver"}
        self.identity["cleanup_evidence"] = {"owned_browser": {"verified": False, "status": "pending"}}
        deadline = time.monotonic() + 3
        while True:
            windows = self._host("list_windows", {"pid": pid})
            matching = {row["window_id"] for row in records(windows)
                        if row.get("pid") == pid and type(row.get("window_id")) is int
                        and row.get("title") == "about:blank"}
            if matching or time.monotonic() >= deadline:
                break
            time.sleep(.1)  # Window creation/title publication can follow launch acknowledgement.
        if len(matching) != 1:
            raise EngineError("Isolated Cua browser does not have one unambiguous blank document window")
        return pid, matching.pop()

    def _schema(self, tool):
        tool = copy.deepcopy(tool)
        schema = tool.setdefault("inputSchema", {"type": "object", "properties": {}})
        props = schema.setdefault("properties", {})
        for field in HIDDEN_FIELDS:
            props.pop(field, None)
        if self.name == "cua-driver":
            props.pop("scope", None)  # Cua's desktop mode; SC scope is a safe AX subtree.
        schema["required"] = [key for key in schema.get("required", []) if key in props]
        schema["additionalProperties"] = False
        name = tool["name"]
        if self.native or self.name == "cua-driver" and name in CUA_WEB_NATIVE:
            props.pop("tab_id", None)
            props.pop("target_id", None)
            if name != "parse_visual_regions":
                props["window_id"] = {"type": "integer", "enum": sorted(self.windows), "description": "Exact permitted fixture window."}
                if "window_id" not in schema["required"]:
                    schema["required"].append("window_id")
            if "pid" in props:
                props["pid"]["enum"] = sorted(set(self.window_pids.values()))
            if not self.native and name != "get_window_state":
                for key in ("x", "y", "from_x", "from_y", "to_x", "to_y", "from_zoom"):
                    props.pop(key, None)
                if "element_token" in props:
                    schema["required"] = list(dict.fromkeys(schema["required"] + ["element_token"]))
        else:
            props.pop("window_id", None)
            props.pop("pid", None)
            if name in {"batch", "act"}:
                props.pop("expect", None)  # Native AX postconditions do not apply to a browser tab.
            if "tab_id" in props and name != "browser_command":
                schema["required"] = list(dict.fromkeys(schema["required"] + ["tab_id"]))
        if "url" in props:
            props["url"]["enum"] = [self.url]
        if "delivery_mode" in props:
            props["delivery_mode"]["enum"] = ["background"]
        if self.request["task_id"] == "canvas_route" and "input_route" in props:
            props["input_route"]["enum"] = ["trusted"]
        if name == "browser_command":
            props["operation"]["enum"] = sorted(BROWSER_OPERATIONS)
            props["arguments"]["description"] += " Fixture-only benchmark: use visible-page locators, text/value, check/select_option, real drag coordinates, wait, or dialog accept. No script, CDP, files, navigation, or evaluator access." + KEY_SCOPE_DESCRIPTION
        for field in ("keys", "key"):
            if field in props:
                props[field]["description"] = props[field].get("description", "") + KEY_SCOPE_DESCRIPTION
        if name in {"batch", "act"}:
            step_props = props.get("steps", {}).get("items", {}).get("properties", {})
            if "keys" in step_props:
                step_props["keys"]["description"] = step_props["keys"].get("description", "") + KEY_SCOPE_DESCRIPTION
        schema["required"] = [key for key in schema["required"] if key in props]
        tool["description"] = tool.get("description", "") + " Benchmark scope: only the provided fixture targets; background delivery only."
        return tool

    def tools(self):
        if not self.started:
            raise EngineError("Engine has not started")
        return copy.deepcopy(list(self.allowed.values()))

    def _keys(self, args):
        value = args.get("keys", args.get("key"))
        if value is None:
            return
        chord = "+".join(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else value
        modifiers = args.get("modifiers", [])
        if modifiers:
            if not isinstance(chord, str) or not isinstance(modifiers, list) or not all(isinstance(x, str) for x in modifiers):
                raise ScopeError("Invalid key modifiers")
            chord = "+".join(modifiers + [chord])
        if isinstance(chord, str):
            parts = [part.strip().lower() for part in chord.split("+")]
            # Chord modifier ordering is immaterial to both public drivers. Keep
            # duplicates/unknown modifiers rejected by the canonical allowlist.
            parts = sorted(MODIFIER_ALIASES.get(part, part) for part in parts[:-1]) + [KEY_ALIASES.get(parts[-1], parts[-1])]
            chord = "+".join(parts)
        if not isinstance(chord, str) or chord not in SAFE_KEYS and not (len(chord) == 1 and chord.isalnum()):
            raise ScopeError("This keyboard shortcut can escape the fixture or touch the clipboard")

    @staticmethod
    def _locator(locator):
        if not isinstance(locator, dict) or set(locator) - {"selector", "role", "label", "name", "text", "placeholder", "test_id", "exact", "nth", "visible"}:
            raise ScopeError("Only rendered-page semantic or simple CSS locators are permitted")
        if locator.get("visible", True) is not True:
            raise ScopeError("Hidden implementation elements are outside the benchmark")
        if "selector" in locator:
            selector = locator["selector"]
            safe = {"button", "input", "select", "textarea", "canvas", "p", "label", "option", "section", "form", "main",
                    "h1", "h2", "h3", "h4", "h5", "h6"}
            if not isinstance(selector, str) or selector not in safe and not re.fullmatch(r"[.#][A-Za-z_][A-Za-z0-9_-]*", selector):
                raise ScopeError("Inspect only rendered controls, simple fixture IDs/classes, or semantic locators")
        elif not any(isinstance(locator.get(key), str) and locator[key] for key in ("role", "label", "name", "text", "placeholder", "test_id")):
            raise ScopeError("A rendered-page locator needs a semantic anchor")
        return {**locator, "visible": True}

    def _validate(self, name, args, nested=False):
        if name not in self.allowed:
            raise ScopeError("Tool is outside this task's allowlist")
        if not isinstance(args, dict):
            raise ScopeError("Tool arguments must be an object")
        args = copy.deepcopy(args)
        schema = self.allowed[name]["inputSchema"]
        if self.name == "second-cursor" and not self.native and name in {"batch", "act"} and "expect" in args:
            raise ScopeError("Browser tab batches do not support expect; inspect the returned observation instead")
        if set(args) - set(schema.get("properties", {})):
            raise ScopeError("Tool includes an unadvertised or host-only argument")
        missing = [key for key in schema.get("required", []) if key not in args]
        if missing:
            # Names come from the public schema; never echo arbitrary values.
            names = [key for key in missing if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)]
            raise ScopeError("Required tool arguments are missing" + (": " + ", ".join(names) if names else ""))
        if args.get("delivery_mode", "background") != "background":
            raise ScopeError("Foreground delivery is outside the benchmark track")
        if "url" in args and args["url"] != self.url:
            raise ScopeError("Navigation must match the exact task URL")
        self._keys(args)
        if self.native or self.name == "cua-driver" and name in CUA_WEB_NATIVE:
            if name == "parse_visual_regions":
                if args.get("capture_id") not in self.capture_ids:
                    raise ScopeError("Capture was not observed from a permitted fixture window")
                args["session"] = self.session
                return args
            wid = args.get("window_id")
            if type(wid) is not int or wid not in self.windows:
                raise ScopeError("Window is not an authorized task fixture")
            if self.name == "cua-driver":
                pid = self.window_pids[wid]
                if "pid" in args and (type(args["pid"]) is not int or args["pid"] != pid):
                    raise ScopeError("PID does not own the authorized fixture window")
                if "pid" in self.catalog[name].get("inputSchema", {}).get("properties", {}):
                    args["pid"] = pid
                if "element_token" in args and args["element_token"] not in self.element_tokens.get(wid, set()):
                    raise ScopeError("Element token was not observed for this exact fixture window")
                if not self.native and name != "get_window_state" and args.get("element_token") not in self.element_tokens.get(wid, set()):
                    raise ScopeError("Browser native action requires an observed page or attached-modal descendant token")
                if "snapshot_id" in args and args["snapshot_id"] not in self.snapshot_ids.get(wid, set()):
                    raise ScopeError("Snapshot was not observed for this exact fixture window")
                if "capture_id" in args and self.capture_ids.get(args["capture_id"]) != wid:
                    raise ScopeError("Capture was not observed for this exact fixture window")
            if name == "perform_action" and args.get("action") not in {"AXPress", "AXConfirm", "AXCancel", "AXPick", "AXShowMenu", "AXIncrement", "AXDecrement"}:
                raise ScopeError("Accessibility action is not fixture-scoped")
        else:
            if self.name == "second-cursor" and name != "browser_open":
                static_help = name == "browser_command" and args.get("operation") == "capabilities" and "tab_id" not in args
                if not static_help and (type(args.get("tab_id")) is not int or args["tab_id"] not in self.owned_tabs):
                    raise ScopeError("Tab was not opened by this trial")
            elif self.name == "cua-driver":
                if args.get("target_id") != self.cua_target or args.get("tab_id") != self.cua_tab:
                    raise ScopeError("Browser target or tab is outside this trial")
                if "pid" in args or "window_id" in args:
                    raise ScopeError("Browser rebinding is a host-only operation")
        if self.request["task_id"] == "canvas_route" and args.get("input_route", "trusted") != "trusted":
            raise ScopeError("Canvas task requires real pointer input")
        if name == "browser_command":
            if args.get("operation") not in BROWSER_OPERATIONS:
                raise ScopeError("Browser operation is outside the task allowlist")
            fields = args.get("arguments", {})
            if not isinstance(fields, dict) or set(fields) - BROWSER_ARGUMENTS:
                raise ScopeError("Browser operation includes an unsafe argument")
            if args["operation"] == "capabilities" and fields:
                raise ScopeError("Static browser capability help takes no operation arguments")
            if args["operation"] == "perform_action" and fields.get("action") not in {"focus", "scroll_into_view", "expand", "collapse", "increment", "decrement"}:
                raise ScopeError("Browser action is outside the rendered-control allowlist")
            if "modifiers" in fields:
                if (args["operation"] not in {"click", "hover"} or not isinstance(fields["modifiers"], list)
                        or any(key not in {"alt", "ctrl", "cmd", "shift"} for key in fields["modifiers"])):
                    raise ScopeError("Invalid pointer modifiers")
            if "clip" in fields:
                clip = fields["clip"]
                if (args["operation"] != "screenshot" or not isinstance(clip, dict)
                        or set(clip) != {"x", "y", "width", "height"}
                        or any(type(value) not in {int, float} or value < 0 for value in clip.values())):
                    raise ScopeError("Screenshot clip must be a nonnegative viewport rectangle")
            if "locator" in fields:
                fields["locator"] = self._locator(fields["locator"])
            if args["operation"] == "inspect" and "locator" not in fields:
                raise ScopeError("Inspect requires a rendered-page locator")
            if "arguments" in args:
                args["arguments"] = fields
            self._keys(fields)
        if name in {"batch", "act"}:
            if nested:
                raise ScopeError("Nested batches are not allowed")
            steps = args.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
                raise ScopeError("Batch must contain 1 to 50 steps")
            for step in steps:
                if not isinstance(step, dict):
                    raise ScopeError("Malformed batch step")
                child = dict(step)
                action = child.pop("tool", None)
                wrapped = child.pop("args", {})
                if not isinstance(wrapped, dict) or set(child) & set(wrapped):
                    raise ScopeError("Ambiguous batch arguments")
                child.update(wrapped)
                if action == "wait":
                    if set(child) != {"ms"} or type(child["ms"]) is not int or not 0 <= child["ms"] <= 10000:
                        raise ScopeError("Invalid bounded batch wait")
                    continue
                if action in {"batch", "act", "browser_open", "browser_close", "browser_navigate", "browser_command"}:
                    raise ScopeError("Batch action is not permitted")
                child.setdefault("window_id" if self.native else "tab_id", args.get("window_id" if self.native else "tab_id"))
                self._validate(action, child, nested=True)
        if self.name == "cua-driver" and "session" in self.catalog[name].get("inputSchema", {}).get("properties", {}):
            args["session"] = self.session
        return args

    def call(self, name, args):
        if self.closed:
            raise EngineError("Engine is closed")
        arguments = self._validate(name, args)
        result = self._invoke(name, arguments)
        if _ok(result) and self.name == "cua-driver" and name == "get_window_state":
            result = self._project_window(result, arguments["window_id"])
            wid = arguments["window_id"]
            rows = list(records(result))
            self.element_tokens[wid] = {row["element_token"] for row in rows if isinstance(row.get("element_token"), str) and row.get("role") not in {"AXWindow", "AXWebArea", "AXSheet", "AXDialog"}
                                        and row.get("subrole") not in {"AXDialog", "AXSystemDialog"}}
            self.snapshot_ids[wid] = {row["snapshot_id"] for row in rows if isinstance(row.get("snapshot_id"), str)}
            for row in rows:
                if isinstance(row.get("capture_id"), str):
                    self.capture_ids[row["capture_id"]] = wid
        if _ok(result) and self.name == "cua-driver" and name in {"browser_navigate", "browser_type", "browser_click", "browser_pointer", "browser_dialog"}:
            self.element_tokens.clear()
            self.snapshot_ids.clear()
        if _ok(result) and name == "browser_open" and self.name == "second-cursor":
            ids = {row["id"] for row in records(result) if type(row.get("id")) is int and row.get("url") == self.url}
            if len(ids) != 1:
                raise EngineError("Browser open did not return exactly one fixture-owned tab")
            self.owned_tabs.add(ids.pop())
        if _ok(result) and name == "browser_close":
            self.owned_tabs.discard(arguments["tab_id"])
        return result

    def _project_window(self, result, wid):
        """Cua also returns app/Apple menus; retain only the permitted AX subtree.

        The projection is explicit and keeps original element handles and image
        blocks. It never manufactures a successful action or hidden UI state.
        """
        result = copy.deepcopy(result)
        data = result.get("structuredContent", {})
        elements = data.get("elements", [])
        by_index = {row["element_index"]: row for row in elements if isinstance(row, dict) and type(row.get("element_index")) is int}
        def descendants(roots):
            kept = []
            for row in elements:
                index, seen = row.get("element_index"), set()
                while index in by_index and index not in seen:
                    if index in roots:
                        kept.append(row)
                        break
                    seen.add(index)
                    index = by_index[index].get("parent_index")
            return kept
        window_roots = {index for index, row in by_index.items() if row.get("role") == "AXWindow"}
        window_rows = descendants(window_roots) if len(window_roots) == 1 and data.get("window_id") == wid else []
        if self.native:
            selected = window_rows
        else:
            content_roots = {row["element_index"] for row in window_rows
                             if row.get("role") in {"AXWebArea", "AXSheet", "AXDialog"}
                             or row.get("subrole") in {"AXDialog", "AXSystemDialog"}}
            selected = descendants(content_roots)
        data["elements"] = selected
        data["tree_markdown"] = "\n".join(
            f"- [{row.get('element_token', row.get('element_index'))}] {row.get('role', '')} "
            + json.dumps({key: row[key] for key in ("label", "value", "actions", "frame") if key in row}, ensure_ascii=False)
            for row in selected)
        data["benchmark_scope_projection"] = {"scope": "fixture_window" if self.native else "fixture_web_area_or_attached_modal",
                                               "returned_elements": len(selected), "omitted_elements": len(elements) - len(selected),
                                               "original_counts_preserved": True}
        result["structuredContent"] = data
        result["content"] = [{"type": "text", "text": "Benchmark AX scope projection; original handles and metadata preserved. "
                               + ("No permitted AXWebArea or attached sheet/dialog was observed. Re-observe the page or use the exact typed browser tools. " if not self.native and not selected else "")
                               + json.dumps(data, ensure_ascii=False)}] + [row for row in result.get("content", []) if row.get("type") != "text"]
        return result

    def _verify_owned_browser_cleanup(self):
        """Independently confirm absence after cleanup requests have returned."""
        if self.owned_browser_pid is None:
            return
        started = time.monotonic()
        deadline = started + 2
        evidence = {"pid": self.owned_browser_pid, "identity_method": "ps-lstart-comm",
                    "verified": False, "status": "pending", "checks": 0}
        self.identity.setdefault("cleanup_evidence", {})["owned_browser"] = evidence
        while True:
            evidence["checks"] += 1
            try:
                current = process_identity(self.owned_browser_pid)
            except (EngineError, OSError, subprocess.SubprocessError):
                evidence["status"] = "observation_failed"
                self.cleanup_errors.append("Could not verify owned browser process absence")
                break
            if current is None:
                evidence.update(verified=True, status="absent", original_process_absent=True)
                break
            if current != self.owned_browser_identity:
                # The original process has stopped. A new process using its PID
                # is outside our ownership and must never be signalled.
                evidence.update(verified=True, status="pid_reused", original_process_absent=True,
                                replacement_not_signalled=True)
                break
            if time.monotonic() >= deadline:
                evidence.update(status="still_running", original_process_absent=False)
                self.cleanup_errors.append("Owned browser process is still running after cleanup")
                break
            time.sleep(.1)
        evidence["elapsed_ms"] = (time.monotonic() - started) * 1000

    def close(self):
        if self.closed:
            return self.cleanup_errors
        self.closed = True
        try:
            for tab in sorted(self.owned_tabs):
                try:
                    self._host("browser_close", {"tab_id": tab}, "cleanup")
                except EngineError as exc:
                    self.cleanup_errors.append(str(exc))
            if self.name == "cua-driver" and self.started:
                # No tab-close exists in Cua's typed browser surface. This process
                # was created with isolated_new, has no personal pages or data,
                # and was proven absent from the pre-launch process inventory.
                if self.owned_browser_pid is not None:
                    try:
                        identity = process_identity(self.owned_browser_pid)
                        if identity == self.owned_browser_identity:
                            self._host("kill_app", {"pid": self.owned_browser_pid}, "cleanup")
                    except (EngineError, OSError, subprocess.SubprocessError):
                        self.cleanup_errors.append("Owned browser termination could not be confirmed")
                try:
                    self._host("end_session", {"session": self.session}, "cleanup")
                except EngineError as exc:
                    self.cleanup_errors.append(str(exc))
        finally:
            try:
                self.client.close()
            except Exception:
                self.cleanup_errors.append("MCP session cleanup failed")
            # The original identity dict is retained by the result writer, so
            # these observations become part of the final engine identity.
            self._verify_owned_browser_cleanup()
        return self.cleanup_errors

    def __enter__(self):
        try:
            return self.start()
        except Exception:
            self.close()
            raise

    def __exit__(self, *exc):
        errors = self.close()
        if errors and not exc[0]:
            raise EngineError("; ".join(errors))


def create_engine(name, request, emit=None, clock_start=None, env=None):
    env = dict(os.environ if env is None else env)
    timeout = min(float(request.get("timeout_seconds", 60)), 60)
    if name in {"second-cursor", "second-cursor-model"}:
        name = "second-cursor"
        url, token = env.get("SC_BENCH_MCP_URL"), env.get("SC_BENCH_MCP_TOKEN")
        if bool(url) != bool(token):
            raise UnsupportedEngine("Explicit benchmark MCP URL and token must be provided together")
        if not url or not token:
            home = env.get("SC_HOME")
            if not home:
                raise UnsupportedEngine("No ephemeral SC_HOME or explicit benchmark MCP credentials were provided")
            try:
                config = json.loads((pathlib.Path(home) / "config.json").read_text())
                url, token = "http://127.0.0.1:" + str(config["port"]) + "/mcp", config["token"]
            except (OSError, ValueError, KeyError) as exc:
                raise UnsupportedEngine("Fixture SC_HOME has no usable MCP configuration") from exc
        client = HTTPMCP(url, token, timeout)
        bundle = pathlib.Path(env.get("SC_E2E_APP") or pathlib.Path.home() / "Applications/SecondCursor.app").expanduser().resolve()
        binary = bundle / "Contents/MacOS/SecondCursor"
        try:
            actual_version = "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()
        except OSError as exc:
            raise UnsupportedEngine("Cannot inspect the fixture's Second Cursor binary") from exc
        if request.get("engine_version") != actual_version:
            raise EngineError("Second Cursor binary differs from the pinned benchmark engine version")
        pinned_identity = {"requested_version": request["engine_version"], "binary_sha256": actual_version[7:],
                           "binary_identity_verified": True, "engine_version": actual_version, "verified": True}
    elif name == "cua-driver":
        binary = env.get("SC_BENCH_CUA_BINARY", "/Applications/CuaDriver.app/Contents/MacOS/cua-driver")
        binary = shutil.which(binary) or binary
        if not pathlib.Path(binary).is_file():
            raise UnsupportedEngine("Cua Driver app binary is unavailable")
        observed = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        actual_version = observed.stdout.strip()
        if observed.returncode or not actual_version:
            raise UnsupportedEngine("Cannot inspect the Cua Driver executable version")
        if request.get("engine_version") != actual_version:
            raise EngineError("Cua Driver executable differs from the pinned benchmark engine version")
        semantic = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][\w.]+)?)\b", actual_version)
        pinned_identity = {"requested_version": request["engine_version"], "cli_version": actual_version,
                           "semantic_version": semantic.group(1) if semantic else None,
                           "binary_sha256": hashlib.sha256(pathlib.Path(binary).read_bytes()).hexdigest(),
                           "binary_identity_verified": True, "engine_version": actual_version, "verified": True}
        command = [binary, "mcp"]
        if env.get("SC_BENCH_CUA_SOCKET"):
            command.extend(["--socket", env["SC_BENCH_CUA_SOCKET"]])
        # Never inherit a host-identity override or permission bypass from a shell.
        for key in list(env):
            if key.startswith("CUA_DRIVER_") and key != "CUA_DRIVER_LOG_LEVEL":
                env.pop(key)
        client = StdioMCP(command, timeout, env)
    else:
        raise UnsupportedEngine("Unknown benchmark engine")
    return ScopedEngine(name, request, client, emit, clock_start, pinned_identity)
