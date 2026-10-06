#!/usr/bin/env python3
"""Where real tool calls spend their time, from the app's own timing log ("recordTimings": true in config.json):

    python3 scripts/timings.py [--days N] [--tool NAME] [--top N] [timings.jsonl ...]

Reads ~/Library/Logs/SecondCursor/timings.jsonl and the file it last rotated to (SC_HOME/logs under SC_HOME). Tools
are listed by total time spent, each with its calls, failures, p50/p75/p95 ms, and its slowest phases at p50/p75.
Phases nest (ext.action holds ext.cursor, ext.settled holds ext.settled_*), so they do not add up to the call. The
browser round trip (bridge, bridge.tab.*, ext.total) is the whole call for browser tools and is left out.
"""
import argparse
import datetime
import json
import math
import os
import pathlib
import sys


def default_logs():
    home = os.environ.get("SC_HOME")
    base = pathlib.Path(home) / "logs" if home else pathlib.Path.home() / "Library/Logs/SecondCursor"
    return [base / "timings.jsonl.1", base / "timings.jsonl"]


def pct(values, p):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="*", type=pathlib.Path)
    parser.add_argument("--days", type=float, help="only calls from the last N days")
    parser.add_argument("--tool", help="only this tool")
    parser.add_argument("--top", type=int, default=8, help="phases shown per tool")
    args = parser.parse_args()
    since = None
    if args.days:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)

    by_tool = {}
    for path in args.logs or default_logs():
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if args.tool and row.get("tool") != args.tool:
                continue
            if since and datetime.datetime.fromisoformat(row["time"].replace("Z", "+00:00")) < since:
                continue
            by_tool.setdefault(row["tool"], []).append(row)
    if not by_tool:
        sys.exit('No timings. Set "recordTimings": true in config.json and restart Second Cursor.')

    print(f"{sum(len(rows) for rows in by_tool.values())} calls")
    print(f"{'tool':<24}{'calls':>7}{'failed':>8}{'p50':>7}{'p75':>7}{'p95':>7}   slowest phases (p50/p75 ms)")
    for tool, rows in sorted(by_tool.items(), key=lambda kv: -sum(row["ms"] for row in kv[1])):
        ms = [row["ms"] for row in rows]
        names = {name for row in rows for name in row.get("phases", {})
                 if name not in ("bridge", "ext.total") and not name.startswith("bridge.tab")}
        phases = {name: [row.get("phases", {}).get(name, 0) for row in rows] for name in names}
        slowest = sorted(names, key=lambda name: -pct(phases[name], 75))[: args.top]
        shown = ", ".join(f"{name} {pct(phases[name], 50):.0f}/{pct(phases[name], 75):.0f}"
                          for name in slowest if pct(phases[name], 75) >= 1)
        failed = sum(1 for row in rows if not row.get("ok", True))
        print(f"{tool:<24}{len(rows):>7}{failed:>8}{pct(ms, 50):>7.0f}{pct(ms, 75):>7.0f}{pct(ms, 95):>7.0f}   {shown}")


if __name__ == "__main__":
    main()
