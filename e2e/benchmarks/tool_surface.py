"""Lossless read surface plus batch-only native actions for an explicit experiment.

This module projects the model's advertised catalog, never the engine allowlist,
arguments, responses, or runtime safety checks. A one-step batch represents a
standalone operation; observations and expectations belong to the outer batch.
The existing scoped engine must still validate every nested action against its
full original catalog. No task-specific targets or answers are encoded here.
"""
import copy
import hashlib
import json


READ_TOOLS = frozenset({"get_window_state", "inspect_element", "find_elements", "screenshot", "wait_for"})
MUTATING_TOOLS = frozenset({"click", "drag", "type_text", "select_text", "set_value", "press_key",
                            "scroll", "perform_action"})
NATIVE_TOOLS = READ_TOOLS | MUTATING_TOOLS | {"batch"}
CATALOG_MODES = frozenset({"full", "compact"})
_BATCH_FIELDS = frozenset({"window_id", "observe", "expect"})
_SCHEMA_KEYS = frozenset({"type", "description", "default", "enum", "properties", "required",
                         "additionalProperties", "items"})


class CatalogProjectionError(ValueError):
    pass


def catalog_bytes(tools):
    """Canonical UTF-8 schema size, explicitly not a provider token count."""
    return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _properties(schema, label):
    if (not isinstance(schema, dict) or schema.get("type") != "object"
            or not isinstance(schema.get("properties"), dict)):
        raise CatalogProjectionError(f"{label} must advertise typed object properties")
    required = schema.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
            or not set(required) <= set(schema["properties"])):
        raise CatalogProjectionError(f"{label} has malformed required arguments")
    return schema["properties"]


def _covers(original, destination, label):
    """Conservative typed-argument coverage, not a general JSON Schema theorem.

    Unknown constraints fail closed. A wider primitive/enum destination is safe
    here only because the unchanged engine still validates the original action.
    """
    if (not isinstance(original, dict) or not isinstance(destination, dict)
            or set(original) - _SCHEMA_KEYS or set(destination) - _SCHEMA_KEYS):
        raise CatalogProjectionError(f"Cannot prove typed batch coverage for {label}")
    kind, target_kind = original.get("type"), destination.get("type")
    if kind not in {"string", "integer", "number", "boolean", "object", "array"}:
        raise CatalogProjectionError(f"{label} is not explicitly typed")
    if kind != target_kind and not (kind == "integer" and target_kind == "number"):
        raise CatalogProjectionError(f"Batch narrows or changes the type of {label}")
    if "enum" in destination:
        values, target_values = original.get("enum"), destination["enum"]
        if (not isinstance(values, list) or not isinstance(target_values, list)
                or any(value not in target_values for value in values)):
            raise CatalogProjectionError(f"Batch cannot represent every advertised value of {label}")
    elif "enum" in original and not isinstance(original["enum"], list):
        raise CatalogProjectionError(f"{label} has a malformed enum")
    if "default" in original and "default" in destination and original["default"] != destination["default"]:
        raise CatalogProjectionError(f"Batch changes the advertised default of {label}")
    if kind == "object" and "properties" in original:
        children = _properties(original, label)
        available = _properties(destination, label + " batch argument")
        for key, child in children.items():
            if key not in available:
                raise CatalogProjectionError(f"Batch lacks {label}.{key}")
            _covers(child, available[key], f"{label}.{key}")
        if not set(destination.get("required", [])) <= set(original.get("required", [])):
            raise CatalogProjectionError(f"Batch adds requirements to {label}")
    if kind == "array":
        _covers(original.get("items"), destination.get("items"), label + "[]")


