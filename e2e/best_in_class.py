#!/usr/bin/env python3
"""Strict serial release gate and independently judged complete-task benchmark.

No UI is touched by --list, compare, or self-test. See docs/best-in-class-acceptance.md.
"""
import argparse
import collections
import datetime
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import signal
import statistics
import subprocess
import sys
import time

from benchmarks.flight_fixtures import FLIGHT_TASKS
from benchmarks.workspace_fixtures import WORKSPACE_TASKS

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
CONTRACT = "second-cursor-benchmark/1"
DEFAULT_TASKS = ("native_notes", "browser_work_order", "canvas_route", "transient_dialog")
TASKS = DEFAULT_TASKS + WORKSPACE_TASKS + FLIGHT_TASKS
FIXTURE_FILES = ("fixture.html", "fixtures.py", "workspace.html", "workspace_fixtures.py", "flight.html", "flight_fixtures.py")
TASK_FILES = (*FIXTURE_FILES, "tasks.py", "workspace_driver.py", "flight_driver.py")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
INTERFACE_TRACKS = ("native-tools", "screenshot-actions")
MODEL_CONFIG_FIELDS = ("model", "reasoning_effort", "max_steps", "timeout_seconds", "model_host_version")
MODEL_HOSTS = ("codex", "cursor-sdk")


def case_seed_semantics(tasks):
    if any(task in WORKSPACE_TASKS or task in FLIGHT_TASKS for task in tasks):
        return ("Workspace and flight seeds generate reproducible case data; legacy task fixtures remain fixed. "
                "No case seed controls provider sampling.")
    return "Trial identity only; this pilot uses fixed fixtures and does not seed provider sampling."


