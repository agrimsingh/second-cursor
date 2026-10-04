"""Offline scoped-MCP tests; no desktop, model API, or real process launches."""
import copy
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks.cross_app_engine import ScopedCrossAppEngine, NATIVE_MUTATIONS
from benchmarks.engines import EngineError, ScopeError

URL = "http://127.0.0.1:8132/fixture.html?task=workspace_recovery&seed=71"
WINDOW = {"id": 41, "pid": 81, "bundle_id": "com.apple.TextEdit"}
TAB = {"id": 21, "url": URL}


def content(data, **extra):
    return {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data, **extra}


def tool(name, props, required=()):
    return {"name": name, "description": "Runtime documentation " + name,
            "inputSchema": {"type": "object", "properties": json.loads(json.dumps(props)), "required": list(required)},
            "annotations": {"readOnlyHint": name.endswith("snapshot")}}


def catalog():
    integer, string, boolean = {"type": "integer"}, {"type": "string"}, {"type": "boolean"}
    target = {"window_id": integer, "app": string, "session_id": string,
              "operation_id": string, "observation_id": string}
    native = {**target, "element_index": integer, "text": string, "value": string, "keys": string,
              "observe": string, "x": {"type": "number"}, "y": {"type": "number"},
              "prefix": string, "suffix": string, "selection_type": string, "direction": string,
              "amount": integer, "button": string, "count": integer, "expect": {"type": "object"}}
    out = [tool(name, native, {"type_text": ["text"], "set_value": ["value", "element_index"],
                              "select_text": ["text", "element_index"], "press_key": ["keys"]}.get(name, []))
           for name in NATIVE_MUTATIONS]
    out.extend([tool("get_window_state", {**target, "full": boolean, "include_menu_bar": boolean,
                                         "include_screenshot": boolean, "scope": {}}),
                tool("inspect_element", {**target, "element_index": integer, "offset": integer, "length": integer}, ["element_index"]),
                tool("operation_status", {"operation_id": string}, ["operation_id"]),
                tool("screenshot", target), tool("find_elements", {**target, "text": string, "role": string}),
                tool("wait_for", {**target, "text": string, "timeout_ms": integer})])
    browser = {"tab_id": integer, "session_id": string, "browser_id": string, "ref": string,
               "keys": string, "text": string, "url": string, "full": boolean,
               "button": string, "x": {"type": "number"}, "y": {"type": "number"}, "submit": boolean}
    out.extend(tool(name, browser) for name in ("browser_snapshot", "browser_click", "browser_type", "browser_press_key",
                                               "browser_scroll", "browser_screenshot", "browser_navigate"))
    out.append(tool("browser_command", {"tab_id": integer, "operation": string, "browser_id": string,
        "arguments": {"type": "object", "properties": {
            "locator": {"type": "object", "properties": {name: string for name in ("label", "role", "name", "text", "selector", "frame")},
                        "additionalProperties": True}, "text": string, "value": {"anyOf": [string, {"type": "array", "items": string}]}},
            "additionalProperties": True}}, ["operation"]))
    step = {**native, "tool": string, "ref": string, "submit": boolean, "ms": integer, "args": {"type": "object"}}
    out.append(tool("batch", {**target, "tab_id": integer, "steps": {"type": "array", "items": {"type": "object", "properties": step}},
                              "expect": {"type": "object"}, "observe": string}, ["steps"]))
    for name in ("launch_app", "browser_open", "browser_close", "browser_repl", "list_windows", "session", "task", "bind_target"):
        out.append(tool(name, {}))
    return out


