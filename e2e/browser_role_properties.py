#!/usr/bin/env python3
"""Bounded seeded browser properties, using Chrome's AX snapshot as the oracle.

This is a deterministic generator, not a property-testing framework with shrinking.
Two seeds generate 20 HTML variants each through normal fixture UI handlers. The
semantic oracle is browser_snapshot(structured=True).elements, which comes from
Chrome Accessibility.getFullAXTree, never dom_snapshot or the shared role helper.
Flat/chained inspection must return that AX role/name and the generated target ID.
Competing labels and whitespace are intentional coverage, not excluded exceptions.

The stateful property retains an observation/ref, then replaces, removes, or
relabels the node through page controls. Stale input must have no page-side effect;
current input must produce exactly one trusted receipt on the current target.
Failures retain seed, complete generated manifest, AX oracle, calls and sequence in
browser-role-properties.json beside the usual public-tool transcript.

SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_role_properties.py
SC_ROLE_PROPERTY_SEEDS=104729,130363 overrides the bounded two-seed default.
No injected evaluation, direct CDP, or fixture HTTP mutation is used.
"""
import json
import os
import random
import re
import sys

from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario

SCENARIOS.clear()
DEFAULT_SEEDS = (104729, 130363)
TARGET_ROLES = {"region", "heading", "button", "link", "textbox", "searchbox", "checkbox", "radio", "switch", "listbox", "combobox"}
CASE_NAME = re.compile(r"^Property case (\d+-\d+)\b")


def configured_seeds():
    values = os.environ.get("SC_ROLE_PROPERTY_SEEDS", "")
    seeds = tuple(int(value.strip()) for value in values.split(",") if value.strip()) if values else DEFAULT_SEEDS
    if not 1 <= len(seeds) <= 2 or any(not 0 <= seed <= 0xFFFFFFFF for seed in seeds):
        raise ValueError("SC_ROLE_PROPERTY_SEEDS requires one or two unsigned 32-bit integers")
    return seeds


class Evidence:
    def __init__(self, ctx):
        self.ctx = ctx
        self.data = {"kind": "bounded-seeded-properties", "shrinking": False, "oracle": "Chrome AX via browser_snapshot structured elements", "seeds": []}
        self.current = None

    def begin(self, seed):
        self.current = {"seed": seed, "calls": [], "checks": [], "sequence": []}
        self.data["seeds"].append(self.current)

    def call(self, tool, **arguments):
        entry = {"tool": tool, "arguments": arguments}
        self.current["calls"].append(entry)
        try:
            entry["result"] = self.ctx.call(tool, **arguments)
            return entry["result"]
        except Exception as error:
            entry["error"] = str(error)
            raise

    def check(self, label, passed, detail=None):
        self.current["checks"].append({"label": label, "passed": bool(passed), "detail": detail})
        return self.ctx.check(f"seed {self.current['seed']}: {label}", passed, detail)

    def save(self):
        path = self.ctx.run.dir / "browser-role-properties.json"
        path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")


def command(evidence, tab, operation, **fields):
    return evidence.call("browser_command", tab_id=tab, operation=operation, arguments=fields)


def inspect(evidence, tab, locator, **fields):
    return command(evidence, tab, "inspect", locator=locator, **fields)


def snapshot(evidence, tab):
    return evidence.call("browser_snapshot", tab_id=tab, full=True, structured=True)


def control(evidence, tab, selector):
    return command(evidence, tab, "click", locator={"selector": selector})


def structured_targets(elements):
    result = {}
    for element in elements:
        match = CASE_NAME.match(element.get("name", ""))
        if match and element.get("role") in TARGET_ROLES:
            result.setdefault(match.group(1), []).append(element)
    return result


def compare_semantics(evidence, tab, manifest, oracle):
    elements = oracle.get("elements", [])
    targets = structured_targets(elements)
    evidence.current["manifest"] = manifest
    evidence.current["oracle"] = oracle
    expected = {case["case_id"] for case in manifest["cases"] if case["named"]}
    evidence.check("AX snapshot exposes every generated named target", set(targets) == expected,
                   {"missing": sorted(expected - set(targets)), "unexpected": sorted(set(targets) - expected)})
    for case in manifest["cases"]:
        if not case["named"]:
            continue
        candidates = targets.get(case["case_id"], [])
        if not evidence.check(case["case_id"] + " has one uniquely named AX target", len(candidates) == 1, candidates):
            continue
        ax = candidates[0]
        for chained in (False, True):
            path = "chained" if chained else "flat"
            step = {"role": ax["role"], "name": ax["name"], "exact": True}
            locator = {"chain": [step]} if chained else step
            try:
                result = inspect(evidence, tab, locator)
                actual = {"id": result.get("attributes", {}).get("id"), "role": result.get("role"), "name": result.get("name")}
                wanted = {"id": case["target_id"], "role": ax["role"], "name": ax["name"]}
                evidence.check(f"{case['case_id']} {case['kind']} {path} matches Chrome AX identity/role/name", actual == wanted,
                               {"case": case, "oracle": ax, "actual": actual})
            except ToolError as error:
                evidence.check(f"{case['case_id']} {case['kind']} {path} matches Chrome AX identity/role/name", False,
                               {"case": case, "oracle": ax, "error": str(error)})
    # Unnamed sections have no unique AX name to address. Compare cardinality
    # independently through Chrome's region nodes and the public strict inspector.
    count = sum(element.get("role") == "region" for element in elements)
    for chained in (False, True):
        locator = {"chain": [{"role": "region"}]} if chained else {"role": "region"}
        observed_count = None
        try:
            inspect(evidence, tab, locator)
            observed_count = 1
        except ToolError as error:
            match = re.search(r"(?:matched|resolved to)\s+(\d+)\s+elements", str(error))
            if match:
                observed_count = int(match.group(1))
        evidence.check(("chained" if chained else "flat") + " region cardinality matches Chrome, including unnamed sections",
                       observed_count == count, {"chrome_ax_count": count, "locator_count": observed_count})


