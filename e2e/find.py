#!/usr/bin/env python3
"""find_elements, act and the compact tool surface, driven over MCP like a client.

Ways the feature could fail, written down before the code:
 1. find_elements reads the app again instead of searching the stored observation: it returns UI that changed since
    the last read, or a new observation_id.
 2. It only searches the rendered outline, so elements a truncated outline (max_elements) left out are not found.
 3. An index it returns does not act: the element was never made actionable, it maps to a different element than the
    outline's index, or it stops working after the next read of the window.
 4. Ranking is wrong: walk order instead of exact, then prefix, then substring; or matching is case-sensitive.
 5. The role filter needs the exact "AXButton" spelling, or lets other roles through.
 6. limit is ignored or unclamped, total counts only the returned lines, or there is no hint to refine.
 7. Lines are not outline lines: no [index], no frame, or text that differs from get_window_state's line.
 8. A window the session has not observed fails, or is read on every search; or that read does not become the
    session's observation (the next get_window_state is not a diff against it).
 9. find_elements by app searches another window than the one the session observed.
10. find_elements skips the gates reads go through (app approval), or refuses a call without text or role unclearly.
11. act diverges from batch: different step results, observation, stop_on_error behavior, refusal text, accepted step
    shapes (flat, args object, args as a JSON string), or action log lines.
12. The compact surface advertises the wrong tools, hides act or find_elements, makes hidden tools uncallable, or gives
    act a schema that drifts from batch's; /api and /mcp list different tools.
13. The full surface advertises act, or omits find_elements.
14. tools/list grows back past its budget, or still lists what only hosts use: session_id, operation_id,
    observation_id or timeout_ms on a tool (wait_for's and browser_repl's timeout_ms are the model's), or the session
    and operation tools.
15. A host parameter left out of tools/list stops working: an unlisted session cannot be created or own a tab, an
    operation_id retry clicks again, a superseded observation_id or an expired timeout_ms lets the click through.

Usage:  SC_E2E_APP=build/SecondCursor.app python3 e2e/find.py [scenario ...]
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, screenshots/)
"""
import json
import re
import sys
import time
import urllib.request

import run as harness
from run import (Run, SCENARIOS, TestInstance, ToolError, clear_calculator, element_index, refused, scenario,
                 value_of)

SCENARIOS.clear()

LINE = re.compile(r'^\[(\d+)\] (AX\w+) "((?:[^"\\]|\\.)*)"')
FRAME = re.compile(r"\((-?\d+),(-?\d+) (\d+)x(\d+)\)")
HIDDEN_IN_COMPACT = {"click", "type_text", "press_key", "set_value", "scroll", "drag", "perform_action", "batch"}
HOST_TOOLS = {"session", "bind_target", "operation_status", "cancel_operation"}
HOST_PARAMETERS = {"session_id", "operation_id", "observation_id", "timeout_ms"}
# The default surface measured 63.8k characters before host parameters were hidden and expect, observe and caption
# were described in full only on click and batch (browser_click for the browser).
LISTING_BUDGET = 42000


def parse(line):
    """(index, role, label) of an indexed outline line, else None."""
    match = LINE.match(line)
    return (int(match.group(1)), match.group(2), json.loads(f'"{match.group(3)}"')) if match else None


def labels(found):
    return [p[2] for p in map(parse, found["matches"]) if p]


