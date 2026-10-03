#!/usr/bin/env python3
"""Kill a real SDK workflow after invoice submission, then reconcile without replay.

SC_E2E_APP=build/SecondCursor.app python3 e2e/invoice_workflow.py [--headed-browser]
Uses an isolated runtime/browser and a deterministic adapter, not a model benchmark.
"""
import json
import pathlib
import shutil
import signal
import subprocess
import sys
import time

from invoice_support import InvoiceFixture, ResponseGate, TARGETS, expected_entry, judge
from run import Run, SCENARIOS, scenario

SCENARIOS.clear()
ROOT = pathlib.Path(__file__).resolve().parent


def decoded_tool_result(response):
    if not isinstance(response, dict) or response.get("isError") or response.get("error"):
        return None
    try:
        return json.loads(next(item["text"] for item in response["content"] if item.get("type") == "text"))
    except (KeyError, ValueError, StopIteration):
        return None


@scenario
def invoice_checkpoint_recovery(ctx):
    node = shutil.which("node")
    ctx.require("Node is available", node)
    instance = ctx.run.instance
    ctx.require("runs in a disposable runtime", instance is not None)
    artifact = ctx.run.dir / "invoice-workflow"
    artifact.mkdir()
    children = []
    output_handles = []

    def launch(mode, config_path):
        stdout = (artifact / f"{mode}.stdout.log").open("w")
        stderr = (artifact / f"{mode}.stderr.log").open("w")
        output_handles.extend((stdout, stderr))
        process = subprocess.Popen([node, str(ROOT / "invoice_agent.mjs"), mode, str(config_path)],
                                   env=instance.env, stdout=stdout, stderr=stderr)
        children.append(process)
        return process

    try:
        with InvoiceFixture(artifact / "ledger.json") as fixture, ResponseGate(instance.base_url, fixture.attempt_count) as proxy:
            config = {"proxy": proxy.url, "fixture": fixture.base + "/", "targets": TARGETS,
                      "checkpoint": str(artifact / "checkpoint.json"), "bootstrap": str(artifact / "bootstrap.json"),
                      "result": str(artifact / "resumed-result.json")}
            config_path = artifact / "runner.json"
            config_path.write_text(json.dumps(config, indent=2))
            first = launch("start", config_path)
            try:
                deadline = time.monotonic() + 90
                while not proxy.held.wait(0.1) and first.poll() is None and time.monotonic() < deadline:
                    pass
                ctx.require("a completed real submission response is withheld", proxy.held.is_set(),
                            (artifact / "start.stderr.log").read_text()[-4000:])
                held = proxy.held_call
                checkpoint = json.loads(pathlib.Path(config["checkpoint"]).read_text())
                first_ledger = json.loads(fixture.path.read_text())
                (artifact / "checkpoint-at-kill.json").write_text(json.dumps(checkpoint, indent=2))
                (artifact / "ledger-at-kill.json").write_text(json.dumps(first_ledger, indent=2))
                ctx.check("the submission was persisted before the SDK received its response",
                          len(first_ledger["attempts"]) == 1 and first_ledger["attempts"][0]["accepted"] is True
                          and first_ledger["attempts"][0]["invoice_id"] == TARGETS[0]
                          and first_ledger["attempts"][0]["entry"] == expected_entry(fixture.initial[TARGETS[0]]), first_ledger["attempts"])
                pending = checkpoint.get("pending", {})
                ctx.check("the checkpoint names the exact pending operation before dispatch",
                          pending.get("operationId") == held["operation_id"]
                          and pending.get("call", {}).get("id") == f"submit-{TARGETS[0]}"
                          and checkpoint.get("sessionId") == held["session_id"], pending)
                held_result = decoded_tool_result(held.get("upstream_result"))
                ctx.check("the withheld runtime batch completed every browser step",
                          isinstance(held_result, dict) and held_result.get("completed") == 6
                          and all(step.get("ok") is True for step in held_result.get("steps", []))
                          and len(held_result.get("steps", [])) == 6, held_result)
                ctx.require("first SDK process is still waiting on the withheld response", first.poll() is None)
                first.kill()
                first.wait(timeout=10)
                ctx.check("the first process died from real SIGKILL", first.returncode == -signal.SIGKILL, first.returncode)
                proxy.release.set()
                split = len(proxy.read())
                resumed = launch("resume", config_path)
                try:
                    resumed.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    resumed.kill()
                    resumed.wait(timeout=10)
                    raise AssertionError("resumed invoice SDK process timed out")
                ctx.check("resume runs in a different OS process", resumed.pid != first.pid, {"first_pid": first.pid, "resumed_pid": resumed.pid})
                ctx.require("resumed workflow exits successfully", resumed.returncode == 0,
                            (artifact / "resume.stderr.log").read_text()[-5000:])
                resumed_result = json.loads(pathlib.Path(config["result"]).read_text())
                ctx.check("resume retains the original runtime session",
                          resumed_result["sessionId"] == checkpoint["sessionId"] == resumed_result["originalSessionId"])
                calls = proxy.read()
                after = calls[split:]
                reconciled = [i for i, call in enumerate(after) if call["tool"] == "operation_status" and call["operation_id"] == held["operation_id"]]
                snapshots = [i for i, call in enumerate(after) if call["tool"] == "browser_snapshot"]
                mutations = [i for i, call in enumerate(after) if call["tool"] in {"batch", "browser_click", "browser_type", "browser_command"}]
                order = bool(reconciled and snapshots and mutations)
                if order:
                    reconciliation = after[reconciled[0]]
                    observation = after[snapshots[0]]
                    mutation = min((after[index] for index in mutations), key=lambda call: call["started_at"])
                    fresh = decoded_tool_result(observation["upstream_result"])
                    order = (reconciliation["upstream_completed_at"] <= observation["started_at"]
                             and observation["upstream_completed_at"] <= mutation["started_at"]
                             and observation["args"].get("full") is True
                             and fresh and fresh.get("freshness", {}).get("status") == "current")
                ctx.check("restart finishes reconciliation and a full current observation before dispatching any mutation",
                          order, {"reconcile_indices": reconciled, "snapshot_indices": snapshots, "mutation_indices": mutations})
                if reconciled:
                    status = decoded_tool_result(after[reconciled[0]]["upstream_result"])
                    ctx.check("the real runtime journal reports the saved operation completed",
                              status and status.get("state") == "completed" and status.get("outcome_known") is True
                              and status.get("result_expired") is not True, status)
                ctx.check("the pending submission is never dispatched twice",
                          sum(call["tool"] == "batch" and call["operation_id"] == held["operation_id"] for call in calls) == 1)
                ctx.check("there are exactly three submission batch dispatches", sum(call["tool"] == "batch" for call in calls) == len(TARGETS))
                durable = json.loads(fixture.path.read_text())
                for label, passed in judge(fixture.initial, durable).items():
                    ctx.check(f"independent ledger: {label}", passed)
                ctx.check("the first accepted entry survives restart unchanged",
                          durable["records"][TARGETS[0]] == first_ledger["records"][TARGETS[0]])
                final_checkpoint = json.loads(pathlib.Path(config["checkpoint"]).read_text())
                ctx.check("the workflow persists completion with no pending operation",
                          final_checkpoint.get("status") == "completed" and final_checkpoint.get("pending") is None
                          and final_checkpoint.get("queue") == [])
                (artifact / "processes.json").write_text(json.dumps({"killed_pid": first.pid, "killed_returncode": first.returncode,
                    "resumed_pid": resumed.pid, "resumed_returncode": resumed.returncode}, indent=2))
                ctx.run.notes.append(f"Invoice recovery evidence: {artifact}")
            finally:
                proxy.release.set()
                (artifact / "proxy-calls.json").write_text(json.dumps(proxy.read(), indent=2))
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for handle in output_handles:
            handle.close()


if __name__ == "__main__":
    sys.exit(0 if Run(browser=True, installed=False).run(sys.argv[1:]) else 1)
