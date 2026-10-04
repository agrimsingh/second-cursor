#!/usr/bin/env python3
"""Browser reconciliation -> existing TextEdit document amid controlled activity.

SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_workflows.py --takeover --seed 41 --rounds 2

This is a deterministic public-tool driver, not a model reasoning benchmark.
It temporarily owns the desktop using disposable apps. Synthetic foreground
activity does not establish coexistence with physical human input. Browser save
uncertainty is application-level; native acknowledgement loss is injected in the
driver after a successful dispatch. Real process-loss coverage remains in
invoice_workflow.py. Native text freshness after pause/resume is not value CAS.
"""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time
import uuid

from benchmarks.workspace_driver import WorkspaceUI, cents, execute, fact
from benchmarks.workspace_fixtures import WorkspaceFixture
from cross_app_support import MODES, VERSION, activity_overlapped, judge_native, original_note, schedule, summary_line
from run import Run, SCENARIOS, ToolError, element_index


class LostAcknowledgement(Exception):
    pass


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


class Driver:
    """Only UI-derived facts enter the solver; fixture expected state stays outside."""

    def __init__(self, ctx, session, events, planned, canary, fired):
        self.ctx, self.session, self.events = ctx, session, events
        self.planned, self.canary, self.fired = planned, canary, fired
        self.browser_calls = 0

    def event(self, kind, **fields):
        self.events.append({"seq": len(self.events) + 1, "at_monotonic": time.monotonic(),
                            "kind": kind, **fields})

    def call(self, name, **args):
        started = time.monotonic()
        try:
            result = self.ctx.call(name, session_id=self.session, **args)
        except Exception as error:
            self.event("tool", name=name, args=args, started_at=started, ok=False,
                       error=f"{type(error).__name__}: {error}")
            raise
        self.event("tool", name=name, args=args, started_at=started, ok=True)
        if name.startswith("browser_"):
            self.browser_calls += 1
            for disruption in self.planned:
                if disruption.get("phase") != "browser" or disruption.get("after_call") != self.browser_calls:
                    continue
                self.canary.checkpoint("before " + disruption["id"])
                fields = {key: disruption[key] for key in ("window", "text") if key in disruption}
                receipt = self.canary.command(disruption["action"], **fields)
                self.fired.append(disruption["id"])
                self.event("disruption", plan=disruption, receipt=receipt)
        return result

    def read_native(self, wid):
        snapshot = self.call("get_window_state", window_id=wid, full=True, include_screenshot=False)
        area = element_index(snapshot["tree"], "AXTextArea")
        self.ctx.require("the current native document exposes one text area", area is not None, snapshot["tree"][:500])
        inspected = self.call("inspect_element", window_id=wid, element_index=area, offset=0, length=100000)
        return snapshot, area, inspected["text"]

    def append(self, wid, snapshot, area, current, text, lose_ack=False):
        # Match the complete current document, not a stale fixed offset or an
        # ambiguous repeated line. select_text uses UTF-16 internally.
        self.call("select_text", window_id=wid, element_index=area,
                  observation_id=snapshot["snapshot_id"], text=current,
                  selection_type="cursor_after", observe="none")
        operation = str(uuid.uuid4())
        self.event("native_summary_dispatch", window_id=wid, text=text, operation_id=operation)
        self.call("type_text", window_id=wid, element_index=area, text=text, observe="none", operation_id=operation)
        if lose_ack:
            self.event("native_acknowledgement_discarded", operation_id=operation,
                       scope="synthetic driver fault after successful public-tool dispatch")
            raise LostAcknowledgement(operation)


def native_probe(ctx, executable, window, action="read", text=None):
    command = [str(executable), str(window["pid"]), window["title"], action]
    if text is not None:
        command.append(text)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if completed.returncode:
        raise AssertionError("independent native AX observer failed: " + completed.stderr[-2000:])
    receipt = json.loads(completed.stdout)
    ctx.require("the independent observer reports the exact scratch native target",
                receipt["pid"] == window["pid"] and receipt["title"] == window["title"], receipt)
    return receipt