def receipts(evidence, tab):
    return json.loads(inspect(evidence, tab, {"selector": "#mutation-receipts"}, length=20000)["text"])


def mutable_target(oracle):
    candidates = [element for element in oracle.get("elements", []) if element.get("role") == "button"
                  and element.get("name", "").startswith("Mutable action ")]
    if len(candidates) != 1 or not candidates[0].get("ref"):
        raise AssertionError(f"Expected one mutable AX button with a ref, got {candidates}")
    return candidates[0]


def check_lifetimes(evidence, tab, seed):
    sequence = ["replace", "remove", "relabel"]
    random.Random(seed ^ 0x524546).shuffle(sequence)
    evidence.current["sequence"] = sequence
    for mutation in sequence:
        control(evidence, tab, "#restore-target")
        before = snapshot(evidence, tab)
        target = mutable_target(before)
        control(evidence, tab, "#" + mutation + "-target")
        unchanged = receipts(evidence, tab)
        error = None
        try:
            evidence.call("browser_click", tab_id=tab, ref=target["ref"], observation_id=before["snapshot_id"], observe="none")
        except ToolError as caught:
            error = str(caught)
        evidence.check(mutation + " refuses the old observation/ref for staleness",
                       bool(error and "stale browser observation" in error.lower()), {"old_target": target, "error": error})
        evidence.check(mutation + " stale input causes no page-side effect", receipts(evidence, tab) == unchanged)
        if mutation in {"replace", "remove"}:
            error = None
            try:
                evidence.call("browser_click", tab_id=tab, ref=target["ref"], observe="none")
            except ToolError as caught:
                error = str(caught)
            detached = (r"Ref\s+" + re.escape(target["ref"]) + r"\s+is (?:no longer|not visible) on the page|"
                        r"Node is detached from (?:the )?document")
            evidence.check(mutation + " cannot rebind a detached ref even without a token",
                           bool(error and re.search(detached, error, re.I)), error)
            evidence.check(mutation + " detached-ref input causes no page-side effect", receipts(evidence, tab) == unchanged)
        if mutation == "remove":
            control(evidence, tab, "#restore-target")
        ready = receipts(evidence, tab)
        current = snapshot(evidence, tab)
        fresh = mutable_target(current)
        evidence.call("browser_click", tab_id=tab, ref=fresh["ref"], observation_id=current["snapshot_id"], observe="none")
        after = receipts(evidence, tab)
        evidence.check(mutation + " fresh ref delivers exactly one trusted click to the current target",
                       len(after["receipts"]) == len(ready["receipts"]) + 1
                       and after["receipts"][:-1] == ready["receipts"]
                       and after["receipts"][-1]["trusted"] is True
                       and after["receipts"][-1]["name"] == fresh["name"]
                       and after["receipts"][-1]["generation"] == ready["target_generation"]
                       and {key: value for key, value in after.items() if key != "receipts"}
                           == {key: value for key, value in ready.items() if key != "receipts"},
                       {"fresh_target": fresh, "receipts": after})


@scenario
def browser_seeded_role_and_ref_properties(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    evidence = Evidence(ctx)
    with FixtureServer() as base:
        tab = ctx.call("browser_open", url=base + "/browser-role-properties.html")["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab))
        try:
            for seed in configured_seeds():
                evidence.begin(seed)
                try:
                    command(evidence, tab, "fill", locator={"selector": "#seed"}, text=str(seed))
                    control(evidence, tab, "#generate-form button")
                    manifest = json.loads(inspect(evidence, tab, {"selector": "#generation-manifest"}, length=20000)["text"])
                    if not evidence.check("page handler generated the requested bounded seed", manifest.get("seed") == seed and len(manifest.get("cases", [])) == 20, manifest):
                        continue
                    compare_semantics(evidence, tab, manifest, snapshot(evidence, tab))
                    check_lifetimes(evidence, tab, seed)
                except (ToolError, AssertionError, ValueError, KeyError) as error:
                    evidence.check("seed completes without a harness or tool exception", False, str(error))
                finally:
                    evidence.save()
        finally:
            evidence.save()


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
