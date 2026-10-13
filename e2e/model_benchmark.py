#!/usr/bin/env python3
"""Run seeded workspace model trials, serially and interleaved across drivers.

--list reads versions and prints the schedule without launching any desktop or model.
Every real trial retains the existing independent judges and disposable fixtures.
Legacy regression tasks remain available through explicit --task selections.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib

import best_in_class as report

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent


def runner_fingerprint():
    digest = hashlib.sha256()
    for path in [ROOT / "model_benchmark.py", ROOT / "model_efficiency.py", ROOT / "model_sweep.py", ROOT / "best_in_class.py", ROOT / "run.py", ROOT / "start-browser.py",
                 *(ROOT / "benchmarks" / name for name in
                 ("model_adapter.py", "model_host.py", "cursor_model_host.py", "engines.py", "completion_timing.py", "tool_surface.py",
                  "workspace.html", "workspace_fixtures.py", "workspace_driver.py",
                  "flight.html", "flight_fixtures.py", "flight_driver.py")),
                 *(ROOT / "cursor_host" / name for name in ("bridge.mjs", "package.json", "package-lock.json"))]:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    # The disposable browser loads the repo/override extension, not the copy
    # inside the pinned app bundle. Hash its content without its local path.
    digest.update(b"\0effective-browser-extension\0")
    digest.update(report.extension_identity()["sha256"].encode("ascii"))
    return digest.hexdigest()


def version(command):
    result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise ValueError(f"Cannot determine version of {command}")
    return result.stdout.strip()


def cursor_host_version(*, sdk_root=None, node_binary="node"):
    from benchmarks.cursor_model_host import host_version
    return host_version(sdk_root=sdk_root, node_binary=node_binary)


def configured_model():
    path = pathlib.Path.home() / ".codex/config.toml"
    config = tomllib.loads(path.read_text())
    # Read only the public model choice. Never copy authentication or MCP config.
    return config.get("model"), config.get("model_reasoning_effort")


def browser_identity():
    spec = importlib.util.spec_from_file_location("benchmark_start_browser", ROOT / "start-browser.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.browser_identity()


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--engine", choices=("both", "second-cursor", "cua-driver"), default="both")
    cli.add_argument("--model")
    cli.add_argument("--model-host", choices=report.MODEL_HOSTS, default="codex")
    cli.add_argument("--model-param", action="append", help="Explicit Cursor model parameter KEY=VALUE; repeat per parameter")
    cli.add_argument("--node-binary", default=shutil.which("node") or "node")
    cli.add_argument("--cursor-sdk-root", help="Directory containing the pinned Cursor SDK bridge and dependency install")
    cli.add_argument("--reasoning-effort", choices=report.REASONING_EFFORTS)
    cli.add_argument("--configured-model", action="store_true", help="Pin this user's configured Codex model and effort")
    cli.add_argument("--rounds", type=int, default=3)
    cli.add_argument("--task", action="append", choices=report.TASKS,
                     help="Defaults to workspace reconciliation, handoff and recovery; repeat to select explicit tasks")
    cli.add_argument("--seed", type=int, default=1, help="First workspace case seed or legacy trial ID; does not seed provider sampling")
    placement = cli.add_mutually_exclusive_group()
    placement.add_argument("--native-placement", choices=("desktop", "agent-display", "textedit-off-space"),
                           default="desktop", help="Native fixture placement for both drivers (default: desktop)")
    placement.add_argument("--off-space", action="store_const", dest="native_placement", const="textedit-off-space",
                           help="Alias for --native-placement textedit-off-space")
    cli.add_argument("--max-steps", type=int, default=100)
    cli.add_argument("--observation-mode", choices=("on-demand", "upfront"), default="on-demand")
    cli.add_argument("--catalog-mode", choices=("full", "compact"), default="full")
    cli.add_argument("--browser-surface", choices=report.BROWSER_SURFACES, default="discrete",
                     help="repl also offers browser_repl beside the discrete browser tools (second-cursor browser tasks only)")
    cli.add_argument("--timeout", type=float, default=300, help="Per-trial solver budget in seconds (default: 300)")
    cli.add_argument("--codex-binary", default=shutil.which("codex") or "codex")
    cli.add_argument("--cua-binary", default=shutil.which("cua-driver") or "cua-driver")
    cli.add_argument("--list", action="store_true")
    return cli


def build_plan(args):
    selection = report.model_selection_for(args)
    host_kind = getattr(args, "model_host", "codex")
    if args.configured_model:
        if host_kind != "codex":
            raise ValueError("--configured-model is supported only for the Codex model host")
        if args.model or args.reasoning_effort:
            raise ValueError("Use either --configured-model or explicit --model/--reasoning-effort")
        args.model, args.reasoning_effort = configured_model()
    if args.rounds < 1 or args.seed < 0:
        raise ValueError("rounds must be positive and seed must be nonnegative")
    host_version = (cursor_host_version(sdk_root=getattr(args, "cursor_sdk_root", None),
                                       node_binary=getattr(args, "node_binary", "node"))
                    if host_kind == "cursor-sdk" else version(args.codex_binary))
    config = report.validate_model_config({"model": args.model, "reasoning_effort": args.reasoning_effort,
        "max_steps": args.max_steps, "timeout_seconds": args.timeout, "model_host_version": host_version, **selection})
    engines = ["second-cursor", "cua-driver"] if args.engine == "both" else [args.engine]
    observation_mode = getattr(args, "observation_mode", "on-demand")
    catalog_mode = getattr(args, "catalog_mode", "full")
    if observation_mode == "upfront" and (engines != ["second-cursor"] or args.task != ["native_notes"]):
        raise ValueError("upfront observations require second-cursor and only native_notes")
    if catalog_mode == "compact" and (engines != ["second-cursor"] or args.task != ["native_notes"]):
        raise ValueError("compact catalog requires second-cursor and only native_notes")
    browser_surface = getattr(args, "browser_surface", "discrete")
    if browser_surface == "repl" and (engines != ["second-cursor"] or not args.task or "native_notes" in args.task):
        raise ValueError("the repl browser surface requires second-cursor and explicit browser tasks")
    bundle = pathlib.Path(os.environ.get("SC_E2E_APP", str(pathlib.Path.home() / "Applications/SecondCursor.app")))
    binary = bundle / "Contents/MacOS/SecondCursor"
    if not binary.is_file():
        raise ValueError(f"Second Cursor fixture runtime is missing: {binary}")
    identities = {"second-cursor": "sha256:" + report.digest(binary)}
    if "cua-driver" in engines:
        identities["cua-driver"] = version(args.cua_binary)
    tasks = args.task or list(report.WORKSPACE_TASKS)
    if len(set(tasks)) != len(tasks):
        raise ValueError("Duplicate task selection")
    pinned_browser = browser_identity() if any(task != "native_notes" for task in tasks) else None
    runs = []
    for index in range(args.rounds):
        order = engines if index % 2 == 0 else list(reversed(engines))
        for engine in order:
            adapter = [sys.executable, str(ROOT / "benchmarks/model_adapter.py"), "--engine", engine,
                       "--codex-binary", args.codex_binary, "--cua-binary", args.cua_binary,
                       "--observation-mode", observation_mode, "--catalog-mode", catalog_mode,
                       "--browser-surface", browser_surface]
            if host_kind == "cursor-sdk":
                adapter.extend(["--model-host", host_kind, "--node-binary", args.node_binary])
                if args.cursor_sdk_root:
                    adapter.extend(["--cursor-sdk-root", args.cursor_sdk_root])
            command = [sys.executable, str(ROOT / "best_in_class.py"), "tasks", "--rounds", "1",
                       "--adapter-command", json.dumps(adapter), "--engine", engine + "-model",
                       "--engine-version", identities[engine], "--model", config["model"],
                       "--reasoning-effort", config["reasoning_effort"], "--max-steps", str(args.max_steps),
                       "--timeout", str(args.timeout), "--model-host-version", host_version,
                       "--interface-track", "native-tools", "--observation-mode", observation_mode,
                       "--catalog-mode", catalog_mode, "--browser-surface", browser_surface,
                       "--seed", str(args.seed + index),
                       "--headed-browser", "--native-placement", args.native_placement,
                       "--browser-identity", json.dumps(pinned_browser), "--adapter-browser"]
            if host_kind == "cursor-sdk":
                command.extend(["--model-host", host_kind])
                for key, value in sorted(config["model_params"].items()):
                    command.extend(["--model-param", f"{key}={value}"])
            for task in tasks:
                command.extend(["--task", task])
            runs.append({"engine": engine, "round": index + 1, "case_seed": args.seed + index,
                         "command": command})
    metadata = report.metadata()
    return {"model_config": config, "engines": identities, "selected_engines": engines,
            "observation_mode": observation_mode, "catalog_mode": catalog_mode, "browser_surface": browser_surface,
            "tasks": tasks, "runs": runs, "runner_sha256": runner_fingerprint(),
            "trial_count": len(runs) * len(tasks), "interface_track": "native-tools",
            "contract": report.CONTRACT, "track": "host-background", "native_placement": args.native_placement,
            "browser_visibility": "headed",
            "browser_identity": pinned_browser,
            "extension_identity": metadata.get("extension_identity"),
            "fixture_sha256": metadata["fixture_sha256"], "task_sha256": metadata["task_sha256"],
            "case_seed_semantics": report.case_seed_semantics(tasks),
            "note": "Integration pilot. " + report.case_seed_semantics(tasks)}


def planned_artifact(plan, engine):
    """Use the preflight manifest, never the first child's claims, as the baseline."""
    return {"kind": "tasks", "execution_profile": "model", "engine": engine + "-model",
            "case_seed_semantics": plan.get("case_seed_semantics", report.case_seed_semantics(plan["tasks"])),
            "observation_mode": plan.get("observation_mode", "on-demand"),
            "catalog_mode": plan.get("catalog_mode", "full"),
            "browser_surface": plan.get("browser_surface", "discrete"),
            "engine_version": plan["engines"][engine], "model": plan["model_config"]["model"],
            "model_config": copy.deepcopy(plan["model_config"]),
            **{key: copy.deepcopy(plan[key]) for key in ("contract", "track", "native_placement", "interface_track",
                                          "fixture_sha256", "task_sha256", "runner_sha256",
                                          "browser_visibility", "browser_identity")}}