def html_window(ctx, name, body):
    path = ctx.scratch / name
    path.write_text(f"<html><body>{body}</body></html>")
    launched = ctx.call("launch_app", app="TextEdit", urls=[str(path)], new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(launched["app"]["pid"])
    return ctx.wait_for_window("TextEdit", pid=launched["app"]["pid"], title=name)["id"]


def text_window(ctx, name, text):
    return ctx.launch_scratch_textedit(name, text, on_agent_display=True)["id"]


def new_session(ctx):
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    return sid


def calculator(ctx):
    launched = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(launched["app"]["pid"])
    # Calculator can replace its first window right after launch.
    for attempt in range(3):
        wid = ctx.wait_for_window("Calculator", pid=launched["app"]["pid"])["id"]
        try:
            return wid, clear_calculator(ctx, wid)
        except ToolError:
            if attempt == 2:
                raise
            time.sleep(1)


@scenario
def find_calculator_buttons(ctx):
    wid, tree = calculator(ctx)
    seven = element_index(tree, "AXButton", "7")
    ctx.require("Calculator's 7 is in the outline", seven is not None, tree[:400])

    exact = ctx.call("find_elements", window_id=wid, text="7")
    first = parse(exact["matches"][0]) if exact["matches"] else None
    ctx.check('text "7" ranks the button labelled exactly 7 first', first and first[1:] == ("AXButton", "7"), exact["matches"][:3])
    ctx.check("with the same index the outline shows", first and first[0] == seven, (first, seven))
    ctx.check("its line carries a window-local frame like the outline's", FRAME.search(exact["matches"][0] if exact["matches"] else ""),
              exact["matches"][:1])
    line_in_tree = next((l.strip() for l in tree.splitlines() if l.strip().startswith(f"[{seven}] ")), None)
    ctx.check("the line is the outline's line for that element", exact["matches"] and exact["matches"][0] == line_in_tree,
              (exact["matches"][:1], line_in_tree))
    ctx.check("the result names the window and the observation it searched",
              exact["window_id"] == wid and isinstance(exact.get("observation_id"), str), exact)

    prefix = ctx.call("find_elements", window_id=wid, text="mult")
    ctx.check('a lowercase prefix ("mult") finds "Multiply" first', labels(prefix)[:1] == ["Multiply"], prefix["matches"][:3])

    clicked = ctx.call("click", window_id=wid, element_index=first[0] if first else -1)
    ctx.check("the found index works with click", clicked.get("route"), clicked)
    display = ctx.call("find_elements", window_id=wid, text="Edit field")
    ctx.check("a search after the click sees its observation: the display shows 7",
              any('value="7"' in line for line in display["matches"]), display["matches"][:3])

    buttons = ctx.call("find_elements", window_id=wid, role="button", limit=100)
    ctx.check('role "button" returns only buttons', buttons["matches"] and all(" AXButton " in f" {l}" for l in buttons["matches"]),
              buttons["matches"][:5])
    in_outline = len(re.findall(r"\] AXButton ", ctx.tree(wid)))
    ctx.check("and every button of the window", buttons["total"] == in_outline, (buttons["total"], in_outline))
    spellings = {ctx.call("find_elements", window_id=wid, role=r)["total"] for r in ("AXButton", "axbutton", "Button")}
    ctx.check("the role matches with or without AX, in any case", spellings == {buttons["total"]}, spellings)
    six = ctx.call("find_elements", window_id=wid, role="AXButton", text="6")
    ctx.check("role and text combine", labels(six)[:1] == ["6"] and all(" AXButton " in f" {l}" for l in six["matches"]),
              six["matches"][:3])
    none = ctx.call("find_elements", window_id=wid, role="AXButton", text="no such label")
    ctx.check("no match returns total 0 and a hint to read again", none["total"] == 0 and not none["matches"]
              and "get_window_state" in none.get("hint", ""), none)

    error = refused(ctx, "find_elements", window_id=wid)
    ctx.check("a search without text or role is refused by name", error and "text" in error and "role" in error, error)
    error = refused(ctx, "find_elements", window_id=wid, text="   ")
    ctx.check("blank text counts as no text", error and "text" in error, error)


@scenario
def find_ranking_and_limit(ctx):
    links = "".join(f'<p><a href="https://example.com/{label}">{label}</a></p>'
                    for label in ["Autosave", "Save As", "SAVE", "save"])
    entries = "".join(f'<p><a href="https://example.com/e{i}">entry {i}</a></p>' for i in range(30))
    wid = html_window(ctx, "rank.html", f"<p>Ranking fixture</p>{links}{entries}")
    ctx.tree(wid)

    found = ctx.call("find_elements", window_id=wid, text="save")
    order = labels(found)
    ctx.check("exact matches, in any case, come first (the document lists them last)", order[:2] == ["SAVE", "save"], order)
    ctx.check("then the prefix match", order[2:3] == ["Save As"], order)
    ctx.check("then substrings", "Autosave" in order and order.index("Autosave") > 2, order)

    many = ctx.call("find_elements", window_id=wid, text="entry", role="link")
    ctx.check("limit defaults to 20", len(many["matches"]) == 20, len(many["matches"]))
    ctx.check("total counts every match", many["total"] == 30, many["total"])
    ctx.check("a truncated result asks to refine the query", "refine" in many.get("hint", ""), many.get("hint"))
    five = ctx.call("find_elements", window_id=wid, text="entry", role="link", limit=5)
    ctx.check("limit=5 returns 5 with the same total", len(five["matches"]) == 5 and five["total"] == 30,
              (len(five["matches"]), five["total"]))
    big = ctx.call("find_elements", window_id=wid, text="entry", role="link", limit=1000)
    ctx.check("limit is clamped to 100 and a complete result has no hint",
              len(big["matches"]) == 30 and "hint" not in big, (len(big["matches"]), big.get("hint")))
    one = ctx.call("find_elements", window_id=wid, text="entry", role="link", limit=0)
    ctx.check("limit is clamped to at least 1", len(one["matches"]) == 1, len(one["matches"]))
    ordered = [int(l.split()[1]) for l in labels(big)]
    ctx.check("equal matches keep outline order", ordered == sorted(ordered), ordered)


@scenario
def find_beyond_max_elements(ctx):
    body = "".join(f'<p>Paragraph {i} <a href="https://example.com/{i}">link {i}</a> trailing text</p>' for i in range(200))
    wid = html_window(ctx, "beyond.html", body)
    cut = ctx.call("get_window_state", window_id=wid, full=True, max_elements=60)
    ctx.require("the outline is truncated at 60 elements", cut["truncated"] and cut["element_count"] == 60,
                {k: cut[k] for k in ("truncated", "element_count")})
    ctx.require('"link 150" and "bold" are not in it', '"link 150"' not in cut["tree"] and '"bold"' not in cut["tree"])

    link = ctx.call("find_elements", window_id=wid, text="link 150")
    hit = parse(link["matches"][0]) if link["matches"] else None
    ctx.check("a link the outline left out is found", hit and hit[1:] == ("AXLink", "link 150"), link["matches"][:3])
    ctx.check("with an index the outline never showed", hit and f"[{hit[0]}]" not in cut["tree"], hit)
    ctx.check("and its frame", FRAME.search(link["matches"][0] if link["matches"] else ""), link["matches"][:1])
    ctx.check("it searched the truncated read", link["observation_id"] == cut["snapshot_id"], (link["observation_id"], cut["snapshot_id"]))

    bold = ctx.call("find_elements", window_id=wid, text="bold", role="checkbox")
    box = parse(bold["matches"][0]) if bold["matches"] else None
    ctx.require('the "bold" checkbox past the cut is found', box and box[1:] == ("AXCheckBox", "bold"), bold["matches"][:3])
    ctx.check("reported unchecked", 'value="0"' in bold["matches"][0], bold["matches"][:1])
    clicked = ctx.call("click", window_id=wid, element_index=box[0])
    ctx.check("clicking the found index works", clicked.get("route") == "ax_press", clicked)
    after = ctx.call("find_elements", window_id=wid, text="bold", role="checkbox")
    ctx.check("the click's observation is what the next search sees: checked", 'value="1"' in after["matches"][0], after["matches"][:1])
    ctx.check("and it searched that observation", after["observation_id"] == (clicked.get("observation") or {}).get("snapshot_id"),
              (after["observation_id"], (clicked.get("observation") or {}).get("snapshot_id")))
    ctx.call("get_window_state", window_id=wid)
    again = ctx.call("click", window_id=wid, element_index=box[0])
    ctx.check("the found index still works after later reads of the window", again.get("route") == "ax_press", again)
    back = ctx.call("find_elements", window_id=wid, text="bold", role="checkbox")
    ctx.check("and toggled it back", 'value="0"' in back["matches"][0], back["matches"][:1])

    whole = ctx.call("get_window_state", window_id=wid, full=True, max_elements=3000)
    size = len(json.dumps(link))
    ctx.check(f"a search result is {size} chars; the whole outline is {len(whole['tree'])}", size * 5 < len(whole["tree"]),
              (size, len(whole["tree"])))


@scenario
def find_does_not_reread(ctx):
    wid = text_window(ctx, "stale.txt", "before edit")
    first = ctx.call("get_window_state", window_id=wid)
    found = ctx.call("find_elements", window_id=wid, text="before edit")
    ctx.require("the text area is found by its value", labels(found) and parse(found["matches"][0])[1] == "AXTextArea",
                found["matches"][:2])
    ctx.check("it searched the latest read", found["observation_id"] == first["snapshot_id"], (found["observation_id"], first["snapshot_id"]))
    ctx.check("without reading again", not found.get("read_window"), found)

    other = new_session(ctx)
    area = element_index(ctx.call("get_window_state", window_id=wid, session_id=other)["tree"], "AXTextArea")
    ctx.call("set_value", window_id=wid, session_id=other, element_index=area, value="after edit", observe="none")
    ctx.call("session", action="close", session_id=other)

    stale = ctx.call("find_elements", window_id=wid, text="before edit")
    ctx.check("after another session changed the text, the search still sees the old text", stale["total"] >= 1, stale)
    ctx.check("with the same observation_id", stale["observation_id"] == first["snapshot_id"], stale["observation_id"])
    missing = ctx.call("find_elements", window_id=wid, text="after edit")
    ctx.check("the new text is not found yet, and the hint says to read again",
              missing["total"] == 0 and "get_window_state" in missing.get("hint", ""), missing)

    fresh = ctx.call("get_window_state", window_id=wid)
    ctx.check("get_window_state shows the change", 'value="after edit"' in fresh["tree"], fresh["tree"][:300])
    now = ctx.call("find_elements", window_id=wid, text="after edit")
    ctx.check("after get_window_state the search sees the new text", now["total"] >= 1, now)
    ctx.check("in the new observation", now["observation_id"] == fresh["snapshot_id"], (now["observation_id"], fresh["snapshot_id"]))

    index = parse(now["matches"][0])[0]
    acted = ctx.call("act", window_id=wid, observation_id=now["observation_id"],
                     steps=[{"tool": "set_value", "element_index": index, "value": "acted"}])
    ctx.check("its observation_id is accepted by an action", acted["steps"][0]["ok"], acted["steps"])
    error = refused(ctx, "act", window_id=wid, observation_id=first["snapshot_id"],
                    steps=[{"tool": "set_value", "element_index": index, "value": "x"}])
    ctx.check("and a superseded one is refused", error and "stale" in error.lower(), error)


@scenario
def find_reads_unobserved_window_once(ctx):
    wid = text_window(ctx, "unseen.txt", "never read")
    sid = new_session(ctx)
    first = ctx.call("find_elements", window_id=wid, session_id=sid, text="never read")
    ctx.check("a window the session never observed is read, and found", first.get("read_window") and first["total"] >= 1, first)
    second = ctx.call("find_elements", window_id=wid, session_id=sid, text="never")
    ctx.check("the next search does not read it again", not second.get("read_window")
              and second["observation_id"] == first["observation_id"], second)
    diff = ctx.call("get_window_state", window_id=wid, session_id=sid)
    ctx.check("that read became the session's observation: the next read is a diff against it",
              diff["is_diff"] and diff.get("diff_base") == first["observation_id"], {k: diff.get(k) for k in ("is_diff", "diff_base")})
    by_app = ctx.call("find_elements", app="TextEdit", session_id=sid, text="never read")
    ctx.check("by app, the search uses the window the session observed", by_app["window_id"] == wid, by_app["window_id"])


def comparable(result):
    return {"keys": sorted(result), "steps": result["steps"], "completed": result["completed"],
            "stopped_early": result["stopped_early"], "is_diff": (result.get("observation") or {}).get("is_diff"),
            "value": value_of((result.get("observation") or {}).get("tree", ""))}


def log_lines(wid, start):
    path = harness.ACTION_LOG
    entries = [json.loads(line) for line in path.read_text().splitlines()[start:]] if path.exists() else []
    return [e for e in entries if e.get("target") == f"window_id={wid}"]


@scenario
def act_matches_batch(ctx):
    windows = {tool: text_window(ctx, f"via-{tool}.txt", "") for tool in ("batch", "act")}
    areas = {tool: element_index(ctx.tree(wid), "AXTextArea") for tool, wid in windows.items()}
    ctx.require("both text areas are indexed", None not in areas.values(), areas)
    ctx.require("at the same index, so identical steps address the same element", len(set(areas.values())) == 1, areas)
    area = areas["batch"]

    def both(**args):
        return {tool: ctx.call(tool, window_id=windows[tool], **args) for tool in windows}

    one = both(steps=[{"tool": "set_value", "element_index": area, "value": "one"}])
    ctx.check("one step: act returns what batch returns", comparable(one["act"]) == comparable(one["batch"]),
              [comparable(r) for r in one.values()])
    ctx.check("and the step ran", comparable(one["act"])["value"] == "one", comparable(one["act"]))

    start = len(harness.ACTION_LOG.read_text().splitlines()) if harness.ACTION_LOG.exists() else 0
    shapes = both(caption="Typing a test", steps=[
        {"tool": "set_value", "element_index": area, "value": ""},
        {"tool": "type_text", "element_index": area, "text": "ab"},
        {"tool": "press_key", "args": {"keys": "left"}},
        {"tool": "wait", "ms": 100},
        {"tool": "type_text", "args": json.dumps({"text": "X"})},
        {"tool": "type_text", "text": "Y"},
    ])
    ctx.check("several steps, flat and nested shapes: act returns what batch returns",
              comparable(shapes["act"]) == comparable(shapes["batch"]), [comparable(r) for r in shapes.values()])
    ctx.check("in order: aXYb", comparable(shapes["act"])["value"] == "aXYb", comparable(shapes["act"]))
    logged = {tool: len(log_lines(wid, start)) for tool, wid in windows.items()}
    ctx.check("act logs like batch: the call and each step", logged["act"] == logged["batch"] >= 7, logged)

    for stop, before, after in ((True, "1", "2"), (False, "3", "4")):
        failed = both(stop_on_error=stop, steps=[
            {"tool": "type_text", "text": before}, {"tool": "click", "element_index": 99999},
            {"tool": "type_text", "text": after}])
        ctx.check(f"stop_on_error={stop}: act reports the failure like batch",
                  comparable(failed["act"]) == comparable(failed["batch"]), [comparable(r) for r in failed.values()])
    ctx.check("stop_on_error skipped 2 and stop_on_error=false ran 4", comparable(failed["act"])["value"] == "aXY134b",
              comparable(failed["act"]))

    blind = both(observe="none", steps=[{"tool": "wait", "ms": 10}])
    ctx.check('observe="none": no observation from either', blind["act"].get("observation") is None
              and blind["batch"].get("observation") is None, blind)
    full = both(full=True, steps=[{"tool": "wait", "ms": 10}])
    ctx.check("full=true: both return the whole outline", full["act"]["observation"]["is_diff"] is False
              and full["batch"]["observation"]["is_diff"] is False, [r["observation"]["is_diff"] for r in full.values()])

    for label, steps in [("an unparseable args string", [{"tool": "click", "args": "[object Object]"}]),
                         ("no steps", []), ("an unknown step tool", [{"tool": "launch_app"}])]:
        errors = {tool: refused(ctx, tool, window_id=windows[tool], steps=steps) for tool in windows}
        ctx.check(f"{label}: act is refused with batch's message", errors["act"] and errors["act"] == errors["batch"], errors)


def api_tool_names(inst):
    request = urllib.request.Request(f"{inst.base_url}/api", data=json.dumps({"tool": "tools/list"}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {inst.token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return {t["name"] for t in json.loads(response.read())}


@scenario
def full_surface(ctx):
    names = {t["name"] for t in ctx.mcp.rpc("tools/list")["tools"]}
    ctx.check("the default surface advertises find_elements", "find_elements" in names, sorted(names))
    ctx.check("and not act", "act" not in names, sorted(names))
    ctx.check("and still every single-action tool and batch", HIDDEN_IN_COMPACT <= names, sorted(HIDDEN_IN_COMPACT - names))
    ctx.check("/api lists the same tools", api_tool_names(ctx.run.instance) == names if ctx.run.instance else True)
    find = next(t for t in ctx.mcp.rpc("tools/list")["tools"] if t["name"] == "find_elements")
    ctx.check("find_elements is read-only and says it does not re-read the app",
              find.get("annotations", {}).get("readOnlyHint") is True and "does not read the app again" in find["description"],
              find.get("annotations"))


@scenario
def host_parameters(ctx):
    """Failure modes 14 and 15. The browser carries the parameter checks, so they run on any Space."""
    tools = ctx.mcp.rpc("tools/list")["tools"]
    size = len(json.dumps(tools))
    ctx.check(f"tools/list is at most {LISTING_BUDGET} characters", size <= LISTING_BUDGET, size)
    names = {t["name"] for t in tools}
    ctx.check("the session and operation tools are not listed", not (HOST_TOOLS & names), sorted(HOST_TOOLS & names))
    leaked = sorted(f'{t["name"]}.{p}' for t in tools for p in t["inputSchema"].get("properties", {})
                    if p in HOST_PARAMETERS and not (p == "timeout_ms" and t["name"] in ("wait_for", "browser_repl")))
    ctx.check("no listed tool takes session_id, operation_id, observation_id or timeout_ms", not leaked, leaked)
    wait_for = next(t for t in tools if t["name"] == "wait_for")["inputSchema"]["properties"]
    ctx.check("wait_for keeps its timeout_ms", "timeout_ms" in wait_for, sorted(wait_for))

    if not ctx.call("browser_status").get("connected"):
        raise harness.Skip("Chrome extension not connected")
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    with harness.FixtureServer() as base:
        tab = ctx.call("browser_open", url=f"{base}/covered.html", session_id=sid)["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab, session_id=sid))
        first = ctx.call("browser_snapshot", tab_id=tab, session_id=sid, full=True)
        target = re.search(r'\[(e\d+)\] button "Label: Spam"', first["outline"])[1]
        clicks = lambda: re.search(r"clicks=\d+", ctx.call("browser_snapshot", tab_id=tab, session_id=sid, full=True)["outline"])[0]
        tabs = ctx.call("browser_tabs", session_id=sid).get("tabs", [])
        ctx.check("an unlisted session is created and owns the tab it opened", any(t["id"] == tab for t in tabs), tabs)

        for _ in range(2):
            ctx.call("browser_click", tab_id=tab, ref=target, session_id=sid, operation_id=f"host-{sid}")
        ctx.check("an operation_id retry returns the first outcome without clicking again", clicks() == "clicks=1", clicks())
        stale = refused(ctx, "browser_click", tab_id=tab, ref=target, session_id=sid, observation_id=first["snapshot_id"])
        ctx.check("a superseded observation_id is refused", stale is not None and "Stale" in stale, stale)
        expired = refused(ctx, "browser_click", tab_id=tab, ref=target, session_id=sid, timeout_ms=1)
        ctx.check("a 1 ms timeout_ms expires before the click is sent", expired is not None, expired)
        ctx.check("neither reached the page", clicks() == "clicks=1", clicks())


@scenario
def compact_surface(ctx):
    if ctx.run.instance is None:
        raise harness.Skip("needs disposable test instances")
    main_tools = {t["name"]: t for t in ctx.mcp.rpc("tools/list")["tools"]}
    calc, _ = calculator(ctx)
    inst = TestInstance({"toolSurface": "compact", "appApproval": "prompt", "approvals": "deny",
                         "approvedBundleIDs": ["com.apple.TextEdit"]}, app=ctx.run.instance.app)
    ctx.cleanups.append(inst.stop)
    inst.mcp.transcript = ctx.run.transcript
    call = lambda tool, **args: inst.mcp.call(tool, **args)[0]

    tools = {t["name"]: t for t in inst.mcp.rpc("tools/list")["tools"]}
    names = set(tools)
    ctx.check("compact advertises act and find_elements", {"act", "find_elements"} <= names, sorted(names))
    ctx.check("and hides the single-action tools and batch", not (HIDDEN_IN_COMPACT & names), sorted(HIDDEN_IN_COMPACT & names))
    kept = {"status", "list_windows", "launch_app", "get_window_state", "screenshot", "select_text", "paste",
            "inspect_element"}
    ctx.check("everything else stays", kept <= names, sorted(kept - names))
    ctx.check("/api lists the same tools as /mcp", api_tool_names(inst) == names, sorted(api_tool_names(inst) ^ names))
    ctx.check("act's schema is batch's", tools.get("act", {}).get("inputSchema") == main_tools["batch"]["inputSchema"])
    ctx.check("act's description presents it as the way to act", "the way to act" in tools.get("act", {}).get("description", ""))

    path = ctx.scratch / "compact.txt"
    path.write_text("compact")
    launched = call("launch_app", app="TextEdit", urls=[str(path)], new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(launched["app"]["pid"])
    deadline, wid = time.time() + 15, None
    while wid is None and time.time() < deadline:
        wid = next((w["id"] for w in call("list_windows", app="TextEdit")["windows"]
                    if w["pid"] == launched["app"]["pid"] and "compact.txt" in w["title"]), None)
        time.sleep(0.3)
    ctx.require("the compact instance opened its TextEdit window", wid is not None)
    area = element_index(call("get_window_state", window_id=wid)["tree"], "AXTextArea")
    hidden = call("set_value", window_id=wid, element_index=area, value="hidden tools work")
    ctx.check("a hidden tool (set_value) is still callable", value_of(hidden["observation"]["tree"]) == "hidden tools work", hidden)
    batched = call("batch", window_id=wid, steps=[{"tool": "type_text", "element_index": area, "text": "!"}])
    ctx.check("and batch", batched["steps"][0]["ok"], batched["steps"])
    acted = call("act", window_id=wid, steps=[{"tool": "set_value", "element_index": area, "value": "via act"}])
    ctx.check("act works", value_of(acted["observation"]["tree"]) == "via act", acted)
    found = call("find_elements", window_id=wid, text="via act")
    ctx.check("find_elements works", found["total"] >= 1, found)

    denied = {tool: None for tool in ("get_window_state", "find_elements")}
    for tool in denied:
        try:
            call(tool, window_id=calc, **({"text": "7"} if tool == "find_elements" else {}))
        except ToolError as e:
            denied[tool] = str(e)
    ctx.check("find_elements is refused for an unapproved app, like get_window_state",
              denied["find_elements"] and denied["find_elements"] == denied["get_window_state"]
              and "approval" in denied["find_elements"].lower(), denied)


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