def finite_number(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def pinned_identifier(value):
    """Require a declared version/fingerprint rather than silently accepting unknown evidence."""
    return (isinstance(value, str) and bool(value.strip())
            and value.strip().lower() not in {"unknown", "unavailable", "unversioned", "latest", "default", "main", "head"})


def validate_model_config(config):
    if not isinstance(config, dict):
        raise ValueError("Model trials need an explicit model_config object")
    missing = [key for key in MODEL_CONFIG_FIELDS if config.get(key) is None]
    if missing:
        raise ValueError(f"Model configuration is not pinned; missing {', '.join(missing)}")
    if not pinned_identifier(config["model"]):
        raise ValueError("Model configuration needs an explicit model identifier")
    if config["reasoning_effort"] not in REASONING_EFFORTS:
        raise ValueError("Model configuration needs an explicit supported reasoning_effort")
    if not isinstance(config["max_steps"], int) or isinstance(config["max_steps"], bool) or config["max_steps"] < 1:
        raise ValueError("Model max_steps must be a positive integer")
    if not finite_number(config["timeout_seconds"], positive=True):
        raise ValueError("Model timeout_seconds must be finite and positive")
    if not pinned_identifier(config["model_host_version"]):
        raise ValueError("Model configuration needs an explicit model_host_version fingerprint")
    host = config.get("model_host", "codex")
    if host not in MODEL_HOSTS:
        raise ValueError("Unknown model host")
    if host == "codex" and "model_params" in config:
        raise ValueError("Codex does not accept Cursor model parameters")
    if host == "cursor-sdk":
        params = config.get("model_params")
        if (not isinstance(params, dict) or any(not isinstance(key, str) or not key.strip()
                or not isinstance(value, str) or not value.strip() for key, value in params.items())):
            raise ValueError("Cursor model parameters must be a mapping of nonempty strings")
        effort_keys = {"effort", "reasoning_effort"}.intersection(params)
        if (len(effort_keys) != 1 or params[next(iter(effort_keys))] != config["reasoning_effort"]
                or params.get("fast") not in {"true", "false"}):
            raise ValueError("Cursor model parameters require exactly one matching effort or reasoning_effort and explicit fast=true|false")
        # Parameter names differ across models. The live host catalog must
        # validate every supplied and required parameter before starting a run.
    # Retain any additional model sampling settings, so comparison cannot silently
    # ignore a newly introduced setting that differs between the two runs.
    return dict(config)


def parse_model_params(values):
    params = {}
    for item in values or []:
        if not isinstance(item, str) or "=" not in item:
            raise ValueError("Model parameters must be KEY=VALUE")
        key, value = item.split("=", 1)
        if not key.strip() or not value.strip() or key in params:
            raise ValueError("Model parameters must have unique nonempty keys and values")
        params[key] = value
    return params


def model_selection_for(args):
    """Keep the legacy Codex configuration unchanged; pin Cursor's extra settings."""
    host = getattr(args, "model_host", "codex")
    if host not in MODEL_HOSTS:
        raise ValueError("Unknown model host")
    values = getattr(args, "model_param", None)
    if host == "codex":
        if values:
            raise ValueError("Codex does not accept Cursor model parameters")
        return {}
    return {"model_host": host, "model_params": parse_model_params(values)}


def model_config_for(args):
    selection = model_selection_for(args)
    if not getattr(args, "model", None):
        if selection:
            raise ValueError("Cursor model trials require an explicit model")
        return None
    return validate_model_config({"model": args.model, "reasoning_effort": getattr(args, "reasoning_effort", None),
                                  "max_steps": getattr(args, "max_steps", None), "timeout_seconds": args.timeout,
                                  "model_host_version": getattr(args, "model_host_version", None), **selection})


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def artifact_dir(prefix):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = ROOT / "artifacts" / f"{prefix}-{stamp}"
    path.mkdir(parents=True)
    return path


def digest(path):
    if not path.is_file():
        return None
    hashed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def extension_identity():
    """Pin the extension start-browser loads, independently of the app bundle.

    Only relative runtime-resource names and bytes enter the digest. A saved
    identical copy has the same identity even when its directory changes.
    """
    root = pathlib.Path(os.environ.get("SC_E2E_EXTENSION") or REPO / "extension").expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str)
            or not manifest["version"].strip()):
        raise ValueError("Effective extension manifest must declare a version")
    suffixes = {".js", ".mjs", ".cjs", ".html", ".css", ".wasm", ".png", ".svg", ".ico",
                ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf", ".otf"}
    paths = {manifest_path, *(path for path in root.rglob("*")
                              if path.is_file() and path.suffix.lower() in suffixes)}
    hashed, files = hashlib.sha256(), {}
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        data = manifest_bytes if path == manifest_path else path.read_bytes()
        encoded = relative.encode("utf-8")
        hashed.update(len(encoded).to_bytes(8, "big"))
        hashed.update(encoded)
        hashed.update(len(data).to_bytes(8, "big"))
        hashed.update(data)
        files[relative] = hashlib.sha256(data).hexdigest()
    return {"path": str(root), "version": manifest["version"], "manifest_version": manifest.get("manifest_version"),
            "sha256": hashed.hexdigest(), "files": files,
            "coverage": "manifest.json, all JavaScript modules, and browser UI resources"}


def metadata():
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip()
    bundle = pathlib.Path(os.environ.get("SC_E2E_APP", str(pathlib.Path.home() / "Applications/SecondCursor.app"))).expanduser().resolve()
    task_hash = hashlib.sha256()
    for name in TASK_FILES:
        task_hash.update(name.encode())
        task_hash.update((ROOT / "benchmarks" / name).read_bytes())
    fixture_hash = hashlib.sha256()
    for name in FIXTURE_FILES:
        fixture_hash.update(name.encode())
        fixture_hash.update((ROOT / "benchmarks" / name).read_bytes())
    return {"recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "commit": git("rev-parse", "HEAD"), "dirty_paths": git("status", "--short").splitlines(),
            "platform": platform.platform(), "macos": platform.mac_ver()[0], "machine": platform.machine(),
            "bundle": str(bundle), "binary_sha256": digest(bundle / "Contents/MacOS/SecondCursor"),
            "fixture_sha256": fixture_hash.hexdigest(), "task_sha256": task_hash.hexdigest(),
            "fixture_files": {name: digest(ROOT / "benchmarks" / name) for name in FIXTURE_FILES},
            "extension_identity": extension_identity(),
            "python": sys.version.split()[0]}


def classify_scenario(row):
    """A harness's inconclusive invariant currently has ok=True. Preserve its uncertainty."""
    status = row.get("status")
    checks = row.get("checks", []) + row.get("invariants", [])
    if status == "FAIL" or any(c.get("ok") is False and not c.get("inconclusive") for c in checks):
        return "FAIL"
    if any(c.get("inconclusive") for c in checks):
        return "INCONCLUSIVE"
    if status == "PASS" and checks and all(c.get("ok") is True for c in checks):
        return "PASS"
    return status if status in {"SKIP", "NOT_RUN", "INCONCLUSIVE"} else "INCOMPLETE"


def overall(rows):
    statuses = [row["status"] for row in rows]
    if "FAIL" in statuses:
        return "FAIL"
    return "PASS" if statuses and all(s == "PASS" for s in statuses) else "INCOMPLETE"


def exit_code(status):
    return {"PASS": 0, "FAIL": 1}.get(status, 2)


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def merge_intervals(intervals):
    duration, end = 0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            duration += stop - start
            end = stop
        elif stop > end:
            duration += stop - end
            end = stop
    return duration


class ReservedGroupProcess(subprocess.Popen):
    """Keep the child unreaped until cleanup, preventing PID/process-group reuse."""
    def __init__(self, *args, **kwargs):
        self.group_reserved = False
        super().__init__(*args, **kwargs)
        self.group_reserved = True

    def observe_exit(self):
        if not self.group_reserved:
            raise ChildProcessError("Child group ownership has already been released")
        try:
            result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            self.group_reserved = False
            raise
        if result is not None:
            if result.si_code == os.CLD_EXITED:
                self.returncode = result.si_status
            elif result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                self.returncode = -result.si_status
        return self.returncode

    def _internal_poll(self, _deadstate=None, **kwargs):
        if self.returncode is None and self.group_reserved:
            self.observe_exit()
        return self.returncode

    def _wait(self, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.observe_exit() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.01)
        return self.returncode

    def reap(self):
        if not self.group_reserved:
            return
        # This is the sole reaping operation: all group signals are already done.
        if self.observe_exit() is None:
            raise RuntimeError("Cannot release ownership while the child remains alive")
        os.waitpid(self.pid, 0)
        self.group_reserved = False


def group_members(pgid):
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,state="],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError("Cannot inspect process-group cleanup")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            raise RuntimeError("Malformed process-group inventory")
        pid, group, state = int(parts[0]), int(parts[1]), parts[2]
        if group == pgid:
            rows.append({"pid": pid, "state": state})
    return rows


def stop_group(process, *, grace_seconds=1):
    evidence = {"group_id": process.pid, "errors": [], "snapshots": [], "signals": []}

    def inspect():
        # WNOWAIT both proves this is still our child and reserves its ID while
        # the snapshot and signal occur. No signal follows a failed ownership check.
        process.observe_exit()
        rows = group_members(process.pid)
        evidence["snapshots"].append(rows)
        return [row for row in rows if not row["state"].startswith("Z")]

    try:
        if not inspect():
            evidence["status"] = "no_live_members"
            return evidence
        for sig, grace in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, 1)):
            if not inspect():
                break
            try:
                os.killpg(process.pid, sig)
                evidence["signals"].append(int(sig))
            except (ProcessLookupError, PermissionError) as error:
                # macOS returns EPERM for a group containing only zombies.
                # A real live-member denial is a cleanup failure, never swallowed.
                if inspect():
                    raise RuntimeError(f"Signal {int(sig)} failed with live owned members: {type(error).__name__} errno={error.errno}") from error
                break
            until = time.monotonic() + grace
            while inspect() and time.monotonic() < until:
                time.sleep(.025)
        if inspect():
            raise RuntimeError("Owned live process-group members survived cleanup")
        evidence["status"] = "cleaned"
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        evidence.update(status="failed", errors=[f"{type(error).__name__}: {error}"])
    return evidence


