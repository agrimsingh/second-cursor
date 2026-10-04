"""Bounded Cursor SDK bridge for explicitly scoped benchmark fixture tools.

The bridge uses Cursor's supported authentication; Python never reads credentials.
Only a version-pinned bridge with verified containment may call the main-thread
fixture callback. SDK token accounting is retained with its own source label.
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from best_in_class import ReservedGroupProcess, stop_group
from benchmarks.engines import ScopeError
from benchmarks.model_host import HostConfig, HostError, HostResult, IsolationError, TrialDeadline, _tool_specs, tool_response

DEFAULT_SDK_ROOT = pathlib.Path(__file__).resolve().parents[1] / "cursor_host"
MAX_PROTOCOL_LINE_BYTES = 32 * 1024 * 1024
USAGE_FIELDS = {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens", "reasoningTokens"}
ENVIRONMENT_KEYS = {
    "HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "CURSOR_API_KEY", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
}


def _child_environment() -> dict[str, str]:
    # Preserve official local authentication and network setup, never arbitrary
    # provider keys, NODE_OPTIONS preload hooks, or repository environment values.
    return {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}


def _bridge_path(sdk_root: str | pathlib.Path | None) -> pathlib.Path:
    return pathlib.Path(sdk_root or DEFAULT_SDK_ROOT).resolve() / "bridge.mjs"


def host_version(sdk_root: str | pathlib.Path | None = None, node_binary: str = "node") -> str:
    """Read the installed SDK/runtime fingerprint without authentication or inference."""
    try:
        result = subprocess.run([node_binary, str(_bridge_path(sdk_root)), "--version"],
                                text=True, capture_output=True, timeout=10,
                                check=True, env=_child_environment())
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostError("Cannot read Cursor SDK host version") from exc
    version = result.stdout.strip()
    if not re.fullmatch(r"cursor-sdk [0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)? node v[0-9]+\.[0-9]+\.[0-9]+(?: [A-Za-z0-9._:-]+)*", version):
        raise HostError("Unexpected Cursor SDK version response")
    return version


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


class _BridgeEOF(HostError):
    pass


class _Bridge:
    """Strict JSONL transport with bounded messages and owned process-group cleanup."""
    def __init__(self, args: list[str], cwd: str, env: dict[str, str]):
        self.process = ReservedGroupProcess(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
        self.messages: queue.Queue[Any] = queue.Queue(maxsize=256)
        self.closing = threading.Event()
        self.cleanup: dict[str, Any] | None = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _put(self, value: Any) -> None:
        while not self.closing.is_set():
            try:
                self.messages.put(value, timeout=.1)
                return
            except queue.Full:
                continue

    def _read(self) -> None:
        try:
            assert self.process.stdout
            while not self.closing.is_set():
                line = self.process.stdout.readline(MAX_PROTOCOL_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_PROTOCOL_LINE_BYTES or not line.endswith(b"\n"):
                    self._put(HostError("Oversized or incomplete Cursor bridge message"))
                    return
                try:
                    value = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
                    if not isinstance(value, dict):
                        raise ValueError("Not an object")
                except (ValueError, UnicodeError):
                    self._put(HostError("Malformed Cursor bridge protocol message"))
                    return
                self._put(value)
        except (OSError, ValueError, UnicodeError):
            self._put(HostError("Cursor bridge protocol read failed"))
        finally:
            self._put(_BridgeEOF("Cursor bridge closed before trial completion"))

    def send(self, value: dict[str, Any]) -> None:
        payload = (json.dumps(value, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_PROTOCOL_LINE_BYTES:
            raise HostError("Cursor bridge request exceeds transport limit")
        try:
            assert self.process.stdin
            while payload:
                written = self.process.stdin.write(payload)
                if not written:
                    raise OSError("Closed pipe")
                payload = payload[written:]
        except (OSError, ValueError) as exc:
            raise HostError("Cursor bridge protocol write failed") from exc

    def receive(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TrialDeadline("Trial wall-clock deadline exceeded")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TrialDeadline("Trial wall-clock deadline exceeded") from exc
        if isinstance(message, Exception):
            raise message
        return message

    def finish(self, deadline: float) -> None:
        """A terminal envelope is valid only when followed by clean protocol EOF."""
        try:
            self.receive(deadline)
        except _BridgeEOF:
            pass
        else:
            raise IsolationError("Cursor sent protocol activity after its terminal result")
        while self.process.observe_exit() is None:
            if time.monotonic() >= deadline:
                raise TrialDeadline("Trial wall-clock deadline exceeded")
            time.sleep(.005)
        if self.process.returncode != 0:
            raise HostError("Cursor bridge exited unsuccessfully after its terminal result")

    def close(self) -> dict[str, Any]:
        if self.cleanup is not None:
            return self.cleanup
        self.closing.set()
        # Never reap the leader until all group signals and ownership checks have
        # completed: an SDK child can outlive a successfully exiting bridge.
        self.cleanup = stop_group(self.process)
        try:
            self.process.reap()
        except (OSError, RuntimeError, ChildProcessError):
            self.cleanup = {"status": "failed", "errors": ["Unable to release owned Cursor process group"]}
        self.reader.join(timeout=.2)
        # A failed process-group cleanup may leave a reader blocked in a live
        # descendant's pipe. Do not wait on a buffered stream's lock indefinitely.
        for stream in (self.process.stdin, self.process.stdout if not self.reader.is_alive() else None):
            if stream:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if self.reader.is_alive():
            self.cleanup = {"status": "failed", "errors": ["Cursor protocol reader survived cleanup"]}
        return self.cleanup


def _model_identity(value: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not isinstance(value.get("params"), list):
        raise IsolationError("Cursor did not attest the pinned model and parameters")
    params: dict[str, str] = {}
    for entry in value["params"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not isinstance(entry.get("value"), str) or entry["id"] in params):
            raise IsolationError("Cursor returned malformed model parameters")
        params[entry["id"]] = entry["value"]
    return value["id"], params


def _usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value or any(key not in USAGE_FIELDS for key in value):
        raise HostError("Malformed Cursor token usage")
    if any(type(number) is not int or number < 0 for number in value.values()):
        raise HostError("Malformed Cursor token usage")
    return {**value, "source": "cursor-sdk", "accounting": "cursor-sdk-total-includes-cache-read-and-write"}


def _mcp_envelope(value: Any) -> dict[str, Any]:
    # The existing validation rejects unsupported content instead of silently
    # dropping screenshots or resources. Cursor custom tools accept text/images,
    # so embedded textual resources use the same text semantics as the Codex host.
    tool_response(value)
    if isinstance(value, dict) and "content" in value:
        envelope = json.loads(json.dumps(value, allow_nan=False))
        envelope["content"] = [{"type": "text", "text": item["resource"]["text"]}
                               if item["type"] == "resource" else item for item in envelope["content"]]
        return envelope
    return {"content": [{"type": "text", "text": value if isinstance(value, str)
                         else json.dumps(value, allow_nan=False)}]}


class CursorModelHost:
    """Fresh local SDK agent; callbacks remain serialized on the main thread."""
    version = staticmethod(host_version)

    def __init__(self, config: HostConfig, model_params: dict[str, str],
                 node_binary: str = "node", sdk_root: str | pathlib.Path | None = None):
        effort_keys = {"effort", "reasoning_effort"}.intersection(model_params) if isinstance(model_params, dict) else set()
        if (not isinstance(model_params, dict) or not model_params
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", key)
                       or not isinstance(value, str) or not value for key, value in model_params.items())
                or len(effort_keys) != 1 or model_params[next(iter(effort_keys))] != config.reasoning_effort
                or ("fast" in model_params and model_params["fast"] not in {"true", "false"})):
            raise ValueError("Cursor parameters must explicitly match the pinned reasoning effort")
        self.config, self.model_params = config, dict(model_params)
        self.node_binary, self.sdk_root = node_binary, sdk_root

    def run(self, prompt: str, tool_specs: list[dict[str, Any]],
            execute_tool: Callable[[str, dict[str, Any]], Any],
            emit_event: Callable[[dict[str, Any]], None] | None = None) -> HostResult:
        if threading.current_thread() is not threading.main_thread():
            raise HostError("Model trials must run on the main thread to enforce callback deadlines")
        if not self.config.model_host_version:
            raise HostError("Pin model_host_version using host_version() before starting a trial")
        _tool_specs(tool_specs)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("A non-empty fixture task prompt is required")
        if signal.getitimer(signal.ITIMER_REAL)[0]:
            raise HostError("Cannot own trial deadline while another real-time alarm is active")
        specs = [{key: tool[key] for key in ("name", "description", "inputSchema")} for tool in tool_specs]
        allowed = {tool["name"] for tool in specs}
        emit = emit_event or (lambda event: None)
        start = time.monotonic()
        deadline = start + self.config.timeout_seconds
        bridge = temporary = None
        ready = False
        actual_version, reported_model = "unknown", None
        status, error, final_response = "failed", None, ""
        observed_final: str | None = None
        usage = None
        isolation: dict[str, Any] = {"verified": False, "model_host": "cursor-sdk", "model_params": dict(self.model_params)}
        milestones: dict[str, float] = {}
        seen_calls: set[str] = set()
        tool_calls = 0

        def now_ms() -> float:
            return (time.monotonic() - start) * 1000

        def milestone(name: str, at_ms: float | None = None) -> None:
            timestamp = now_ms() if at_ms is None else at_ms
            milestones[name] = timestamp
            emit({"type": "host_milestone", "name": name, "at_ms": timestamp})

        def activity() -> None:
            if "first_model_activity" not in milestones:
                milestone("first_model_activity")

        def attest_model(value: Any) -> str:
            identifier, params = _model_identity(value)
            if identifier != self.config.model or params != self.model_params:
                raise IsolationError("Cursor did not honor the pinned model and parameters")
            return identifier

        def accept_ready(message: dict[str, Any]) -> None:
            nonlocal ready, isolation, reported_model
            if ready:
                raise IsolationError("Duplicate Cursor bridge readiness response")
            reported_model = attest_model(message.get("model"))
            if message.get("version") != actual_version:
                raise IsolationError("Cursor bridge version changed during startup")
            claims = message.get("isolation")
            expected = {"verified": True, "non_fixture_tools": [], "inherited_mcp_tool_count": 0,
                        "workspace_roots": [], "environment_access": False, "setting_sources": [],
                        "tools": ["mcp_tool_call", "get_mcp_tools_tool_call"], "model_host": "cursor-sdk", "model_params": self.model_params}
            if (not isinstance(claims, dict) or any(claims.get(key) != value for key, value in expected.items())
                    or claims.get("verified") is not True or claims.get("environment_access") is not False
                    or not isinstance(claims.get("fixture_tools"), list)
                    or sorted(claims["fixture_tools"]) != sorted(allowed)):
                raise IsolationError("Cursor bridge did not verify fixture-only containment")
            # Keep the bridge's explicit audit basis, never infer an observed
            # inventory from an SDK configuration/source audit.
            isolation = dict(claims)
            json.dumps(isolation, allow_nan=False)
            ready = True
            milestone("startup_ready")
            milestone("thread_ready")
            emit({"type": "host", "model": reported_model, "reasoning_effort": self.config.reasoning_effort,
                  "model_host_version": actual_version, "isolation": isolation, "at_ms": now_ms()})
            milestone("turn_submitted")

        def sdk_event(value: Any) -> None:
            nonlocal usage, observed_final
            if not ready:
                raise IsolationError("Cursor activity arrived before verified containment")
            if not isinstance(value, dict):
                raise HostError("Malformed Cursor SDK event")
            kind = value.get("type")
            if kind == "activity":
                activity()
            elif kind == "assistant":
                text = value.get("text")
                if not isinstance(text, str):
                    raise HostError("Malformed Cursor assistant event")
                activity()
                emit({"type": "assistant", "text": text, "phase": "commentary", "at_ms": now_ms()})
            elif kind == "final_response":
                text = value.get("text")
                if not isinstance(text, str) or observed_final is not None:
                    raise HostError("Malformed or duplicate Cursor final response event")
                observed_final = text
                activity()
                milestone("final_response_completed")
                emit({"type": "assistant", "text": text, "phase": "final_answer", "at_ms": now_ms()})
            elif kind == "usage":
                usage = _usage(value.get("usage"))
                emit({"type": "usage", "usage": usage, "source": "cursor-sdk", "at_ms": now_ms()})
            elif kind == "status":
                value_status = value.get("status")
                if not isinstance(value_status, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value_status):
                    raise HostError("Malformed Cursor status event")
                emit({"type": "host_status", "status": value_status, "at_ms": now_ms()})
            else:
                raise IsolationError("Unexpected Cursor SDK event type")

        def fixture_call(message: dict[str, Any]) -> None:
            nonlocal tool_calls, status
            tool_calls += 1  # Attempts include rejected and over-budget requests.
            if not ready:
                raise IsolationError("Cursor tool request arrived before verified containment")
            if tool_calls > self.config.max_steps:
                status = "step_limit"
                raise HostError("Maximum fixture tool calls reached")
            call_id, name, arguments = message.get("call_id"), message.get("name"), message.get("arguments")
            if (not isinstance(call_id, str) or not call_id or len(call_id) > 256 or call_id in seen_calls
                    or not isinstance(name, str) or name not in allowed):
                raise IsolationError("Unknown or duplicate Cursor fixture tool request")
            if not isinstance(arguments, dict):
                raise HostError("Fixture tool arguments must be an object")
            try:
                json.dumps(arguments, allow_nan=False)
            except (ValueError, TypeError) as exc:
                raise HostError("Malformed fixture tool arguments") from exc
            seen_calls.add(call_id)
            activity()
            call_start = now_ms()
            emit({"type": "tool_request", "call_id": call_id, "name": name,
                  "arguments": arguments, "start_ms": call_start})
            try:
                response = _mcp_envelope(execute_tool(name, arguments))
            except TrialDeadline:
                emit({"type": "tool", "call_id": call_id, "name": name, "arguments": arguments,
                      "start_ms": call_start, "end_ms": now_ms(), "status": "timeout"})
                raise
            except ScopeError as exc:
                # Only the local validator's authored public contract refusals
                # are safe to forward. They let Cursor recover from a rejected
                # call just as Codex can; arbitrary runtime errors stay masked.
                response = {"content": [{"type": "text", "text": f"ScopeError: {exc}"}], "isError": True}
            except Exception:
                # Exceptions can contain headers, endpoints or configuration.
                # Give the solver an honest error without forwarding their text.
                response = {"content": [{"type": "text", "text": "Fixture tool failed or returned unsupported content."}],
                            "isError": True}
            emit({"type": "tool", "call_id": call_id, "name": name, "arguments": arguments,
                  "start_ms": call_start, "end_ms": now_ms(),
                  "status": "error" if response.get("isError") else "completed", "result": response})
            assert bridge
            bridge.send({"type": "tool_result", "call_id": call_id, "result": response})

        previous_handler = signal.getsignal(signal.SIGALRM)

        def expired(signum: int, frame: Any) -> None:
            raise TrialDeadline("Trial wall-clock deadline exceeded")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, self.config.timeout_seconds)
        try:
            milestone("host_started", 0.0)
            actual_version = host_version(self.sdk_root, self.node_binary)
            if actual_version != self.config.model_host_version:
                raise IsolationError("Installed Cursor host does not match pinned version")
            temporary = tempfile.TemporaryDirectory(prefix="sc-cursor-model-trial-")
            workspace = pathlib.Path(temporary.name) / "workspace"
            workspace.mkdir(mode=0o700)
            store_dir = pathlib.Path(temporary.name) / "sdk-store"
            child_environment = _child_environment()
            # The SDK's transcript writer uses CURSOR_DATA_DIR independently of
            # official authentication under HOME/.cursor/sdk. Do not inherit a
            # caller override or leave model traces in the user's project store.
            child_environment["CURSOR_DATA_DIR"] = str(pathlib.Path(temporary.name) / "cursor-data")
            bridge = _Bridge([self.node_binary, str(_bridge_path(self.sdk_root)), "--run"],
                             str(workspace), child_environment)
            bridge.send({"type": "init", "prompt": prompt, "tools": specs, "cwd": str(workspace),
                         "store_dir": str(store_dir),
                         "model": {"id": self.config.model, "params": [
                             {"id": key, "value": value} for key, value in self.model_params.items()]},
                         "max_steps": self.config.max_steps, "timeout_seconds": self.config.timeout_seconds})
            while True:
                message = bridge.receive(deadline)
                if not isinstance(message, dict):
                    raise HostError("Malformed Cursor bridge protocol message")
                kind = message.get("type")
                if kind == "ready":
                    accept_ready(message)
                elif kind == "call":
                    fixture_call(message)
                elif kind == "event":
                    sdk_event(message.get("event"))
                elif kind == "result":
                    if not ready:
                        raise IsolationError("Cursor completed before verifying containment")
                    reported_model = attest_model(message.get("model"))
                    usage = _usage(message.get("usage"))
                    if message.get("status") not in {"finished", "error", "cancelled"}:
                        raise HostError("Malformed Cursor completion status")
                    if message["status"] == "finished" and message.get("error"):
                        raise HostError("Cursor returned a contradictory completion result")
                    if not isinstance(message.get("result"), str):
                        raise HostError("Malformed Cursor final response")
                    if observed_final is not None and observed_final != message["result"]:
                        raise HostError("Cursor terminal response differs from its completion event")
                    final_response = message["result"]
                    status = "completed" if message["status"] == "finished" else "failed"
                    if status != "completed":
                        error = "Cursor model turn ended without successful completion"
                    if final_response and observed_final is None:
                        activity()
                        milestone("final_response_completed")
                        emit({"type": "assistant", "text": final_response, "phase": "final_answer", "at_ms": now_ms()})
                    bridge.finish(deadline)
                    milestone("turn_completed")
                    break
                else:
                    raise IsolationError("Unexpected Cursor bridge protocol message type")
        except TrialDeadline:
            status, error = "timeout", "Trial wall-clock deadline exceeded"
        except (HostError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            if status != "step_limit":
                status = "isolation_error" if isinstance(exc, IsolationError) else "failed"
            if isinstance(exc, IsolationError):
                isolation["verified"] = False
            error = str(exc) if isinstance(exc, HostError) else "Cursor host operation failed"
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if bridge:
                cleanup = bridge.close()
                isolation["cleanup"] = cleanup
                if cleanup.get("status") not in {"cleaned", "no_live_members"}:
                    status, error = "failed", "Cursor host cleanup could not be verified"
            if temporary:
                temporary.cleanup()
        wall_ms = now_ms()
        milestone("host_finished", wall_ms)
        return HostResult(status=status, final_response=final_response, model=reported_model,
                          reasoning_effort=self.config.reasoning_effort, model_host_version=actual_version,
                          usage=usage, tool_calls=tool_calls, wall_ms=wall_ms, error=error,
                          isolation=isolation, milestones_ms=milestones)
