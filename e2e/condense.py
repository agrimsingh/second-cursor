#!/usr/bin/env python3
"""Outline condensing (config condenseOutlines: "rules"), driven over MCP/HTTP like a client.

A full outline of 80 lines or more comes back with footers and side panels hidden and long runs of like items (rows,
list items, links) cut to their first chunk; each run of hidden lines is one '… N lines hidden (expand "r3"): <first
line>' line, and get_window_state or browser_snapshot with expand returns those lines as they were read.

Ways this can fail, and the scenarios that catch them:
- An element in a hidden part can no longer be used: a native index left out of the window's elements, or a browser ref
  that no longer resolves (native_condensed inspects a hidden link by its index; browser_condensed changes Maya Chen's
  role, row 48 of 80, through refs taken from an expanded part, and the fixture's backend records it).
- The diff baseline becomes the condensed outline, so the next read reports hidden lines as removed, or a change inside
  a hidden part is condensed away instead of shown (both scenarios read again right after a condensed read and expect
  "(no changes)"; browser_condensed expects the role change in the action's diff).
- find_elements, or a browser locator, misses what is hidden (native_condensed, browser_condensed).
- A scoped read, an outline under 80 lines, or a diff is condensed (native_condensed reads the text area by scope;
  browser_condensed opens a short form page; both check diffs come back whole).
- An action's observation is condensed, hiding what the action just revealed: the console re-renders on every click,
  so opening Maya Chen's role menu returns the whole page, with the menu in a row the rules would hide
  (browser_condensed). Only reads are condensed, as in the benchmarked rules arm of e2e/jev_prune.py.
- Lines are lost or duplicated: the shown lines plus every expanded part are not the outline (native_condensed checks
  every element index appears exactly once; browser_condensed checks every team member appears once and the line
  counts add up to the header's total).
- An expand id from an older read returns stale lines, an unknown id or another window's id returns something, or the
  error does not say what to do (both scenarios).
- Navigation is cut: the console's product and settings navigation must be shown in full, while its activity panel
  (complementary) and footer (contentinfo) are hidden (browser_condensed).
- A single line is hidden behind a '… 1 lines hidden' line that is longer than it (browser_condensed: the activity
  panel's heading sits alone between its container and its list).
- A truncated outline's closing line naming what was left out is hidden (native_condensed, max_elements 120).
- structured=true elements lose the hidden elements (native_condensed).
- Condensing is on, or expand is advertised, without the config (off_by_default, on a second instance with defaults).

Usage:  e2e/condense.py [scenario ...]   (same targeting flags as e2e/run.py, e.g. SC_E2E_APP=build/SecondCursor.app)
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl)
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from jev_prune import ConsoleBackend
from run import FixtureServer, Run, SCENARIOS, Skip, TestInstance, ToolError, element_index, links_document, scenario

SCENARIOS.clear()

HEADER = re.compile(r"^\(condensed: (\d+) of (\d+) lines shown\b")
HIDDEN = re.compile(r'^( *)… (\d+) lines hidden \(expand "(r\d+)"\): ', re.M)
INDEX = re.compile(r"^ *\[(\d+)\] ", re.M)


def hidden_parts(outline):
    return [(m[3], int(m[2])) for m in HIDDEN.finditer(outline)]


def expanded_lines(text):
    return [line for line in text.split("\n") if not line.startswith("(hidden part ")]


def error_of(ctx, tool, **args):
    try:
        ctx.call(tool, **args)
    except ToolError as e:
        return str(e)
    return None


def block(outline, header):
    """The lines under the first line containing `header`, down to the next line indented no deeper."""
    lines = outline.split("\n")
    start = next((i for i, line in enumerate(lines) if header in line), None)
    if start is None:
        return None
    depth = len(lines[start]) - len(lines[start].lstrip(" "))
    out = []
    for line in lines[start + 1:]:
        if len(line) - len(line.lstrip(" ")) <= depth:
            break
        out.append(line)
    return out


def reconstruct(outline, expanded):
    """The outline with every hidden line replaced by the lines it stands for, without the header."""
    parts = {}
    for chunk in re.split(r"^(?=\(hidden part )", expanded, flags=re.M):
        match = re.match(r"\(hidden part (r\d+), as it was when read\)\n", chunk)
        if match:
            parts[match[1]] = chunk[match.end():].rstrip("\n").split("\n")
    out = []
    for line in outline.split("\n")[1:]:
        match = HIDDEN.match(line)
        out.extend(parts.get(match[3], [line]) if match else [line])
    return out


@scenario
def native_condensed(ctx):
    wid = links_document(ctx, "condense.html")["id"]
    read = ctx.call("get_window_state", window_id=wid, full=True, structured=True)
    tree = read["tree"]
    header = HEADER.match(tree)
    parts = hidden_parts(tree)
    ctx.require("a full read of the 200-link document is condensed, with a header and hidden parts",
                header and parts and not read["is_diff"], tree[:600])
    shown, total = int(header[1]), int(header[2])
    ctx.check("the header's counts add up: shown plus hidden is the whole outline",
              shown + sum(n for _, n in parts) == total and total > 200, (shown, total, parts))
    ctx.check("the link list keeps its first chunk", element_index(tree, "AXLink", "link 0") is not None
              and element_index(tree, "AXLink", "link 150") is None, tree[:1200])
    ctx.check("structured elements still list every element", len(read.get("elements", [])) == read["element_count"]
              and read["element_count"] > 200, (len(read.get("elements", [])), read["element_count"]))

    ids = [part for part, _ in parts]
    expanded = ctx.call("get_window_state", window_id=wid, expand=ids)
    lines = reconstruct(tree, expanded["tree"])
    indexes = [int(i) for i in INDEX.findall("\n".join(lines))]
    ctx.check("expanding every hidden part gives back the whole outline: each element index exactly once",
              sorted(indexes) == list(range(read["element_count"])) and len(lines) == total,
              (len(indexes), read["element_count"], len(lines), total))
    hidden_link = element_index("\n".join(lines), "AXLink", "link 150")
    ctx.require("link 150 is in a hidden part", hidden_link is not None, expanded["tree"][:400])

    inspected = ctx.call("inspect_element", window_id=wid, element_index=hidden_link)
    ctx.check("a hidden element's index still resolves", "link 150" in str(inspected), str(inspected)[:300])
    found = ctx.call("find_elements", window_id=wid, text="link 150")
    ctx.check("find_elements finds a hidden element by its text",
              any(f"[{hidden_link}]" in line for line in found["matches"]), found["matches"])
    again = ctx.call("get_window_state", window_id=wid)
    ctx.check("the diff baseline is the whole outline: the next read reports no changes",
              again["is_diff"] and again["tree"] == "(no changes)", again["tree"][:300])

    area = element_index(tree, "AXTextArea")
    scoped = ctx.call("get_window_state", window_id=wid, scope=area)["tree"]
    ctx.check("a scoped read is never condensed", not HEADER.match(scoped) and '"link 199"' in scoped, scoped[:300])

    fresh = ctx.call("get_window_state", window_id=wid, full=True)["tree"]
    newer = [part for part, _ in hidden_parts(fresh)]
    old = error_of(ctx, "get_window_state", window_id=wid, expand=ids[:1])
    ctx.check("an id from an older read is refused, saying it is from an older read",
              old and "older read" in old and set(newer).isdisjoint(ids), (old, ids, newer))
    unknown = error_of(ctx, "get_window_state", window_id=wid, expand=["r999"])
    ctx.check("an unknown id is refused, saying where ids come from", unknown and "lines hidden" in unknown, unknown)

    truncated = ctx.call("get_window_state", window_id=wid, full=True, max_elements=120)["tree"]
    last = truncated.rstrip("\n").split("\n")[-1]
    ctx.check("a truncated outline keeps the line naming what was left out",
              HEADER.match(truncated) and "… truncated at 120 elements" in truncated and not HIDDEN.match(last)
              and not any("truncated at" in line for line in HIDDEN.findall(truncated)), truncated[-400:])


@scenario
def browser_condensed(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("isolated test browser not connected")
    with ConsoleBackend() as backend:
        opened = ctx.call("browser_open", url=f"{backend.base}/console.html")
        tid = opened["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        ctx.require("the console's full outline is condensed", HEADER.match(outline), outline[:400])
        shown_lines = [line for line in outline.split("\n")[1:] if not HIDDEN.match(line)]
        shown = "\n".join(shown_lines)
        nav = ["Dashboard", "Changelog", "Community", "Overview", "Team", "Integrations", "Delete workspace"]
        blocks = [block(outline, 'navigation "Product"'), block(outline, 'navigation "Settings"')]
        ctx.check("navigation is shown in full: product links and every settings section",
                  all(blocks) and not any(HIDDEN.match(line) for b in blocks for line in b)
                  and all(f'"{name}"' in shown for name in nav) and len(blocks[1]) >= 30,
                  shown[:1500])
        ctx.check("no single line is hidden: the activity panel's heading is shown as is",
                  all(n > 1 for _, n in hidden_parts(outline)) and 'heading "Recent activity"' in shown,
                  hidden_parts(outline))
        ctx.check("the activity panel and the footer are hidden, their containers still named",
                  'complementary "Recent activity"' in shown and 'button "View"' not in shown and '"Careers"' not in shown
                  and "contentinfo" in shown, shown[-1500:])

        sidebar_team = re.search(r'\[(\w+)\] button "Team"', shown)
        ctx.require("the Team section's button has a ref", sidebar_team, shown[:1500])
        ctx.call("browser_click", tab_id=tid, ref=sidebar_team[1], observe="none")
        team = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        header, parts = HEADER.match(team), hidden_parts(team)
        ctx.require("the team view is condensed", header and parts, team[:400])
        ctx.check("the member table keeps its first rows and hides Maya Chen's (row 48)",
                  '"Ava Chen"' in team and "Maya Chen" not in team.replace("Maya Cheng", ""), team[:2000])
        again = ctx.call("browser_snapshot", tab_id=tid)
        ctx.check("the diff baseline is the whole page: the next read reports no changes",
                  again.get("is_diff") and again["outline"] == "(no changes)", again.get("outline", "")[:300])

        ids = [part for part, _ in parts]
        expanded = ctx.call("browser_snapshot", tab_id=tid, expand=ids)
        lines = reconstruct(team, expanded["outline"])
        members = re.findall(r'button "Change role for ([^"]+)"', "\n".join(lines))
        ctx.check("expanding every hidden part gives back every member once, and the line counts add up",
                  len(members) == 80 and len(set(members)) == 80 and len(lines) == int(header[2]),
                  (len(members), len(lines), header[2]))
        change = re.search(r'\[(\w+)\] button "Change role for Maya Chen"', expanded["outline"])
        ctx.require("Maya Chen's role button is in a hidden part", change, expanded["outline"][:600])
        found = ctx.call("browser_command", tab_id=tid, operation="inspect",
                         locator={"role": "button", "name": "Remove Maya Chen"})
        ctx.check("a locator finds a hidden element", "Remove Maya Chen" in str(found), str(found)[:300])

        menu = ctx.call("browser_click", tab_id=tid, ref=change[1])["observation"].get("outline", "")
        make = re.search(r'\[(\w+)\] button "Make Maya Chen Admin"', menu)
        ctx.require("clicking a hidden ref opens the role menu, and the action's observation (not condensed) shows it",
                    make and not HEADER.match(menu) and not HIDDEN.search(menu), menu[:600])
        done = ctx.call("browser_click", tab_id=tid, ref=make[1])["observation"].get("outline", "")
        ctx.check("the change inside a hidden part comes back in the observation, not condensed: the menu is gone",
                  done and not HEADER.match(done) and not HIDDEN.search(done) and "Make Maya Chen" not in done, done[:600])
        ctx.check("the backend recorded the role change", backend.changes() == {"role:Maya Chen": "Admin"},
                  backend.changes())

        old = error_of(ctx, "browser_snapshot", tab_id=tid, expand=ids[:1])
        ctx.check("an id from an older read is still valid until the next condensed read", old is None, old)
        ctx.call("browser_snapshot", tab_id=tid, full=True)
        old = error_of(ctx, "browser_snapshot", tab_id=tid, expand=ids[:1])
        ctx.check("after a newer condensed read, an older id is refused as from an older read",
                  old and "older read" in old, old)

    with FixtureServer() as base:
        short = ctx.call("browser_open", url=f"{base}/form.html")
        other = short["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=other))
        outline = (short.get("observation") or {}).get("outline") or ctx.call("browser_snapshot", tab_id=other,
                                                                               full=True)["outline"]
        ctx.check("a page under 80 lines is never condensed", outline and not HEADER.match(outline), outline[:300])
        foreign = error_of(ctx, "browser_snapshot", tab_id=other, expand=ids[:1])
        ctx.check("another tab's id is refused", foreign and "lines hidden" in foreign, foreign)


@scenario
def off_by_default(ctx):
    wid = links_document(ctx, "plain.html")["id"]
    with TestInstance({}) as plain:
        tools = {t["name"]: t["inputSchema"].get("properties", {}) for t in plain.mcp.rpc("tools/list")["tools"]}
        ctx.check("without the config, expand is not advertised",
                  "expand" not in tools["get_window_state"] and "expand" not in tools["browser_snapshot"],
                  sorted(tools["get_window_state"]))
        tree = plain.mcp.call("get_window_state", window_id=wid, full=True)[0]["tree"]
        ctx.check("without the config, a full read is the whole outline",
                  not HEADER.match(tree) and not HIDDEN.search(tree) and '"link 199"' in tree, tree[:300])
    tools = {t["name"]: t["inputSchema"].get("properties", {}) for t in ctx.mcp.rpc("tools/list")["tools"]}
    ctx.check("with the config, get_window_state and browser_snapshot advertise expand",
              "expand" in tools["get_window_state"] and "expand" in tools["browser_snapshot"], sorted(tools))


if __name__ == "__main__":
    sys.exit(0 if Run(config_overrides={"condenseOutlines": "rules"}).run(set(sys.argv[1:])) else 1)
