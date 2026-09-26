#!/usr/bin/env python3
"""Latency of the calls an agent makes most, timed at the client against a test instance (server work plus localhost
HTTP). Uses only arguments every build understands, so two builds can be compared:

    SC_E2E_APP=build/SecondCursor.app python3 e2e/latency.py [--rounds N]

Writes latency.json (medians and every sample, in ms, plus observation sizes) next to the usual report.
"""
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, SCENARIOS, ToolError, scenario, element_index, links_document

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

    wid = links_document(ctx, "latency.html")["id"]
    calc = ctx.call("launch_app", app="Calculator", new_instance=True)
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


if __name__ == "__main__":
    run = Run()
    run.run([a for a in sys.argv[1:] if not a.isdigit()])
    sys.exit(1 if any(r["status"] == "FAIL" for r in run.results) else 0)
