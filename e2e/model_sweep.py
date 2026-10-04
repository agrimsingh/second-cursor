#!/usr/bin/env python3
"""Compare pinned model and host configurations on seeded Second Cursor workspaces.

This is a model-plus-host comparison, not an isolated inference or driver speed
benchmark. --list only reads source/runtime identities; it starts no model or UI.
Legacy regression tasks remain available through explicit --task selections.
"""
import argparse
import copy
import json
import pathlib
import re
import shutil
import statistics
import subprocess
import sys

import best_in_class as report
import model_benchmark as pilot

PROFILES = {
    "grok-4.5-fast-low": {"model_host": "cursor-sdk", "model": "grok-4.5", "reasoning_effort": "low",
                          "model_params": {"effort": "low", "fast": "true"}},
    "grok-4.5-fast-medium": {"model_host": "cursor-sdk", "model": "grok-4.5", "reasoning_effort": "medium",
                             "model_params": {"effort": "medium", "fast": "true"}},
    "grok-4.7-fast-medium": {"model_host": "cursor-sdk", "model": "grok-4.7", "reasoning_effort": "medium",
                             "model_params": {"reasoning_effort": "medium", "fast": "true", "context": "256k"}},
    "sol-6.1-medium": {"model_host": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "medium"},
    "sol-6.1-high": {"model_host": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
    "opus-5.5-fast-low": {"model_host": "cursor-sdk", "model": "claude-opus-5-5", "reasoning_effort": "low",
                          "model_params": {"context": "300k", "effort": "low", "fast": "true"}},
    "opus-5.5-fast-medium": {"model_host": "cursor-sdk", "model": "claude-opus-5-5", "reasoning_effort": "medium",
                             "model_params": {"context": "300k", "effort": "medium", "fast": "true"}},
}
DEFAULT_PROFILES = ("grok-4.5-fast-low", "grok-4.5-fast-medium", "grok-4.7-fast-medium",
                    "sol-6.1-medium", "sol-6.1-high")
VARYING_CONFIG = {"model_host", "model", "reasoning_effort", "model_params", "model_host_version"}
MATCHED_FIELDS = ("engines", "selected_engines", "tasks", "runner_sha256", "trial_count", "interface_track",
                  "contract", "track", "native_placement", "browser_visibility", "browser_identity",
                  "fixture_sha256", "task_sha256", "observation_mode", "catalog_mode")
MEASUREMENTS = ("wall_ms", "tool_ms", "verified_completion_ms", "post_verification_tail_ms",
                "model_tool_calls", "driver_tool_calls", "prefetch_tool_calls")


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--rounds", type=int, default=3)
    cli.add_argument("--profile", action="append", choices=PROFILES,
                     help="Run only these profiles, in the supplied order; repeat to select multiple. "
                          "Defaults to the original five Grok/Sol profiles.")
    cli.add_argument("--task", action="append", choices=report.TASKS,
                     help="Defaults to workspace reconciliation, handoff and recovery; repeat to select explicit tasks")
    cli.add_argument("--seed", type=int, default=1, help="First generated workspace case seed or legacy trial ID; not a model sampling seed")
    cli.add_argument("--native-placement", choices=("desktop", "agent-display", "textedit-off-space"), default="desktop")
    cli.add_argument("--timeout", type=float, default=300)
    cli.add_argument("--max-steps", type=int, default=100)
    cli.add_argument("--codex-binary", default=shutil.which("codex") or "codex")
    cli.add_argument("--node-binary", default=shutil.which("node") or "node")
    cli.add_argument("--cursor-sdk-root", help="Pinned Cursor SDK bridge/dependency directory; defaults to e2e/cursor_host")
    cli.add_argument("--list", action="store_true")
    return cli


def build_schedule(profiles, rounds, seed):
    """Pure deterministic schedule: fresh profile trial per round, alternating order."""
    names = list(profiles)
    if (type(rounds) is not int or rounds < 1 or type(seed) is not int or seed < 0
            or not names or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("Unique profiles, positive rounds and a nonnegative integer seed are required")
    return [{"profile": profile, "round": index + 1, "case_seed": seed + index}
            for index in range(rounds) for profile in (names if index % 2 == 0 else list(reversed(names)))]


def select_profiles(names=None):
    names = list(DEFAULT_PROFILES if names is None else names)
    if not names or any(not isinstance(name, str) or name not in PROFILES for name in names):
        raise ValueError("Select at least one known model profile")
    if len(set(names)) != len(names):
        raise ValueError("Selected model profiles must be unique")
    return {name: copy.deepcopy(PROFILES[name]) for name in names}


def validate_profile_plans(plans, profile_names=None):
    profiles = select_profiles(profile_names)
    if list(plans) != list(profiles):
        raise ValueError("Profile plans must match the selected profiles and their order")
    baseline = next(iter(plans.values()))
    shared_config = {key: value for key, value in baseline["model_config"].items() if key not in VARYING_CONFIG}
    host_versions = {}
    for profile, plan in plans.items():
        config = report.validate_model_config(plan["model_config"])
        expected = profiles[profile]
        actual = {"model_host": config.get("model_host", "codex"), "model": config["model"],
                  "reasoning_effort": config["reasoning_effort"]}
        if "model_params" in config:
            actual["model_params"] = config["model_params"]
        if actual != expected:
            raise ValueError(f"Profile {profile} changed its declared model configuration")
        for field in MATCHED_FIELDS:
            if plan.get(field) != baseline.get(field):
                raise ValueError(f"Profile {profile} changed shared {field} during preflight")
        if {key: value for key, value in config.items() if key not in VARYING_CONFIG} != shared_config:
            raise ValueError(f"Profile {profile} changed shared model budget or sampling configuration")
        host = actual["model_host"]
        if host in host_versions and host_versions[host] != config["model_host_version"]:
            raise ValueError(f"Profile {profile} changed the version of host {host}")
        host_versions[host] = config["model_host_version"]


def build_plan(args):
    profiles = select_profiles(args.profile)
    schedule = build_schedule(profiles, args.rounds, args.seed)
    plans = {}
    for profile, config in profiles.items():
        selected = copy.deepcopy(args)
        selected.engine = "second-cursor"
        selected.cua_binary = "cua-driver"
        selected.configured_model = False
        selected.task = args.task or list(report.WORKSPACE_TASKS)
        selected.catalog_mode = "full"
        selected.observation_mode = "upfront" if selected.task == ["native_notes"] else "on-demand"
        selected.model_host, selected.model = config["model_host"], config["model"]
        selected.reasoning_effort = config["reasoning_effort"]
        selected.model_param = [f"{key}={value}" for key, value in config.get("model_params", {}).items()]
        plans[profile] = pilot.build_plan(selected)
    validate_profile_plans(plans, profiles)
    baseline = next(iter(plans.values()))
    runs = []
    for slot in schedule:
        item = copy.deepcopy(plans[slot["profile"]]["runs"][slot["round"] - 1])
        if item["round"] != slot["round"] or item["case_seed"] != slot["case_seed"]:
            raise ValueError("Profile child schedule disagrees with the matched round")
        runs.append({**item, **slot})
    return {**{field: copy.deepcopy(baseline[field]) for field in MATCHED_FIELDS},
            "kind": "model-host-sweep-plan", "comparison_axis": "model-plus-host",
            "profiles": profiles, "profile_plans": plans, "runs": runs,
            "rounds": args.rounds, "trial_count": len(runs) * len(baseline["tasks"]),
            "note": "Model-plus-host comparison: Cursor SDK and Codex have different orchestration and tool framing. "
                    "Every trial starts fresh fixtures and a fresh host. " + report.case_seed_semantics(baseline["tasks"]) + " "
                    "Round order alternates forward/reverse, which limits but does not "
                    "eliminate order and provider-cache effects, especially with an odd number of rounds."}


def validate_child(plan, item, payload, process):
    # The ordinary comparator remains strict: validate each child against its
    # own exact preflight profile, never compare unlike model configs with it.
    pilot.validate_child(plan["profile_plans"][item["profile"]], item, payload, process)


def summarize(rows):
    eligible = [row for row in rows if row["status"] == "PASS" and row.get("independent_task_success") is True]
    statuses = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    summary = {"scheduled": len(rows), "passed": statuses.get("PASS", 0),
               "independent_verified": sum(row.get("independent_task_success") is True for row in rows),
               "statuses": statuses, "latency_eligible": len(eligible)}
    for key in MEASUREMENTS:
        source = "adapter_result" if key.endswith("tool_calls") else "metrics"
        values = [row.get(source, {}).get(key) for row in eligible]
        known = [value for value in values if report.finite_number(value)]
        summary[key] = {"median": statistics.median(known) if known else None, "measured_trials": len(known)}
    return summary


def missing_rows(plan, item, entry, status="NOT_RUN"):
    return [{"task_id": task, "trial": item["round"], "case_seed": item["case_seed"],
             "profile": item["profile"], "status": status, "metrics": {},
             "runner_sha256": plan["runner_sha256"], "error": entry.get("error", "Planned trial has not run"),
             "child_artifact": entry.get("artifact")} for task in plan["tasks"]]


def display(metric):
    return "unavailable" if metric["median"] is None else f"{metric['median'] / 1000:.3f} ({metric['measured_trials']})"


def write_results(output, plan, profiles, entries):
    for profile, payload in profiles.items():
        payload["summary"] = summarize(payload["trials"])
        payload["by_task"] = {task: summarize([row for row in payload["trials"] if row["task_id"] == task])
                              for task in plan["tasks"]}
        payload["evidence_complete"] = all(entry["validated"] for entry in entries if entry["profile"] == profile)
        payload["status"] = report.overall(payload["trials"])
        report.write_json(output / f"{profile}.tasks.json", payload)
    rows = [row for payload in profiles.values() for row in payload["trials"]]
    result = {"kind": "model-host-sweep", "comparison_axis": "model-plus-host", "scheduled": plan["trial_count"],
              "status": report.overall(rows), "evidence_complete": all(e["validated"] for e in entries),
              "runner_sha256": plan["runner_sha256"], "profiles": profiles, "runs": entries, "note": plan["note"]}
    report.write_json(output / "results.json", result)
    lines = ["# Model and host comparison", "", plan["note"], "",
             "Latency medians include only PASS trials with independent task success. Failure, inconclusive, "
             "setup-skip and missing trials stay in the scheduled denominator. Independent task success is reported "
             "separately from coexistence checks. Counts in parentheses are timing samples.", "",
             "Whole-task wall time includes host startup, prefetch, model/tool execution, final response and cleanup; "
             "fixture setup and later independent judging are outside that clock. Verified completion is retrospective "
             "tool-result evidence, not the time at which the independent judge ran.", "",
             "| Configuration | Passed / scheduled | Independent task successes | Status counts | Wall median s | Verified completion median s |",
             "| --- | ---: | ---: | --- | ---: | ---: |"]
    for profile, payload in profiles.items():
        summary = payload["summary"]
        lines.append(f"| [{profile}]({profile}.tasks.json) | {summary['passed']}/{summary['scheduled']} | "
                     f"{summary['independent_verified']}/{summary['scheduled']} | {json.dumps(summary['statuses'], sort_keys=True)} | "
                     f"{display(summary['wall_ms'])} | {display(summary['verified_completion_ms'])} |")
    if len(plan["tasks"]) > 1:
        lines.extend(["", "Aggregate medians mix task types; use the per-task summaries in results.json for task-specific comparisons."])
    lines.extend(["", "Small task samples do not establish an overall winner or general model intelligence. "
                  "A configuration with failed or missing trials cannot be declared superior from its successful-only latency. "
                  "Raw per-trial judges, host isolation, usage, tool traces and pinned model parameters remain in the child artifacts."])
    if not result["evidence_complete"]:
        lines.extend(["", "Evidence is incomplete or mismatched; remaining planned trials remain visible and no overall comparative conclusion is valid."])
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return result


def run_plan(plan):
    output = report.artifact_dir("model-sweep")
    report.write_json(output / "plan.json", plan)
    profiles = {name: {**pilot.planned_artifact(child, "second-cursor"), "profile": name,
                       "rounds": plan["rounds"], "trials": []} for name, child in plan["profile_plans"].items()}
    entries = []
    for item in plan["runs"]:
        label = f"{item['profile']}-round-{item['round']}"
        entry = {**item, "status": "NOT_RUN", "validated": False, "returncode": None, "timed_out": False,
                 "stdout_log": f"{label}.stdout.log", "stderr_log": f"{label}.stderr.log"}
        entries.append(entry)
        profiles[item["profile"]]["trials"].extend(missing_rows(plan, item, entry))
    # Persist every scheduled case before any side effect, including hard-stop cases.
    result = write_results(output, plan, profiles, entries)
    for item, entry in zip(plan["runs"], entries):
        label = f"{item['profile']}-round-{item['round']}"
        print(f"Running {label}", flush=True)
        process = {"stdout": "", "stderr": ""}
        stop = False
        entry["status"] = "INCOMPLETE"
        try:
            timeout = plan["profile_plans"][item["profile"]]["model_config"]["timeout_seconds"]
            process = report.run_process(item["command"], len(plan["tasks"]) * (timeout + 35) + 90)
            entry.update({key: process[key] for key in ("returncode", "timed_out", "cleanup", "cleanup_errors") if key in process})
            matches = re.findall(r"^Benchmark: (.+)$", process["stdout"], re.M)
            if len(matches) != 1:
                raise ValueError("Child must produce exactly one task artifact path")
            entry["artifact"] = matches[0]
            payload = json.loads(pathlib.Path(matches[0]).read_text())
            validate_child(plan, item, payload, process)
            rows = [{**copy.deepcopy(row), "trial": item["round"], "profile": item["profile"],
                     "child_artifact": matches[0]} for row in payload["trials"]]
            entry.update(status=payload["status"], validated=True)
        except KeyboardInterrupt:
            entry["error"] = "KeyboardInterrupt: interrupted; remaining planned trials were not run"
            rows, stop = missing_rows(plan, item, entry, "INCOMPLETE"), True
        except Exception as error:
            entry["error"] = f"{type(error).__name__}: {error}"
            rows = missing_rows(plan, item, entry, "INCOMPLETE")
        joined = profiles[item["profile"]]["trials"]
        offset = (item["round"] - 1) * len(plan["tasks"])
        joined[offset:offset + len(plan["tasks"])] = rows
        for key in ("stdout", "stderr"):
            (output / entry[f"{key}_log"]).write_text(process.get(key, ""))
        result = write_results(output, plan, profiles, entries)
        print(f"{label}: {entry['status']}", flush=True)
        if stop:
            break
    print(f"Sweep: {output / 'report.md'}")
    return 0 if result["evidence_complete"] and result["status"] == "PASS" else 2


def main():
    try:
        args = parser().parse_args()
        plan = build_plan(args)
        if args.list:
            print(json.dumps(plan, indent=2))
            return 0
        return run_plan(plan)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"Model sweep: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
