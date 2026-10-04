#!/usr/bin/env python3
"""Public browser_command schema and semantic-locator regressions.

All operation fields use the nested arguments object advertised to model clients.
The disposable page supplies ordinary HTML semantics and records its own input
events; tests do not inject JavaScript or alter production/benchmark allowlists.

SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_command_contracts.py
"""
import re
import sys

from run import FixtureServer, Run, SCENARIOS, ToolError, scenario

SCENARIOS.clear()


def open_fixture(ctx, base):
    tab = ctx.call("browser_open", url=base + "/browser-command-contracts.html")["tab"]["id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab))
    return tab


def command(ctx, tab, operation, **fields):
    return ctx.call("browser_command", tab_id=tab, operation=operation, arguments=fields)


def inspect(ctx, tab, locator):
    return command(ctx, tab, "inspect", locator=locator)


def role_locator(role, name, chained=False):
    item = {"role": role, "name": name, "exact": True}
    return {"chain": [item]} if chained else item


def selector_locator(selector, chained=False):
    return {"chain": [{"css": selector}]} if chained else {"selector": selector}


def metadata(value):
    # Geometry can move without changing semantics. Compare identity, content,
    # control state and option metadata, not elapsed time or viewport position.
    fields = ("role", "name", "text", "total_length", "has_more", "enabled", "visible", "checked",
              "multiple", "options", "option_count", "options_truncated")
    return {"id": value.get("attributes", {}).get("id"),
            **{key: value.get(key) for key in fields}}


def inspect_pair(ctx, tab, role, name, expected_id, text=None):
    flat = inspect(ctx, tab, role_locator(role, name))
    chained = inspect(ctx, tab, role_locator(role, name, True))
    ctx.check(name + " resolves the same element and metadata through both locator forms",
              metadata(flat) == metadata(chained), {"flat": metadata(flat), "chained": metadata(chained)})
    ctx.check(name + " has its expected identity and semantic role",
              flat.get("attributes", {}).get("id") == expected_id and flat.get("role") == role
              and flat.get("name") == name, flat)
    if text is not None:
        ctx.check(name + " exposes its actual rendered content", text in flat.get("text", ""), flat)
    return flat


def refused(ctx, tab, operation, count, **fields):
    error = None
    try:
        command(ctx, tab, operation, **fields)
    except ToolError as caught:
        error = str(caught)
    ctx.check(operation + " refuses with the expected locator match count",
              bool(error) and bool(re.search(r"(?:matched|resolved to)\s+" + str(count) + r"\s+elements", error)),
              {"arguments": fields, "error": error})


def snapshot_has(outline, role, name):
    # The compact snapshot may append visible content to a landmark's name.
    return bool(re.search(r"\b" + re.escape(role) + r' "' + re.escape(name) + r'(?:"| ·)', outline))


def accepts_type(schema, expected):
    kind = schema.get("type")
    return (kind == expected or isinstance(kind, list) and expected in kind
            or any(accepts_type(item, expected) for key in ("anyOf", "oneOf")
                   for item in schema.get(key, []) if isinstance(item, dict)))


@scenario
def browser_command_catalog_advertises_canonical_fields(ctx):
    catalog = ctx.mcp.rpc("tools/list")["tools"]
    tool = next(item for item in catalog if item["name"] == "browser_command")
    properties = tool["inputSchema"]["properties"]
    arguments = properties["arguments"]
    ctx.require("browser_command advertises typed nested operation arguments",
                arguments.get("type") == "object" and isinstance(arguments.get("properties"), dict), arguments)
    fields = arguments["properties"]
    locator = fields.get("locator", {})
    ctx.require("locator is a typed object with discoverable fields",
                locator.get("type") == "object" and isinstance(locator.get("properties"), dict), locator)
    for name, kind in (("selector", "string"), ("role", "string"), ("name", "string"), ("exact", "boolean")):
        ctx.check("locator." + name + " has its public type", accepts_type(locator["properties"].get(name, {}), kind), locator)
    value = fields.get("value", {})
    ctx.check("option value supports exact strings and arrays of strings",
              accepts_type(value, "string") and accepts_type(value, "array")
              and any(item.get("type") == "array" and item.get("items", {}).get("type") == "string"
                      for item in [value, *value.get("anyOf", []), *value.get("oneOf", [])]), value)
    ctx.check("fill/type text is advertised as a string", accepts_type(fields.get("text", {}), "string"), fields.get("text"))
    help_text = " ".join([tool.get("description", ""), arguments.get("description", ""),
                          locator.get("description", ""), value.get("description", "")])
    ctx.check("public metadata explains the canonical inspect and select_option fields",
              all(word in help_text for word in ("inspect", "select_option", "locator", "value")), help_text)
    role_help = locator["properties"].get("role", {}).get("description", "")
    ctx.check("role metadata distinguishes the ARIA region from an HTML section",
              "region" in role_help and "section" in role_help, role_help)


@scenario
def browser_command_named_regions_match_snapshot_and_dom(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        ax = ctx.call("browser_snapshot", tab_id=tab, full=True, structured=True)
        dom = command(ctx, tab, "dom_snapshot").get("elements", [])
        for role, name, element_id, text in (
            ("region", "Account summary", "named-region", "Account balance: 125 credits."),
            ("region", "Shipment details", "labelled-region", "Shipment status: Ready."),
            ("note", "Explicit note", "explicit-note", "Explicit note content."),
        ):
            inspect_pair(ctx, tab, role, name, element_id, text)
            # The compact outline may elide a wrapper whose label is repeated by
            # its child heading. The structured Chrome AX data retains its role.
            ctx.check(name + " retains the matching role in browser_snapshot",
                      any(item.get("role") == role and item.get("name") == name
                          for item in ax.get("elements", [])), ax)
            ctx.check(name + " retains the matching role in dom_snapshot",
                      any(item.get("role") == role and item.get("name") == name for item in dom), dom)


@scenario
def browser_command_unnamed_regions_and_ambiguity_refuse(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        for element_id in ("unnamed", "empty", "whitespace", "unresolved"):
            for chained in (False, True):
                item = inspect(ctx, tab, selector_locator("#" + element_id, chained))
                ctx.check(element_id + " does not become a region merely from a section or empty label",
                          item.get("attributes", {}).get("id") == element_id and item.get("role") != "region", item)
            refused(ctx, tab, "inspect", 0, locator={"selector": "#" + element_id, "role": "region"})
        for chained in (False, True):
            refused(ctx, tab, "inspect", 2, locator=role_locator("region", "Duplicate region", chained))
            refused(ctx, tab, "click", 2, locator=selector_locator(".duplicate-region button", chained))
        ctx.check("ambiguous actions dispatch no input to either possible target",
                  inspect(ctx, tab, {"selector": "#events"}).get("text") == "No input events")


@scenario
def browser_command_heading_and_header_roles_agree(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        for level, word in ((3, "three"), (4, "four"), (5, "five"), (6, "six")):
            name = "Depth " + word
            inspect_pair(ctx, tab, "heading", name, "heading" + str(level), name)
            ctx.check(name + " is a heading in the accessibility snapshot", snapshot_has(outline, "heading", name), outline)
        inspect_pair(ctx, tab, "banner", "Site header", "top-header", "Browser command contracts")
        ctx.check("top-level header is a banner in the snapshot", snapshot_has(outline, "banner", "Site header"), outline)
        for chained in (False, True):
            item = inspect(ctx, tab, selector_locator("#nested-header", chained))
            ctx.check("article header does not become a page banner",
                      item.get("attributes", {}).get("id") == "nested-header" and item.get("role") != "banner", item)
            refused(ctx, tab, "inspect", 0, locator=role_locator("banner", "Article header", chained))
        ctx.check("snapshot does not promote the nested header to a banner",
                  not snapshot_has(outline, "banner", "Article header"), outline)


@scenario
def browser_command_select_metadata_and_nested_arguments_agree(ctx):
    with FixtureServer() as base:
        tab = open_fixture(ctx, base)
        outline = ctx.call("browser_snapshot", tab_id=tab, full=True)["outline"]
        before = inspect_pair(ctx, tab, "listbox", "Channels", "multiple")
        ctx.require("multiselect exposes its three options and multiple capability",
                    before.get("multiple") is True and before.get("option_count") == 3, before)
        ctx.check("snapshot exposes the same multiselect listbox role", snapshot_has(outline, "listbox", "Channels"), outline)
        selected = command(ctx, tab, "select_option", locator=role_locator("listbox", "Channels"), value=["SMS", "Push"])
        after = inspect_pair(ctx, tab, "listbox", "Channels", "multiple")
        ctx.check("nested select_option resolves labels to exactly the requested persisted values",
                  selected.get("outcome", {}).get("verified") is True
                  and selected.get("outcome", {}).get("actual") == ["sms", "push"]
                  and [item.get("value") for item in after.get("options", []) if item.get("selected")] == ["sms", "push"],
                  {"result": selected, "inspect": after})
        inspect_pair(ctx, tab, "combobox", "Priority", "single")
        ctx.check("single select remains a combobox in the snapshot", snapshot_has(outline, "combobox", "Priority"), outline)
        selected = command(ctx, tab, "select_option", locator=role_locator("combobox", "Priority", True), value="Express")
        after = inspect_pair(ctx, tab, "combobox", "Priority", "single")
        ctx.check("chained selection uses the same canonical nested value argument",
                  selected.get("outcome", {}).get("verified") is True and after.get("text") == "express",
                  {"result": selected, "inspect": after})


if __name__ == "__main__":
    sys.exit(0 if Run(browser=True).run(sys.argv[1:]) else 1)