def validate_child(plan, item, payload, result):
    if not isinstance(payload, dict):
        raise ValueError("Child artifact is not a JSON object")
    report.validate_comparison([payload])
    for key, expected in planned_artifact(plan, item["engine"]).items():
        if payload.get(key) != expected:
            raise ValueError(f"Child {key} disagrees with the pinned plan")
    expected_schedule = sorted((task, 1, item["case_seed"]) for task in plan["tasks"])
    actual_schedule = sorted((row["task_id"], row["trial"], row["case_seed"]) for row in payload["trials"])
    if actual_schedule != expected_schedule or payload.get("rounds") != 1:
        raise ValueError("Child task/trial/case_seed schedule disagrees with the pinned plan")
    for row in payload["trials"]:
        if row.get("runner_sha256") != plan["runner_sha256"]:
            raise ValueError("Child trial runner fingerprint disagrees with the pinned plan")
        if row.get("status") not in {"PASS", "FAIL", "INCOMPLETE", "INCONCLUSIVE", "SKIP", "NOT_RUN"}:
            raise ValueError("Child trial has an invalid status")
    expected_status = "FAIL" if payload.get("error") else report.overall(payload["trials"])
    if payload.get("status") != expected_status:
        raise ValueError("Child status disagrees with its trial evidence")
    if result["timed_out"]:
        raise ValueError("Child exceeded its deadline; emitted evidence is incomplete")
    if result.get("cleanup_errors"):
        raise ValueError("Child process cleanup failed; emitted evidence is incomplete")
    if result["returncode"] != report.exit_code(expected_status):
        raise ValueError("Child exit code disagrees with its artifact status")
    # A harness error after otherwise passing rows is still a failed child. Keep
    # its raw artifact, but do not certify those rows as clean completed trials.
    if payload.get("error") and any(row["status"] == "PASS" for row in payload["trials"]):
        raise ValueError("Child reported a harness error alongside passing trial evidence")