def compile_probe(ctx):
    executable = ctx.run.dir / "cross-app-edit"
    if executable.exists():
        return executable
    source = pathlib.Path(__file__).parent / "fixtures/cross-app-edit.swift"
    built = subprocess.run(["xcrun", "swiftc", str(source), "-o", str(executable)],
                           capture_output=True, text=True, timeout=120)
    ctx.require("independent scratch-document observer compiled", built.returncode == 0,
                (built.stderr or built.stdout)[-2000:])
    return executable


def refused(ctx, driver, label, pattern, **args):
    try:
        driver.call("set_value", value="FORBIDDEN STALE OVERWRITE", observe="none", **args)
    except ToolError as error:
        ctx.require(label, re.search(pattern, str(error), re.I) is not None, str(error))
        return
    raise AssertionError(label + ": mutation unexpectedly succeeded")


def pause_and_edit(ctx, driver, window, planned, executable, external_receipts):
    snapshot, area, before = driver.read_native(window["id"])
    paused = driver.call("task", action="pause")["task"]
    ctx.require("pause gates this task after its synchronous operations drained",
                paused["status"] == "paused" and paused["in_flight_count"] == 0, paused)
    driver.fired.append("native-pause")
    driver.event("disruption", action="task_pause", task=paused)
    args = dict(window_id=window["id"], element_index=area, observation_id=snapshot["snapshot_id"])
    paused_before = native_probe(ctx, executable, window)["after"]
    refused(ctx, driver, "a paused native write is refused", r"paused|pause", **args)
    ctx.require("independent native read: paused refusal has no document effect",
                native_probe(ctx, executable, window)["after"] == paused_before)
    for disruption in planned:
        if disruption["action"] == "external-edit":
            receipt = native_probe(ctx, executable, window, "append", disruption["text"])
            external_receipts.append(receipt)
            driver.fired.append(disruption["id"])
            driver.event("disruption", plan=disruption, receipt=receipt)
            ctx.require("the external edit appends to the actual current document",
                        receipt["before"] == before and receipt["after"] == before + disruption["text"], receipt)
    # Reading while paused must not grant mutation rights for the resumed task.
    paused_snapshot, paused_area, _ = driver.read_native(window["id"])
    resumed = driver.call("task", action="resume")["task"]
    ctx.require("resume explicitly requires a new target observation", resumed["requires_fresh_observation"], resumed)
    stale_before = native_probe(ctx, executable, window)["after"]
    refused(ctx, driver, "a read taken while paused cannot authorize the resumed write", r"fresh|read (?:the )?target|resum.*observ",
            window_id=window["id"], element_index=paused_area, observation_id=paused_snapshot["snapshot_id"])
    ctx.require("independent native read: stale refusal preserves the external correction",
                native_probe(ctx, executable, window)["after"] == stale_before)
    driver.event("disruption", action="task_resume", task=resumed)


