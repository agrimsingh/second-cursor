#!/usr/bin/env python3
"""Latency of the calls an agent makes most, timed at the client against a test instance (server work plus localhost
HTTP). Uses only arguments every build understands, so two builds can be compared:

    SC_E2E_APP=build/SecondCursor.app python3 e2e/latency.py [latency | browser_latency] [--rounds N] [--cursor]

Writes latency.json (medians and every sample, in ms, plus observation sizes) next to the usual report, and
browser-latency.json for browser actions: each one's latency and whether its own observation already showed the
page's reaction (a faster action that returns before a slow page renders only moves the wait to the next read).

Builds that record timings (recordTimings, on in every test instance) also get, per call, the server's own time, the
client's time on top of it (HTTP, JSON), and the median of each phase the server timed (settling, the cursor, the tree
walk, capture, the browser round trip and the extension's phases inside it, as ext.*).

--cursor draws the agent cursor on the agent display (cursorOnAgentDisplay), so native calls wait for its glide as
they do on the user's screen. Without it nothing on the agent display draws a cursor: that measures the server alone.

--quiet-ms N sets browserQuietMs, the browser's quiet period once an action's requests have finished, to compare it
with the default. Observation sizes are ratchets (ratchet.py) in default-config runs and only reported otherwise.
"""
import json
import os
import pathlib
import re
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import ratchet
from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario, element_index, links_document

SCENARIOS.clear()
ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 7
CURSOR = "--cursor" in sys.argv
QUIET_MS = int(sys.argv[sys.argv.index("--quiet-ms") + 1]) if "--quiet-ms" in sys.argv else None
GATED = not CURSOR and QUIET_MS is None
EXTENSION = os.environ.get("SC_E2E_EXTENSION") or "extension"


class Phases:
    """The server's timing line for each timed call. Lines are written off the call path, in call order, so the
    line for the call just made is the newest one once it is there; a build that records nothing is noticed on the
    first call and skipped from then on."""

    def __init__(self, ctx):
        self.instance = ctx.run.instance
        self.supported = self.instance is not None
        self.seen = len(self.instance.timings()) if self.supported else 0
        self.by_label = {}
        self.first = True

    def take(self, label, tool, client_ms):
        if not self.supported:
            return
        deadline = time.time() + (2 if self.first else 0.5)
        lines = []
        while time.time() < deadline:
            lines = self.instance.timings(self.seen)
            if lines and lines[-1]["tool"] == tool:
                break
            time.sleep(0.005)
        self.seen += len(lines)
        found = bool(lines) and lines[-1]["tool"] == tool
        if self.first and not found:
            self.supported = False
        self.first = False
        if found:
            self.by_label.setdefault(label, []).append({**lines[-1], "client_ms": client_ms})

    def summary(self):
        """Per label: median server ms, median client ms on top of it, and each phase's median (a phase missing from
        a sample counts as 0 there)."""
        out = {}
        for label, rows in self.by_label.items():
            names = sorted({name for row in rows for name in row["phases"]})
            out[label] = {
                "server_ms": statistics.median(row["ms"] for row in rows),
                "client_overhead_ms": statistics.median(row["client_ms"] - row["ms"] for row in rows),
                "phases_ms": {name: round(statistics.median(row["phases"].get(name, 0) for row in rows), 1)
                              for name in names},
            }
        return out

    @staticmethod
    def describe(entry, top=6):
        phases = sorted(entry["phases_ms"].items(), key=lambda kv: -kv[1])
        shown = ", ".join(f"{name} {ms:.0f}" for name, ms in phases[:top] if ms >= 1)
        return f"server {entry['server_ms']:.0f} ms (+{entry['client_overhead_ms']:.0f} client): {shown}"


def report_phases(ctx, phases):
    summary = phases.summary()
    for label, entry in summary.items():
        ctx.check(f"{label}: {Phases.describe(entry)}", True, entry)
    return summary


