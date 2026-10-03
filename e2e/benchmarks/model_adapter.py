#!/usr/bin/env python3
"""One fresh model conversation over a task-scoped driver. JSONL adapter protocol."""
import argparse
import dataclasses
import json
import os
import pathlib
import signal
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import best_in_class as report
from model_benchmark import runner_fingerprint
from benchmarks.engines import create_engine, UnsupportedEngine
from benchmarks.model_host import CodexModelHost, HostConfig


CLEANUP_TIMEOUT_SECONDS = 5


class AdapterInterrupted(BaseException):
    """Cannot be converted into a recoverable model tool error."""


class AdapterDeadline(AdapterInterrupted):
    pass


def validate_request(request, engine):
    if not isinstance(request, dict) or request.get("protocol") != report.CONTRACT:
        raise ValueError("Unsupported benchmark request")
    if (request.get("task_id") not in report.TASKS or not isinstance(request.get("prompt"), str)
            or not request["prompt"].strip()):
        raise ValueError("A known task and prompt are required")
    if request.get("track") != "host-background" or request.get("interface_track") != "native-tools":
        raise ValueError("This adapter implements only host-background/native-tools")
    if request.get("engine") != engine + "-model":
        raise ValueError("Adapter backend disagrees with the requested engine")
    config = report.validate_model_config(request.get("model_config"))
    if request.get("model") != config["model"] or request.get("timeout_seconds") != config["timeout_seconds"]:
        raise ValueError("Request model/budget disagrees with model configuration")
    if request.get("runner_sha256") != runner_fingerprint():
        raise ValueError("Shared runner changed after the trial was prepared")
    if not report.pinned_identifier(request.get("engine_version")):
        raise ValueError("A pinned engine version is required")
    return config


