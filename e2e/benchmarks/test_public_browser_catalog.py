"""Compile the public Swift catalog in isolation; no app, browser or model starts."""
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks import cursor_model_host, model_host, test_cursor_model_host as cursor_fakes
from benchmarks.engines import BROWSER_OPERATIONS, ScopeError, ScopedEngine
from benchmarks.test_engines import BROWSER, FakeMCP, URL, content

REPO = pathlib.Path(__file__).resolve().parents[2]
OPERATIONS = {
    "capabilities", "user_tabs", "claim", "release", "retain", "dom_snapshot", "inspect",
    "click", "hover", "fill", "type", "paste", "select_text", "perform_action", "check", "select_option",
    "press_key", "scroll", "drag", "back", "forward", "reload", "wait", "dialog", "upload",
    "downloads", "logs", "history", "export", "assets", "viewport", "screenshot", "cdp", "batch", "clipboard",
}


class PublicBrowserCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dependencies = ("MCP", "Logging", "EventSource", "SystemPackage", "CSystem")
        products = next((REPO / ".build" / mode for mode in ("debug", "release")
                        if all((REPO / ".build" / mode / (name + ".o")).is_file() for name in dependencies)
                        and (REPO / ".build" / mode / "MCP.swiftmodule").exists()), None)
        if not products or not shutil.which("xcrun"):
            raise unittest.SkipTest("Build Swift dependencies once to run the isolated production-catalog probe")
        with tempfile.TemporaryDirectory(prefix="sc-public-catalog-") as directory:
            scratch = pathlib.Path(directory)
            main = scratch / "main.swift"
            main.write_text('import Foundation\n'
                'let catalogs = ["default": ToolCatalog.advertised(surface: nil), '
                '"full": ToolCatalog.advertised(surface: "full"), '
                '"compact": ToolCatalog.advertised(surface: "compact")]\n'
                'FileHandle.standardOutput.write(try JSONEncoder().encode(catalogs))\n')
            command = ["xcrun", "swiftc", "-I", str(products), "-module-cache-path", str(scratch / "module-cache"),
                       str(REPO / "Sources/SCServer/ToolCatalog.swift"), str(main)]
            command += [str(products / (name + ".o")) for name in dependencies]
            command += ["-o", str(scratch / "catalog-probe")]
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if compiled.returncode:
                raise AssertionError("Production catalog failed to compile:\n" + compiled.stderr)
            cls.catalogs = json.loads(subprocess.check_output([scratch / "catalog-probe"], timeout=10))
        cls.tools = {tool["name"]: tool for tool in cls.catalogs["full"]}
        cls.command = cls.tools["browser_command"]

    def test_public_dom_arguments_are_typed_and_canonical(self):
        arguments = self.command["inputSchema"]["properties"]["arguments"]
        locator = arguments["properties"]["locator"]
        self.assertEqual(locator["type"], "object")
        for field in ("selector", "role", "name", "label", "text", "placeholder", "test_id"):
            self.assertEqual(locator["properties"][field]["type"], "string", field)
        self.assertEqual(locator["properties"]["exact"]["type"], "boolean")
        self.assertEqual(locator["properties"]["nth"]["type"], "integer")
        self.assertEqual(arguments["properties"]["value"]["anyOf"],
                         [{"type": "string"}, {"type": "array", "items": {"type": "string"}}])
        self.assertEqual(arguments["properties"]["text"]["type"], "string")
        help_text = self.command["description"] + arguments["description"]
        for fragment in ("inspect requires arguments.locator", "select_option requires arguments.locator and arguments.value",
                         '"locator":{"role":"region"', '"locator":{"label":"Route"}', '"value":"Priority"',
                         "not top-level ref", "not top-level label"):
            self.assertIn(fragment, help_text)
        for fragment in ("ARIA", "region", "section"):
            self.assertIn(fragment, locator["properties"]["role"]["description"])
        self.assertIn("values take precedence", arguments["properties"]["value"]["description"])

    def test_other_operations_fields_and_native_batch_remain_available(self):
        schema = self.command["inputSchema"]
        self.assertEqual(set(schema["properties"]["operation"]["enum"]), OPERATIONS)
        arguments = schema["properties"]["arguments"]
        self.assertTrue(arguments["additionalProperties"])
        self.assertTrue(arguments["properties"]["locator"]["additionalProperties"])
        self.assertFalse(arguments.get("required"))  # Requirements vary by operation.
        self.assertFalse(arguments.get("oneOf"))
        self.assertIn("window_id", self.tools["batch"]["inputSchema"]["properties"])
        compact = {tool["name"]: tool for tool in self.catalogs["compact"]}
        self.assertEqual(self.tools["batch"]["inputSchema"], compact["act"]["inputSchema"])
        for surface in self.catalogs.values():
            self.assertEqual(next(tool for tool in surface if tool["name"] == "browser_command"), self.command)

    def test_scoped_projection_preserves_contract_without_advertising_forbidden_frames(self):
        original = copy.deepcopy(self.command)
        with ScopedEngine("second-cursor", BROWSER, FakeMCP(list(self.tools.values()))) as engine:
            command = next(tool for tool in engine.tools() if tool["name"] == "browser_command")
            schema = command["inputSchema"]
            self.assertEqual(set(schema["properties"]["operation"]["enum"]), BROWSER_OPERATIONS)
            arguments = schema["properties"]["arguments"]
            self.assertNotIn("frame", arguments["properties"]["locator"]["properties"])
            self.assertEqual(arguments["properties"]["value"],
                             original["inputSchema"]["properties"]["arguments"]["properties"]["value"])
            self.assertTrue(arguments["description"].startswith(original["inputSchema"]["properties"]["arguments"]["description"]))
        self.assertEqual(self.command, original)

    def test_refusals_explain_public_form_and_do_not_dispatch(self):
        client = FakeMCP(list(self.tools.values()), {"browser_open": content({"tab": {"id": 4, "url": URL}})})
        with ScopedEngine("second-cursor", BROWSER, client) as engine:
            engine.call("browser_open", {"url": URL})
            calls_before = len(client.calls)
            for operation, fields, message in (
                ("inspect", {"ref": "e1"}, "arguments.locator"),
                ("select_option", {"ref": "e1", "label": "Priority"}, "arguments.value"),
                ("select_option", {"locator": {"label": "Route"}, "label": "Priority"}, "arguments.value"),
                ("inspect", {"locator": {"frame": "iframe", "role": "button"}}, "rendered-page"),
                ("inspect", {"locator": {"chain": [{"ref": "e1"}]}}, "rendered-page"),
            ):
                with self.subTest(operation=operation, fields=fields), self.assertRaisesRegex(ScopeError, message):
                    engine.call("browser_command", {"tab_id": 4, "operation": operation, "arguments": fields})
            self.assertEqual(len(client.calls), calls_before)

    def test_codex_receives_the_public_schema(self):
        projected = model_host._tool_specs([self.command])[0]
        self.assertEqual(projected["inputSchema"], self.command["inputSchema"])
        self.assertEqual(projected["description"], self.command["description"])

    def test_cursor_receives_the_public_schema(self):
        ready = cursor_fakes.ready()
        ready["isolation"]["fixture_tools"] = ["browser_command"]
        bridge = cursor_fakes.FakeBridge([ready, cursor_fakes.finished()])
        config = model_host.HostConfig("grok-4.7", "medium", 3, 2, cursor_fakes.VERSION)
        with mock.patch.object(cursor_model_host, "host_version", return_value=cursor_fakes.VERSION), \
                mock.patch.object(cursor_model_host, "_Bridge", return_value=bridge):
            result = cursor_model_host.CursorModelHost(config, cursor_fakes.PARAMS).run(
                "Synthetic contract only.", [self.command], lambda *_: self.fail("Unexpected tool execution"))
        self.assertEqual(result.status, "completed", result.error)
        init = next(message for message in bridge.sent if message["type"] == "init")
        self.assertEqual(init["tools"][0], {key: self.command[key] for key in ("name", "description", "inputSchema")})


if __name__ == "__main__":
    unittest.main()