def missing_trials(plan, item, entry):
    return [{"task_id": task, "trial": item["round"], "case_seed": item["case_seed"],
             "status": "INCOMPLETE", "metrics": {}, "runner_sha256": plan["runner_sha256"],
             "error": entry["error"], "child_artifact": entry.get("artifact"),
             "child_run": f"{item['engine']}-round-{item['round']}"} for task in plan["tasks"]]


def run_plan(plan):
    output = report.artifact_dir("model-pilot")
    report.write_json(output / "plan.json", plan)
    combined = {engine: {**planned_artifact(plan, engine), "trials": [], "rounds": 0,
                         "child_artifacts": [], "child_runs": [], "evidence_complete": True}
                for engine in plan["selected_engines"]}
    run_results = []
    for item in plan["runs"]:
        label = f"{item['engine']}-round-{item['round']}"
        print(f"Running {label}", flush=True)
        deadline = len(plan["tasks"]) * (plan["model_config"]["timeout_seconds"] + 35) + 90
        entry = {**item, "returncode": None, "timed_out": False, "status": "INCOMPLETE",
                 "stdout_log": f"{label}.stdout.log", "stderr_log": f"{label}.stderr.log"}
        result = {"stdout": "", "stderr": ""}
        joined = combined[item["engine"]]
        try:
            result = report.run_process(item["command"], deadline)
            entry.update(returncode=result["returncode"], timed_out=result["timed_out"])
            entry.update({key: result[key] for key in ("cleanup", "cleanup_errors") if key in result})
            matches = re.findall(r"^Benchmark: (.+)$", result["stdout"], re.M)
            if len(matches) != 1:
                raise ValueError("Child must produce exactly one task artifact path; comparison is incomplete")
            artifact = pathlib.Path(matches[0])
            entry["artifact"] = str(artifact)
            payload = json.loads(artifact.read_text())
            validate_child(plan, item, payload, result)
            for row in payload["trials"]:
                copied = copy.deepcopy(row)
                copied["trial"] = item["round"]
                copied["child_artifact"] = str(artifact)
                joined["trials"].append(copied)
            entry["status"] = payload["status"]
        except Exception as error:
            # Child failures must not erase the rest of the paired schedule.
            # Quarantine unvalidated rows and retain one explicit denominator
            # entry per planned case; raw artifact and logs remain reviewable.
            entry["error"] = f"{type(error).__name__}: {error}"
            joined["evidence_complete"] = False
            joined["trials"].extend(missing_trials(plan, item, entry))
        (output / entry["stdout_log"]).write_text(result.get("stdout", ""))
        (output / entry["stderr_log"]).write_text(result.get("stderr", ""))
        joined["rounds"] += 1
        if entry.get("artifact"):
            joined["child_artifacts"].append(entry["artifact"])
        joined["child_runs"].append(copy.deepcopy(entry))
        joined["status"] = ("FAIL" if any(run["status"] == "FAIL" for run in joined["child_runs"])
                            else report.overall(joined["trials"]))
        joined["summary"] = report.aggregate(joined["trials"])
        report.write_json(output / f"{item['engine']}.tasks.json", joined)
        run_results.append(entry)
        report.write_json(output / "runs.json", run_results)
        print(f"{label}: {entry.get('status', 'INCOMPLETE')}", flush=True)
    paths = [str(output / f"{engine}.tasks.json") for engine in plan["selected_engines"]]
    complete = all(payload["evidence_complete"] for payload in combined.values())
    lines = ["# Model-driven pilot", "", plan["note"], "", f"Scheduled trials: {plan['trial_count']}.",
             f"Model configuration: `{json.dumps(plan['model_config'], sort_keys=True)}`.", "",
             f"Browser visibility: `{plan['browser_visibility']}`. Native placement: `{plan['native_placement']}`.",
             f"Pinned browser: `{json.dumps(plan['browser_identity'], sort_keys=True)}`.", "",
             "| Engine | Passed / scheduled | Child evidence |", "| --- | ---: | --- |"]
    for engine, payload in combined.items():
        passed = sum(row["status"] == "PASS" for row in payload["trials"])
        lines.append(f"| {engine} | {passed}/{len(payload['trials'])} | [{engine}.tasks.json]({engine}.tasks.json) |")
    lines.extend(["", f"Browser surface: `{plan.get('browser_surface', 'discrete')}`. Mixed means browser_repl and another tool in one trial.", "",
                  "| Engine | Task | Round | Status | Answer correct | Model calls | browser_repl calls | Mixed | Wall s | Tool s | Calls by tool |",
                  "| --- | --- | ---: | --- | --- | ---: | ---: | --- | ---: | ---: | --- |"])
    seconds = lambda value: f"{value / 1000:.1f}" if report.finite_number(value) else "unavailable"
    for engine, payload in combined.items():
        for row in payload["trials"]:
            metrics = row.get("metrics", {})
            by_tool = ", ".join(f"{name} {count}" for name, count in metrics.get("calls_by_tool", {}).items())
            lines.append(f"| {engine} | {row['task_id']} | {row['trial']} | {row['status']} | {row.get('independent_task_success', 'unavailable')} | "
                         f"{metrics.get('model_tool_calls', 'unavailable')} | {metrics.get('repl_calls', 'unavailable')} | "
                         f"{metrics.get('mixed_tools', 'unavailable')} | {seconds(metrics.get('wall_ms'))} | {seconds(metrics.get('tool_ms'))} | {by_tool} |")
    if complete and len(paths) > 1:
        try:
            compared = subprocess.run([sys.executable, str(ROOT / "best_in_class.py"), "compare", *paths],
                                      capture_output=True, text=True, timeout=15)
            lines.extend(["", compared.stdout if compared.returncode == 0 else compared.stderr])
            complete = compared.returncode == 0
        except (OSError, subprocess.SubprocessError) as error:
            lines.extend(["", f"Comparison failed: {type(error).__name__}: {error}"])
            complete = False
    if not complete:
        lines.extend(["", "Evidence is incomplete or mismatched; no comparative conclusion is valid."])
    lines.extend(["", "A small task pilot does not establish superiority. Inspect independent judges, failures,"
                  " safety refusals and coexistence uncertainty in the linked artifacts."])
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print(f"Pilot: {output / 'report.md'}")
    return 0 if complete and all(p["status"] == "PASS" for p in combined.values()) else 2


def main():
    try:
        args = parser().parse_args()
        plan = build_plan(args)
        if args.list:
            print(json.dumps(plan, indent=2))
            return 0
        return run_plan(plan)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"Model benchmark: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