def redacted(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in {"authorization", "token", "api_key", "secret", "password"}
                else redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def run_adapter(request, args):
    started = time.monotonic()
    config = validate_request(request, args.engine)
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
        raise ValueError("Model adapter requires the main thread and exclusive deadline ownership")
    trace_dir = pathlib.Path(os.environ["SC_BENCH_TRACE_DIR"]).resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / "model-trace.jsonl"
    trace = trace_path.open("w")
    os.chmod(trace_path, 0o600)
    engine, host_result, failure, unsupported = None, None, None, False
    callback_failure = None
    final = {"type": "result", "status": "failed", "model": config["model"], "model_config": config,
             "model_ms": None, "cost_usd": None, "usage": None, "engine_trace": str(trace_path),
             "isolation": {"verified": False}, "cleanup_errors": []}

    def record(source, event):
        trace.write(json.dumps(redacted({"source": source, **event}), allow_nan=False) + "\n")
        trace.flush()

    previous_alarm = signal.getsignal(signal.SIGALRM)
    previous_term = signal.getsignal(signal.SIGTERM)

    def expired(signum, frame):
        raise AdapterDeadline("Adapter wall-clock deadline exceeded")

    def terminated(signum, frame):
        raise AdapterInterrupted("Adapter received SIGTERM")

    # The host owns SIGALRM while executing model callbacks. Setup and cleanup
    # need separate bounded phases so a stalled driver cannot bypass that clock.
    work_deadline = started + config["timeout_seconds"] - min(5, config["timeout_seconds"] / 10)
    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, work_deadline - time.monotonic()))
    try:
        env = {**os.environ, "SC_BENCH_CUA_BINARY": args.cua_binary}
        engine = create_engine(args.engine, request, emit=lambda event: record("engine", event),
                               clock_start=started, env=env)
        engine.start()
        if (not isinstance(engine.identity, dict) or engine.identity.get("engine") != args.engine
                or engine.identity.get("interface_track") != request["interface_track"]
                or engine.identity.get("scope_policy") != "fixture-only-v1"
                or engine.identity.get("engine_version") != request["engine_version"]
                or engine.identity.get("verified") is not True):
            raise ValueError("Driver did not report the expected scoped engine identity")
        final["engine_identity"] = engine.identity
        context = json.dumps(engine.context, ensure_ascii=False, allow_nan=False)
        prompt = request["prompt"] + "\n\nPermitted driver targets and identifiers:\n" + context
        tool_specs = engine.tools()
        remaining = work_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Engine setup exhausted the trial budget")
        host = CodexModelHost(HostConfig(model=config["model"], reasoning_effort=config["reasoning_effort"],
            max_steps=config["max_steps"], timeout_seconds=remaining,
            model_host_version=config["model_host_version"], codex_binary=args.codex_binary))
        host_offset = (time.monotonic() - started) * 1000

        def event(event):
            nonlocal callback_failure
            try:
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("Malformed model host event")
                record("model_host", event)
                if event["type"] == "tool":
                    if (not isinstance(event.get("name"), str) or not event["name"]
                            or not report.finite_number(event.get("start_ms"))
                            or not report.finite_number(event.get("end_ms"))
                            or event["start_ms"] < 0 or event["end_ms"] < event["start_ms"]
                            or event.get("status") not in {"completed", "error", "timeout"}):
                        raise ValueError("Malformed model tool timing or status")
                    # Host intervals include rejected calls; setup/cleanup stays
                    # separate so these intervals are not counted twice.
                    item = {"type": "tool", "name": event["name"],
                            "start_ms": event["start_ms"] + host_offset,
                            "end_ms": event["end_ms"] + host_offset,
                            "ok": event["status"] == "completed", "retry": False}
                    print(json.dumps(item, allow_nan=False), flush=True)
            except Exception as error:
                callback_failure = f"Model evidence callback failed: {type(error).__name__}: {error}"
                raise

        signal.setitimer(signal.ITIMER_REAL, 0)
        host_result = host.run(prompt, tool_specs, engine.call, emit_event=event)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, work_deadline - time.monotonic()))
        if time.monotonic() >= work_deadline:
            raise AdapterDeadline("Model host exceeded the remaining trial budget")
        if callback_failure:
            raise ValueError(callback_failure)
        data = host_result.as_dict() if hasattr(host_result, "as_dict") else dataclasses.asdict(host_result)
        record("host_result", data)
        if (not isinstance(data, dict) or data.get("status") not in
                {"completed", "failed", "timeout", "step_limit", "isolation_error"}):
            raise ValueError("Model host returned an invalid completion status")
        if data["status"] == "completed" and (not isinstance(data.get("isolation"), dict)
                or data["isolation"].get("verified") is not True or data.get("error")):
            raise ValueError("Model completion did not prove successful restricted-tool isolation")
        final.update(status="completed" if data["status"] == "completed" else "failed",
                     usage=data.get("usage"), isolation=data.get("isolation", {}),
                     final_response=data.get("final_response", ""), error=data.get("error"),
                     host_status=data["status"])
        identity_fields = ("model", "reasoning_effort", "model_host_version")
        if data["status"] == "completed":
            if any(data.get(key) != config[key] for key in identity_fields):
                raise ValueError("Actual model host identity differs from the pinned configuration")
        elif any(data.get(key) is not None and data[key] != config[key] for key in identity_fields):
            # Setup can fail before the host learns the provider model. Preserve
            # that root cause; a reported mismatch is additional failure evidence.
            final["identity_error"] = "Actual model host identity differs from the pinned configuration"
            if not final.get("error"):
                final["error"] = final["identity_error"]
    except UnsupportedEngine as error:
        failure, unsupported = str(error), True
    except (Exception, AdapterInterrupted) as error:
        failure = f"{type(error).__name__}: {error}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, CLEANUP_TIMEOUT_SECONDS)
        try:
            if engine is not None:
                try:
                    engine.close()
                except (Exception, AdapterInterrupted) as error:
                    final["cleanup_errors"].append(f"{type(error).__name__}: {error}")
                errors = getattr(engine, "cleanup_errors", [])
                if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
                    final["cleanup_errors"].append("Malformed engine cleanup evidence")
                else:
                    final["cleanup_errors"].extend(errors)
            if failure:
                final.update(status="unsupported" if unsupported else "failed", error=failure)
            if final["cleanup_errors"]:
                final["status"] = "failed"
            final["adapter_wall_ms"] = (time.monotonic() - started) * 1000
            record("adapter_result", final)
            print(json.dumps(redacted(final), allow_nan=False), flush=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_alarm)
            signal.signal(signal.SIGTERM, previous_term)
            trace.close()
    return 0 if final["status"] in {"completed", "unsupported"} else 1


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--engine", required=True, choices=("second-cursor", "cua-driver"))
    cli.add_argument("--codex-binary", default="codex")
    cli.add_argument("--cua-binary", default="cua-driver")
    args = cli.parse_args()
    try:
        request = json.load(sys.stdin)
        return run_adapter(request, args)
    except Exception as error:
        print(f"Model adapter refused to start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