def run_process(command, timeout, *, stdin=None, env=None):
    started = time.monotonic()
    process = ReservedGroupProcess(command, cwd=REPO,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, env=env)
    timed_out, stdout, stderr, cleanup = False, "", "", None
    errors = []
    def text(value):
        return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
    try:
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout, stderr = text(error.output), text(error.stderr)
            cleanup = stop_group(process)
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired as later:
                stdout, stderr = text(later.output), text(later.stderr)
                errors.append("Output pipes remain open after cleanup")
    finally:
        if cleanup is None:
            cleanup = stop_group(process)
        try:
            process.reap()
        except (OSError, RuntimeError) as error:
            errors.append(f"Child reap failed: {type(error).__name__}: {error}")
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
    return {"stdout": stdout, "stderr": stderr, "returncode": process.returncode,
            "timed_out": timed_out, "seconds": time.monotonic() - started,
            "cleanup": cleanup, "cleanup_errors": cleanup["errors"] + errors}


def suite_rows(payload, expected, fmt="suite"):
    raw = payload.get("trials" if fmt == "tasks" else "results", [])
    rows = []
    found = collections.defaultdict(list)
    for item in raw:
        name = item.get("task_id" if fmt == "tasks" else "scenario")
        if name:
            found[name].append(item)
    for name in expected:
        if not found[name]:
            rows.append({"scenario": name, "status": "INCOMPLETE", "reason": "Expected scenario absent from artifact"})
            continue
        for item in found[name]:
            rows.append({**item, "scenario": name,
                         "status": item.get("status", "INCOMPLETE") if fmt == "tasks" else classify_scenario(item)})
    return rows