def _compact(tools):
    by_name = {tool["name"]: tool for tool in tools}
    if set(by_name) != NATIVE_TOOLS:
        raise CatalogProjectionError("Compact native projection requires the full known native catalog; "
                                     "unknown or missing tools must be reviewed")
    batch = by_name["batch"]
    batch_props = _properties(batch["inputSchema"], "batch")
    steps = batch_props.get("steps")
    if (not isinstance(steps, dict) or steps.get("type") != "array"
            or "steps" not in batch["inputSchema"].get("required", [])):
        raise CatalogProjectionError("Batch must require an explicitly typed steps array")
    child = steps.get("items")
    child_props = _properties(child, "batch steps")
    action = child_props.get("tool", {})
    actions = action.get("enum") if isinstance(action, dict) else None
    if (not isinstance(action, dict) or action.get("type") != "string" or not isinstance(actions, list)
            or any(not isinstance(name, str) for name in actions)
            or "tool" not in child.get("required", [])
            or not MUTATING_TOOLS <= set(actions)):
        raise CatalogProjectionError("Batch must explicitly enumerate every hidden action")
    guidance = ["Standalone actions are available as batch steps, including a single-step batch. "
                "Set window_id, observe, and expect on the outer batch; expect applies after its last step. "
                "The same action argument requirements and scope checks still apply."]
    for name in sorted(MUTATING_TOOLS):
        action_tool = by_name[name]
        props = _properties(action_tool["inputSchema"], name)
        for key, schema in props.items():
            available = batch_props if key in _BATCH_FIELDS else child_props
            if key not in available:
                raise CatalogProjectionError(f"Batch lacks the {name}.{key} argument")
            _covers(schema, available[key], f"{name}.{key}")
        required = [key for key in action_tool["inputSchema"].get("required", []) if key not in _BATCH_FIELDS]
        guidance.append(f"{name}: {action_tool['description']}" +
                        (" Required step arguments: " + ", ".join(required) + "." if required else ""))
    projected = [copy.deepcopy(tool) for tool in tools if tool["name"] in READ_TOOLS | {"batch"}]
    for tool in projected:
        if tool["name"] == "batch":
            # Keep its complete schema and safety prose; retain the standalone
            # behavioral descriptions even though their duplicate schemas go.
            tool["description"] += "\n\n" + "\n".join(guidance)
    return projected


def project_native_catalog(tools, catalog_mode="full"):
    """Return an independent model-facing catalog and auditable size metadata."""
    if catalog_mode not in CATALOG_MODES or not isinstance(tools, list) or not tools:
        raise CatalogProjectionError("A nonempty native catalog and known catalog mode are required")
    names = []
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]
                or not isinstance(tool.get("description"), str) or not isinstance(tool.get("inputSchema"), dict)):
            raise CatalogProjectionError("Every tool must have a name, description, and input schema")
        names.append(tool["name"])
    if len(set(names)) != len(names):
        raise CatalogProjectionError("Duplicate tool names make catalog coverage ambiguous")
    try:
        original_bytes = catalog_bytes(tools)
        projected = copy.deepcopy(tools) if catalog_mode == "full" else _compact(tools)
        projected_bytes = catalog_bytes(projected)
    except (TypeError, ValueError) as error:
        if isinstance(error, CatalogProjectionError):
            raise
        raise CatalogProjectionError(f"Catalog is not serializable JSON: {error}") from error
    advertised = [tool["name"] for tool in projected]
    metadata = {"catalog_mode": catalog_mode, "advertised_tool_names": advertised,
                "advertised_tool_count": len(projected), "original_tool_count": len(tools),
                "hidden_tool_names": [name for name in names if name not in advertised],
                "catalog_sha256": hashlib.sha256(projected_bytes).hexdigest(),
                "original_catalog_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "original_schema_bytes": len(original_bytes), "advertised_schema_bytes": len(projected_bytes),
                "schema_bytes_saved": len(original_bytes) - len(projected_bytes),
                "schema_reduction_percent": 100 * (1 - len(projected_bytes) / len(original_bytes)),
                "measurement": "Canonical UTF-8 catalog bytes, not provider token usage"}
    return projected, metadata
