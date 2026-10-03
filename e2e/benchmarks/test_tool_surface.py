"""Offline catalog projection tests; no models, MCP processes, or GUI are started."""
import copy
import hashlib
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks import tool_surface
from benchmarks.engines import ScopedEngine, ScopeError
from benchmarks.test_engines import FakeMCP, NATIVE, content


def definition(name, properties, required=(), description=None):
    return {"name": name, "description": description or f"Use {name} within the current window only.",
            "inputSchema": {"type": "object", "properties": copy.deepcopy(properties),
                            "required": list(required), "additionalProperties": False}}


def native_catalog():
    # Same flattened typed step contract as ToolCatalog.batchStepArguments. The
    # projection must also survive differing property descriptions and defaults.
    fields = {
        "window_id": {"type": "integer", "enum": [11, 12]},
        "element_index": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"},
        "button": {"type": "string", "enum": ["left", "right", "middle"]}, "count": {"type": "integer"},
        "coordinate_space": {"type": "string", "enum": ["window", "screenshot"]},
        "text": {"type": "string"}, "prefix": {"type": "string"}, "suffix": {"type": "string"},
        "selection_type": {"type": "string", "enum": ["text", "cursor_before", "cursor_after"]},
        "keystrokes_only": {"type": "boolean"}, "keys": {"type": "string"}, "value": {"type": "string"},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "amount": {"type": "integer"},
        "from_x": {"type": "number"}, "from_y": {"type": "number"},
        "to_x": {"type": "number"}, "to_y": {"type": "number"},
        "action": {"type": "string"}, "caption": {"type": "string"}, "ms": {"type": "integer"},
    }
    action_fields = {
        "click": (["element_index", "x", "y", "button", "count", "coordinate_space"], []),
        "drag": (["from_x", "from_y", "to_x", "to_y", "coordinate_space"], ["from_x", "from_y", "to_x", "to_y"]),
        "type_text": (["text", "element_index", "keystrokes_only"], ["text"]),
        "select_text": (["text", "element_index", "prefix", "suffix", "selection_type"], ["element_index", "text"]),
        "set_value": (["element_index", "value"], ["element_index", "value"]),
        "press_key": (["keys"], ["keys"]),
        "scroll": (["direction", "amount", "element_index", "x", "y", "coordinate_space"], ["direction"]),
        "perform_action": (["element_index", "action"], ["element_index", "action"]),
    }
    common = {"window_id": fields["window_id"], "caption": fields["caption"],
              "observe": {"type": "string", "enum": ["tree", "screenshot", "both", "none"], "default": "tree"},
              "expect": {"type": "object", "description": "The action must verify the supplied postcondition."}}
    tools = []
    for name, (properties, required) in action_fields.items():
        tools.append(definition(name, {**common, **{key: fields[key] for key in properties}},
                                ["window_id", *required]))
    step_fields = copy.deepcopy(fields)
    step_fields["window_id"].pop("enum")
    step_fields["tool"] = {"type": "string", "enum": [*action_fields, "wait", "browser_click"]}
    batch = definition("batch", {**common,
        "steps": {"type": "array", "minItems": 1, "maxItems": 50,
                  "items": {"type": "object", "properties": step_fields, "required": ["tool"]}},
        "stop_on_error": {"type": "boolean", "default": True},
        "stop_on_layout_change": {"type": "boolean", "default": True},
        "full": {"type": "boolean", "default": False}}, ["window_id", "steps"],
        "Run known steps only. Stop before unknown UI outcomes. Stale coordinates must be refused.")
    tools.append(batch)
    for name in sorted(tool_surface.READ_TOOLS):
        props = {"window_id": fields["window_id"]}
        if name == "get_window_state":
            props["include_screenshot"] = {"type": "boolean", "default": False}
        tools.append(definition(name, props, ["window_id"]))
    return tools


