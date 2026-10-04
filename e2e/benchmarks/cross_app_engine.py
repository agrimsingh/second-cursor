"""Fixture-scoped adapter for any model host using Second Cursor's real MCP tools.

This is a trusted benchmark guard, not an OS sandbox. It restricts public runtime
schemas and forwards raw MCP replies; it has no workflow solver or provider API.
The harness owns setup, expected answers, disruptions, judging, and cleanup.
"""
from __future__ import annotations

import copy
import math
import re
import time
import urllib.parse
import uuid

from benchmarks.engines import (
    BROWSER_ARGUMENTS, BROWSER_LOCATOR_FIELDS, BROWSER_OPERATIONS,
    KEY_ALIASES, MODIFIER_ALIASES, EngineError, ScopeError, ScopedEngine, _ok, records,
)

NATIVE_READS = {"get_window_state", "find_elements", "inspect_element", "screenshot", "wait_for"}
NATIVE_MUTATIONS = {"click", "type_text", "set_value", "press_key", "scroll", "select_text"}
BROWSER_TOOLS = {"browser_snapshot", "browser_click", "browser_type", "browser_press_key",
                 "browser_scroll", "browser_screenshot", "browser_navigate", "browser_command"}
BATCHES = {"batch", "act"}
ALLOWED_TOOLS = NATIVE_READS | NATIVE_MUTATIONS | BROWSER_TOOLS | BATCHES | {"operation_status"}
BROWSER_READ_OPERATIONS = {"capabilities", "dom_snapshot", "inspect", "wait", "screenshot"}
HOST_FIELDS = {"session", "session_id", "operation_id", "observation_id", "app", "target",
               "browser_id", "pid", "screenshot_out_file", "debug_image_out", "include_menu_bar"}
ELEMENT_MUTATIONS = {"click", "type_text", "set_value", "select_text"}
CONDITIONS = {"text", "element_index", "value", "gone", "timeout_ms"}
# Document navigation/save are safe only in the exact already-open scratch
# native window. Keep these separate from the browser keyboard allowlist.
NATIVE_DOCUMENT_KEYS = {"cmd+s", "cmd+home", "cmd+end", "cmd+shift+home", "cmd+shift+end"}
# Native coordinates can hit a title-bar control or leave the document. The
# scratch-document track intentionally uses observed text-area AX handles.
PIXELS = {"x", "y", "from_x", "from_y", "to_x", "to_y", "coordinate_space"}
STEP_FIELDS = {
    "click": {"element_index", "button", "count"},
    "type_text": {"element_index", "text", "keystrokes_only"},
    "set_value": {"element_index", "value"},
    "select_text": {"element_index", "text", "prefix", "suffix", "selection_type"},
    "press_key": {"keys"}, "scroll": {"element_index", "direction", "amount"},
    "browser_click": {"ref", "x", "y", "button", "count"},
    "browser_type": {"ref", "text", "submit"},
    "browser_press_key": {"keys"}, "browser_scroll": {"direction", "amount"},
    "wait": {"ms"},
}
STEP_REQUIRED = {"type_text": {"text"}, "set_value": {"element_index", "value"},
                 "select_text": {"element_index", "text"}, "press_key": {"keys"},
                 "scroll": {"direction"}, "browser_type": {"text"},
                 "browser_press_key": {"keys"}, "browser_scroll": {"direction"}, "wait": {"ms"}}


def _schema_value(value, schema):
    """Validate declared public JSON schema constraints, without coercing input."""
    if not isinstance(schema, dict):
        return
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            try:
                _schema_value(value, candidate)
                break
            except ScopeError:
                pass
        else:
            raise ScopeError("Argument does not match the public schema")
    kind = schema.get("type")
    kinds = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "integer": lambda v: type(v) is int, "number": lambda v: type(v) in (int, float) and math.isfinite(v),
             "boolean": lambda v: type(v) is bool, "string": lambda v: isinstance(v, str),
             "null": lambda v: v is None}
    if kind in kinds and not kinds[kind](value):
        raise ScopeError("Argument type does not match the public schema")
    if "enum" in schema and value not in schema["enum"]:
        raise ScopeError("Argument is outside the public schema's permitted values")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ScopeError("Argument includes an unadvertised or host-only field")
        if set(schema.get("required", [])) - set(value):
            raise ScopeError("Required public tool arguments are missing")
        for key in value.keys() & props.keys():
            _schema_value(value[key], props[key])
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 100000):
            raise ScopeError("Argument array exceeds the public schema bounds")
        for child in value:
            _schema_value(child, schema.get("items", {}))
    if type(value) in (int, float):
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ScopeError("Argument exceeds the public schema bounds")


