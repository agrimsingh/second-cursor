#!/usr/bin/env python3
"""Latency of the calls an agent makes most, timed at the client against a test instance (server work plus localhost
HTTP). Uses only arguments every build understands, so two builds can be compared:

    SC_E2E_APP=build/SecondCursor.app python3 e2e/latency.py [latency | browser_latency] [--rounds N]

Writes latency.json (medians and every sample, in ms, plus observation sizes) next to the usual report, and
browser-latency.json for browser actions: each one's latency and whether its own observation already showed the
page's reaction (a faster action that returns before a slow page renders only moves the wait to the next read).
"""
import json
import pathlib
import re
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario, element_index, links_document

SCENARIOS.clear()
ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 7


@scenario
def latency(ctx):
    samples, sizes = {}, {}

    def timed(label, tool, **args):
        started = time.perf_counter()
        data, _ = ctx.mcp.call(tool, **args)
        samples.setdefault(label, []).append(round((time.perf_counter() - started) * 1000, 1))
        return data

    # Off the user's screen, so no call waits for the agent cursor to land: this measures the server.
    wid = links_document(ctx, "latency.html", on_agent_display=True)["id"]
    calc = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(calc["app"]["pid"])
    # Calculator can replace its first window right after launch.
    seven = None
    for _ in range(3):
        calc_wid = ctx.wait_for_window("Calculator", pid=calc["app"]["pid"])["id"]
        try:
            seven = element_index(ctx.tree(calc_wid), "AXButton", "7")
            break
        except ToolError:
            time.sleep(1)
    ctx.require("Calculator's 7 is indexed", seven is not None)
    ctx.tree(wid)

    for i in range(ROUNDS):
        timed("list_windows", "list_windows")
        full = timed("get_window_state full (links doc)", "get_window_state", window_id=wid, full=True)
        sizes["get_window_state full (links doc)"] = len(full["tree"])
        timed("get_window_state diff, no change", "get_window_state", window_id=wid)
        timed("screenshot, idle window", "screenshot", window_id=wid)

        timed("click observe=none", "click", window_id=calc_wid, element_index=seven, observe="none")
        ctx.call("get_window_state", window_id=calc_wid)
        timed("click observe=tree", "click", window_id=calc_wid, element_index=seven)
        timed("press_key observe=tree", "press_key", window_id=calc_wid, keys="escape")
        timed("click observe=none, then screenshot", "click", window_id=calc_wid, element_index=seven, observe="none")
        started = time.perf_counter()
        ctx.mcp.call("screenshot", window_id=calc_wid)
        samples.setdefault("screenshot right after an action", []).append(round((time.perf_counter() - started) * 1000, 1))
        ctx.call("get_window_state", window_id=calc_wid)

        scrolled = timed("scroll observe=tree (links doc)", "scroll", window_id=wid,
                         direction="down" if i % 2 == 0 else "up", amount=5)
        sizes.setdefault("scroll observation (links doc)", []).append(len((scrolled.get("observation") or {}).get("tree", "")))

    medians = {label: statistics.median(values) for label, values in samples.items()}
    (ctx.run.dir / "latency.json").write_text(json.dumps(
        {"app": str(ctx.run.instance.app) if ctx.run.instance else ctx.run.target, "rounds": ROUNDS,
         "median_ms": medians, "samples_ms": samples, "sizes_chars": sizes}, indent=2))
    for label, value in medians.items():
        ctx.check(f"{label}: median {value:.0f} ms", True, samples[label])
    scroll_sizes = sizes["scroll observation (links doc)"]
    ctx.check(f"scroll observation: median {statistics.median(scroll_sizes):.0f} chars "
              f"(full tree {sizes['get_window_state full (links doc)']})", True, scroll_sizes)
    # An on-screen target also waits for the agent arrow's glide (capped at ~150 ms) so the arrow lands with the click.
    ctx.check("click observe=none stays fast (median under 250 ms, arrow glide included)",
              medians["click observe=none"] < 250, medians["click observe=none"])


@scenario
def browser_latency(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    samples, seen = {}, {}
    with FixtureServer() as base:
        tid = ctx.call("browser_open", url=f"{base}/reactions.html")["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]

        def ref(pattern):
            match = re.search(r"\[(\w+)\] " + pattern, outline)
            ctx.require(f"the outline has {pattern}", match, outline[:800])
            return match.group(1)

        name, search, agree = ref('textbox "Name"'), ref('textbox "Search"'), ref('checkbox "Agree"')
        details, add, noop, load = (ref(f'button "{label}"') for label in ("Details", "Add row", "Nothing", "Load"))

        def timed(label, tool, reaction=None, **args):
            started = time.perf_counter()
            data, _ = ctx.mcp.call(tool, tab_id=tid, **args)
            samples.setdefault(label, []).append(round((time.perf_counter() - started) * 1000, 1))
            if reaction:
                observed = (data.get("observation") or {}).get("outline", "")
                seen.setdefault(label, []).append(bool(re.search(reaction, observed)))

        for i in range(ROUNDS):
            timed("type into a plain field", "browser_type", r'value="x', ref=name, text="x")
            timed("type into a search box (results ~450 ms later)", "browser_type", r"result 1", ref=search,
                  text="abcdefghij"[i])
            timed("click a checkbox", "browser_click", r'checkbox "Agree"', ref=agree)
            timed("click an aria-expanded toggle", "browser_click", r"Panel content", ref=details)
            timed("click a button that adds a row", "browser_click", rf"Row {i + 1}", ref=add)
            timed("click a button that does nothing", "browser_click", ref=noop)
            timed("click a button whose content loads 400 ms later", "browser_click", rf"Loaded {i + 1}", ref=load)
            timed("press Tab", "browser_press_key", r"focused", keys="Tab")
            timed("click with observe=none", "browser_click", ref=noop, observe="none")

    medians = {label: statistics.median(values) for label, values in samples.items()}
    shown = {label: f"{sum(values)}/{len(values)}" for label, values in seen.items()}
    (ctx.run.dir / "browser-latency.json").write_text(json.dumps(
        {"app": str(ctx.run.instance.app) if ctx.run.instance else ctx.run.target, "rounds": ROUNDS,
         "median_ms": medians, "reaction_in_own_observation": shown, "samples_ms": samples}, indent=2))
    for label, value in medians.items():
        ctx.check(f"{label}: median {value:.0f} ms" + (f", reaction shown {shown[label]}" if label in shown else ""),
                  True, samples[label])
    for label in ("type into a search box (results ~450 ms later)", "click a button whose content loads 400 ms later"):
        ctx.check(f"{label}: its own observation shows the late content every time", all(seen[label]), shown[label])
    toggle = medians["click an aria-expanded toggle"]
    ctx.check("a toggle that only changes aria-expanded and hidden counts as the page reacting (median under 1000 ms, "
              "not the 1.2 s wait for a reaction)", toggle < 1000, toggle)
    plain = medians["type into a plain field"]
    ctx.check("typing into a field that shows no suggestions does not wait for them (median under 800 ms)", plain < 800,
              plain)


if __name__ == "__main__":
    run = Run()
    run.run([a for a in sys.argv[1:] if not a.isdigit()])
    sys.exit(1 if any(r["status"] == "FAIL" for r in run.results) else 0)
