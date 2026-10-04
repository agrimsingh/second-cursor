"""No driver is launched and no desktop or fixture actions occur in these tests."""
import copy
import hashlib
import http.server
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks.engines import (EngineError, HTTPMCP, ScopeError, ScopedEngine, StdioMCP,
                                UnsupportedEngine, _ok, create_engine, process_identity)

FAKE_PROCESSES = {}

URL = "http://127.0.0.1:8123/fixture.html?task=form"
NATIVE = {"task_id": "native_notes", "targets": {"calculator_window_id": 11, "notes_window_id": 12,
           "calculator_pid": 101, "notes_pid": 102}, "timeout_seconds": 5}
BROWSER = {"task_id": "browser_work_order", "targets": {"url": URL}, "timeout_seconds": 5}


def definition(name, fields=(), required=()):
    return {"name": name, "description": "Native tool " + name,
            "inputSchema": {"type": "object", "properties": {key: {} for key in fields}, "required": list(required)}}


def content(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}


class FakeMCP:
    def __init__(self, tools, responses=None):
        self.definitions, self.responses, self.calls = tools, responses or {}, []
        self.protocol, self.server_info, self.closed = "2025-03-26", {"name": "fake", "version": "1"}, False

    def initialize(self):
        pass

    def list_tools(self):
        return copy.deepcopy(self.definitions)

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        result = self.responses.get(name, content({"ok": True}))
        if callable(result):
            result = result(args)
        if isinstance(result, Exception):
            raise result
        if name == "browser_prepare" and result.get("structuredContent", {}).get("action") == "launched_isolated_browser":
            self.created_pid = result.get("structuredContent", {}).get("prepared_pid")
            if self.created_pid is not None:
                FAKE_PROCESSES[self.created_pid] = "fixture-process-birth"
        if name == "kill_app":
            FAKE_PROCESSES.pop(args["pid"], None)
        if name == "end_session":
            FAKE_PROCESSES.pop(getattr(self, "created_pid", None), None)
        return copy.deepcopy(result)

    def close(self):
        self.closed = True


def sc_tools():
    tools = [definition("get_window_state", ["window_id", "app", "include_menu_bar", "include_screenshot", "scope", "session_id"]),
             definition("click", ["window_id", "element_index", "x", "y", "session_id"]),
             definition("press_key", ["window_id", "keys"], ["keys"]),
             definition("batch", ["window_id", "tab_id", "steps", "expect"], ["steps"]),
             definition("browser_open", ["url"], ["url"]),
             definition("browser_close", ["tab_id"], ["tab_id"]),
             definition("browser_snapshot", ["tab_id", "include_screenshot"]),
             definition("browser_navigate", ["tab_id", "url"], ["tab_id", "url"]),
             definition("browser_press_key", ["tab_id", "keys"], ["tab_id", "keys"]),
             definition("browser_click", ["tab_id", "ref"]),
             definition("browser_command", ["tab_id", "operation", "arguments"], ["operation"]),
             definition("browser_repl", ["code"]), definition("list_windows")]
    tools[-3]["inputSchema"]["properties"]["arguments"] = {"type": "object", "description": "Operation-specific fields."}
    tools[3]["inputSchema"]["properties"]["steps"] = {"type": "array", "items": {"type": "object", "properties": {"keys": {"type": "string"}}}}
    return tools


def cua_tools():
    return [definition("get_window_state", ["pid", "window_id", "session", "screenshot_out_file"], ["pid", "window_id"]),
            definition("click", ["pid", "window_id", "session", "element_token", "delivery_mode", "scope", "target"]),
            definition("press_key", ["pid", "window_id", "key", "modifiers", "session", "element_token"]),
            definition("set_value", ["pid", "window_id", "value", "session", "element_token"], ["pid", "value"]),
            definition("list_apps"), definition("browser_prepare", ["profile", "allow_launch", "session", "pid", "window_id", "strategy"]),
            definition("list_windows", ["pid"]),
            definition("get_browser_state", ["pid", "window_id", "target_id", "tab_id", "session", "include_screenshot"]),
            definition("browser_navigate", ["target_id", "tab_id", "url", "session"], ["target_id", "tab_id", "url"]),
            definition("browser_pointer", ["target_id", "tab_id", "action", "input_route", "delivery_mode", "session"]),
            definition("kill_app", ["pid"]), definition("end_session", ["session"]),
            definition("page", ["javascript"])]


def cua_responses(pid=777):
    return {"list_apps": content({"apps": [{"pid": 101}, {"pid": 102}]}),
            "browser_prepare": content({"prepared_pid": pid, "prepared": True, "action": "launched_isolated_browser",
                                        "endpoint_ownership": {"owner_pid": pid, "method": "spawned_by_driver"}}),
            "list_windows": content({"windows": [{"pid": pid, "window_id": 7777, "title": "about:blank"},
                                                    {"pid": pid, "window_id": 7778, "title": ""}]}),
            "get_browser_state": content({"target_id": "target-1", "tabs": [{"tab_id": "tab-1"}]})}


def cua_harness_responses(pid=888):
    return {"browser_prepare": content({"prepared": True, "prepared_pid": pid,
                                        "action": "attached_existing_profile",
                                        "attachment": {"kind": "existing_profile"}}),
            "get_browser_state": content({"target_id": "target-1", "tabs": [{"tab_id": "tab-1"}]})}


