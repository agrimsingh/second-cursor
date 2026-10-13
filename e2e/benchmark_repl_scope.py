#!/usr/bin/env python3
"""The model benchmark's --browser-surface repl scope, driven like the benchmark adapter drives it: a ScopedEngine
over a disposable instance and its isolated test browser, against two local flight fixtures. Covers the failure modes
listed above BROWSER_SURFACES in benchmarks/engines.py that a script can provoke: a literal foreign URL is refused
before the call (1), a fixture navigation passes the action-log and tab audits and its tab is adopted for discrete
tools (9), and a URL built at run time is caught after the call, fails the trial, and locks the engine (1, 12).

    SC_E2E_APP=/abs/path/build/SecondCursor.app python3 e2e/benchmark_repl_scope.py
"""
import hashlib
import os
import pathlib
import sys

import run as harness
from benchmarks.engines import ScopeBreach, ScopeError, _ok, create_engine
from benchmarks.flight_fixtures import FlightFixture


def repl_scope(ctx):
    binary = pathlib.Path(os.environ["SC_E2E_APP"]).resolve() / "Contents/MacOS/SecondCursor"
    with FlightFixture(1, ctx.run.dir / "fixture") as fixture, FlightFixture(2, ctx.run.dir / "foreign") as foreign:
        request = {"task_id": "flight_policy_search", "track": "host-background", "targets": {"url": fixture.url},
                   "browser_surface": "repl", "timeout_seconds": 60,
                   "engine_version": "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()}
        engine = create_engine("second-cursor", request)
        ctx.cleanups.append(engine.close)
        engine.start()
        ctx.require("the repl surface advertises browser_repl beside the discrete tools",
                    {"browser_repl", "browser_navigate", "browser_snapshot"} <= set(engine.allowed), sorted(engine.allowed))

        try:
            engine.call("browser_repl", {"code": f"await page.goto({foreign.url!r})"})
            refused = None
        except ScopeError as error:
            refused = error
        ctx.check("code naming a foreign URL is refused before it runs, without failing the trial",
                  refused is not None and not isinstance(refused, ScopeBreach) and engine.breach is None, refused)
        ctx.check("the refused code never reached the foreign fixture", not foreign.read(), foreign.read())

        result = engine.call("browser_repl", {"code": f"await page.goto({fixture.url!r}); await page.title()"})
        audit = engine.repl_audit
        ctx.check("a fixture navigation through browser_repl runs and passes the audit",
                  not result.get("isError") and engine.breach is None, result)
        ctx.check("the audit read the REPL's navigation from the action log",
                  audit["navigations"] and all(row["on_fixture_origin"] for row in audit["navigations"]), audit)
        ctx.check("the REPL's own tab was adopted", len(audit["tabs_adopted"]) == 1, audit)
        if audit["tabs_adopted"]:
            snapshot = engine.call("browser_snapshot", {"tab_id": audit["tabs_adopted"][0]})
            ctx.check("discrete tools act on the adopted REPL tab", _ok(snapshot), str(snapshot)[:300])

        host, port = foreign.origin.split("//")[1].split(":")
        built = f"const u = ['http:', '', '{host}:' + {port}, 'fixture.html'].join('/'); await page.goto(u); 'went'"
        try:
            engine.call("browser_repl", {"code": built})
            breach = None
        except ScopeBreach as error:
            breach = error
        ctx.check("a URL built at run time is caught after the call as a breach", breach is not None, breach)
        ctx.check("the breach really navigated (detection is after the fact)",
                  any(event.get("kind") == "page_load" for event in foreign.read()), foreign.read())
        ctx.check("the breach is recorded for the adapter, which fails the trial", engine.audit_repl() == engine.breach
                  and engine.repl_audit["breach"], engine.repl_audit)
        try:
            engine.call("browser_snapshot", {"tab_id": audit["tabs_adopted"][0]})
            locked = False
        except ScopeBreach:
            locked = True
        ctx.check("after a breach every later call is refused", locked)


if __name__ == "__main__":
    harness.SCENARIOS.clear()
    harness.SCENARIOS.append(repl_scope)
    run = harness.Run(browser=True, installed=False)
    sys.exit(0 if run.run([]) else 1)
