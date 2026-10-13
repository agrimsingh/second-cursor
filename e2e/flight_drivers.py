#!/usr/bin/env python3
"""The flight search benchmark task, driven three scripted ways over MCP, to compare how many calls each needs.

Each scenario gets a fresh FlightFixture (Navan-like: a trip-type menu whose open state covers the form with a
transparent backdrop, a destination autocomplete that needs a picked suggestion, a date picker with Confirm, results
after a 3 s spinner, ~140 flights, Nonstop and In-policy filters) and a blank agent tab, then:
  flight_single  one action per call, refs read from each action's own observation
  flight_batch   batch with tab_id for every run of actions whose refs are already known
  flight_repl    one browser_repl script with locators
The fixture's judge checks the reply's ANSWER: line against the server's expected nonstop in-policy flights, that the
one-way SIN→MAA Oct 28 search reached the server, and that no fare was selected.

What this catches: a driver that answers from the wrong search (round-trip fares, the wrong date or airport) or from
unfiltered results; a click sent while the trip menu's backdrop covers the form; a suggestion or Confirm that never
lands; results read before the spinner ends; a call count that regresses.

Usage:  SC_E2E_APP=build/SecondCursor.app python3 e2e/flight_drivers.py [scenario ...] [--seed N]
Output: e2e/artifacts/<run-id>/ (report.md, flight-drivers.md/.json with calls, wall and tool ms per driver,
        session-report.md/.json from the instance's own timing and action logs, instance/ logs)
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import session_report
from benchmarks.flight_driver import DRIVERS, execute
from benchmarks.flight_fixtures import FlightFixture
from benchmarks.tasks import Meter
from run import Run, SCENARIOS, scenario

SCENARIOS.clear()
SEED = 1
ROWS = []


def drive(ctx, driver):
    with FlightFixture(SEED, ctx.run.dir / "flight" / driver) as fixture:
        targets = {"url": fixture.url}
        if driver != "repl":
            tab = ctx.call("browser_open", url="about:blank")["tab"]["id"]
            ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab))
            targets["tab_id"] = tab
        row = {"driver": driver, "seed": SEED, "answer": None, "error": None}
        ROWS.append(row)
        meter = Meter(ctx)
        row["window_start"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            row["answer"] = execute(ctx, targets, meter, driver)
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
        finally:
            row["window_end"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            metrics = meter.finish()
            row.update(calls=metrics["tool_calls"], wall_ms=round(metrics["wall_ms"]), tool_ms=round(metrics["tool_ms"]),
                       tools=[event["name"] for event in meter.events],
                       failed_calls=sum(not event["ok"] for event in meter.events))
        time.sleep(0.3)
        events = fixture.read()
        row["checks"] = fixture.judge(events, row["answer"])
        row["diagnostics"] = fixture.diagnostics(events, row["answer"])
        ctx.require("the driver produced an answer", row["answer"] is not None, row["error"])
        for name, ok in row["checks"].items():
            ctx.check("independent: " + name, ok, row["diagnostics"])


@scenario
def flight_single(ctx):
    """browser_navigate, browser_click, browser_type, browser_command one call each."""
    drive(ctx, "single")


@scenario
def flight_batch(ctx):
    """batch with tab_id."""
    drive(ctx, "batch")


@scenario
def flight_repl(ctx):
    """browser_repl."""
    drive(ctx, "repl")


def summarize(directory):
    logs = directory / "instance"
    reports = []
    for row in ROWS:
        args = argparse.Namespace(logs=str(logs), sc_home=None, session=None, since=row["window_start"],
                                  until=row["window_end"], gap=180, min_calls=1, top=3, request_at=None, answer_at=None)
        report = session_report.build(args)
        row["session_report"] = report["runs"]
        reports.append((row["driver"], report))
    (directory / "flight-drivers.json").write_text(json.dumps(ROWS, indent=2) + "\n")
    lines = ["# Flight search: scripted drivers", "",
             f"Seed {SEED}. Calls are MCP tool calls from the first navigation to the answer; a client model pays one "
             "turn per call. Wall and tool ms are measured by the harness.", "",
             "| Driver | Calls | Failed | Wall ms | Tool ms | Judge | Tools |", "| --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for row in ROWS:
        verdict = "pass" if row.get("checks") and all(row["checks"].values()) else "FAIL"
        lines.append(f"| {row['driver']} | {row['calls']} | {row['failed_calls']} | {row['wall_ms']} | {row['tool_ms']} | "
                     f"{verdict} | {', '.join(row['tools'])} |")
    for row in ROWS:
        lines += ["", f"## {row['driver']}", "", "```", row["answer"] or row["error"] or "", "```",
                  f"Checks: `{json.dumps(row.get('checks'))}`"]
    (directory / "flight-drivers.md").write_text("\n".join(lines) + "\n")
    combined = "\n".join(f"# Driver: {driver}\n\n" + session_report.markdown(report) for driver, report in reports)
    (directory / "session-report.md").write_text(combined)
    (directory / "session-report.json").write_text(json.dumps({d: r for d, r in reports}, indent=2) + "\n")
    print("\n".join(lines))
    print(f"\nFlight drivers: {directory / 'flight-drivers.md'}")


def main():
    global SEED
    if "--seed" in sys.argv:
        SEED = int(sys.argv[sys.argv.index("--seed") + 1])
    names = {a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith("--") and sys.argv[i - 1] != "--seed"}
    harness = Run(browser=True)
    ok = harness.run(names)
    summarize(harness.dir)
    return 0 if ok and all(row.get("checks") and all(row["checks"].values()) for row in ROWS) else 1


if __name__ == "__main__":
    sys.exit(main())
