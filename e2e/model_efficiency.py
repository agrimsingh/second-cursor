#!/usr/bin/env python3
"""Compare native observation or catalog strategies with fresh matched model trials.

Only Second Cursor's native_notes fixture runs. --rounds is the number of pairs;
order alternates AB/BA. --list is read-only and never launches a fixture or model.
"""
import argparse
import collections
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
from benchmarks.tool_surface import MUTATING_TOOLS, NATIVE_TOOLS, READ_TOOLS

EXPERIMENTS = {
    "observations": {"baseline": {"observation_mode": "on-demand", "catalog_mode": "full"},
                     "upfront": {"observation_mode": "upfront", "catalog_mode": "full"}},
    "tool-surface": {"upfront-full": {"observation_mode": "upfront", "catalog_mode": "full"},
                     "upfront-compact": {"observation_mode": "upfront", "catalog_mode": "compact"}},
}
MEASUREMENTS = ("wall_ms", "tool_ms", "model_tool_calls", "driver_tool_calls",
                "prefetch_tool_calls", "prefetch_ms", "verified_completion_ms", "post_verification_tail_ms",
                "advertised_tool_count", "advertised_schema_bytes", "original_schema_bytes")
MATCHED_FIELDS = ("model_config", "engines", "selected_engines", "tasks", "runner_sha256",
                  "trial_count", "interface_track", "contract", "track", "native_placement",
                  "browser_visibility", "browser_identity", "fixture_sha256", "task_sha256")


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--experiment", choices=EXPERIMENTS, default="observations",
                     help="Vary initial observations, or hold upfront observations fixed and vary the advertised tool catalog")
    cli.add_argument("--model")
    cli.add_argument("--reasoning-effort", choices=report.REASONING_EFFORTS)
    cli.add_argument("--configured-model", action="store_true")
    cli.add_argument("--rounds", type=int, default=5, help="Matched pairs; each pair runs both profiles")
    cli.add_argument("--seed", type=int, default=1, help="First case identity; fixtures and provider sampling are not randomized")
    cli.add_argument("--native-placement", choices=("desktop", "agent-display", "textedit-off-space"), default="desktop")
    cli.add_argument("--max-steps", type=int, default=40)
    cli.add_argument("--timeout", type=float, default=240)
    cli.add_argument("--codex-binary", default=shutil.which("codex") or "codex")
    cli.add_argument("--list", action="store_true")
    return cli


def build_plan(args):
    configurations = EXPERIMENTS[args.experiment]
    reference, candidate = configurations
    profile_plans = {}
    # Each preflight reopens runtime/source identity. A change between profile
    # preparations is an error, never an accidental second treatment.
    for profile, configuration in configurations.items():
        selected = copy.deepcopy(args)
        selected.engine, selected.task = "second-cursor", ["native_notes"]
        selected.cua_binary = "cua-driver"  # Unused by the fixed Second Cursor engine.
        selected.observation_mode = configuration["observation_mode"]
        selected.catalog_mode = configuration["catalog_mode"]
        profile_plans[profile] = pilot.build_plan(selected)
        for key, expected in configuration.items():
            if profile_plans[profile].get(key) != expected:
                raise ValueError(f"Efficiency profile {key} disagrees with the requested treatment")
    baseline = profile_plans[reference]
    for field in MATCHED_FIELDS:
        if profile_plans[candidate].get(field) != baseline.get(field):
            raise ValueError(f"Efficiency profile {field} changed during preflight")
    runs = []
    for index in range(args.rounds):
        order = list(configurations) if index % 2 == 0 else list(reversed(configurations))
        for profile in order:
            item = copy.deepcopy(profile_plans[profile]["runs"][index])
            item.update(profile=profile, **configurations[profile])
            runs.append(item)
    return {**{field: copy.deepcopy(baseline[field]) for field in MATCHED_FIELDS},
            "kind": "model-efficiency-plan", "experiment": args.experiment,
            "profile_plans": profile_plans, "profile_configurations": copy.deepcopy(configurations), "runs": runs,
            "profiles": {profile: configuration["observation_mode"] for profile, configuration in configurations.items()},
            "reference_profile": reference, "candidate_profile": candidate,
            "trial_count": len(runs), "pairs": args.rounds,
            "note": "Repeated fixed-task integration experiment; seeds identify pairs, not randomized fixtures or provider sampling. "
                    "Every profile uses fresh fixtures and a fresh model host. Upfront observations remain inside the measured solver wall clock."}