def command_for(suite, args):
    bundle = pathlib.Path(os.environ.get("SC_E2E_APP", str(pathlib.Path.home() / "Applications/SecondCursor.app"))).expanduser().resolve()
    extra = [part.replace("{native_binary}", str(bundle / "Contents/MacOS/SecondCursor")) for part in suite.get("args", [])]
    command = [sys.executable, str(ROOT / suite["script"]), *extra]
    if suite.get("format") == "tasks":
        command.extend(["--rounds", "1"])
    elif suite.get("format") != "unittest":
        command.extend(suite["scenarios"])
    # Approval-enabled instances affect every tool. Forward only to explicitly attended groups.
    if suite["group"] == "takeover" and args.takeover:
        command.append("--takeover")
    if suite["group"] == "approvals" and args.approvals:
        command.append("--approvals")
    return command


def gate(args):
    manifest = json.loads((ROOT / "benchmarks/release.json").read_text())
    suites = manifest["suites"]
    known = {s["id"] for s in suites}
    if args.suite and set(args.suite) - known:
        raise ValueError(f"Unknown suite IDs: {sorted(set(args.suite) - known)}")
    selected = [s for s in suites if (not args.suite or s["id"] in args.suite)
                and (args.profile == "release" or s["group"] == "quiet" or args.suite)]
    if args.list:
        for suite in selected:
            print(json.dumps({"suite": suite["id"], "group": suite["group"], "command": command_for(suite, args)}))
        return 0
    output = artifact_dir("best-in-class")
    result = {"contract": CONTRACT, "profile": args.profile, "subset": args.suite or None,
              "metadata": metadata(), "suites": [], "status": "INCOMPLETE"}
    child_env = dict(os.environ)
    child_env.pop("SC_E2E_TARGET", None)
    for suite in selected:
        command = command_for(suite, args)
        row = {"id": suite["id"], "group": suite["group"], "command": command, "status": "INCOMPLETE", "scenarios": []}
        result["suites"].append(row)
        blocked = ((suite["group"] == "takeover" and not args.takeover)
                   or (suite["group"] == "approvals" and not args.approvals))
        if blocked:
            row.update(status="NOT_RUN", reason=f"Needs explicit --{suite['group']} flag")
        elif not (ROOT / suite["script"]).is_file():
            row.update(reason="Required suite entrypoint is missing")
        else:
            print(f"Running {suite['id']} ({suite['group']})", flush=True)
            try:
                proc = run_process(command, args.timeout, env=child_env)
                (output / f"{suite['id']}.stdout.log").write_text(proc["stdout"])
                (output / f"{suite['id']}.stderr.log").write_text(proc["stderr"])
                row.update({k: proc[k] for k in ("returncode", "timed_out", "seconds", "cleanup_errors")})
                matches = re.findall(r"^(?:Artifact|Benchmark):\s*(.+)$", proc["stdout"], flags=re.MULTILINE)
                if suite.get("format") == "unittest":
                    log = proc["stdout"] + "\n" + proc["stderr"]
                    count = re.search(r"Ran (\d+) tests? in", log)
                    passed = bool(count and int(count[1]) >= suite["minimum_tests"] and re.search(r"^OK$", log, re.MULTILINE))
                    row.update(scenarios=[{"scenario": suite["id"], "status": "PASS" if passed else "INCOMPLETE",
                                          "checks": [{"label": "Expected process tests ran with no skips", "ok": passed}],
                                          "tests": int(count[1]) if count else 0}],
                               status="PASS" if passed else "INCOMPLETE")
                elif matches:
                    artifact = pathlib.Path(matches[-1].strip()).resolve()
                    artifact.relative_to(ROOT / "artifacts")
                    datafile = artifact if artifact.suffix == ".json" else artifact.parent / "results.json"
                    payload = json.loads(datafile.read_text())
                    row.update(artifact=str(datafile), scenarios=suite_rows(payload, suite["scenarios"], suite.get("format", "suite")))
                    row["status"] = overall(row["scenarios"])
                else:
                    row["reason"] = "No result artifact was reported"
                if proc["timed_out"] or proc["returncode"] not in (0, 2) or proc["cleanup_errors"]:
                    row.update(status="FAIL", reason="Suite timed out, exited unsuccessfully or failed process cleanup; inspect raw logs")
                elif proc["returncode"] == 2 and row["status"] == "PASS":
                    row.update(status="INCOMPLETE", reason="Child reported incomplete evidence despite a passing artifact")
            except Exception as error:
                row.update(status="FAIL", reason=f"{type(error).__name__}: {error}")
        print(f"{row['status']:12} {suite['id']}", flush=True)
        result["status"] = overall(result["suites"])
        write_json(output / "results.json", result)
    write_gate_report(output, result)
    print(f"Artifact: {output / 'report.md'}")
    return exit_code(result["status"])


