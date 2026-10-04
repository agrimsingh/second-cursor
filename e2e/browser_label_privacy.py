#!/usr/bin/env python3
"""Live privacy regressions through nested public browser_command arguments.

Only disposable synthetic values are used. All calls after opening the fixture
are reads; no evaluation, raw CDP, or fixture mutation is injected. Full results
and errors are scanned, so leaking a marker through name/attributes/diagnostics
fails even when the primary value is redacted. Locators still use public names.

SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_label_privacy.py
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario

SCENARIOS.clear()
SECRET_PREFIX = "SC_LABEL_PRIVACY_SECRET_"
HERE = Path(__file__).resolve().parent
PUBLIC_INPUT = "SC_LABEL_PRIVACY_PUBLIC_INPUT"
PUBLIC_NOTE = "SC_LABEL_PRIVACY_PUBLIC_NOTE"
PUBLIC_OPTION = "SC_LABEL_PRIVACY_PUBLIC_STANDARD"


def fingerprints():
    extension = Path(os.environ.get("SC_E2E_EXTENSION") or HERE.parent / "extension").expanduser().resolve()
    paths = [Path(__file__).resolve(), HERE / "fixtures/browser-label-privacy.html",
             extension / "extended.js", extension / "snapshot.js"]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}


class Evidence:
    def __init__(self, ctx):
        self.ctx = ctx
        self.data = {"kind": "browser-label-privacy", "target": ctx.run.target,
                     "fingerprint_basis": "files on disk at scenario start; not an assertion of installed extension identity",
                     "sha256": fingerprints(), "calls": [], "checks": []}

    def check(self, label, passed, detail=None):
        self.data["checks"].append({"label": label, "passed": bool(passed), "detail": detail})
        return self.ctx.check(label, passed, detail)

    def private(self, label, result):
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        markers = sorted(set(re.findall(SECRET_PREFIX + r"[A-Z_]+", serialized)))
        self.check(label + " contains no synthetic sensitive marker in any returned field", not markers, markers)

    def command(self, tab, operation, label, **fields):
        arguments = {"tab_id": tab, "operation": operation, "arguments": fields}
        entry = {"label": label, "arguments": arguments}
        self.data["calls"].append(entry)
        try:
            result = self.ctx.call("browser_command", **arguments)
        except ToolError as error:
            entry["error"] = str(error)
            self.private(label + " error", str(error))
            self.check(label + " completes successfully", False, str(error))
            return None
        entry["result"] = result
        self.private(label, result)
        return result

    def ambiguous(self, tab, locator, path):
        arguments = {"tab_id": tab, "operation": "inspect", "arguments": {"locator": locator}}
        entry = {"label": path + " ambiguous option inspect", "arguments": arguments}
        self.data["calls"].append(entry)
        try:
            entry["result"] = self.ctx.call("browser_command", **arguments)
            self.private(entry["label"], entry["result"])
            self.check(path + " ambiguous option inspection refuses", False, entry["result"])
        except ToolError as error:
            entry["error"] = str(error)
            self.private(entry["label"] + " diagnostic", str(error))
            self.check(path + " ambiguity diagnostic retains the exact match count",
                       bool(re.search(r"(?:matched|resolved to)\s+2\s+elements", str(error))), str(error))

    def save(self):
        (self.ctx.run.dir / "browser-label-privacy.json").write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")


def locator(element_id, chained):
    selector = "#" + element_id
    return {"chain": [{"css": selector}]} if chained else {"selector": selector}


def role_locator(role, name, chained):
    step = {"role": role, "name": name, "exact": True}
    return {"chain": [step]} if chained else step


def inspect_variants(evidence, tab, element_id, *, name=None, role=None, ordinary_text=None, ordinary_inner_text=None):
    for chained in (False, True):
        path = "chained" if chained else "flat"
        grounded = role_locator(role, name, chained) if name is not None else locator(element_id, chained)
        for text_kind in (None, "inner", "content"):
            label = f"{element_id} {path} inspect {text_kind or 'default'}"
            result = evidence.command(tab, "inspect", label, locator=grounded,
                                      **({"text_kind": text_kind} if text_kind else {}))
            if result is None:
                continue
            evidence.check(label + " retains its exact target identity",
                           result.get("attributes", {}).get("id") == element_id, result.get("attributes", {}).get("id"))
            if name is not None:
                evidence.check(label + " retains its grounded public accessible name", result.get("name") == name,
                               {"name": result.get("name"), "role": result.get("role")})
            if ordinary_text is not None and (text_kind is None or element_id != "ordinary-input"):
                expected = ordinary_inner_text if text_kind == "inner" and ordinary_inner_text is not None else ordinary_text
                contract = " matches native textarea innerText" if text_kind == "inner" and ordinary_inner_text is not None else " keeps ordinary text readable"
                evidence.check(label + contract, result.get("text") == expected, result.get("text"))


def query_texts(evidence, tab, element_id, *, expected_count=1, expected_text=None, expected_inner_text=None):
    # query is supported by the public command route, although omitted from its
    # advertised operation enum; this checks runtime privacy, not schema parity.
    for texts in ("inner", "content"):
        label = f"{element_id} chained query {texts}"
        result = evidence.command(tab, "query", label, locator=locator(element_id, True), texts=texts)
        if result is None:
            continue
        evidence.check(label + " returns the observed target count and text entries",
                       result.get("count") == expected_count and isinstance(result.get("texts"), list)
                       and len(result["texts"]) == expected_count, result)
        if expected_text is not None:
            expected = expected_inner_text if texts == "inner" and expected_inner_text is not None else expected_text
            contract = " matches native textarea innerText" if texts == "inner" and expected_inner_text is not None else " keeps ordinary content readable"
            evidence.check(label + contract, result.get("texts") == [expected], result)


@scenario
def browser_label_sources_and_option_reads_preserve_privacy(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    evidence = Evidence(ctx)
    try:
        with FixtureServer() as base:
            tab = ctx.call("browser_open", url=base + "/browser-label-privacy.html")["tab"]["id"]
            ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab))
            tree = evidence.command(tab, "dom_snapshot", "whole DOM snapshot")
            if tree is not None:
                rows = tree.get("elements", [])
                for role, names in (("main", [""]), ("article", ["", "Saved payment choices"]),
                                    ("form", ["", "Delivery preferences"])):
                    actual = [row.get("name") for row in rows if row.get("role") == role]
                    evidence.check("DOM snapshot " + role + " names exclude descendant control text",
                                   len(actual) == len(names) and sorted(actual) == sorted(names), actual)
                evidence.check("DOM snapshot retains the ordinary text input value",
                               any(row.get("name") == "Display name" and row.get("value") == PUBLIC_INPUT for row in rows))
                mixed = [row for row in rows if row.get("name") == "Shipping route"]
                evidence.check("DOM snapshot retains the ordinary selected value beside a sensitive unselected option",
                               len(mixed) == 1 and mixed[0].get("value") == PUBLIC_OPTION, mixed)

            controls = (
                ("native-aria", "textbox", "Account nickname"),
                ("native-labelledby", "textbox", "Account code"),
                ("legacy-aria", "textbox", "Account alias"),
                ("cleared-input", "textbox", "Archived account code"),
                ("cleared-textarea", "textbox", "Archived recovery text"),
                ("sensitive-select", "listbox", "Card number"),
                ("native-select", "listbox", "Payment choice"),
                ("mixed-select", "listbox", "Shipping route"),
            )
            for element_id, role, name in controls:
                inspect_variants(evidence, tab, element_id, name=name, role=role)
                query_texts(evidence, tab, element_id)

            for element_id in ("direct-selected", "direct-unselected", "native-selected", "native-unselected", "mixed-sensitive"):
                inspect_variants(evidence, tab, element_id)
                query_texts(evidence, tab, element_id)

            for element_id, name, text in (("ordinary-input", "Display name", PUBLIC_INPUT),
                                           ("ordinary-textarea", "Delivery note", PUBLIC_NOTE)):
                # Chrome's native textarea innerText is empty. Its live value
                # (default inspection) and textContent (content) remain readable;
                # only the explicit innerText expectation differs.
                inspect_variants(evidence, tab, element_id, name=name, role="textbox", ordinary_text=text,
                                 ordinary_inner_text="" if element_id == "ordinary-textarea" else None)
            query_texts(evidence, tab, "ordinary-textarea", expected_text=PUBLIC_NOTE, expected_inner_text="")
            inspect_variants(evidence, tab, "mixed-ordinary", ordinary_text="Standard shipping")
            query_texts(evidence, tab, "mixed-ordinary", expected_text="Standard shipping")
            for chained in (False, True):
                path = "chained" if chained else "flat"
                detail = evidence.command(tab, "inspect", path + " mixed selected ordinary value",
                                          locator=role_locator("listbox", "Shipping route", chained))
                if detail is not None:
                    evidence.check(path + " mixed select preserves its ordinary selected value",
                                   detail.get("text") == PUBLIC_OPTION and any(
                                       option.get("value") == PUBLIC_OPTION and option.get("label") == "Standard shipping"
                                       and option.get("selected") is True for option in detail.get("options", [])), detail)
                ambiguous = ({"chain": [{"css": "#sensitive-select"}, {"role": "option"}]}
                             if chained else {"selector": "#sensitive-select option"})
                evidence.ambiguous(tab, ambiguous, path)
    finally:
        evidence.save()


if __name__ == "__main__":
    sys.exit(0 if Run(browser=True).run(set(sys.argv[1:])) else 1)