class ScopedCrossAppEngine:
    """One caller-owned task session, one disposable tab, one scratch document.

    ``client`` implements the standard ``rpc(method, params)`` MCP client API.
    Hooks run only after validation: before_call(name, dispatched_args), then
    after_call(name, dispatched_args, raw_result). The latter may return a raw
    replacement reply for explicit acknowledgement-loss injection. Hooks receive
    copies, cannot change dispatched arguments, and never run for scope refusals.
    """

    def __init__(self, client, session_id, tab_id, url, window_id, pid, *,
                 before_call=None, after_call=None, emit=None, own_session=False, enable_batches=False):
        if not isinstance(session_id, str) or not session_id or session_id == "legacy":
            raise ScopeError("A named fixture task session is required")
        if any(type(value) is not int or value <= 0 for value in (tab_id, window_id, pid)):
            raise ScopeError("Fixture targets require exact positive tab, window and PID identities")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path != "/fixture.html" or parsed.username or parsed.fragment):
            raise ScopeError("Cross-app browser scope requires the exact local fixture.html URL")
        self.client, self.session_id = client, session_id
        self.tab_id, self.url, self.window_id, self.pid = tab_id, url, window_id, pid
        self.before_call, self.after_call = before_call, after_call
        self.emit, self.own_session = emit or (lambda event: None), own_session
        self.enable_batches = enable_batches
        self.allowed, self.catalog, self.operation_ids, self.text_areas = {}, {}, set(), set()
        self.started = self.closed = False
        self.initial_observations = []
        self.context = {"engine": "second-cursor", "interface": "standard-mcp",
                        "permitted_targets": {"tab_id": tab_id, "url": url, "window_id": window_id, "pid": pid}}

    def _raw(self, name, args):
        result = self.client.rpc("tools/call", {"name": name, "arguments": args})
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise EngineError("Runtime returned malformed MCP content")
        return result

    def _host(self, name, args):
        result = self._raw(name, {**args, "session_id": self.session_id})
        if not _ok(result):
            raise EngineError("Runtime refused cross-app fixture setup")
        return result

    def start(self):
        if self.closed:
            raise EngineError("Cross-app engine is closed")
        if self.started:
            return self
        if not getattr(self.client, "session_id", None):
            self.client.initialize()
        cursor, seen = None, set()
        while True:
            result = self.client.rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result.get("tools"), list):
                raise EngineError("Runtime tools/list did not return a catalog")
            for tool in result["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                    raise EngineError("Runtime tools/list returned an invalid tool")
                if tool["name"] in self.catalog:
                    raise EngineError("Runtime tools/list returned a duplicate tool")
                self.catalog[tool["name"]] = copy.deepcopy(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise EngineError("Runtime tools/list repeated a page")
            seen.add(cursor)
        required = {"browser_snapshot", "browser_command", "get_window_state", "inspect_element", "operation_status"}
        if required - self.catalog.keys():
            raise EngineError("Runtime catalog lacks cross-app observation or recovery tools")
        selected = ALLOWED_TOOLS if self.enable_batches else ALLOWED_TOOLS - BATCHES
        self.allowed = {name: self._scope_schema(tool) for name, tool in self.catalog.items() if name in selected}
        # This tab was created in the named session by the harness; no discovery
        # or personal-tab claim is performed by the model or this adapter.
        browser = self._host("browser_snapshot", {"tab_id": self.tab_id, "full": True})
        self._attest(browser, browser=True, required=True)
        native = self._host("bind_target", {"target": f"window:{self.window_id}", "action": "claim"})
        self._attest(native, browser=False, required=True)
        self._remember(native)
        self.initial_observations = [{"tool": "browser_snapshot", "result": browser},
                                     {"tool": "bind_target", "result": native}]
        self.started = True
        return self

    def _scope_schema(self, source):
        tool = copy.deepcopy(source)
        name, schema = tool["name"], tool["inputSchema"]
        props = schema.setdefault("properties", {})
        for key in HOST_FIELDS - ({"operation_id"} if name == "operation_status" else set()):
            props.pop(key, None)
        schema["additionalProperties"] = False
        if name in NATIVE_READS | NATIVE_MUTATIONS:
            for key in PIXELS | {"tab_id"}:
                props.pop(key, None)
            props["window_id"] = {**props.get("window_id", {}), "type": "integer", "enum": [self.window_id]}
            schema["required"] = list(dict.fromkeys([*schema.get("required", []), "window_id"]))
            if name in ELEMENT_MUTATIONS:
                schema["required"] = list(dict.fromkeys([*schema["required"], "element_index"]))
        elif name in BROWSER_TOOLS:
            props.pop("window_id", None)
            props["tab_id"] = {**props.get("tab_id", {}), "type": "integer", "enum": [self.tab_id]}
            schema["required"] = list(dict.fromkeys([*schema.get("required", []), "tab_id"]))
            if "url" in props:
                props["url"]["enum"] = [self.url]
        elif name in BATCHES:
            props["window_id"] = {**props.get("window_id", {}), "type": "integer", "enum": [self.window_id]}
            props["tab_id"] = {**props.get("tab_id", {}), "type": "integer", "enum": [self.tab_id]}
            steps = props.get("steps", {}).get("items", {})
            step_props = steps.get("properties", {})
            if "tool" in step_props:
                step_props["tool"]["enum"] = sorted(STEP_FIELDS)
            for field in set(step_props) - (set().union(*STEP_FIELDS.values()) | {"tool", "window_id", "caption"}):
                step_props.pop(field)
            steps["additionalProperties"] = False
        if name == "browser_command":
            props["operation"]["enum"] = sorted(BROWSER_OPERATIONS)
            fields = props["arguments"].get("properties", {})
            for field in set(fields) - BROWSER_ARGUMENTS:
                fields.pop(field)
            locator = fields.get("locator", {})
            for field in set(locator.get("properties", {})) - BROWSER_LOCATOR_FIELDS:
                locator["properties"].pop(field)
            locator["additionalProperties"] = False
        if "expect" in props:
            props["expect"] = {**props["expect"], "type": "object"}
        schema["required"] = [field for field in schema.get("required", []) if field in props]
        tool["description"] = tool.get("description", "") + (
            " Fixture scope: only the supplied tab and scratch document. Native input targets its observed AXTextArea; "
            "Cmd+S saves that already-open scratch document. No clipboard, file dialog, shell, script, or other target access.")
        return tool

    def tools(self):
        if not self.started or self.closed:
            raise EngineError("Cross-app engine is not active")
        return copy.deepcopy(list(self.allowed.values()))

    def _attest(self, result, *, browser, required=False):
        key = "tab" if browser else "window"
        targets = [row[key] for row in records(result) if isinstance(row.get(key), dict)]
        if required and not targets:
            raise EngineError("Runtime did not attest the fixture target")
        for target in targets:
            if browser:
                valid = type(target.get("id")) is int and target["id"] == self.tab_id and target.get("url") == self.url
            else:
                valid = (type(target.get("id")) is int and target["id"] == self.window_id
                         and type(target.get("pid")) is int and target["pid"] == self.pid
                         and target.get("bundle_id") == "com.apple.TextEdit")
            if not valid:
                self.closed = True
                raise EngineError("Runtime target changed outside the permitted cross-app fixture")

    def _remember(self, result):
        for row in records(result):
            tree = row.get("tree")
            if not isinstance(tree, str):
                continue
            if not row.get("is_diff", False):
                self.text_areas.clear()
            # Runtime outline nodes begin at a line boundary. Labels and values
            # escape their newlines, so index-looking text inside them cannot
            # invent an actionable handle. Diffs revoke removed/changed roles.
            for match in re.finditer(r"(?m)^([+-] )?[ \t]*\[(\d+)\]\s+(AX[A-Za-z]+)\b", tree):
                marker, index, role = match.groups()
                index = int(index)
                if marker == "- " or role != "AXTextArea":
                    self.text_areas.discard(index)
                else:
                    self.text_areas.add(index)

    def _keys(self, args, native):
        keys = args.get("keys")
        if native and isinstance(keys, str):
            parts = [part.strip().lower() for part in keys.split("+")]
            canonical = "+".join(sorted(MODIFIER_ALIASES.get(part, part) for part in parts[:-1])
                                 + [KEY_ALIASES.get(parts[-1], parts[-1])])
            if canonical in NATIVE_DOCUMENT_KEYS:
                return
        ScopedEngine._keys(self, args)

    def _native(self, name, args):
        if type(args.get("window_id")) is not int or args["window_id"] != self.window_id:
            raise ScopeError("Native action must target the exact scratch window")
        if set(args) & (PIXELS | {"tab_id"}):
            raise ScopeError("Native coordinates and cross-target input are outside this fixture")
        if name in ELEMENT_MUTATIONS and (type(args.get("element_index")) is not int or args["element_index"] not in self.text_areas):
            raise ScopeError("Native input needs an AXTextArea index observed in the scratch document")
        self._keys(args, native=True)
        if name == "click" and (args.get("button", "left") != "left" or args.get("count", 1) not in (1, 2, 3)):
            raise ScopeError("Scratch native clicks are left clicks on the text area")

    def _browser(self, name, args):
        if type(args.get("tab_id")) is not int or args["tab_id"] != self.tab_id or "window_id" in args:
            raise ScopeError("Browser action must target the exact fixture tab")
        if "url" in args and args["url"] != self.url:
            raise ScopeError("Navigation must match the exact fixture URL")
        self._keys(args, native=False)
        if args.get("button", "left") != "left":
            raise ScopeError("Browser clicks are limited to the fixture's left-click interaction")
        for field in ("x", "y"):
            if field in args and (type(args[field]) not in (int, float) or not math.isfinite(args[field]) or args[field] < 0):
                raise ScopeError("Browser pointer coordinates must be nonnegative viewport coordinates")
        if name != "browser_command":
            return
        operation, fields = args.get("operation"), args.get("arguments", {})
        if operation not in BROWSER_OPERATIONS or not isinstance(fields, dict) or set(fields) - BROWSER_ARGUMENTS:
            raise ScopeError("Browser operation or arguments are outside the rendered-page allowlist")
        if operation == "capabilities" and fields:
            raise ScopeError("Capability help takes no operation arguments")
        if "locator" in fields:
            fields["locator"] = ScopedEngine._locator(fields["locator"])
        if operation in {"inspect", "check", "select_option"} and "locator" not in fields:
            raise ScopeError("This browser operation requires arguments.locator")
        if operation == "perform_action" and fields.get("action") not in {"focus", "scroll_into_view", "expand", "collapse", "increment", "decrement"}:
            raise ScopeError("Browser accessibility action is outside the fixture allowlist")
        if fields.get("button", "left") != "left":
            raise ScopeError("Browser clicks are limited to the fixture's left-click interaction")
        if fields.get("modifiers"):
            # Ctrl/Cmd clicks can open other tabs; all modified clicks are outside
            # this single-tab workflow, including their combinations.
            raise ScopeError("Modified browser pointer input can escape the fixture tab")
        self._keys(fields, native=False)

    def _validate(self, name, args):
        if name not in self.allowed or not isinstance(args, dict):
            raise ScopeError("Tool or arguments are outside the fixture scope")
        args = copy.deepcopy(args)
        _schema_value(args, self.allowed[name]["inputSchema"])
        if name == "operation_status":
            if args.get("operation_id") not in self.operation_ids:
                raise ScopeError("Operation status is limited to this adapter's dispatched operations")
            return args
        if name in NATIVE_READS | NATIVE_MUTATIONS:
            self._native(name, args)
        elif name in BROWSER_TOOLS:
            self._browser(name, args)
        elif name in BATCHES:
            native = "window_id" in args
            if native == ("tab_id" in args):
                raise ScopeError("Batch requires exactly one fixture window or tab")
            if native:
                self._native(name, args)
            else:
                self._browser(name, args)
                if "expect" in args:
                    raise ScopeError("Native expectations do not apply to browser batches")
            steps = args.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
                raise ScopeError("Batch requires 1 to 50 flat steps")
            for step in steps:
                if not isinstance(step, dict):
                    raise ScopeError("Batch steps must be objects")
                action = step.get("tool")
                if action not in STEP_FIELDS or set(step) - (STEP_FIELDS[action] | {"tool", "caption", "window_id"}):
                    raise ScopeError("Batch step contains an unsafe tool, nested arguments, or override")
                if STEP_REQUIRED.get(action, set()) - step.keys():
                    raise ScopeError("Batch step is missing required arguments")
                if action == "wait":
                    if set(step) - {"tool", "ms", "caption"} or type(step.get("ms")) is not int or not 0 <= step["ms"] <= 10000:
                        raise ScopeError("Batch wait must be bounded to ten seconds")
                    continue
                child = {key: value for key, value in step.items() if key != "tool"}
                if native:
                    if action not in NATIVE_MUTATIONS:
                        raise ScopeError("A native batch cannot contain browser actions")
                    child.setdefault("window_id", self.window_id)
                    self._native(action, child)
                else:
                    if action not in BROWSER_TOOLS or "window_id" in child:
                        raise ScopeError("A browser batch cannot contain native targets or actions")
                    child["tab_id"] = self.tab_id
                    self._browser(action, child)
        if "expect" in args:
            expectation = args["expect"]
            if not isinstance(expectation, dict) or set(expectation) - CONDITIONS:
                raise ScopeError("Expectation may inspect only the same fixture target")
        return args

    @staticmethod
    def is_mutation(name, args):
        return (name in NATIVE_MUTATIONS | BATCHES
                or name in BROWSER_TOOLS - {"browser_snapshot", "browser_screenshot", "browser_command"}
                or name == "browser_command" and args.get("operation") not in BROWSER_READ_OPERATIONS)

    def call(self, name, args):
        if not self.started or self.closed:
            raise EngineError("Cross-app engine is not active")
        dispatched = self._validate(name, args)
        dispatched["session_id"] = self.session_id
        if self.is_mutation(name, dispatched):
            dispatched["operation_id"] = str(uuid.uuid4())
            self.operation_ids.add(dispatched["operation_id"])
        if self.before_call:
            self.before_call(name, copy.deepcopy(dispatched))
        started = time.monotonic()
        result = self._raw(name, dispatched)
        if name in NATIVE_READS | NATIVE_MUTATIONS or name in BATCHES and "window_id" in dispatched:
            self._attest(result, browser=False)
            self._remember(result)
        if name in BROWSER_TOOLS or name in BATCHES and "tab_id" in dispatched:
            self._attest(result, browser=True)
        self.emit({"type": "tool", "name": name, "arguments": copy.deepcopy(dispatched),
                   "started_monotonic": started, "ended_monotonic": time.monotonic(),
                   "ok": _ok(result), "result": copy.deepcopy(result), "phase": "model"})
        if self.after_call:
            replacement = self.after_call(name, copy.deepcopy(dispatched), copy.deepcopy(result))
            if replacement is not None:
                if not isinstance(replacement, dict) or not isinstance(replacement.get("content"), list):
                    raise EngineError("Fault hook returned malformed MCP content")
                return replacement
        return result

    execute = call

    def close(self):
        if self.own_session:
            self._raw("session", {"action": "close", "session_id": self.session_id})
        self.closed = True


CrossAppEngine = ScopedCrossAppEngine
