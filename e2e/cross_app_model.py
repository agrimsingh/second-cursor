#!/usr/bin/env python3
"""A real AI host solves the cross-app task using Second Cursor's public MCP.

SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_model.py --takeover

Defaults: Sol 6.1 high, five unseen fixture seeds 101..105, fresh isolated host
conversation per seed. This is synthetic coexistence/fault evidence, not physical
typing or process-death coverage. All model tool calls and host lifecycles remain
available alongside independent browser, native-disk and foreground judges.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import sys
import time

from benchmarks.model_host import CodexModelHost, HostConfig, host_version
from benchmarks.workspace_fixtures import WorkspaceFixture
from cross_app_support import activity_overlapped, original_note
from cross_app_workflows import compile_probe, native_probe, write_json
from cross_app_model_support import (VERSION, REQUIRED_NATIVE_FAULTS, FaultController,
    digest, expected_summary, external_note, fault_schedule, goal_prompt,
    host_evidence_checks, judge_model_native)
from run import APP_BUNDLE, Run, SCENARIOS

ROOT = pathlib.Path(__file__).resolve().parent


def fingerprints():
    files = [ROOT / name for name in (
        "cross_app_model.py", "cross_app_model_support.py", "benchmarks/cross_app_engine.py",
        "cross_app_workflows.py", "cross_app_support.py", "coexistence_canary.py", "run.py",
        "fixtures/coexistence-canary.swift", "fixtures/cross-app-edit.swift",
        "benchmarks/model_host.py", "benchmarks/engines.py", "benchmarks/workspace_fixtures.py",
        "benchmarks/workspace.html")]
    # Both loaded runtime and source are pinned. Extension code is loaded from
    # source by the disposable browser, whereas native code comes from the app.
    files.extend(sorted((ROOT.parent / "extension").glob("*.js")))
    files.append(ROOT.parent / "extension/manifest.json")
    files.append(APP_BUNDLE / "Contents/MacOS/SecondCursor")
    result = {}
    for path in files:
        key = path.relative_to(ROOT.parent).as_posix() if path.is_relative_to(ROOT.parent) else "native-app-binary"
        if not path.is_file():
            raise FileNotFoundError(f"A required runtime/source fingerprint is unavailable: {path}")
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def trial(ctx, seed, config, frozen_sources, profile):
    from coexistence_canary import CoexistenceCanary
    from benchmarks.cross_app_engine import ScopedCrossAppEngine

    artifact = ctx.run.dir / ctx.name
    artifact.mkdir()
    sources_before = fingerprints()
    ctx.require("all model trials use the frozen source/runtime profile", sources_before == frozen_sources)
    write_json(artifact / "source-before.json", sources_before)
    write_json(artifact / "profile.json", profile)
    write_json(artifact / "browser-runtime.json", ctx.run.browser_runtime)
    write_json(artifact / "fault-schedule.json", {"version": VERSION, "seed": seed,
        "foreground": fault_schedule(seed), "native_faults": list(REQUIRED_NATIVE_FAULTS)})

    note, filename = original_note(seed), f"cross-app-model-handoff-{seed}.txt"
    window = ctx.launch_scratch_textedit(filename, note, on_agent_display=False)
    path = ctx.scratch / filename
    executable = compile_probe(ctx)
    native_probe(ctx, executable, window)
    server = WorkspaceFixture("workspace_recovery", seed, artifact).__enter__()
    ctx.cleanups.append(lambda: server.__exit__(None, None, None))
    canary = CoexistenceCanary(ctx)
    engine, fault, host, host_events, failure = None, None, {}, [], None
    trace_path = artifact / "model-trace.jsonl"
    trace_path.touch(mode=0o600)
    with trace_path.open("w") as trace:
        def record(source, event):
            # Only the fixture/public-tool event stream reaches here, never
            # provider configuration, authentication or reasoning contents.
            trace.write(json.dumps({"source": source, **event}, ensure_ascii=False, allow_nan=False) + "\n")
            trace.flush()

        def host_event(event):
            record("model_host", event)
            host_events.append(event)
            if event.get("type") == "tool":
                print(f"  seed {seed}: {len([row for row in host_events if row.get('type') == 'tool'])} "
                      f"{event.get('name')} {event.get('status')} "
                      f"({event.get('end_ms', 0) - event.get('start_ms', 0):.0f}ms)", flush=True)

        try:
            ctx.call("bind_target", action="release", target=f"window:{window['id']}")
            sid = ctx.call("session", action="create")["session_id"]
            ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
            ctx.call("task", action="create", session_id=sid, title=f"Model cross-app invoice handoff {seed}")
            tab = ctx.call("browser_open", session_id=sid, url=server.url)["tab"]["id"]
            ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", session_id=sid, tab_id=tab))
            fault = FaultController(seed=seed,
                task=lambda action: ctx.call("task", action=action, session_id=sid),
                probe=lambda text=None: native_probe(ctx, executable, window,
                    "read" if text is None else "append", text), canary=canary)
            engine = ScopedCrossAppEngine(ctx.mcp, sid, tab, server.url, window["id"], window["pid"],
                before_call=fault.before_call, after_call=fault.after_call,
                emit=lambda event: record("scoped_mcp", event), own_session=False)
            engine.start()
            tools = engine.tools()
            prompt = goal_prompt(server.prompt, tab_id=tab,
                                 window_id=window["id"], title=window["title"])
            write_json(artifact / "model-tool-catalog.json", tools)
            (artifact / "prompt.txt").write_text(prompt)
            trial_profile = {"profile_sha256": digest(profile), "seed": seed,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "public_mcp_catalog_sha256": digest(tools), "public_mcp_catalog_characters": len(json.dumps(tools)),
                "fixture": server.case_metadata(), "model_host": dataclasses.asdict(config),
                "sources_sha256": digest(frozen_sources)}
            write_json(artifact / "trial-profile.json", trial_profile)
            canary.start()
            canary.command("start_activity", interval_ms=150)
            fault.event("model_trial_started", seed=seed, profile=trial_profile)
            host = CodexModelHost(config).run(prompt, tools, engine.call, emit_event=host_event).as_dict()
            write_json(artifact / "host-result.json", host)
            # A host completion is not success. All fixture outcome checks below
            # execute regardless of the final model answer or host status.
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            record("harness", {"type": "error", "error": failure})
        finally:
            checks = {"harness_completed_without_exception": failure is None}
            evidence = {"seed": seed, "profile_sha256": digest(profile), "harness_error": failure,
                "scope": {"driver": "isolated AI host using public Second Cursor MCP schemas",
                    "foreground_activity": "synthetic in-process first-responder activity, not physical human typing",
                    "native_history": "independent AX before/after each permitted native mutation, not continuous TextEdit history",
                    "native_uncertainty": "successful public write response suppressed locally; not process/transport death",
                    "browser_uncertainty": "application-level stale revision and committed save response loss",
                    "freshness": "real task pause/resume invalidation, not value compare-and-swap"},
                "host": host}
            try:
                durable = server.read()
                evidence["workspace"] = durable
                checks.update({"workspace_" + key: value for key, value in server.judge(durable).items()})
            except Exception as error:
                checks["workspace_evidence_readable"] = False
                evidence["workspace_error"] = str(error)
            try:
                live = native_probe(ctx, executable, window)
                saved = path.read_text()
                # Save completion can lag the synchronous Cmd+S response. This
                # bounded observer wait does not dispatch another save or edit.
                deadline = time.monotonic() + 4
                while saved != live["after"] and time.monotonic() < deadline:
                    time.sleep(.05)
                    saved = path.read_text()
                checks.update(judge_model_native(original=note, external=external_note(seed),
                    summary=expected_summary(server), live=live["after"], saved=saved,
                    receipts=fault.native_receipts if fault else [],
                    required_faults=[event["id"] for event in fault_schedule(seed)] + list(REQUIRED_NATIVE_FAULTS),
                    fired_faults=fault.fired if fault else [], reconciled_by=fault.reconciled_by if fault else []))
                evidence["native"] = {"live": live, "saved": saved,
                    "receipts": fault.native_receipts if fault else [],
                    "external_edits": fault.external_receipts if fault else [],
                    "uncertain_write_observations": fault.reconciled_by if fault else []}
            except Exception as error:
                checks["native_evidence_readable"] = False
                evidence["native_error"] = str(error)
            try:
                canary.command("stop_activity")
                protection = canary.checkpoint("final independent model cross-app judgement")
                checks["canary_protected_throughout"] = protection["ok"]
                evidence["canary"] = protection
            except Exception as error:
                checks["canary_evidence_readable"] = False
                evidence["canary_error"] = str(error)
            for phase in ("browser", "native"):
                activity = fault.phase_activity if fault else {}
                checks["synthetic_activity_overlapped_" + phase] = activity_overlapped(
                    activity.get(phase + "_before"), activity.get(phase + "_after"))
            checks.update(host_evidence_checks(host, host_events, model=config.model,
                reasoning_effort=config.reasoning_effort, host_version=config.model_host_version))
            sources_after = fingerprints()
            checks["source_and_runtime_unchanged_during_trial"] = sources_after == frozen_sources
            checks["no_unfinished_scoped_dispatch"] = fault is not None and fault.pending is None
            evidence["phase_activity"] = fault.phase_activity if fault else {}
            evidence["checks"] = checks
            evidence["passed"] = bool(checks) and all(checks.values())
            write_json(artifact / "independent-result.json", evidence)
            write_json(artifact / "events.json", fault.events if fault else [])
            write_json(artifact / "disruptions-fired.json", fault.fired if fault else [])
            write_json(artifact / "source-after.json", sources_after)
            for label, passed in checks.items():
                ctx.check(label, passed)
            canary.stop()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model", default="gpt-6.1-sol")
    result.add_argument("--reasoning-effort", default="high", choices=("low", "medium", "high", "xhigh"))
    result.add_argument("--seed", type=int, default=101)
    result.add_argument("--rounds", type=int, default=5)
    result.add_argument("--max-steps", type=int, default=100)
    result.add_argument("--timeout", type=float, default=420)
    result.add_argument("--codex-binary", default="codex")
    result.add_argument("--takeover", action="store_true")
    result.add_argument("--list", action="store_true", help="Print the pinned plan without starting apps or models")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.rounds <= 20:
        raise SystemExit("--rounds must be between 1 and 20")
    fault_schedule(args.seed)
    fault_schedule(args.seed + args.rounds - 1)
    config = HostConfig(model=args.model, reasoning_effort=args.reasoning_effort,
        max_steps=args.max_steps, timeout_seconds=args.timeout, codex_binary=args.codex_binary,
        model_host_version=host_version(args.codex_binary))
    frozen_sources = fingerprints()
    profile = {"version": VERSION, "model_host": dataclasses.asdict(config),
        "seeds": list(range(args.seed, args.seed + args.rounds)), "sources": frozen_sources,
        "browser_visibility": "headed", "native_placement": "desktop", "prompt_policy": "goal-and-targets-only",
        "scope": "single existing native document and single isolated browser fixture tab"}
    if args.list:
        print(json.dumps(profile, indent=2))
        return 0
    if not args.takeover:
        raise SystemExit("This suite needs --takeover while the user is away from the Mac")
    SCENARIOS.clear()
    for seed in profile["seeds"]:
        def scenario(ctx, seed=seed):
            trial(ctx, seed, config, frozen_sources, profile)
        scenario.__name__ = f"cross_app_model_{seed}"
        SCENARIOS.append(scenario)
    run = Run(browser=True, installed=False, headed_browser=True)
    write_json(run.dir / "cross-app-model-profile.json", profile)
    run.notes.append("Goal-only model cross-app trial; synthetic foreground activity and MCP acknowledgement loss. "
                     "Independent judges, complete model trace and pinned public MCP catalog retained per seed.")
    ok = run.run([])
    return 0 if ok and len(run.results) == args.rounds and all(row["status"] == "PASS" for row in run.results) else 1


if __name__ == "__main__":
    sys.exit(main())