@scenario
def latency(ctx):
    samples, sizes = {}, {}
    phases = Phases(ctx)

    def timed(label, tool, **args):
        started = time.perf_counter()
        data, _ = ctx.mcp.call(tool, **args)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        samples.setdefault(label, []).append(elapsed)
        phases.take(label, tool, elapsed)
        return data

    # On the agent display, so the user's screen is untouched; --cursor draws the arrow there anyway.
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
        sizes.setdefault("get_window_state full (links doc)", []).append(len(full["tree"]))
        timed("get_window_state diff, no change", "get_window_state", window_id=wid)
        timed("screenshot, idle window", "screenshot", window_id=wid)

        timed("click observe=none", "click", window_id=calc_wid, element_index=seven, observe="none")
        ctx.call("get_window_state", window_id=calc_wid)
        timed("click observe=tree", "click", window_id=calc_wid, element_index=seven)
        timed("press_key observe=tree", "press_key", window_id=calc_wid, keys="escape")
        timed("click observe=none, then screenshot", "click", window_id=calc_wid, element_index=seven, observe="none")
        timed("screenshot right after an action", "screenshot", window_id=calc_wid)
        timed("screenshot, idle Calculator", "screenshot", window_id=calc_wid)
        ctx.call("get_window_state", window_id=calc_wid)

        scrolled = timed("scroll observe=tree (links doc)", "scroll", window_id=wid,
                         direction="down" if i % 2 == 0 else "up", amount=5)
        sizes.setdefault("scroll observation (links doc)", []).append(len((scrolled.get("observation") or {}).get("tree", "")))

    medians = {label: statistics.median(values) for label, values in samples.items()}
    for label, value in medians.items():
        ctx.check(f"{label}: median {value:.0f} ms", True, samples[label])
    summary = report_phases(ctx, phases)
    (ctx.run.dir / "latency.json").write_text(json.dumps(
        {"app": str(ctx.run.instance.app) if ctx.run.instance else ctx.run.target, "rounds": ROUNDS, "cursor": CURSOR,
         "median_ms": medians, "samples_ms": samples, "sizes_chars": sizes, "server": summary}, indent=2))
    scroll_sizes = sizes["scroll observation (links doc)"]
    ctx.check(f"scroll observation: median {statistics.median(scroll_sizes):.0f} chars "
              f"(full tree {sizes['get_window_state full (links doc)'][0]})", True, scroll_sizes)
    ratchet.check(ctx, "native: full read of the links document, characters",
                  sizes["get_window_state full (links doc)"], GATED)
    ratchet.check(ctx, "native: first scroll-down observation of the links document, characters", scroll_sizes[0], GATED)
    # An on-screen target also waits for the agent arrow's glide (capped at ~150 ms) so the arrow lands with the click.
    ctx.check("click observe=none stays fast (median under 250 ms, arrow glide included)",
              medians["click observe=none"] < 250, medians["click observe=none"])


@scenario
def browser_latency(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    samples, seen, first_sizes = {}, {}, {}
    with FixtureServer() as base:
        tid = ctx.call("browser_open", url=f"{base}/reactions.html")["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
        outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
        ratchet.check(ctx, "browser: full read of reactions.html, characters", len(outline), GATED)

        def ref(pattern):
            match = re.search(r"\[(\w+)\] " + pattern, outline)
            ctx.require(f"the outline has {pattern}", match, outline[:800])
            return match.group(1)

        name, search, agree = ref('textbox "Name"'), ref('textbox "Search"'), ref('checkbox "Agree"')
        details, add, noop, load, save = (ref(f'button "{label}"')
                                          for label in ("Details", "Add row", "Nothing", "Load", "Save"))

        phases = Phases(ctx)

        def timed(label, tool, reaction=None, **args):
            started = time.perf_counter()
            data, _ = ctx.mcp.call(tool, tab_id=tid, **args)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            samples.setdefault(label, []).append(elapsed)
            phases.take(label, tool, elapsed)
            observed = (data.get("observation") or {}).get("outline", "")
            if "observation" in data:
                first_sizes.setdefault(label, len(observed))
            if reaction:
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
            timed("click a button that says Saving, then Saved 200 ms later", "browser_click", rf"Saved {i + 1}",
                  ref=save)
            timed("press Tab", "browser_press_key", r"focused", keys="Tab")
            timed("click with observe=none", "browser_click", ref=noop, observe="none")

    medians = {label: statistics.median(values) for label, values in samples.items()}
    shown = {label: f"{sum(values)}/{len(values)}" for label, values in seen.items()}
    for label, value in medians.items():
        ctx.check(f"{label}: median {value:.0f} ms" + (f", reaction shown {shown[label]}" if label in shown else ""),
                  True, samples[label])
    summary = report_phases(ctx, phases)
    for label, size in first_sizes.items():
        ratchet.check(ctx, f"browser: first observation after '{label}', characters", size, GATED)
    (ctx.run.dir / "browser-latency.json").write_text(json.dumps(
        {"app": str(ctx.run.instance.app) if ctx.run.instance else ctx.run.target, "rounds": ROUNDS,
         "extension": str(EXTENSION), "quiet_ms": QUIET_MS, "median_ms": medians,
         "reaction_in_own_observation": shown, "first_observation_chars": first_sizes, "samples_ms": samples,
         "server": summary}, indent=2))
    for label in ("type into a search box (results ~450 ms later)", "click a button whose content loads 400 ms later",
                  "click a button that says Saving, then Saved 200 ms later"):
        ctx.check(f"{label}: its own observation shows the late content every time", all(seen[label]), shown[label])
    toggle = medians["click an aria-expanded toggle"]
    ctx.check("a toggle that only changes aria-expanded and hidden counts as the page reacting (median under 1000 ms, "
              "not the 1.2 s wait for a reaction)", toggle < 1000, toggle)
    plain = medians["type into a plain field"]
    ctx.check("typing into a field that shows no suggestions does not wait for them (median under 800 ms)", plain < 800,
              plain)
    tab = medians["press Tab"]
    ctx.check("Tab, which only moves focus, does not wait for a request (median under 700 ms, not the 1.2 s wait)",
              tab < 700, tab)
    ctx.check("and its own observation shows where focus went every time", all(seen["press Tab"]), shown["press Tab"])


if __name__ == "__main__":
    overrides = {**({"cursorOnAgentDisplay": True} if CURSOR else {}),
                 **({"browserQuietMs": QUIET_MS} if QUIET_MS is not None else {})}
    run = Run(overrides or None)
    run.run([a for a in sys.argv[1:] if not a.isdigit()])
    sys.exit(1 if any(r["status"] == "FAIL" for r in run.results) else 0)
