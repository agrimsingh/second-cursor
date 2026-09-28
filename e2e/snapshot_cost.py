#!/usr/bin/env python3
"""Size and latency of browser_snapshot on the local fixtures, timed at the client against a test instance. The
outline goes to a model on every read, so its size is a cost like its latency. Compare two extension folders by
running this from each worktree:

    SC_E2E_APP=build/SecondCursor.app python3 e2e/snapshot_cost.py [--rounds N]

Writes snapshot-cost.json (median ms, every sample, outline chars, lines and refs per page) and outlines/<page>.txt
next to the usual report.
"""
import functools
import http.server
import json
import pathlib
import re
import statistics
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import ROOT, Run, SCENARIOS, Skip, scenario

SCENARIOS.clear()
ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 9
# Directory served as the site root, and the page in it. parity/ is its own root, as in e2e/parity.py, so its
# cross-origin frame (the same path on localhost) resolves.
PAGES = [("fixtures", "inbox.html"), ("fixtures", "cards.html"), ("fixtures", "form.html"),
         ("fixtures/parity", "index.html"), ("fixtures", "hints.html"), ("fixtures", "big.html")]


def serve(directory):
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@scenario
def browser_snapshot_cost(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    report, outlines = {}, ctx.run.dir / "outlines"
    outlines.mkdir()
    for directory, page in PAGES:
        server = serve(directory)
        ctx.cleanups.append(server.shutdown)
        tid = ctx.call("browser_open", url=f"http://127.0.0.1:{server.server_port}/{page}")["tab"]["id"]
        ctx.cleanups.append(lambda tid=tid: ctx.mcp.call("browser_close", tab_id=tid))
        ctx.call("browser_snapshot", tab_id=tid, full=True)
        samples, outline = [], ""
        for _ in range(ROUNDS):
            started = time.perf_counter()
            outline = ctx.call("browser_snapshot", tab_id=tid, full=True)["outline"]
            samples.append(round((time.perf_counter() - started) * 1000, 1))
        label = f"{directory.removeprefix('fixtures').strip('/') + '/' if directory != 'fixtures' else ''}{page}"
        (outlines / label.replace("/", "_").replace(".html", ".txt")).write_text(outline + "\n")
        report[label] = {"median_ms": statistics.median(samples), "samples_ms": samples, "chars": len(outline),
                         "lines": len(outline.splitlines()), "refs": len(re.findall(r"^\s*\[\w+\]", outline, re.M))}
        ctx.check(f"{label}: median {report[label]['median_ms']:.0f} ms, {len(outline)} chars, "
                  f"{report[label]['lines']} lines, {report[label]['refs']} refs", True, samples)
    (ctx.run.dir / "snapshot-cost.json").write_text(json.dumps(
        {"extension": str(ROOT.parent / "extension"), "rounds": ROUNDS, "pages": report}, indent=2))


if __name__ == "__main__":
    run = Run()
    run.run([a for a in sys.argv[1:] if not a.isdigit()])
    sys.exit(1 if any(r["status"] == "FAIL" for r in run.results) else 0)