def trial(ctx, seed, mode):
    from coexistence_canary import CoexistenceCanary

    artifact = ctx.run.dir / ctx.name
    artifact.mkdir()
    def fingerprints():
        root = pathlib.Path(__file__).resolve().parent
        sources = [root / name for name in ("cross_app_workflows.py", "cross_app_support.py",
            "coexistence_canary.py", "fixtures/coexistence-canary.swift", "fixtures/cross-app-edit.swift",
            "benchmarks/workspace_driver.py", "benchmarks/workspace_fixtures.py", "benchmarks/workspace.html")]
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sources if path.exists()}
    sources_before = fingerprints()
    write_json(artifact / "source-before.json", sources_before)
    write_json(artifact / "browser-runtime.json", ctx.run.browser_runtime)
    planned = schedule(seed, mode)
    write_json(artifact / "schedule.json", {"version": VERSION, "seed": seed, "mode": mode, "events": planned})
    events, fired, external_receipts, phase_activity = [], [], [], {}
    note = original_note(seed)
    filename = f"cross-app-handoff-{seed}.txt"
    window = ctx.launch_scratch_textedit(filename, note, on_agent_display=False)
    path = ctx.scratch / filename
    executable = compile_probe(ctx)
    native_probe(ctx, executable, window)
    # The fixture, rather than the solver, owns this private expected state.
    server = WorkspaceFixture("workspace_recovery", seed, artifact).__enter__()
    ctx.cleanups.append(lambda: server.__exit__(None, None, None))
    canary = CoexistenceCanary(ctx)
    driver = None
    try:
        ctx.call("bind_target", action="release", target=f"window:{window['id']}")
        sid = ctx.call("session", action="create")["session_id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
        ctx.call("task", action="create", session_id=sid, title=f"Cross-app invoice handoff {seed}")
        tab = ctx.call("browser_open", session_id=sid, url=server.url)["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", session_id=sid, tab_id=tab))
        # Foreground claims begin after setup; no setup activation is excused
        # after the canary starts. Its event counters capture brief focus theft.
        canary.start()
        canary.command("start_activity", interval_ms=150)
        phase_activity["browser_before"] = canary.state()
        driver = Driver(ctx, sid, events, planned, canary, fired)
        driver.event("start", seed=seed, mode=mode, native_window=window, browser_tab=tab)
        execute(ctx, "workspace_recovery", {"tab_id": tab, "goal": server.prompt}, driver)
        canary.checkpoint("browser reconciliation complete")
        phase_activity["browser_after"] = canary.state()
        ui = WorkspaceUI(ctx, {"tab_id": tab}, driver)
        detail = ui.detail()
        line = summary_line(fact(detail, "Invoice ID"), cents(fact(detail, "Saved approved amount")),
                            fact(detail, "Saved decision"), fact(detail, "Saved note"))
        phase_activity["native_before"] = canary.state()
        if any(event["action"] == "pause-resume" for event in planned):
            pause_and_edit(ctx, driver, window, planned, executable, external_receipts)
            canary.checkpoint("native pause and external edit complete")
        snapshot, area, current = driver.read_native(window["id"])
        lost = any(event["action"] == "lost-acknowledgement" for event in planned)
        try:
            driver.append(window["id"], snapshot, area, current, line, lose_ack=lost)
        except LostAcknowledgement as error:
            fired.append("native-lost-ack")
            operation = str(error)
            journal = driver.call("operation_status", operation_id=operation)
            ctx.require("runtime journal reconciles the exact uncertain native operation",
                        journal.get("state") == "completed" and journal.get("outcome_known") is True
                        and journal.get("result_expired") is not True, journal)
            driver.event("native_operation_reconciled", operation_id=operation, journal=journal)
            # Reconcile the runtime journal, then use a fresh public UI read to
            # decide against retry. The local fault is never called a disconnect.
            _, _, reconciled = driver.read_native(window["id"])
            ctx.require("uncertain native append is reconciled without resubmission", reconciled == current + line, reconciled)
            driver.event("native_uncertain_write_reconciled", retry_dispatched=False)
        _, _, actual = driver.read_native(window["id"])
        ctx.require("native append preserved the entire freshly observed document", actual == current + line, actual)
        driver.call("press_key", window_id=window["id"], keys="cmd+s", observe="none")
        deadline = time.monotonic() + 4
        while path.read_text() != actual and time.monotonic() < deadline:
            time.sleep(.05)
        canary.checkpoint("native append and save complete")
        phase_activity["native_after"] = canary.state()
        driver.call("browser_screenshot", tab_id=tab)
        driver.call("screenshot", window_id=window["id"])
        driver.call("task", action="update", status="completed")
    finally:
        # Judge before any fixture cleanup, even when a solver operation fails.
        evidence = {"seed": seed, "mode": mode, "scope": {
            "driver": "deterministic public tools, no model",
            "foreground_activity": "synthetic in-process fixture keyboard activity, not physical input",
            "browser_uncertainty": "application-level stale revision and committed response lost",
            "native_uncertainty": "synthetic local driver acknowledgement loss, not process/transport death",
            "native_freshness": "task pause/resume invalidation; no content compare-and-swap guarantee",
            "native_history": "exact final live and persisted text plus solver dispatch count; no continuous TextEdit mutation history"}}
        checks = {}
        try:
            durable = server.read()
            evidence["workspace"] = durable
            checks.update({"workspace_" + key: value for key, value in server.judge(durable).items()})
        except Exception as error:
            checks["workspace_evidence_readable"] = False
            evidence["workspace_error"] = str(error)
        try:
            receipt = native_probe(ctx, executable, window)
            saved = path.read_text()
            expected_invoice = next(row for row in server._expected_state()["sections"]["invoices"]
                                    if row["id"] == server._private["invoice_id"])
            expected_line = summary_line(expected_invoice["id"], expected_invoice["approved_amount_cents"],
                                         {"standard": "Standard", "finance_review": "Finance review", "hold": "Hold"}[expected_invoice["route"]],
                                         expected_invoice["note"])
            external = "".join(event["text"] for event in planned if event["action"] == "external-edit")
            checks.update(judge_native(original=note, external=external, summary=expected_line,
                live=receipt["after"], saved=saved, events=events,
                required_events=[event["id"] for event in planned], fired_events=fired))
            evidence["native"] = {"live": receipt, "saved": saved, "external_edits": external_receipts}
        except Exception as error:
            checks["native_evidence_readable"] = False
            evidence["native_error"] = str(error)
        try:
            canary.command("stop_activity")
            protection = canary.checkpoint("final independent cross-app judgement")
            checks["canary_protected_throughout"] = protection["ok"]
            evidence["canary"] = protection
        except Exception as error:
            checks["canary_evidence_readable"] = False
            evidence["canary_error"] = str(error)
        for phase in ("browser", "native"):
            before, after = phase_activity.get(phase + "_before"), phase_activity.get(phase + "_after")
            checks["synthetic_activity_overlapped_" + phase] = activity_overlapped(before, after)
        evidence["phase_activity"] = phase_activity
        sources_after = fingerprints()
        checks["fixture_sources_unchanged_during_trial"] = sources_after == sources_before
        evidence["checks"] = checks
        write_json(artifact / "independent-result.json", evidence)
        write_json(artifact / "events.json", events)
        write_json(artifact / "disruptions-fired.json", fired)
        write_json(artifact / "source-after.json", sources_after)
        for label, passed in checks.items():
            ctx.check(label, passed)
        canary.stop()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--seed", type=int, default=41)
    result.add_argument("--rounds", type=int, default=1)
    result.add_argument("--mode", choices=MODES, default="interleaved")
    result.add_argument("--takeover", action="store_true", help="Temporarily own the desktop with disposable canary windows")
    result.add_argument("--headed-browser", action="store_true", default=True, help="Use a visible isolated browser (the default)")
    result.add_argument("scenarios", nargs="*", help="Optional exact scenario names, for release runner compatibility")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.takeover:
        raise SystemExit("This suite needs --takeover while the user is away from the Mac")
    if not 1 <= args.rounds <= 20:
        raise SystemExit("--rounds must be between 1 and 20")
    schedule(args.seed + args.rounds - 1, args.mode)
    SCENARIOS.clear()
    for seed in range(args.seed, args.seed + args.rounds):
        def scenario(ctx, seed=seed):
            trial(ctx, seed, args.mode)
        scenario.__name__ = f"cross_app_{args.mode.replace('-', '_')}_{seed}"
        SCENARIOS.append(scenario)
    unknown = set(args.scenarios) - {scenario.__name__ for scenario in SCENARIOS}
    if unknown:
        raise SystemExit("Unknown generated scenario name(s): " + ", ".join(sorted(unknown)))
    run = Run(browser=True, installed=False, headed_browser=True)
    run.notes.append("Cross-app synthetic coexistence suite; seed schedules and independent outcomes retained per trial. No model or real physical user activity is involved.")
    ok = run.run(args.scenarios)
    # Run.run treats SKIP as nonfailure; this standalone proof must fail closed.
    return 0 if ok and run.results and all(row["status"] == "PASS" for row in run.results) else 1


if __name__ == "__main__":
    sys.exit(main())
