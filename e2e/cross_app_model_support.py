"""Pure model-run contract, seeded faults and independent evidence checks.

Expected business answers never enter the prompt or fault controller. The
controller interrupts ordinary public MCP calls; it never chooses solver actions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import time

from benchmarks.engines import _ok, records
from cross_app_support import summary_line

VERSION = "cross-app-model/2"
NATIVE_MUTATIONS = {"click", "type_text", "press_key", "set_value", "select_text",
                    "perform_action", "scroll", "drag"}
NATIVE_READS = {"get_window_state", "inspect_element", "screenshot"}
REQUIRED_NATIVE_FAULTS = ("native-pause", "native-external-edit", "native-resume",
                          "native-stale-refusal", "native-lost-ack")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def fault_schedule(seed):
    if type(seed) is not int or not 0 <= seed < 2 ** 63:
        raise ValueError("seed must be a nonnegative 63-bit integer")
    rng = random.Random(f"{VERSION}:{seed}")
    return [{"id": f"foreground-{index + 1}", "after_call": boundary,
             "action": "focus", "window": "B" if index % 2 == 0 else "A"}
            for index, boundary in enumerate(sorted(rng.sample(range(2, 9), 3)))]


def external_note(seed):
    return f"\nUser correction {seed}: preserve this newer note."


def goal_prompt(workspace_goal, *, tab_id, window_id, title):
    # The fixture's task-specific business goal is useful; its recovery advice
    # would prescribe an approach, so leave that instruction out of this trial.
    business_goal = workspace_goal.split(" Updates may arrive while you work.", 1)[0]
    return (business_goal + "\n\n"
            f"Append one summary line at the end of the existing TextEdit document {title!r}, "
            "using the final saved invoice values, and save the existing document. "
            "Begin the appended line with a newline and use exactly this format:\n"
            "```text\n"
            "Reconciled <invoice ID>: $<amount with two decimal places>; <routing label>; <saved memo>.\n"
            "```\n"
            "The final period is part of the required document text; do not include the code fences or placeholders. "
            "Preserve all existing document content, including any newer notes added while you work. "
            "Preserve all other browser records.\n\n"
            "Permitted targets (all other applications and documents are out of scope):\n" +
            json.dumps({"browser_tab_id": tab_id,
                        "native_window_id": window_id, "native_document_title": title},
                       ensure_ascii=False, sort_keys=True))


class FaultController:
    """Hooks around validated model calls with injected host-only collaborators.

    task(action) invokes the real public task tool. probe() observes the actual
    native document and probe(text) appends the synthetic external edit. Canary
    methods never target the solver's document. No returned observation contains
    private judge values or hidden probe results.
    """
    def __init__(self, *, seed, task, probe, canary, clock=time.monotonic):
        self.seed, self.task, self.probe, self.canary, self.clock = seed, task, probe, canary, clock
        self.schedule = fault_schedule(seed)
        self.events, self.fired, self.native_receipts, self.external_receipts = [], [], [], []
        self.phase_activity, self.calls, self.paused_once = {}, 0, False
        self.pending = None
        self.lost_operation = None
        self.reconciled_by = []

    def event(self, kind, **fields):
        value = {"seq": len(self.events) + 1, "at_monotonic": self.clock(), "kind": kind, **fields}
        self.events.append(value)
        return value

    def _fire(self, fault_id, **fields):
        self.fired.append(fault_id)
        self.event("disruption", id=fault_id, **fields)

    def before_call(self, name, args):
        if self.pending is not None:
            raise RuntimeError("Overlapping or unfinished model tool dispatch")
        self.calls += 1
        native = name in NATIVE_MUTATIONS | NATIVE_READS
        phase = "native" if native else "browser" if name.startswith("browser_") else None
        if phase and phase + "_before" not in self.phase_activity:
            self.phase_activity[phase + "_before"] = self.canary.state()
        pending = {"name": name, "arguments": copy.deepcopy(args), "call": self.calls,
                   "started_at": self.clock(), "phase": phase, "stale_candidate": False}
        if name in NATIVE_MUTATIONS:
            if not self.paused_once:
                self.paused_once = True
                paused = self.task("pause")["task"]
                if paused.get("status") != "paused" or paused.get("in_flight_count") != 0:
                    raise AssertionError("Task did not drain and pause before external edit")
                self._fire("native-pause", task=paused)
                receipt = self.probe(external_note(self.seed))
                if receipt["after"] != receipt["before"] + external_note(self.seed):
                    raise AssertionError("External native edit did not preserve current text")
                self.external_receipts.append(receipt)
                self._fire("native-external-edit", receipt=receipt)
                resumed = self.task("resume")["task"]
                if resumed.get("requires_fresh_observation") is not True:
                    raise AssertionError("Resumed task did not require a fresh observation")
                self._fire("native-resume", task=resumed)
                pending["stale_candidate"] = True
            pending["before"] = self.probe()["after"]
        self.pending = pending

    def after_call(self, name, args, result):
        pending, self.pending = self.pending, None
        if not pending or pending["name"] != name or pending["arguments"] != args:
            raise RuntimeError("Model tool hook identity changed during dispatch")
        succeeded = _ok(result)
        original = copy.deepcopy(result)
        if name in NATIVE_MUTATIONS:
            receipt = {"call": pending["call"], "name": name, "arguments": copy.deepcopy(args),
                       "before": pending["before"], "after": self.probe()["after"],
                       "runtime_ok": succeeded, "known_guard_refusal": False,
                       "acknowledgement_suppressed": False}
            self.native_receipts.append(receipt)
            if pending["stale_candidate"]:
                text = json.dumps(result, ensure_ascii=False)
                if (not succeeded and receipt["before"] == receipt["after"]
                        and re.search(r"fresh|paused|resume|read (?:the )?target", text, re.I)):
                    receipt["known_guard_refusal"] = True
                    self._fire("native-stale-refusal", name=name, result=original)
            if succeeded and receipt["before"] != receipt["after"] and self.lost_operation is None:
                ids = {row["operation_id"] for row in records(result)
                       if isinstance(row.get("operation_id"), str) and row["operation_id"]}
                if isinstance(args.get("operation_id"), str):
                    ids.add(args["operation_id"])
                if len(ids) != 1:
                    raise AssertionError("Successful uncertain native write lacks one public operation ID")
                self.lost_operation = ids.pop()
                receipt["acknowledgement_suppressed"] = True
                self._fire("native-lost-ack", name=name, operation_id=self.lost_operation,
                           scope="synthetic MCP response loss after successful dispatch, not transport death")
                result = {"isError": True, "content": [{"type": "text", "text": json.dumps({
                    "error": "acknowledgement_lost", "outcome": "unknown",
                    "operation_id": self.lost_operation,
                    "message": "The tool response was lost after dispatch; its outcome is unknown."})}]}
        elif self.lost_operation and succeeded:
            if name == "operation_status" and args.get("operation_id") == self.lost_operation:
                if any(row.get("state") == "completed" and row.get("outcome_known") is True
                       and row.get("result_expired") is not True for row in records(result)):
                    self.reconciled_by.append({"call": pending["call"], "method": name})
            elif name in NATIVE_READS:
                self.reconciled_by.append({"call": pending["call"], "method": name})
        if pending["phase"]:
            self.phase_activity[pending["phase"] + "_after"] = self.canary.state()
        self.event("model_dispatch", name=name, arguments=copy.deepcopy(args),
                   call=pending["call"], started_at=pending["started_at"], ended_at=self.clock(),
                   runtime_ok=succeeded, returned_ok=_ok(result), runtime_result=original,
                   returned_result=copy.deepcopy(result))
        for disruption in self.schedule:
            if disruption["after_call"] == self.calls:
                receipt = self.canary.command(disruption["action"], window=disruption["window"])
                self._fire(disruption["id"], plan=disruption, receipt=receipt)
        return result


def judge_model_native(*, original, external, summary, live, saved, receipts,
                       required_faults, fired_faults, reconciled_by):
    prefix, expected = original + external, original + external + summary
    changed = [row for row in receipts if row["before"] != row["after"]]
    # Multiple incremental append calls are legal. An append becoming complete
    # exactly once is not the same as demanding exactly one type_text call.
    completions = [row for row in changed if row["before"].count(summary) == 0
                   and row["after"].count(summary) == 1]
    lost = [row for row in receipts if row.get("acknowledgement_suppressed") is True]
    receipt_calls = [row.get("call") for row in receipts]
    receipt_ordered = bool(receipt_calls) and all(type(call) is int and call > 0 for call in receipt_calls)
    if receipt_ordered:
        receipt_ordered = receipt_calls == sorted(set(receipt_calls))
    lost_once = (len(lost) == 1 and lost[0].get("runtime_ok") is True
                 and lost[0]["before"] != lost[0]["after"]
                 and type(lost[0].get("call")) is int and lost[0]["call"] > 0)
    observations, mutations_after_loss = [], []
    if lost_once:
        observations = [row["call"] for row in reconciled_by
                        if type(row.get("call")) is int and row["call"] > lost[0]["call"]
                        and row.get("method") in NATIVE_READS | {"operation_status"}]
        mutations_after_loss = [row["call"] for row in receipts
                                if type(row.get("call")) is int and row["call"] > lost[0]["call"]]
    return {
        "native_live_text_exact": live == expected,
        "native_saved_text_exact": saved == expected,
        "native_original_and_external_notes_preserved": isinstance(live, str) and live.startswith(prefix),
        "native_summary_present_exactly_once": isinstance(live, str) and live.count(summary) == 1,
        "native_summary_completed_once": len(completions) == 1,
        "native_mutation_receipts_ordered_and_identified": receipt_ordered,
        "native_each_observed_mutation_preserves_notes": bool(receipts) and all(
            row["before"].startswith(prefix) and row["after"].startswith(prefix) for row in receipts),
        "native_no_observed_duplicate_summary": all(row["after"].count(summary) <= 1 for row in receipts),
        "native_guard_refused_mutations_have_no_effect": all(
            row["before"] == row["after"] for row in receipts if row.get("known_guard_refusal")),
        "native_lost_write_receipt_accounted_once": lost_once,
        "all_required_faults_exercised_once": sorted(fired_faults) == sorted(required_faults),
        "model_observed_state_after_uncertain_native_write": bool(observations),
        "model_reconciled_uncertainty_before_next_native_mutation": bool(observations) and (
            not mutations_after_loss or min(observations) < min(mutations_after_loss)),
    }


def host_evidence_checks(host, events, *, model, reasoning_effort, host_version):
    calls = [event for event in events if event.get("type") == "tool"]
    requests = [event for event in events if event.get("type") == "tool_request"]
    telemetry = host.get("tool_telemetry") or {}
    request_map = {row.get("call_id"): row for row in requests}
    identities = lambda a, b: a.get("name") == b.get("name") and a.get("arguments") == b.get("arguments")
    return {
        "model_turn_completed": host.get("status") == "completed" and not host.get("error"),
        "model_identity_pinned": host.get("model") == model and host.get("reasoning_effort") == reasoning_effort
                                 and host.get("model_host_version") == host_version,
        "model_host_isolated": host.get("isolation", {}).get("verified") is True,
        "host_tool_lifecycle_coverage_complete": telemetry.get("coverage") == "complete",
        "host_all_callbacks_accounted": bool(calls) and len(calls) == len(requests) == len(request_map)
             == host.get("tool_calls") == telemetry.get("callback_calls") == telemetry.get("matched_calls")
             == telemetry.get("host_observed_attempts")
             and all(row.get("call_id") in request_map and identities(row, request_map[row["call_id"]]) for row in calls),
        "host_no_unmatched_or_unknown_outcomes": all(telemetry.get(key) == 0 for key in
             ("unmatched_host_items", "unmatched_callback_calls", "unknown_host_outcomes", "host_only_errors")),
        "host_fault_errors_present_in_trace": sum(row.get("status") == "error" for row in calls) >= 2
             and isinstance(telemetry.get("errors"), int) and telemetry["errors"] >= 2,
        "host_tool_timings_valid": bool(calls) and all(type(row.get("start_ms")) in (int, float)
             and type(row.get("end_ms")) in (int, float) and 0 <= row["start_ms"] <= row["end_ms"] for row in calls),
    }


def expected_summary(server):
    """Judge-only; never called while constructing prompts or model tools."""
    invoice = next(row for row in server._expected_state()["sections"]["invoices"]
                   if row["id"] == server._private["invoice_id"])
    route = {"standard": "Standard", "finance_review": "Finance review", "hold": "Hold"}[invoice["route"]]
    return summary_line(invoice["id"], invoice["approved_amount_cents"], route, invoice["note"])