class ProjectionTests(unittest.TestCase):
    def project(self, tools=None, mode="compact"):
        return tool_surface.project_native_catalog(native_catalog() if tools is None else tools, mode)

    def test_full_is_an_independent_exact_copy(self):
        tools = native_catalog()
        projected, metadata = self.project(tools, "full")
        self.assertEqual(projected, tools)
        projected[0]["inputSchema"]["properties"].clear()
        self.assertNotEqual(projected, tools)
        self.assertEqual(metadata["hidden_tool_names"], [])
        self.assertEqual(metadata["catalog_mode"], "full")

    def test_compact_preserves_all_reads_batch_schema_and_operational_instructions(self):
        tools = native_catalog()
        original = copy.deepcopy(tools)
        projected, metadata = self.project(tools)
        by_name = {item["name"]: item for item in projected}
        source = {item["name"]: item for item in tools}
        self.assertEqual(set(by_name), tool_surface.READ_TOOLS | {"batch"})
        for name in tool_surface.READ_TOOLS:
            self.assertEqual(by_name[name], source[name])
        self.assertEqual(by_name["batch"]["inputSchema"], source["batch"]["inputSchema"])
        self.assertTrue(by_name["batch"]["description"].startswith(source["batch"]["description"]))
        for name in tool_surface.MUTATING_TOOLS:
            self.assertIn(source[name]["description"], by_name["batch"]["description"])
        self.assertEqual(metadata["advertised_tool_count"], 6)
        self.assertEqual(metadata["original_tool_count"], 14)
        self.assertEqual(set(metadata["hidden_tool_names"]), tool_surface.MUTATING_TOOLS)
        self.assertEqual(tools, original)

    def test_metadata_counts_exact_serialized_catalog_bytes_and_hash(self):
        tools = native_catalog()
        projected, metadata = self.project(tools)
        data = tool_surface.catalog_bytes(projected)
        self.assertEqual(metadata["catalog_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(metadata["advertised_schema_bytes"], len(data))
        self.assertEqual(metadata["original_schema_bytes"], len(tool_surface.catalog_bytes(tools)))
        self.assertGreater(metadata["schema_bytes_saved"], 0)
        self.assertAlmostEqual(metadata["schema_reduction_percent"],
                               100 * (1 - len(data) / len(tool_surface.catalog_bytes(tools))))
        self.assertNotIn("tokens", metadata)  # Byte counts must never impersonate provider token usage.

    def test_missing_operation_or_untyped_child_fails_closed(self):
        for edit in (lambda p: p["tool"]["enum"].remove("select_text"),
                     lambda p: p.pop("text"), lambda p: p["keys"].pop("type"),
                     lambda p: p["selection_type"].update(enum=["text"]),
                     lambda p: p["from_x"].update(type="integer"),
                     lambda p: p["tool"].pop("enum")):
            tools = native_catalog()
            batch = next(t for t in tools if t["name"] == "batch")
            edit(batch["inputSchema"]["properties"]["steps"]["items"]["properties"])
            with self.assertRaises(tool_surface.CatalogProjectionError):
                self.project(tools)

    def test_batch_observation_and_expectation_coverage_cannot_disappear(self):
        for key in ("observe", "expect", "window_id"):
            tools = native_catalog()
            next(t for t in tools if t["name"] == "batch")["inputSchema"]["properties"].pop(key)
            with self.subTest(key=key), self.assertRaises(tool_surface.CatalogProjectionError):
                self.project(tools)

    def test_unknown_missing_duplicate_or_malformed_tools_are_not_silently_hidden(self):
        for tools in (native_catalog() + [definition("new_mutation", {}, [])], native_catalog()[1:],
                      native_catalog() + [native_catalog()[0]], [{"name": "batch"}], "not a catalog"):
            with self.subTest(tools=tools), self.assertRaises(tool_surface.CatalogProjectionError):
                self.project(tools)
        with self.assertRaises(tool_surface.CatalogProjectionError):
            self.project(mode="invented")

    def test_complex_or_unsupported_parameter_schema_is_not_assumed_covered(self):
        for field in ({"anyOf": [{"type": "number"}, {"type": "string"}]},
                      {"type": "number", "minimum": 0}, {"type": "string", "pattern": "^safe$"}):
            tools = native_catalog()
            next(t for t in tools if t["name"] == "click")["inputSchema"]["properties"]["x"] = field
            with self.subTest(field=field), self.assertRaises(tool_surface.CatalogProjectionError):
                self.project(tools)

    def test_projection_leaves_nested_scope_enforcement_and_mcp_content_unchanged(self):
        client = FakeMCP(native_catalog(), {"batch": {"content": [
            {"type": "text", "text": "Actual native state"},
            {"type": "image", "mimeType": "image/png", "data": "eA=="}],
            "structuredContent": {"status": "uncertain", "snapshot_id": "observed"}}})
        engine = ScopedEngine("second-cursor", NATIVE, client).start()
        original_allowed = copy.deepcopy(engine.allowed)
        projected, _ = self.project(engine.tools())
        self.assertNotIn("click", {t["name"] for t in projected})
        self.assertIn("click", engine.allowed)
        response = engine.call("batch", {"window_id": 11,
            "steps": [{"tool": "click", "element_index": 3}]})
        self.assertEqual(response, client.responses["batch"])
        self.assertEqual(engine.allowed, original_allowed)
        for step in ({"tool": "click", "window_id": 999, "element_index": 3},
                     {"tool": "press_key", "keys": "cmd+w"},
                     {"tool": "browser_click", "ref": "hidden"},
                     {"tool": "batch", "steps": []}):
            with self.subTest(step=step), self.assertRaises(ScopeError):
                engine.call("batch", {"window_id": 11, "steps": [step]})
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