class FakeMCP:
    def __init__(self):
        self.session_id, self.calls, self.responses = "transport-session", [], {}
        self.definitions = catalog()
        self.initialized = 0

    def initialize(self):
        self.initialized += 1
        self.session_id = "transport-session"

    def rpc(self, method, params):
        if method == "tools/list":
            return {"tools": copy.deepcopy(self.definitions)}
        self.calls.append(copy.deepcopy(params))
        name, args = params["name"], params["arguments"]
        if name in self.responses:
            result = self.responses[name]
            return result(args) if callable(result) else copy.deepcopy(result)
        if name == "browser_snapshot":
            return content({"tab": TAB, "outline": "[e1] button Save"})
        if name == "bind_target":
            return content({"target": "window:41", "session_id": "task-session", "observation": {
                "window": WINDOW, "snapshot_id": "native-1", "tree": "[1] AXWindow\n  [5] AXTextArea value=Original"}})
        return content({"ok": True})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeMCP()
        self.engine = ScopedCrossAppEngine(self.client, "task-session", 21, URL, 41, 81, enable_batches=True).start()
        self.client.calls.clear()

    def assertRejected(self, name, args):
        count = len(self.client.calls)
        with self.assertRaises(ScopeError):
            self.engine.call(name, args)
        self.assertEqual(count, len(self.client.calls), "Refusal must occur before any MCP call")

    def test_runtime_catalog_and_existing_initialized_session(self):
        self.assertEqual(self.client.initialized, 0)
        self.assertIn(5, self.engine.text_areas)
        exposed = {row["name"]: row for row in self.engine.tools()}
        self.assertNotIn("browser_repl", exposed)
        self.assertNotIn("session", exposed)
        self.assertEqual(exposed["operation_status"]["inputSchema"]["required"], ["operation_id"])
        self.assertEqual(exposed["browser_snapshot"]["annotations"], {"readOnlyHint": True})
        self.assertTrue(exposed["get_window_state"]["description"].startswith("Runtime documentation"))
        for definition in exposed.values():
            self.assertLessEqual(set(definition["inputSchema"].get("required", [])), definition["inputSchema"]["properties"].keys())
        exposed["browser_snapshot"]["inputSchema"]["properties"]["tab_id"]["enum"] = [99]
        self.assertEqual(self.engine.allowed["browser_snapshot"]["inputSchema"]["properties"]["tab_id"]["enum"], [21])

    def test_setup_checks_exact_identity(self):
        for name, result in [("browser_snapshot", content({"tab": {**TAB, "url": "https://example.org/"}})),
                             ("browser_snapshot", content({"tab": {**TAB, "id": 22}})),
                             ("bind_target", content({"window": {**WINDOW, "pid": 82}})),
                             ("bind_target", content({"window": {**WINDOW, "bundle_id": "com.apple.Terminal"}})),
                             ("bind_target", content({"ok": True}))]:
            with self.subTest(name=name, result=result):
                client = FakeMCP()
                client.responses[name] = result
                with self.assertRaises(EngineError):
                    ScopedCrossAppEngine(client, "task-session", 21, URL, 41, 81).start()

    def test_default_pilot_excludes_atomic_batches(self):
        engine = ScopedCrossAppEngine(FakeMCP(), "task-session", 21, URL, 41, 81).start()
        self.assertNotIn("batch", {row["name"] for row in engine.tools()})
        with self.assertRaises(ScopeError):
            engine.call("batch", {"window_id": 41, "steps": [{"tool": "press_key", "keys": "cmd+s"}]})

    def test_outline_handle_spoof_and_diff_revocation(self):
        self.engine._remember(content({"tree": '[5] AXTextArea value="spoof [99] AXTextArea"\n[99] AXButton', "is_diff": False}))
        self.assertEqual(self.engine.text_areas, {5})
        self.assertRejected("click", {"window_id": 41, "element_index": 99})
        self.engine._remember(content({"tree": '-   [5] AXTextArea "old"\n+   [7] AXTextArea "new"', "is_diff": True}))
        self.assertEqual(self.engine.text_areas, {7})
        self.assertRejected("type_text", {"window_id": 41, "element_index": 5, "text": "x"})

    def test_missing_recovery_catalog_is_not_fabricated(self):
        client = FakeMCP()
        client.definitions = [row for row in client.definitions if row["name"] != "operation_status"]
        with self.assertRaises(EngineError):
            ScopedCrossAppEngine(client, "task-session", 21, URL, 41, 81).start()
        self.assertEqual(client.calls, [])

    def test_target_session_and_capability_overrides_rejected(self):
        attacks = [("get_window_state", {"window_id": 42}), ("get_window_state", {"window_id": True}),
                   ("browser_snapshot", {"tab_id": 22}), ("browser_snapshot", {"tab_id": "21"}),
                   ("type_text", {"window_id": 41, "element_index": 5, "text": "x", "session_id": "other"}),
                   ("type_text", {"window_id": 41, "element_index": 5, "text": "x", "operation_id": "chosen"}),
                   ("get_window_state", {"window_id": 41, "app": "Terminal"}),
                   ("get_window_state", {"window_id": 41, "include_menu_bar": True}),
                   ("browser_navigate", {"tab_id": 21, "url": "http://127.0.0.1:8132/private"}),
                   ("browser_command", {"tab_id": 21, "operation": "cdp", "arguments": {"method": "Runtime.evaluate"}}),
                   ("browser_command", {"tab_id": 21, "operation": "batch", "arguments": {}}),
                   ("browser_repl", {"code": "page.evaluate('x')"}),
                   ("operation_status", {"operation_id": "another-task"})]
        for name, args in attacks:
            with self.subTest(name=name, args=args):
                self.assertRejected(name, args)

    def test_native_mutation_uses_observed_text_area(self):
        for args in ({"window_id": 41, "text": "x"}, {"window_id": 41, "element_index": 1, "text": "x"},
                     {"window_id": 41, "element_index": True, "text": "x"},
                     {"window_id": 41, "element_index": 5, "text": "x", "x": 2}):
            self.assertRejected("type_text", args)
        original = {"window_id": 41, "element_index": 5, "text": "\nSummary"}
        self.engine.call("type_text", original)
        dispatched = self.client.calls[-1]["arguments"]
        self.assertEqual(original, {"window_id": 41, "element_index": 5, "text": "\nSummary"})
        self.assertEqual(dispatched["session_id"], "task-session")
        self.assertIn(dispatched["operation_id"], self.engine.operation_ids)

    def test_keyboard_scope_includes_native_save_only(self):
        for key in ("cmd+o", "cmd+shift+s", "cmd+w", "cmd+q", "cmd+v", "cmd+l", "ctrl+cmd+q"):
            self.assertRejected("press_key", {"window_id": 41, "keys": key})
        self.assertRejected("browser_press_key", {"tab_id": 21, "keys": "cmd+s"})
        for key in ("cmd+s", "Command+s", "Super+s", "cmd+a", "cmd+shift+right", "ArrowDown", "return"):
            self.engine.call("press_key", {"window_id": 41, "keys": key})
        self.assertEqual(len(self.client.calls), 7)

    def test_native_document_navigation_and_selection_aliases(self):
        keys = ("cmd+home", "cmd+end", "cmd+shift+home", "cmd+shift+end",
                "Command+Home", "Super+End", "Shift+Cmd+Home", "shift + COMMAND + END",
                "cmd+left", "cmd+right", "cmd+up", "cmd+down",
                "cmd+shift+left", "cmd+shift+right", "cmd+shift+up", "cmd+shift+down")
        for key in keys:
            with self.subTest(key=key):
                self.engine.call("press_key", {"window_id": 41, "keys": key})
                self.assertEqual(self.client.calls[-1]["arguments"]["keys"], key)
        self.assertEqual(len(self.client.calls), len(keys))
        self.engine.call("batch", {"window_id": 41, "steps": [
            {"tool": "press_key", "keys": "Cmd+End"},
            {"tool": "press_key", "keys": "Shift+Super+Home"}]})
        self.assertEqual(len(self.client.calls), len(keys) + 1)

    def test_native_navigation_does_not_widen_browser_or_escape_shortcuts(self):
        for key in ("cmd+home", "cmd+end", "cmd+shift+home", "cmd+shift+end", "Shift+Command+End"):
            with self.subTest(browser_key=key):
                self.assertRejected("browser_press_key", {"tab_id": 21, "keys": key})
                self.assertRejected("browser_command", {"tab_id": 21, "operation": "press_key", "arguments": {"keys": key}})
                self.assertRejected("batch", {"tab_id": 21, "steps": [{"tool": "browser_press_key", "keys": key}]})
        for key in ("cmd+cmd+end", "cmd+shift+shift+home", "cmd+alt+end", "ctrl+end",
                    "cmd+o", "cmd+shift+s", "cmd+w", "cmd+q", "cmd+c", "cmd+x", "cmd+v", "cmd+l"):
            with self.subTest(native_key=key):
                self.assertRejected("press_key", {"window_id": 41, "keys": key})
                self.assertRejected("batch", {"window_id": 41, "steps": [{"tool": "press_key", "keys": key}]})

    def test_raw_images_errors_and_unknown_fields_are_preserved(self):
        raw = {"content": [{"type": "text", "text": "Actual refused output"},
                           {"type": "image", "mimeType": "image/png", "data": "fixture"}],
               "isError": True, "structuredContent": {"refusal": {"code": "stale_observation"}},
               "_meta": {"runtime": "unchanged"}}
        self.client.responses["type_text"] = raw
        self.assertEqual(self.engine.call("type_text", {"window_id": 41, "element_index": 5, "text": "x"}), raw)
        self.assertEqual(len(self.client.calls), 1, "No auto-observe or replay after failure")

    def test_hook_order_original_result_and_journal_recovery(self):
        events, raw_events = [], []
        def before(name, args):
            events.append(("before", len(self.client.calls), args["operation_id"]))
            args["window_id"] = 999  # Hook receives a copy, not dispatch authority.
        def after(name, args, result):
            events.append(("after", len(self.client.calls), args["operation_id"]))
            self.assertTrue(result["structuredContent"]["ok"])
            return content({"outcome_unknown": True, "operation_id": args["operation_id"]}, isError=True)
        self.engine.before_call, self.engine.after_call, self.engine.emit = before, after, raw_events.append
        reply = self.engine.call("type_text", {"window_id": 41, "element_index": 5, "text": "x"})
        operation = reply["structuredContent"]["operation_id"]
        self.assertEqual(events, [("before", 0, operation), ("after", 1, operation)])
        self.assertEqual(self.client.calls[-1]["arguments"]["window_id"], 41)
        self.assertEqual(raw_events[-1]["result"], content({"ok": True}))
        self.engine.before_call = self.engine.after_call = None
        self.engine.call("operation_status", {"operation_id": operation})
        self.assertEqual(self.client.calls[-1], {"name": "operation_status", "arguments": {"operation_id": operation, "session_id": "task-session"}})

    def test_invalid_calls_do_not_trigger_faults(self):
        hooks = []
        self.engine.before_call = lambda *args: hooks.append(args)
        self.engine.after_call = lambda *args: hooks.append(args)
        self.assertRejected("type_text", {"window_id": 999, "element_index": 5, "text": "x"})
        self.assertEqual(hooks, [])

    def test_native_batch_exact_scope_and_single_dispatch(self):
        self.engine.call("batch", {"window_id": 41, "steps": [
            {"tool": "select_text", "element_index": 5, "text": "Original", "selection_type": "cursor_after"},
            {"tool": "type_text", "element_index": 5, "text": "\nSummary"},
            {"tool": "press_key", "keys": "cmd+s"}]})
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(self.client.calls[0]["arguments"]["steps"]), 3)

    def test_batch_escape_shapes_rejected_before_any_step(self):
        attacks = [{"tool": "type_text", "args": {"window_id": 99, "text": "x"}},
                   {"tool": "type_text", "window_id": 99, "element_index": 5, "text": "x"},
                   {"tool": "type_text", "element_index": 5, "text": "x", "session_id": "other"},
                   {"tool": "batch", "steps": [{"tool": "press_key", "keys": "cmd+s"}]},
                   {"tool": "browser_command", "operation": "cdp"},
                   {"tool": "browser_type", "text": "x"},
                   {"tool": "press_key", "keys": "cmd+o"},
                   {"tool": "wait", "ms": 10001}, {"tool": "wait", "ms": True},
                   {"tool": "type_text", "text": "x"}, {"tool": "type_text", "element_index": 5}]
        for step in attacks:
            with self.subTest(step=step):
                self.assertRejected("batch", {"window_id": 41, "steps": [{"tool": "press_key", "keys": "cmd+s"}, step]})
        self.assertRejected("batch", {"window_id": 41, "tab_id": 21, "steps": [{"tool": "wait", "ms": 1}]})
        self.assertRejected("batch", {"steps": [{"tool": "wait", "ms": 1}]})

    def test_browser_batch_no_cross_target_or_native_expectation(self):
        self.engine.call("batch", {"tab_id": 21, "steps": [{"tool": "browser_click", "ref": "e1"}, {"tool": "wait", "ms": 5}]})
        for step in ({"tool": "press_key", "keys": "cmd+s"},
                     {"tool": "browser_click", "ref": "e1", "window_id": 41},
                     {"tool": "browser_type", "tab_id": 99, "text": "x"}):
            self.assertRejected("batch", {"tab_id": 21, "steps": [step]})
        self.assertRejected("batch", {"tab_id": 21, "steps": [{"tool": "wait", "ms": 1}], "expect": {"text": "hidden"}})

    def test_browser_locator_and_nested_escape_rejections(self):
        for fields in ({"locator": {"selector": "script"}}, {"locator": {"label": "Price", "frame": "iframe"}},
                       {"locator": {"label": "Price", "visible": False}},
                       {"locator": {"label": "Price"}, "session_id": "other"},
                       {"locator": {"label": "Price"}, "arguments": {"operation": "cdp"}}):
            self.assertRejected("browser_command", {"tab_id": 21, "operation": "inspect", "arguments": fields})
        self.engine.call("browser_command", {"tab_id": 21, "operation": "inspect", "arguments": {"locator": {"role": "region", "name": "Details"}}})
        self.assertEqual(len(self.client.calls), 1)

    def test_locator_visibility_normalization_preserves_caller_input(self):
        args = {"tab_id": 21, "operation": "inspect", "arguments": {"locator": {"role": "region", "name": "Details"}}}
        self.engine.call("browser_command", args)
        self.assertNotIn("visible", args["arguments"]["locator"])
        self.assertIs(self.client.calls[-1]["arguments"]["arguments"]["locator"]["visible"], True)

    def test_modified_and_middle_browser_clicks_rejected(self):
        self.assertRejected("browser_click", {"tab_id": 21, "ref": "e1", "button": "middle"})
        self.assertRejected("browser_command", {"tab_id": 21, "operation": "click", "arguments": {"locator": {"text": "View"}, "modifiers": ["cmd"]}})
        self.assertRejected("browser_command", {"tab_id": 21, "operation": "click", "arguments": {"locator": {"text": "View"}, "button": "right"}})

    def test_changed_runtime_identity_stops_followup_dispatch(self):
        self.client.responses["browser_snapshot"] = content({"tab": {**TAB, "url": "https://outside.invalid"}})
        with self.assertRaises(EngineError):
            self.engine.call("browser_snapshot", {"tab_id": 21})
        with self.assertRaises(EngineError):
            self.engine.call("browser_type", {"tab_id": 21, "text": "x"})
        self.assertEqual(len(self.client.calls), 1)

    def test_expectation_target_override_rejected(self):
        for expectation in ({"text": "x", "window_id": 99}, '{"text":"x"}', {"value": "x", "session_id": "other"}):
            self.assertRejected("type_text", {"window_id": 41, "element_index": 5, "text": "x", "expect": expectation})

    def test_no_transport_retry(self):
        def broken(args):
            raise EngineError("transport delivery uncertain")
        self.client.responses["type_text"] = broken
        with self.assertRaises(EngineError):
            self.engine.call("type_text", {"window_id": 41, "element_index": 5, "text": "x"})
        self.assertEqual(len(self.client.calls), 1)

    def test_close_preserves_harness_owned_session(self):
        self.engine.close()
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(EngineError):
            self.engine.tools()


if __name__ == "__main__":
    unittest.main()