class ScopeTests(unittest.TestCase):
    def setUp(self):
        FAKE_PROCESSES.clear()
        self.identity_patch = patch("benchmarks.engines.process_identity", side_effect=lambda pid: FAKE_PROCESSES.get(pid))
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def harness_browser(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        binary = pathlib.Path(directory.name).resolve() / "Chrome for Testing"
        binary.write_bytes(b"test browser binary; never executed")
        expected = {"binary": str(binary), "version": "Chrome for Testing 123.0.0.1",
                    "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
        request = {**BROWSER, "targets": {**BROWSER["targets"], "browser_pid": 888, "browser_window_id": 8888},
                   "browser_identity": expected, "browser_visibility": "headed"}
        FAKE_PROCESSES[888] = "harness-browser-birth"
        def inspect(command, **kwargs):
            if command == ["/bin/ps", "-p", "888", "-o", "comm="]:
                return subprocess.CompletedProcess(command, 0, str(binary) + "\n", "")
            if command == [str(binary), "--version"]:
                return subprocess.CompletedProcess(command, 0, expected["version"] + "\n", "")
            raise AssertionError(f"Unexpected subprocess: {command}")
        return request, inspect

    def test_both_engines_attest_harness_browser_and_cua_prepares_same_exact_session(self):
        request, inspect = self.harness_browser()
        for name, definitions in [("second-cursor", sc_tools()), ("cua-driver", cua_tools())]:
            responses = cua_harness_responses()
            responses["browser_command"] = lambda args: content({"tabs": [{"id": 9, "url": "about:blank"}]}) \
                if args["operation"] == "user_tabs" else content({"tab": {"id": 9, "url": "about:blank"}})
            client = FakeMCP(definitions, responses)
            with self.subTest(name=name), patch("benchmarks.engines.subprocess.run", side_effect=inspect):
                with ScopedEngine(name, request, client) as engine:
                    self.assertEqual(engine.identity["browser_identity"], request["browser_identity"])
                    self.assertTrue(engine.identity["browser_identity_verified"])
                    self.assertEqual(engine.identity["harness_browser"]["cleanup_owner"], "harness")
                    self.assertIsNone(engine.owned_browser_pid)
                    if name == "cua-driver":
                        self.assertEqual(engine.windows, {8888})
                        self.assertEqual(engine.context["browser"]["pid"], 888)
                        self.assertEqual(client.calls[0][0], "browser_prepare")
                        self.assertEqual(client.calls[0][1]["window_id"], 8888)
                        self.assertEqual(client.calls[0][1]["pid"], 888)
                        self.assertEqual(client.calls[0][1]["strategy"], {"kind": "existing_profile"})
                        self.assertNotIn("allow_launch", client.calls[0][1])
                        self.assertNotIn("profile", client.calls[0][1])
                        self.assertEqual(client.calls[1][0], "get_browser_state")
                        self.assertEqual(client.calls[0][1]["session"], client.calls[1][1]["session"])
                    else:
                        self.assertEqual(engine.context["browser"]["tab_id"], 9)
                        self.assertNotIn("browser_open", {tool["name"] for tool in engine.tools()})
                        with self.assertRaises(ScopeError):
                            engine.call("browser_open", {"url": URL})
                        engine.call("browser_navigate", {"tab_id": 9, "url": URL})
                self.assertEqual(FAKE_PROCESSES[888], "harness-browser-birth")
                self.assertFalse(any(tool in {"list_apps", "kill_app"} for tool, _ in client.calls))
                if name == "second-cursor":
                    self.assertEqual(client.calls[-1], ("browser_close", {"tab_id": 9}))

    def test_sc_visible_binding_never_claims_ambiguous_or_nonblank_tabs(self):
        request, inspect = self.harness_browser()
        for tabs in [[], [{"id": 9, "url": "https://personal.example"}],
                     [{"id": 9, "url": "about:blank"}, {"id": 10, "url": "about:blank"}],
                     [{"id": 9}], [{"id": True, "url": "about:blank"}]]:
            client = FakeMCP(sc_tools(), {"browser_command": content({"tabs": tabs})})
            with self.subTest(tabs=tabs), patch("benchmarks.engines.subprocess.run", side_effect=inspect):
                with self.assertRaises(EngineError):
                    with ScopedEngine("second-cursor", request, client):
                        pass
            self.assertEqual(client.calls, [("browser_command", {"operation": "user_tabs"})])

    def test_harness_binding_refusal_never_launches_or_kills_a_replacement(self):
        request, inspect = self.harness_browser()
        client = FakeMCP(cua_tools(), {**cua_harness_responses(), "get_browser_state": {"isError": True, "content": []}})
        with patch("benchmarks.engines.subprocess.run", side_effect=inspect):
            with self.assertRaisesRegex(EngineError, "refused"):
                with ScopedEngine("cua-driver", request, client):
                    pass
        self.assertEqual([tool for tool, _ in client.calls], ["browser_prepare", "get_browser_state", "end_session"])
        self.assertEqual(FAKE_PROCESSES[888], "harness-browser-birth")

    def test_structured_binding_refusal_retains_public_reason_without_granting_target(self):
        request, inspect = self.harness_browser()
        refusal = content({"status": "refused", "refusal": {
            "code": "browser_consent_required", "message": "PRIVATE profile metadata",
            "detail": {"reason": "consumer_profile_endpoint_requires_grant", "private_path": "/private/profile"}}})
        client = FakeMCP(cua_tools(), {**cua_harness_responses(), "get_browser_state": refusal})
        events = []
        with patch("benchmarks.engines.subprocess.run", side_effect=inspect):
            with self.assertRaisesRegex(EngineError, "browser_consent_required: consumer_profile_endpoint_requires_grant") as error:
                with ScopedEngine("cua-driver", request, client, events.append) as engine:
                    self.fail("Refused binding must never start the model interface")
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("/private", str(error.exception))
        self.assertFalse(events[1]["ok"])
        self.assertEqual([tool for tool, _ in client.calls], ["browser_prepare", "get_browser_state", "end_session"])

    def test_refused_or_wrong_harness_attachment_never_binds_or_dispatches_model(self):
        request, inspect = self.harness_browser()
        refusal = content({"status": "refused", "refusal": {"code": "browser_consent_required",
                           "detail": {"reason": "existing_profile_requires_authorization"}}})
        for preparation, expected in [
            (refusal, "browser_consent_required: existing_profile_requires_authorization"),
            (content({"prepared": True, "prepared_pid": 999, "action": "attached_existing_profile"}), "exact harness browser"),
            (content({"prepared": True, "prepared_pid": 888, "action": "already_prepared"}), "exact harness browser"),
            (content({"prepared": False, "prepared_pid": 888, "action": "attached_existing_profile"}), "exact harness browser"),
        ]:
            client = FakeMCP(cua_tools(), {"browser_prepare": preparation})
            with self.subTest(preparation=preparation), patch("benchmarks.engines.subprocess.run", side_effect=inspect):
                with self.assertRaisesRegex(EngineError, expected):
                    with ScopedEngine("cua-driver", request, client):
                        self.fail("Unconfirmed attachment cannot start model dispatch")
            self.assertEqual([tool for tool, _ in client.calls], ["browser_prepare", "end_session"])
            self.assertEqual(FAKE_PROCESSES[888], "harness-browser-birth")

    def test_structured_model_refusal_is_preserved_and_cannot_grant_element_tokens(self):
        refusal = content({"status": "refused", "window_id": 11, "refusal": {"code": "target_denied"},
                           "elements": [{"element_index": 0, "role": "AXWindow"},
                                        {"element_index": 1, "parent_index": 0, "role": "AXButton", "element_token": "untrusted"}]})
        events = []
        with ScopedEngine("cua-driver", NATIVE, FakeMCP(cua_tools(), {"get_window_state": refusal}), events.append) as engine:
            self.assertEqual(engine.call("get_window_state", {"window_id": 11, "pid": 101}), refusal)
            with self.assertRaises(ScopeError):
                engine.call("click", {"window_id": 11, "element_token": "untrusted"})
        self.assertFalse(events[0]["ok"])

    def test_harness_browser_mismatch_stops_before_driver_initialization(self):
        request, inspect = self.harness_browser()
        failures = [
            {"browser_visibility": "headless"}, {"browser_identity": None},
            {"targets": {**request["targets"], "browser_pid": None}},
            {"browser_identity": {**request["browser_identity"], "sha256": "0" * 64}},
            {"browser_identity": {**request["browser_identity"], "version": "Different version"}},
            {"browser_identity": {**request["browser_identity"], "binary": "/different/browser"}},
        ]
        for change in failures:
            client = FakeMCP(cua_tools(), cua_responses())
            with self.subTest(change=change), patch("benchmarks.engines.subprocess.run", side_effect=inspect), \
                    patch.object(client, "initialize") as initialize:
                with self.assertRaises(EngineError):
                    with ScopedEngine("cua-driver", {**request, **change}, client):
                        pass
                initialize.assert_not_called()
                self.assertEqual(client.calls, [])

    def test_harness_browser_process_reuse_cannot_pass_attestation(self):
        request, inspect = self.harness_browser()
        client = FakeMCP(cua_tools(), cua_responses())
        with patch("benchmarks.engines.subprocess.run", side_effect=inspect), \
                patch("benchmarks.engines.process_identity", side_effect=["birth-one", "birth-two"]):
            with self.assertRaisesRegex(EngineError, "changed"):
                with ScopedEngine("cua-driver", request, client):
                    pass
        self.assertEqual(client.calls, [])

    def test_raw_text_and_images_are_preserved(self):
        raw = {"content": [{"type": "text", "text": "Unstructured native outline"},
                           {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}],
               "structuredContent": {"elements": [{"element_index": 1}]}}
        client = FakeMCP(sc_tools(), {"get_window_state": raw})
        events = []
        with ScopedEngine("second-cursor", NATIVE, client, events.append) as engine:
            self.assertEqual(engine.call("get_window_state", {"window_id": 11}), raw)
        self.assertTrue(client.closed)
        self.assertEqual(events[0]["phase"], "model")
        self.assertLessEqual(events[0]["start_ms"], events[0]["end_ms"])

    def test_foreign_or_implicit_native_targets_never_dispatch(self):
        client = FakeMCP(sc_tools())
        with ScopedEngine("second-cursor", NATIVE, client) as engine:
            for args in ({}, {"window_id": 999}, {"window_id": True}, {"window_id": 11, "app": "Safari"},
                         {"window_id": 11, "session_id": "foreign"}, {"window_id": 11, "include_menu_bar": True}):
                with self.subTest(args=args), self.assertRaises(ScopeError):
                    engine.call("get_window_state", args)
            self.assertEqual(client.calls, [])

    def test_native_schemas_preserve_descriptions_and_narrow_targets(self):
        with ScopedEngine("second-cursor", NATIVE, FakeMCP(sc_tools())) as engine:
            defs = {tool["name"]: tool for tool in engine.tools()}
            self.assertNotIn("list_windows", defs)
            self.assertNotIn("browser_repl", defs)
            schema = defs["get_window_state"]["inputSchema"]
            self.assertEqual(schema["properties"]["window_id"]["enum"], [11, 12])
            self.assertFalse(schema["additionalProperties"])
            self.assertTrue(defs["get_window_state"]["description"].startswith("Native tool"))

    def test_nested_batch_foreign_target_and_escape_chord_never_dispatch(self):
        client = FakeMCP(sc_tools())
        with ScopedEngine("second-cursor", NATIVE, client) as engine:
            bad = [{"tool": "click", "window_id": 999, "element_index": 1},
                   {"tool": "press_key", "args": {"keys": "cmd+o"}},
                   {"tool": "batch", "steps": []},
                   {"tool": "click", "args": {"window_id": 11}, "window_id": 12}]
            for step in bad:
                with self.subTest(step=step), self.assertRaises(ScopeError):
                    engine.call("batch", {"window_id": 11, "steps": [step]})
            self.assertEqual(client.calls, [])
            engine.call("batch", {"window_id": 11, "steps": [{"tool": "click", "element_index": 1}, {"tool": "wait", "ms": 10}]})
            self.assertEqual(len(client.calls), 1)

    def test_batch_cannot_switch_target_kind(self):
        for request, args in [(NATIVE, {"window_id": 11, "tab_id": 5, "steps": []}),
                              (BROWSER, {"window_id": 11, "tab_id": 5, "steps": []})]:
            with ScopedEngine("second-cursor", request, FakeMCP(sc_tools())) as engine:
                with self.assertRaises(ScopeError):
                    engine.call("batch", args)

    def test_browser_batch_projection_omits_native_expect_and_rejects_before_dispatch(self):
        client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": URL}})})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            schema = next(tool["inputSchema"] for tool in engine.tools() if tool["name"] == "batch")
            self.assertNotIn("expect", schema["properties"])
            self.assertNotIn("expect", schema["required"])
            engine.call("browser_open", {"url": URL})
            with self.assertRaisesRegex(ScopeError, "Browser tab batches do not support expect"):
                engine.call("batch", {"tab_id": 4, "steps": [{"tool": "browser_click", "ref": "e1"}],
                                      "expect": {"text": "Saved"}})
            self.assertEqual([name for name, _ in client.calls], ["browser_open"])
        with ScopedEngine("second-cursor", NATIVE, FakeMCP(sc_tools())) as engine:
            schema = next(tool["inputSchema"] for tool in engine.tools() if tool["name"] == "batch")
            self.assertIn("expect", schema["properties"])

    def test_key_aliases_work_in_direct_browser_commands_and_batches_without_scope_escape(self):
        client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": URL}})})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            engine.call("browser_open", {"url": URL})
            engine.call("browser_press_key", {"tab_id": 4, "keys": "ArrowDown"})
            engine.call("browser_command", {"tab_id": 4, "operation": "press_key", "arguments": {"keys": "ArrowUp"}})
            engine.call("batch", {"tab_id": 4, "steps": [{"tool": "browser_press_key", "keys": "Shift+Command+ArrowLeft"}]})
            self.assertEqual(client.calls[-1][1]["steps"][0]["keys"], "Shift+Command+ArrowLeft")
            prior_calls = len(client.calls)
            for key in ("Command+L", "Super+C", "Control+V", "Command+Command+A", "Alt+ArrowLeft"):
                with self.subTest(key=key), self.assertRaises(ScopeError):
                    engine.call("browser_press_key", {"tab_id": 4, "keys": key})
            self.assertEqual(len(client.calls), prior_calls)
            tools = {tool["name"]: tool for tool in engine.tools()}
            self.assertIn("ArrowDown", tools["browser_press_key"]["inputSchema"]["properties"]["keys"]["description"])
            self.assertIn("ArrowDown", tools["browser_command"]["inputSchema"]["properties"]["arguments"]["description"])
            self.assertIn("ArrowDown", tools["batch"]["inputSchema"]["properties"]["steps"]["items"]["properties"]["keys"]["description"])
        with ScopedEngine("second-cursor", NATIVE, FakeMCP(sc_tools())) as engine:
            engine.call("press_key", {"window_id": 11, "keys": "ArrowDown"})
            engine.call("press_key", {"window_id": 11, "keys": "Shift+Command+ArrowRight"})

    def test_partial_batch_telemetry_retains_failure_and_allows_recovery(self):
        failed = content({"outcome": {"state": "failed", "verified": False},
                          "steps": [{"ok": True}, {"ok": False}, {"ok": False, "skipped": True}]})
        recovered = content({"outcome": {"state": "verified", "verified": True}, "steps": [{"ok": True}]})
        responses, events = iter([failed, recovered]), []
        client = FakeMCP(sc_tools(), {"batch": lambda args: next(responses)})
        with ScopedEngine("second-cursor", NATIVE, client, events.append) as engine:
            args = {"window_id": 11, "steps": [{"tool": "click", "element_index": 1}]}
            self.assertEqual(engine.call("batch", args), failed)
            self.assertEqual(engine.call("batch", args), recovered)
        self.assertEqual([event["ok"] for event in events], [False, True])
        self.assertEqual([event["transport_status"] for event in events], ["completed", "completed"])

    def test_public_outcome_failures_include_text_only_legacy_steps_but_not_ui_contents(self):
        for data in ({"steps": [{"ok": True}, {"ok": False}]}, {"results": [{"skipped": True}]},
                     {"expect": {"status": "failed"}}, {"outcome": {"state": "uncertain"}},
                     {"results": [{"outcome": {"state": "failed"}}]}, {"status": "refused"}):
            with self.subTest(data=data):
                self.assertFalse(_ok(content(data)))
                self.assertFalse(_ok({"content": [{"type": "text", "text": json.dumps(data)}]}))
        self.assertTrue(_ok(content({"observation": {"outcome": {"state": "failed"}, "ok": False,
                                                   "steps": [{"ok": False}]}})))
        self.assertTrue(_ok(content({"outcome": {"state": "observed", "verified": False}})))

    def test_browser_only_owns_successfully_created_exact_fixture_tab(self):
        client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": URL}})})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            for url in [URL + "&extra=1", "http://127.0.0.1:8123/events", "file:///etc/passwd", "javascript:alert(1)"]:
                with self.subTest(url=url), self.assertRaises(ScopeError):
                    engine.call("browser_open", {"url": url})
            engine.call("browser_open", {"url": URL})
            engine.call("browser_snapshot", {"tab_id": 4})
            with self.assertRaises(ScopeError):
                engine.call("browser_snapshot", {"tab_id": 5})
        self.assertEqual(client.calls[-1], ("browser_close", {"tab_id": 4}))

    def test_failed_open_cannot_grant_tab_ownership(self):
        client = FakeMCP(sc_tools(), {"browser_open": {**content({"tab": {"id": 4, "url": URL}}), "isError": True}})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            engine.call("browser_open", {"url": URL})
            with self.assertRaises(ScopeError):
                engine.call("browser_close", {"tab_id": 4})
        self.assertEqual(len(client.calls), 1)

    def test_browser_commands_deny_raw_access_and_nested_target_override(self):
        client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": URL}})})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            engine.call("browser_open", {"url": URL})
            for op, arguments in [("cdp", {}), ("logs", {}), ("upload", {}), ("batch", {}),
                                   ("inspect", {"tabId": 999}), ("inspect", {"url": URL + "/events"}),
                                   ("press_key", {"keys": "cmd+l"})]:
                with self.subTest(op=op, arguments=arguments), self.assertRaises(ScopeError):
                    engine.call("browser_command", {"tab_id": 4, "operation": op, "arguments": arguments})
            engine.call("browser_command", {"tab_id": 4, "operation": "fill", "arguments": {"locator": {"label": "Name"}, "text": "Ada"}})
            self.assertEqual(len(client.calls), 2)

    def test_browser_capabilities_and_documented_safe_arguments_preserved(self):
        help_result = content({"operations": ["select_option", "cdp"], "arguments": {"select_option": "locator, value"}})
        client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": URL}}), "browser_command": help_result})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            self.assertEqual(engine.call("browser_command", {"operation": "capabilities"}), help_result)
            with self.assertRaises(ScopeError):
                engine.call("browser_command", {"tab_id": 99, "operation": "capabilities"})
            engine.call("browser_open", {"url": URL})
            self.assertEqual(engine.call("browser_command", {"tab_id": 4, "operation": "capabilities"}), help_result)
            for operation, fields in [("click", {"locator": {"role": "button", "name": "Create work order"}, "modifiers": ["shift"]}),
                                      ("screenshot", {"full_page": True, "clip": {"x": 0, "y": 0, "width": 100, "height": 100}}),
                                      ("perform_action", {"locator": {"label": "Name"}, "action": "focus"}),
                                      ("select_option", {"locator": {"label": "Route"}, "value": "priority"})]:
                engine.call("browser_command", {"tab_id": 4, "operation": operation, "arguments": fields})
            for operation, fields in [("cdp", {}), ("capabilities", {"action": "anything"}),
                                      ("perform_action", {"action": "run_script"}),
                                      ("select_option", {"ref": "e3", "label": "Priority"}),
                                      ("click", {"modifiers": ["unsupported"]}),
                                      ("screenshot", {"clip": {"url": "file:///private"}})]:
                with self.subTest(operation=operation), self.assertRaises(ScopeError):
                    engine.call("browser_command", {"tab_id": 4, "operation": operation, "arguments": fields})

    def test_cua_native_pid_pair_and_background_are_enforced(self):
        client = FakeMCP(cua_tools(), {"get_window_state": content({"window_id": 11, "elements": [
            {"element_index": 0, "role": "AXWindow"},
            {"element_index": 1, "parent_index": 0, "role": "AXTextField", "element_token": "token"}]})})
        with ScopedEngine("cua-driver", NATIVE, client) as engine:
            for args in [{"window_id": 11, "pid": 102}, {"window_id": 11, "delivery_mode": "foreground"},
                         {"window_id": 11, "scope": "desktop"}, {"window_id": 11, "target": {"kind": "desktop"}}]:
                with self.subTest(args=args), self.assertRaises(ScopeError):
                    engine.call("click", args)
            with self.assertRaises(ScopeError):
                engine.call("click", {"window_id": 11, "element_token": "token"})
            engine.call("get_window_state", {"window_id": 11, "pid": 101})
            with self.assertRaises(ScopeError):
                engine.call("click", {"window_id": 12, "element_token": "token"})
            engine.call("click", {"window_id": 11, "element_token": "token"})
            sent = client.calls[-1][1]
            self.assertEqual(sent["pid"], 101)
            self.assertEqual(sent["session"], engine.session)
        self.assertEqual(client.calls[-1][0], "end_session")

    def test_cua_browser_setup_binding_and_cleanup_stay_owned(self):
        client = FakeMCP(cua_tools(), cua_responses())
        with ScopedEngine("cua-driver", BROWSER, client) as engine:
            self.assertEqual(engine.context["browser"]["target_id"], "target-1")
            self.assertNotIn("apps", engine.context)
            self.assertNotIn("browser_prepare", {tool["name"] for tool in engine.tools()})
            engine.call("browser_navigate", {"target_id": "target-1", "tab_id": "tab-1", "url": URL})
            with self.assertRaises(ScopeError):
                engine.call("get_browser_state", {"pid": 101, "window_id": 11})
            with self.assertRaises(ScopeError):
                engine.call("browser_navigate", {"target_id": "personal", "tab_id": "tab-1", "url": URL})
        self.assertIn(("kill_app", {"pid": 777}), client.calls)
        self.assertTrue(client.closed)

    def test_cua_browser_waits_for_blank_document_without_repeating_launch(self):
        client = FakeMCP(cua_tools(), cua_responses())
        original, reads = client.call, []
        def call(name, args):
            if name == "list_windows" and not reads:
                reads.append(True)
                client.calls.append((name, args))
                return content({"windows": []})
            return original(name, args)
        with patch.object(client, "call", side_effect=call):
            with ScopedEngine("cua-driver", BROWSER, client) as engine:
                self.assertEqual(engine.context["browser"]["window_id"], 7777)
        self.assertEqual(sum(name == "browser_prepare" for name, _ in client.calls), 1)
        self.assertEqual(sum(name == "list_windows" for name, _ in client.calls), 2)

    def test_cua_browser_native_fallback_projects_and_requires_webarea_tokens(self):
        responses = cua_responses()
        responses["get_window_state"] = content({"window_id": 7777, "tree_markdown": "PRIVATE APP MENU", "element_count": 5,
            "elements": [{"element_index": 0, "role": "AXWindow", "element_token": "window"},
                         {"element_index": 1, "role": "AXTextField", "parent_index": 0, "element_token": "address", "label": "Address bar"},
                         {"element_index": 2, "role": "AXWebArea", "parent_index": 0, "element_token": "web"},
                         {"element_index": 3, "role": "AXPopUpButton", "parent_index": 2, "element_token": "route", "label": "Route"},
                         {"element_index": 4, "role": "AXMenuBar", "element_token": "private", "label": "Private recent files"}]})
        responses["get_window_state"]["content"].append({"type": "image", "mimeType": "image/png", "data": "eA=="})
        client = FakeMCP(cua_tools(), responses)
        with ScopedEngine("cua-driver", BROWSER, client) as engine:
            result = engine.call("get_window_state", {"pid": 777, "window_id": 7777})
            self.assertEqual([row["element_token"] for row in result["structuredContent"]["elements"]], ["web", "route"])
            self.assertEqual(result["content"][-1]["type"], "image")
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertNotIn("Private recent", json.dumps(result))
            self.assertNotIn("Address bar", json.dumps(result))
            self.assertEqual(result["structuredContent"]["element_count"], 5)
            for token in ["address", "private", "web", "not-observed"]:
                with self.subTest(token=token), self.assertRaises(ScopeError):
                    engine.call("set_value", {"pid": 777, "window_id": 7777, "element_token": token, "value": "Priority"})
            engine.call("set_value", {"pid": 777, "window_id": 7777, "element_token": "route", "value": "Priority"})
            with self.assertRaises(ScopeError):
                engine.call("press_key", {"pid": 777, "window_id": 7777, "key": "down"})
            engine.call("press_key", {"pid": 777, "window_id": 7777, "element_token": "route", "key": "down"})

    def test_cua_browser_without_webarea_exposes_no_native_action_tokens(self):
        responses = cua_responses()
        responses["get_window_state"] = content({"window_id": 7777, "elements": [
            {"element_index": 0, "role": "AXWindow"},
            {"element_index": 1, "role": "AXTextField", "parent_index": 0, "element_token": "address"}]})
        with ScopedEngine("cua-driver", BROWSER, FakeMCP(cua_tools(), responses)) as engine:
            result = engine.call("get_window_state", {"pid": 777, "window_id": 7777})
            self.assertEqual(result["structuredContent"]["elements"], [])
            self.assertIn("No permitted AXWebArea", result["content"][0]["text"])
            with self.assertRaises(ScopeError):
                engine.call("set_value", {"pid": 777, "window_id": 7777, "element_token": "address", "value": "Priority"})

    def test_cua_native_confirmation_fallback_requires_attached_modal_ancestry(self):
        for modal_shape in [{"role": "AXSheet"}, {"role": "AXDialog"}, {"role": "AXGroup", "subrole": "AXDialog"}]:
            with self.subTest(modal_shape=modal_shape):
                responses = cua_responses()
                responses["get_window_state"] = content({"window_id": 7777, "elements": [
                    {"element_index": 0, "role": "AXWindow", "element_token": "window"},
                    {"element_index": 1, "role": "AXButton", "parent_index": 0, "element_token": "chrome", "label": "Browser menu"},
                    {"element_index": 2, "parent_index": 0, "element_token": "modal", **modal_shape},
                    {"element_index": 3, "role": "AXButton", "parent_index": 2, "element_token": "modal-button", "label": "Generic action"},
                    {"element_index": 4, "role": "AXDialog", "element_token": "unattached-dialog"},
                    {"element_index": 5, "role": "AXButton", "parent_index": 4, "element_token": "unattached-button"},
                    {"element_index": 6, "role": "AXMenuBar", "element_token": "global-menu"}]})
                with ScopedEngine("cua-driver", BROWSER, FakeMCP(cua_tools(), responses)) as engine:
                    result = engine.call("get_window_state", {"pid": 777, "window_id": 7777})
                    self.assertEqual([row["element_token"] for row in result["structuredContent"]["elements"]], ["modal", "modal-button"])
                    self.assertEqual(result["structuredContent"]["benchmark_scope_projection"]["scope"], "fixture_web_area_or_attached_modal")
                    engine.call("click", {"pid": 777, "window_id": 7777, "element_token": "modal-button"})
                    for token in ["chrome", "modal", "unattached-button", "global-menu"]:
                        with self.subTest(token=token), self.assertRaises(ScopeError):
                            engine.call("click", {"pid": 777, "window_id": 7777, "element_token": token})
                    with self.assertRaises(ScopeError):
                        engine.call("click", {"pid": 777, "window_id": 7777, "x": 50, "y": 50})

    def test_native_cua_projection_excludes_global_menu_roots(self):
        response = content({"window_id": 11, "elements": [{"element_index": 0, "role": "AXWindow"},
            {"element_index": 1, "role": "AXTextField", "parent_index": 0, "label": "Fixture text", "element_token": "field"},
            {"element_index": 2, "role": "AXMenuBar", "element_token": "menu", "label": "Recent private file"}]})
        with ScopedEngine("cua-driver", NATIVE, FakeMCP(cua_tools(), {"get_window_state": response})) as engine:
            result = engine.call("get_window_state", {"pid": 101, "window_id": 11})
            self.assertIn("Fixture text", json.dumps(result))
            self.assertNotIn("Recent private file", json.dumps(result))
            with self.assertRaises(ScopeError):
                engine.call("click", {"window_id": 11, "element_token": "menu"})

    def test_cua_refuses_reused_pid_without_killing_personal_app(self):
        client = FakeMCP(cua_tools(), cua_responses(pid=101))
        with self.assertRaises(EngineError):
            with ScopedEngine("cua-driver", BROWSER, client):
                pass
        self.assertFalse(any(name == "kill_app" for name, _ in client.calls))
        self.assertTrue(client.closed)

    def test_canvas_rejects_synthetic_dom_gesture(self):
        request = {**BROWSER, "task_id": "canvas_route"}
        with ScopedEngine("cua-driver", request, FakeMCP(cua_tools(), cua_responses())) as engine:
            with self.assertRaises(ScopeError):
                engine.call("browser_pointer", {"target_id": "target-1", "tab_id": "tab-1", "action": "drag", "input_route": "dom_event"})

    def test_cleanup_failure_cannot_silently_pass(self):
        responses = cua_responses()
        responses["kill_app"] = EngineError("Driver refused cleanup")
        client = FakeMCP(cua_tools(), responses)
        with self.assertRaises(EngineError):
            with ScopedEngine("cua-driver", BROWSER, client):
                pass
        self.assertTrue(client.closed)
        self.assertEqual(client.calls[-1][0], "end_session")

    def test_no_installed_sc_fallback_or_remote_mcp(self):
        with self.assertRaises(UnsupportedEngine):
            create_engine("second-cursor", NATIVE, env={})
        with self.assertRaises(EngineError):
            create_engine("second-cursor", NATIVE, env={"SC_BENCH_MCP_URL": "https://example.com/mcp", "SC_BENCH_MCP_TOKEN": "secret"})

    def test_cua_cleanup_refuses_reused_pid(self):
        client = FakeMCP(cua_tools(), cua_responses())
        with patch("benchmarks.engines.process_identity", side_effect=["created", "different-process", "different-process"]):
            with ScopedEngine("cua-driver", BROWSER, client) as engine:
                pass
        self.assertEqual(engine.identity["cleanup_evidence"]["owned_browser"]["status"], "pid_reused")
        self.assertTrue(engine.identity["cleanup_evidence"]["owned_browser"]["replacement_not_signalled"])
        self.assertFalse(any(name == "kill_app" for name, _ in client.calls))
        self.assertEqual(client.calls[-1][0], "end_session")

    def test_cleanup_evidence_confirms_process_absence_in_original_identity(self):
        client = FakeMCP(cua_tools(), cua_responses())
        with ScopedEngine("cua-driver", BROWSER, client) as engine:
            identity = engine.identity
            self.assertEqual(identity["owned_browser"]["pid"], 777)
            self.assertEqual(identity["owned_browser"]["process_identity"], "fixture-process-birth")
            self.assertFalse(identity["cleanup_evidence"]["owned_browser"]["verified"])
        self.assertIs(identity, engine.identity)
        evidence = identity["cleanup_evidence"]["owned_browser"]
        self.assertEqual(evidence["status"], "absent")
        self.assertTrue(evidence["verified"])
        self.assertTrue(evidence["original_process_absent"])
        self.assertEqual(evidence["checks"], 1)
        self.assertEqual(engine.cleanup_errors, [])

    def test_successful_cleanup_commands_do_not_prove_process_stopped(self):
        client = FakeMCP(cua_tools(), cua_responses())
        with patch("benchmarks.engines.process_identity", return_value="still-alive"):
            with self.assertRaisesRegex(EngineError, "still running"):
                with ScopedEngine("cua-driver", BROWSER, client) as engine:
                    pass
        self.assertFalse(engine.identity["cleanup_evidence"]["owned_browser"]["verified"])
        self.assertEqual(engine.identity["cleanup_evidence"]["owned_browser"]["status"], "still_running")
        self.assertLess(engine.identity["cleanup_evidence"]["owned_browser"]["elapsed_ms"], 3000)

    def test_unreadable_cleanup_identity_is_not_claimed_absent(self):
        client = FakeMCP(cua_tools(), cua_responses())
        with patch("benchmarks.engines.process_identity", side_effect=["created", "created", OSError("unavailable")]):
            with self.assertRaisesRegex(EngineError, "verify"):
                with ScopedEngine("cua-driver", BROWSER, client) as engine:
                    pass
        self.assertEqual(engine.identity["cleanup_evidence"]["owned_browser"]["status"], "observation_failed")
        self.assertFalse(engine.identity["cleanup_evidence"]["owned_browser"]["verified"])

    def test_inspect_cannot_read_hidden_implementation(self):
        client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": URL}})})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            engine.call("browser_open", {"url": URL})
            for locator in [{"selector": "script"}, {"selector": "body"}, {"selector": "html"},
                            {"selector": "head"}, {"selector": "*", "nth": 5}, {"nth": 1},
                            {"selector": "#route-canvas", "visible": False}, {"chain": [{"selector": "script"}]}]:
                with self.subTest(locator=locator), self.assertRaises(ScopeError):
                    engine.call("browser_command", {"tab_id": 4, "operation": "inspect", "arguments": {"locator": locator}})
            engine.call("browser_command", {"tab_id": 4, "operation": "inspect", "arguments": {"locator": {"selector": "#route-canvas"}}})
            self.assertIs(client.calls[-1][1]["arguments"]["locator"]["visible"], True)

    def test_sc_engine_pin_checked_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "SecondCursor.app"
            binary = bundle / "Contents/MacOS/SecondCursor"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture binary")
            expected = "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()
            env = {"SC_E2E_APP": str(bundle), "SC_BENCH_MCP_URL": "http://127.0.0.1:1/mcp", "SC_BENCH_MCP_TOKEN": "secret"}
            engine = create_engine("second-cursor", {**NATIVE, "engine_version": expected}, env=env)
            self.assertEqual(engine.pinned_identity["engine_version"], expected)
            self.assertTrue(engine.pinned_identity["verified"])
            binary.write_bytes(b"changed")
            with self.assertRaisesRegex(EngineError, "differs"):
                create_engine("second-cursor", {**NATIVE, "engine_version": expected}, env=env)

    def test_cua_cli_and_daemon_versions_are_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = pathlib.Path(directory) / "cua-driver"
            binary.write_text("#!/bin/sh\nprintf 'cua-driver 0.33.0\\n'\n")
            binary.chmod(0o700)
            env = {"SC_BENCH_CUA_BINARY": str(binary), "CUA_DRIVER_EMBEDDED": "1"}
            engine = create_engine("cua-driver", {**NATIVE, "engine_version": "cua-driver 0.33.0"}, env=env)
            self.assertEqual(engine.client.command, [str(binary), "mcp"])
            self.assertNotIn("CUA_DRIVER_EMBEDDED", engine.client.env)
            self.assertIsNone(engine.client.process)
            with self.assertRaisesRegex(EngineError, "differs"):
                create_engine("cua-driver", {**NATIVE, "engine_version": "cua-driver 0.32.0"}, env=env)
            client = FakeMCP(cua_tools())
            client.server_info["version"] = "0.32.0"
            with self.assertRaisesRegex(EngineError, "daemon version"):
                with ScopedEngine("cua-driver", NATIVE, client, pinned_identity=engine.pinned_identity):
                    pass


class TransportTests(unittest.TestCase):
    def test_process_identity_distinguishes_absence_from_observer_failure(self):
        import subprocess
        for output, expected in [(subprocess.CompletedProcess([], 0, "birth command\n", ""), "birth command"),
                                 (subprocess.CompletedProcess([], 1, "", ""), None)]:
            with patch("benchmarks.engines.subprocess.run", return_value=output):
                self.assertEqual(process_identity(123), expected)
        with patch("benchmarks.engines.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaises(EngineError):
                process_identity(123)

    def test_proxy_exit_does_not_leave_sigterm_ignoring_helper(self):
        program = '''import sys,json,subprocess,time
child=subprocess.Popen([sys.executable,'-u','-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)'])
time.sleep(.1)
for line in sys.stdin:
 m=json.loads(line)
 if 'id' in m: print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'content':[]}}),flush=True)
'''
        client = StdioMCP([sys.executable, "-u", "-c", program], timeout=2)
        client.call("probe", {})
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(client.reader.is_alive())

    def test_http_session_headers_notifications_sse_and_delete(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(("POST", request, dict(self.headers)))
                if "id" not in request:
                    body, code, ctype = b"", 202, "application/json"
                else:
                    result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "test"}} if request["method"] == "initialize" else {"content": [{"type": "text", "text": "ok"}]}
                    body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                    code, ctype = 200, "application/json"
                    if request["method"] == "tools/call":
                        body = b'event: message\ndata: {"jsonrpc":"2.0","method":"note"}\n\nevent: message\ndata: ' + body + b"\n\n"
                        ctype = "text/event-stream"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if request["method"] == "initialize":
                    self.send_header("Mcp-Session-Id", "fixture-session")
                self.end_headers()
                self.wfile.write(body)
            def do_DELETE(self):
                seen.append(("DELETE", None, dict(self.headers)))
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        client = HTTPMCP(f"http://127.0.0.1:{server.server_port}/mcp", "test-secret", 2)
        try:
            client.initialize()
            self.assertEqual(client.call("observe", {})["content"][0]["text"], "ok")
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.assertEqual([entry[0] for entry in seen], ["POST", "POST", "POST", "DELETE"])
        self.assertEqual(seen[1][2]["Mcp-Session-Id"], "fixture-session")
        self.assertEqual(seen[-1][2]["Mcp-Session-Id"], "fixture-session")

    def test_stdio_multimodal_content_and_subprocess_cleanup(self):
        program = '''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 result={'protocolVersion':'2025-03-26'} if m['method']=='initialize' else {'content':[{'type':'text','text':'outline'},{'type':'image','mimeType':'image/png','data':'eA=='}]}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''
        client = StdioMCP([sys.executable, "-u", "-c", program], timeout=2)
        try:
            client.initialize()
            process = client.process
            result = client.call("get_window_state", {})
            self.assertEqual([row["type"] for row in result["content"]], ["text", "image"])
        finally:
            client.close()
        self.assertIsNotNone(process.poll())

    def test_stdio_timeout_never_retries_mutation(self):
        client = StdioMCP([sys.executable, "-u", "-c", "import time; time.sleep(20)"], timeout=.02)
        try:
            with self.assertRaises(EngineError):
                client.call("click", {})
            process = client.process
            with self.assertRaisesRegex(EngineError, "uncertain"):
                client.call("click", {})
            self.assertEqual(client.next_id, 1)
        finally:
            client.close()
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