def valid_count(value):
    return type(value) is int and value >= 0


def validate_catalog(final, mode):
    if final.get("catalog_mode") != mode:
        raise ValueError("Adapter did not prove the pinned catalog_mode")
    catalog = final.get("tool_catalog")
    # Setup failures can precede catalog projection; a successful model trial
    # must prove the catalog it actually advertised, not merely a CLI choice.
    if catalog is None and final.get("status") != "completed":
        return
    if not isinstance(catalog, dict) or catalog.get("catalog_mode") != mode:
        raise ValueError("Adapter catalog metadata is missing or disagrees with its mode")
    names = catalog.get("advertised_tool_names")
    expected = READ_TOOLS | {"batch"} if mode == "compact" else NATIVE_TOOLS
    if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
            or len(names) != len(set(names)) or set(names) != expected
            or not valid_count(catalog.get("advertised_tool_count"))
            or catalog["advertised_tool_count"] != len(expected)
            or not valid_count(catalog.get("original_tool_count"))
            or catalog["original_tool_count"] != len(NATIVE_TOOLS)):
        raise ValueError("Adapter advertised catalog names or counts disagree with the treatment")
    hidden = catalog.get("hidden_tool_names")
    expected_hidden = MUTATING_TOOLS if mode == "compact" else set()
    if (not isinstance(hidden, list) or any(not isinstance(name, str) for name in hidden)
            or len(hidden) != len(set(hidden)) or set(hidden) != expected_hidden):
        raise ValueError("Adapter catalog hid unexpected tool capabilities")
    for key in ("catalog_sha256", "original_catalog_sha256"):
        if not isinstance(catalog.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", catalog[key]) is None:
            raise ValueError("Adapter catalog needs canonical schema hashes")
    for key in ("advertised_schema_bytes", "original_schema_bytes"):
        if not valid_count(catalog.get(key)) or catalog[key] == 0:
            raise ValueError("Adapter catalog needs positive serialized schema byte counts")
    if (type(catalog.get("schema_bytes_saved")) is not int
            or catalog["schema_bytes_saved"] != catalog["original_schema_bytes"] - catalog["advertised_schema_bytes"]):
        raise ValueError("Adapter catalog byte accounting is inconsistent")
    if mode == "full" and (catalog["catalog_sha256"] != catalog["original_catalog_sha256"]
                            or catalog["schema_bytes_saved"] != 0):
        raise ValueError("Full catalog must retain its original schemas unchanged")


def validate_child(plan, item, payload, process):
    pilot.validate_child(plan["profile_plans"][item["profile"]], item, payload, process)
    for key in ("observation_mode", "catalog_mode"):
        if payload.get(key) != item[key]:
            raise ValueError(f"Child {key} disagrees with the pinned efficiency profile")
    for row in payload["trials"]:
        final = row.get("adapter_result")
        if not isinstance(final, dict) or final.get("observation_mode") != item["observation_mode"]:
            raise ValueError("Adapter did not prove the pinned observation_mode")
        validate_catalog(final, item["catalog_mode"])
        for key in ("prefetch_tool_calls", "model_tool_calls", "driver_tool_calls"):
            if not valid_count(final.get(key)):
                raise ValueError(f"Adapter {key} needs a nonnegative integer")
        if final["driver_tool_calls"] != final["prefetch_tool_calls"] + final["model_tool_calls"]:
            raise ValueError("Adapter driver count disagrees with prefetch and model counts")
        prefetched = final["prefetch_tool_calls"]
        if item["observation_mode"] == "on-demand" and prefetched != 0:
            raise ValueError("On-demand profile must not perform hidden prefetch calls")
        if item["observation_mode"] == "upfront" and (prefetched > 2 or final.get("status") == "completed" and prefetched != 2):
            raise ValueError("Completed upfront profile must observe exactly both permitted native targets")
        events = row.get("adapter_tools")
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ValueError("Adapter tool events are required to audit all driver calls")
        origins = collections.Counter(event.get("origin") for event in events)
        if (set(origins) - {"model", "host-prefetch"} or len(events) != final["driver_tool_calls"]
                or origins["model"] != final["model_tool_calls"] or origins["host-prefetch"] != prefetched):
            raise ValueError("Adapter call counts disagree with normalized tool events")
        wall = row.get("metrics", {}).get("wall_ms")
        if not report.finite_number(wall):
            raise ValueError("Efficiency trials require measured solver wall time")
        for event in events:
            start, end = event.get("start_ms"), event.get("end_ms")
            if (not report.finite_number(start) or not report.finite_number(end)
                    or start > end or end > wall + 100):
                raise ValueError("Efficiency tool interval is outside the measured trial")
        for key in ("verified_completion_ms", "post_verification_tail_ms"):
            value = row.get("metrics", {}).get(key)
            if value is not None and (not report.finite_number(value) or value > wall):
                raise ValueError(f"{key} is outside the measured trial")


def measurement(values):
    known = [value for value in values if report.finite_number(value)]
    return {"median": statistics.median(known) if known else None,
            "p95": report.percentile(known, .95), "measured_trials": len(known)}


def metrics(row):
    measured = {key: row.get("metrics", {}).get(key) for key in MEASUREMENTS}
    final = row.get("adapter_result", {})
    for key in ("model_tool_calls", "driver_tool_calls", "prefetch_tool_calls"):
        measured[key] = final.get(key)
    catalog = final.get("tool_catalog") or {}
    for key in ("advertised_tool_count", "advertised_schema_bytes", "original_schema_bytes"):
        measured[key] = catalog.get(key)
    if isinstance(row.get("adapter_tools"), list):
        measured["prefetch_ms"] = report.merge_intervals([
            (event["start_ms"], event["end_ms"]) for event in row["adapter_tools"]
            if event["origin"] == "host-prefetch"])
    # Earlier action completion is retrospectively eligible only after both
    # the independent task judge and the whole trial pass. It is not a claim
    # that independent judging itself happened at this earlier timestamp.
    if row.get("status") != "PASS" or row.get("independent_task_success") is not True:
        measured["verified_completion_ms"] = None
        measured["post_verification_tail_ms"] = None
    return measured


def summarize(rows):
    values = [metrics(row) for row in rows]
    return {"scheduled": len(rows), "passed": sum(row["status"] == "PASS" for row in rows),
            "independent_verified": sum(row.get("independent_task_success") is True for row in rows),
            "statuses": dict(collections.Counter(row["status"] for row in rows)),
            **{key: measurement(value.get(key) for value in values) for key in MEASUREMENTS}}


def paired_summary(profiles, pairs, reference, candidate):
    baseline = {row["trial"]: row for row in profiles[reference]["trials"]}
    treatment = {row["trial"]: row for row in profiles[candidate]["trials"]}
    differences, ratios = [], []
    for pair in range(1, pairs + 1):
        left, right = baseline[pair], treatment[pair]
        if not all(row["status"] == "PASS" and row.get("independent_task_success") is True for row in (left, right)):
            continue
        a, b = left.get("metrics", {}).get("wall_ms"), right.get("metrics", {}).get("wall_ms")
        if report.finite_number(a) and report.finite_number(b) and b > 0:
            differences.append(a - b)
            ratios.append(a / b)
    # Time saved may be negative, so it must not pass through the duration
    # validator that intentionally rejects negative input timings.
    saved = {"median": statistics.median(differences) if differences else None,
             "p95": report.percentile(differences, .95), "measured_trials": len(differences)}
    return {"scheduled_pairs": pairs, "eligible_pairs": len(differences),
            "reference_profile": reference, "candidate_profile": candidate,
            "wall_saved_ms": saved, "wall_speedup_ratio": measurement(ratios),
            "note": "Paired effects include only pairs where both trials pass all checks and independent task judging. "
                    "Profile timing summaries include every measured trial, including failures."}


def incomplete_row(plan, item, entry):
    return {"task_id": "native_notes", "trial": item["round"], "case_seed": item["case_seed"],
            "profile": item["profile"], "status": "INCOMPLETE", "metrics": {},
            "runner_sha256": plan["runner_sha256"], "error": entry.get("error", "Planned trial has not completed"),
            "child_artifact": entry.get("artifact")}


def display(metric, total, *, p95=False):
    value = metric["p95" if p95 else "median"]
    return "unavailable" if value is None else f"{value:.1f} ({metric['measured_trials']}/{total})"


def write_results(output, plan, profiles, entries):
    for profile, payload in profiles.items():
        payload["summary"] = summarize(payload["trials"])
        payload["evidence_complete"] = all(entry["validated"] for entry in entries if entry["profile"] == profile)
        payload["status"] = report.overall(payload["trials"])
        report.write_json(output / f"{profile}.tasks.json", payload)
    complete = all(payload["evidence_complete"] for payload in profiles.values())
    status = report.overall([row for payload in profiles.values() for row in payload["trials"]])
    paired = paired_summary(profiles, plan["pairs"], plan["reference_profile"], plan["candidate_profile"])
    result = {"kind": "model-efficiency", "experiment": plan["experiment"], "status": status, "evidence_complete": complete,
              "profiles": profiles, "paired": paired, "runs": entries}
    report.write_json(output / "results.json", result)
    report.write_json(output / "runs.json", entries)
    lines = ["# Native model efficiency experiment", "", plan["note"], "",
             f"Treatment: {plan['candidate_profile']} versus {plan['reference_profile']}.", "",
             f"Model configuration: `{json.dumps(plan['model_config'], sort_keys=True)}`.", "",
             "Whole-task wall time includes prefetch, model and tool work, final model response, and adapter cleanup. "
             "It excludes identical fixture setup and independent judging. Whole-task time uses the harness clock; verified edit and completion tail use the adapter clock and omit subprocess startup. "
             "Metric sample counts are shown in parentheses; unavailable values never count as zero.", "",
             "| Profile | Passed / scheduled | Wall median ms | Wall p95 ms | Model calls median | Driver calls median | Prefetch median ms | Verified edit median ms | Completion tail median ms |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for profile, payload in profiles.items():
        summary = payload["summary"]
        count = summary["scheduled"]
        fields = [display(summary[key], count) for key in ("wall_ms",)]
        fields.append(display(summary["wall_ms"], count, p95=True))
        fields.extend(display(summary[key], count) for key in ("model_tool_calls", "driver_tool_calls", "prefetch_ms", "verified_completion_ms", "post_verification_tail_ms"))
        lines.append(f"| [{profile}]({profile}.tasks.json) | {summary['passed']}/{count} | " + " | ".join(fields) + " |")
    lines.extend(["", "| Profile | Advertised tools median | Original schema bytes median | Advertised schema bytes median |",
                  "| --- | ---: | ---: | ---: |"])
    for profile, payload in profiles.items():
        summary = payload["summary"]
        fields = [display(summary[key], summary["scheduled"]) for key in
                  ("advertised_tool_count", "original_schema_bytes", "advertised_schema_bytes")]
        lines.append(f"| {profile} | " + " | ".join(fields) + " |")
    lines.extend(["", "Catalog sizes are canonical UTF-8 schema bytes, not provider token counts. Full catalog hashes and hidden tool names remain in each trial's adapter evidence."])
    lines.extend(["", "Driver calls include host prefetch and model-requested calls; moving a read upfront does not remove its execution cost. "
                  "Verified edit timing requires actual tool-result evidence and a later independent PASS. The judge runs after model completion, so this does not measure when independent verification itself finished.", "",
                  f"Eligible successful pairs: {paired['eligible_pairs']}/{paired['scheduled_pairs']}. "
                  f"Median paired wall time saved by {plan['candidate_profile']}: {display(paired['wall_saved_ms'], paired['scheduled_pairs'])} ms. "
                  "Negative time saved means the candidate was slower."])
    if not complete:
        lines.extend(["", "Evidence is incomplete or mismatched; no comparative conclusion is valid. Raw child artifacts and logs remain available."])
    lines.extend(["", "Small fixed-task samples cannot establish best-in-class performance; p95 is especially coarse with few pairs. "
                  "Failures and inconclusive coexistence checks remain in the scheduled denominator. Paired speed effects only describe eligible successful pairs."])
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return result


def run_plan(plan):
    output = report.artifact_dir("model-efficiency")
    report.write_json(output / "plan.json", plan)
    profiles = {profile: {**pilot.planned_artifact(plan["profile_plans"][profile], "second-cursor"),
                          "efficiency_profile": profile, "observation_mode": mode,
                          "rounds": plan["pairs"], "trials": []} for profile, mode in plan["profiles"].items()}
    entries = []
    for item in plan["runs"]:
        label = f"{item['profile']}-pair-{item['round']}"
        entry = {**item, "status": "INCOMPLETE", "validated": False, "returncode": None,
                 "timed_out": False, "stdout_log": f"{label}.stdout.log", "stderr_log": f"{label}.stderr.log"}
        entries.append(entry)
        profiles[item["profile"]]["trials"].append(incomplete_row(plan, item, entry))
    # Persist the full denominator before launching the first process. A hard
    # termination must leave an incomplete experiment, not a partial victory.
    write_results(output, plan, profiles, entries)
    for item, entry in zip(plan["runs"], entries):
        label = f"{item['profile']}-pair-{item['round']}"
        print(f"Running {label}", flush=True)
        process = {"stdout": "", "stderr": ""}
        stop = False
        try:
            deadline = plan["model_config"]["timeout_seconds"] + 35 + 90
            process = report.run_process(item["command"], deadline)
            entry.update({key: process[key] for key in ("returncode", "timed_out", "cleanup", "cleanup_errors") if key in process})
            matches = re.findall(r"^Benchmark: (.+)$", process["stdout"], re.M)
            if len(matches) != 1:
                raise ValueError("Child must produce exactly one task artifact path")
            entry["artifact"] = matches[0]
            payload = json.loads(pathlib.Path(matches[0]).read_text())
            validate_child(plan, item, payload, process)
            row = copy.deepcopy(payload["trials"][0])
            row.update(trial=item["round"], profile=item["profile"], child_artifact=matches[0])
            entry.update(status=payload["status"], validated=True)
        except KeyboardInterrupt:
            entry["error"] = "KeyboardInterrupt: experiment interrupted; remaining planned trials were not run"
            row, stop = incomplete_row(plan, item, entry), True
        except Exception as error:
            entry["error"] = f"{type(error).__name__}: {error}"
            row = incomplete_row(plan, item, entry)
        profiles[item["profile"]]["trials"][item["round"] - 1] = row
        for key in ("stdout", "stderr"):
            (output / entry[f"{key}_log"]).write_text(process.get(key, ""))
        result = write_results(output, plan, profiles, entries)
        print(f"{label}: {entry['status']}", flush=True)
        if stop:
            break
    print(f"Efficiency: {output / 'report.md'}")
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
        print(f"Model efficiency: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