def write_gate_report(output, result):
    lines = [f"# Second Cursor release evidence — {result['status']}", "",
             f"Scope: `{result['profile']}`; subset: `{result['subset']}`. Commit: `{result['metadata']['commit']}`.", "",
             "This status covers only the selected suites. Skipped and disturbed checks never count as passing.", "",
             "| Suite | Status | Seconds | Evidence |", "| --- | --- | ---: | --- |"]
    for suite in result["suites"]:
        evidence = f"[results](<{suite['artifact']}>)" if suite.get("artifact") else suite.get("reason", "")
        lines.append(f"| {suite['id']} | {suite['status']} | {suite.get('seconds', 0):.1f} | {evidence} |")
    for suite in result["suites"]:
        incomplete = [r for r in suite["scenarios"] if r["status"] != "PASS"]
        if incomplete:
            lines.extend(["", f"## {suite['id']}: unresolved evidence", ""])
            for row in incomplete:
                lines.append(f"- `{row['scenario']}`: {row['status']}. {row.get('error') or row.get('reason') or 'Inspect scenario invariants.'}")
    (output / "report.md").write_text("\n".join(lines) + "\n")


def aggregate(trials):
    result = {}
    for task in dict.fromkeys(t["task_id"] for t in trials):
        rows = [t for t in trials if t["task_id"] == task]
        summary = {"completed": sum(t["status"] == "PASS" for t in rows), "trials": len(rows),
                   "statuses": dict(collections.Counter(t["status"] for t in rows))}
        independent = [t["independent_task_success"] for t in rows
                       if type(t.get("independent_task_success")) is bool]
        summary["independent_verified"] = sum(independent) if independent else None
        summary["independent_measured_trials"] = len(independent)
        for field in ("wall_ms", "tool_ms", "tool_calls", "model_ms", "cost_usd", "retries"):
            samples = [t.get("metrics", {}).get(field) for t in rows]
            measured = [v for v in samples if finite_number(v)]
            summary[field] = {"median": statistics.median(measured) if measured else None,
                              "p95": percentile(measured, .95), "measured_trials": len(measured)}
        result[task] = summary
    return result


def execution_profile(payload):
    # Pre-model-runner artifacts did not have this field. A model name in such an
    # artifact still requires model evidence; absence must not turn it scripted.
    return payload.get("execution_profile", "model" if payload.get("model") else "scripted")


def validate_comparison(payloads):
    fields = ("contract", "track", "native_placement", "model", "fixture_sha256", "task_sha256")
    if not payloads or any(p.get("kind") != "tasks" for p in payloads):
        raise ValueError("compare needs tasks.json artifacts")
    profiles = [execution_profile(p) for p in payloads]
    if any(profile not in {"scripted", "external-scripted", "model"} for profile in profiles):
        raise ValueError(f"Unknown execution profile: {profiles}")
    if any(profile != profiles[0] for profile in profiles[1:]):
        raise ValueError(f"Cannot compare different execution profiles: {profiles}")
    if profiles[0] != "model" and any(p.get("model") or p.get("model_config") for p in payloads):
        raise ValueError("A scripted execution profile cannot contain a model configuration")
    for field in fields:
        values = [p.get(field) for p in payloads]
        if any(v != values[0] for v in values[1:]):
            raise ValueError(f"Cannot compare different {field}: {values}")
    if any(not isinstance(p.get("trials"), list) or not p["trials"] for p in payloads):
        raise ValueError("Cannot compare empty or missing trial evidence")
    if any(not isinstance(t, dict) or not isinstance(t.get("task_id"), str) or not t["task_id"]
           for p in payloads for t in p["trials"]):
        raise ValueError("Every trial needs a task_id")
    task_sets = [{t["task_id"] for t in p["trials"]} for p in payloads]
    if any(t != task_sets[0] for t in task_sets[1:]):
        raise ValueError("Cannot compare different task selections")
    if profiles[0] != "model" and any(task in WORKSPACE_TASKS or task in FLIGHT_TASKS for task in task_sets[0]):
        schedules = []
        for payload in payloads:
            schedule = []
            for trial in payload["trials"]:
                if (type(trial.get("trial")) is not int or trial["trial"] < 1
                        or type(trial.get("case_seed")) is not int or trial["case_seed"] < 0):
                    raise ValueError("Workspace comparisons require positive trial numbers and nonnegative case seeds")
                schedule.append((trial["task_id"], trial["trial"], trial["case_seed"]))
            if len(schedule) != len(set(schedule)):
                raise ValueError("Workspace comparison cannot contain duplicate task/trial/seed identities")
            schedules.append(sorted(schedule))
        if any(schedule != schedules[0] for schedule in schedules[1:]):
            raise ValueError("Cannot compare different workspace task/trial/case_seed schedules")
    if profiles[0] == "model":
        modes = [p.get("observation_mode", "on-demand") for p in payloads]
        if any(mode not in {"on-demand", "upfront"} for mode in modes) or len(set(modes)) != 1:
            raise ValueError("Cannot compare different or unknown observation_mode values; use the explicit efficiency experiment")
        catalogs = [p.get("catalog_mode", "full") for p in payloads]
        if any(mode not in {"full", "compact"} for mode in catalogs) or len(set(catalogs)) != 1:
            raise ValueError("Cannot compare different or unknown catalog_mode values; use the explicit efficiency experiment")
        browser_tasks = any(task != "native_notes" for task in task_sets[0])
        configs = [validate_model_config(p.get("model_config")) for p in payloads]
        if any(c != configs[0] for c in configs[1:]):
            raise ValueError("Cannot compare different model configurations (model, effort, budget or host/version)")
        schedules = []
        for payload, config in zip(payloads, configs):
            if config["model"] != payload["model"]:
                raise ValueError("Artifact model disagrees with its model configuration")
            if not pinned_identifier(payload.get("engine_version")):
                raise ValueError("Model comparison requires a pinned engine_version for every engine")
            if payload.get("interface_track") not in INTERFACE_TRACKS:
                raise ValueError("Model comparison requires an explicit interface_track")
            if payload.get("browser_visibility") not in {"headed", "headless"}:
                raise ValueError("Model comparison requires an explicit browser_visibility")
            browser = payload.get("browser_identity")
            if browser_tasks:
                if (not isinstance(browser, dict) or not pinned_identifier(browser.get("version"))
                        or not isinstance(browser.get("binary"), str) or not pathlib.Path(browser["binary"]).is_absolute()
                        or not isinstance(browser.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-fA-F]{64}", browser["sha256"])):
                    raise ValueError("Model browser comparison requires a pinned browser_identity with binary, version and sha256")
            elif browser is not None:
                raise ValueError("Native-only model artifacts must not claim a browser_identity")
            if any(not pinned_identifier(payload.get(field)) for field in ("contract", "track", "native_placement", "fixture_sha256", "task_sha256", "runner_sha256")):
                raise ValueError("Model comparison requires pinned task/fixture, runner and placement evidence")
            schedule = []
            for trial in payload["trials"]:
                if (not isinstance(trial.get("trial"), int) or isinstance(trial["trial"], bool) or trial["trial"] < 1
                        or not isinstance(trial.get("case_seed"), int) or isinstance(trial["case_seed"], bool)
                        or trial["case_seed"] < 0):
                    raise ValueError("Every model trial needs a positive trial number and nonnegative integer case_seed")
                schedule.append((trial["task_id"], trial["trial"], trial["case_seed"]))
            if len(schedule) != len(set(schedule)):
                raise ValueError("Model comparison cannot contain duplicate task/trial/seed identities")
            if len(schedule) != len({(task, trial) for task, trial, seed in schedule}):
                raise ValueError("A task/trial cannot use more than one case_seed")
            if len(schedule) != len({(task, seed) for task, trial, seed in schedule}):
                raise ValueError("A task cannot reuse its case_seed across trial rounds")
            round_seeds = collections.defaultdict(set)
            for task, trial, seed in schedule:
                round_seeds[trial].add(seed)
            if any(len(seeds) != 1 for seeds in round_seeds.values()):
                raise ValueError("Each trial round must use the same case_seed for every task")
            schedules.append(sorted(schedule))
        if any(p["interface_track"] != payloads[0]["interface_track"] for p in payloads[1:]):
            raise ValueError("Cannot compare different interface_track values")
        if any(p["browser_visibility"] != payloads[0]["browser_visibility"] for p in payloads[1:]):
            raise ValueError("Cannot compare different browser_visibility values")
        if any(p.get("browser_identity") != payloads[0].get("browser_identity") for p in payloads[1:]):
            raise ValueError("Cannot compare different browser_identity values")
        if any(p["runner_sha256"] != payloads[0]["runner_sha256"] for p in payloads[1:]):
            raise ValueError("Cannot compare different runner_sha256 fingerprints")
        if any(schedule != schedules[0] for schedule in schedules[1:]):
            raise ValueError("Cannot compare different task/trial/case_seed schedules")


def compare(args):
    payloads = [json.loads(pathlib.Path(p).read_text()) for p in args.paths]
    validate_comparison(payloads)
    print("Task outcomes come from independent judges; all checks passed additionally requires clean execution and coexistence evidence.")
    print("Every trial stays in the denominator; times include failures. External telemetry is self-reported.")
    print("Partial metric samples show their measured count; unavailable values never count as zero.")
    print("Coexistence checks cover task endpoints only; transient interference is unverified in these task artifacts.")
    print("| Engine | Task | Task outcome verified | All checks passed | Wall median / p95 ms | Tool median ms | Model median ms | Cost median USD |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    def number(metric, total, statistic="median"):
        value = metric[statistic]
        if value is None:
            return "unavailable"
        coverage = f" ({metric['measured_trials']}/{total} measured)" if metric["measured_trials"] != total else ""
        return f"{value:.3f}{coverage}"
    for payload in payloads:
        for task, row in aggregate(payload["trials"]).items():
            count = row["trials"]
            judged = row["independent_measured_trials"]
            outcome = "unavailable" if judged == 0 else f"{row['independent_verified']}/{count}"
            if 0 < judged < count:
                outcome += f" ({judged}/{count} judged)"
            print(f"| {payload['engine']} ({payload.get('engine_version') or 'unversioned'}) | {task} | {outcome} | {row['completed']}/{count} | "
                  f"{number(row['wall_ms'], count)} / {number(row['wall_ms'], count, 'p95')} | {number(row['tool_ms'], count)} | "
                  f"{number(row['model_ms'], count)} | {number(row['cost_usd'], count)} |")
    return 0


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    modes = cli.add_subparsers(dest="mode", required=True)
    gate_cli = modes.add_parser("gate", help="Run release suites serially and classify strict evidence")
    gate_cli.add_argument("--profile", choices=("quiet", "release"), default="quiet")
    gate_cli.add_argument("--suite", action="append")
    gate_cli.add_argument("--list", action="store_true", help="Print commands without launching anything")
    gate_cli.add_argument("--takeover", action="store_true")
    gate_cli.add_argument("--approvals", action="store_true")
    gate_cli.add_argument("--timeout", type=float, default=1200, help="Maximum seconds per suite")
    task_cli = modes.add_parser("tasks", help="Run independent complete-task trials")
    task_cli.add_argument("--rounds", type=int, default=1)
    task_cli.add_argument("--task", choices=TASKS, action="append")
    task_cli.add_argument("--adapter-command", help="External command as a JSON argv array")
    task_cli.add_argument("--adapter-browser", action="store_true",
                          help="Start the disposable Second Cursor browser bridge for an external adapter")
    task_cli.add_argument("--headed-browser", action="store_true",
                          help="Use a visible disposable browser; model comparisons require matching visibility")
    task_cli.add_argument("--browser-identity",
                          help="Pinned shared browser identity as JSON; absent or null for native-only trials")
    task_cli.add_argument("--engine", default="second-cursor-scripted")
    task_cli.add_argument("--engine-version")
    task_cli.add_argument("--model")
    task_cli.add_argument("--model-host", choices=MODEL_HOSTS, default="codex")
    task_cli.add_argument("--model-param", action="append", help="Explicit Cursor model parameter KEY=VALUE; repeat per parameter")
    task_cli.add_argument("--reasoning-effort", choices=REASONING_EFFORTS,
                          help="Explicit model reasoning setting, required for model runs")
    task_cli.add_argument("--max-steps", type=int, default=40, help="Maximum total control-tool attempts per trial, including upfront observations")
    task_cli.add_argument("--observation-mode", choices=("on-demand", "upfront"), default="on-demand")
    task_cli.add_argument("--catalog-mode", choices=("full", "compact"), default="full")
    task_cli.add_argument("--seed", type=int, action="append",
                          help="Workspace case seed or legacy trial ID; repeat once per round (not a model sampling seed)")
    task_cli.add_argument("--model-host-version", help="Pinned model host/version fingerprint, required for model runs")
    task_cli.add_argument("--interface-track", choices=INTERFACE_TRACKS, default="native-tools",
                          help="Action and observation interface; engines may explicitly reject unsupported tracks")
    task_cli.add_argument("--track", choices=("host-background",), default="host-background")
    task_cli.add_argument("--native-placement", choices=("desktop", "agent-display", "textedit-off-space"),
                          default="agent-display", help="Placement of disposable native fixture windows")
    task_cli.add_argument("--off-space", action="store_true",
                          help="Leave TextEdit on its normal inactive Space and require it to remain off screen; Calculator placement is unchanged")
    task_cli.add_argument("--timeout", type=float, default=180)
    compare_cli = modes.add_parser("compare", help="Compare matching task artifacts without claiming superiority")
    compare_cli.add_argument("paths", nargs="+")
    modes.add_parser("self-test", help="Check evidence classification and independent judges without macOS/UI")
    return cli


def main():
    args = parser().parse_args()
    try:
        if not finite_number(getattr(args, "timeout", 1), positive=True):
            raise ValueError("timeout must be finite and positive")
        if args.mode == "gate":
            return gate(args)
        if args.mode == "compare":
            return compare(args)
        if args.mode == "tasks":
            if args.rounds < 1:
                raise ValueError("rounds must be positive")
            from benchmarks.tasks import run_tasks
            return run_tasks(args)
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / "benchmarks"), pattern="test_harness.py")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
