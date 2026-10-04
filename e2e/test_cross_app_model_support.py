"""Offline negative controls for the goal-only cross-app model evidence path."""
import copy
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from cross_app_model_support import (FaultController, REQUIRED_NATIVE_FAULTS, digest,
    external_note, fault_schedule, goal_prompt, host_evidence_checks, judge_model_native)
from cross_app_support import original_note, summary_line


def raw(value, error=False):
    return {"isError": error, "content": [{"type": "text", "text": json.dumps(value)}]}


class Canary:
    def __init__(self):
        self.calls, self.ticks = [], 0

    def state(self):
        self.ticks += 1
        return {"activity_running": True, "activity_count": self.ticks}

    def command(self, action, **fields):
        self.calls.append({"action": action, **fields})
        return {"ok": True}


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.seed = 101
        self.text, self.tasks = original_note(self.seed), []
        self.canary = Canary()
        def task(action):
            self.tasks.append(action)
            return {"task": {"status": "paused" if action == "pause" else "active",
                             "in_flight_count": 0, "requires_fresh_observation": action == "resume"}}
        def probe(add=None):
            before = self.text
            if add is not None:
                self.text += add
            return {"before": before, "after": self.text}
        self.fault = FaultController(seed=self.seed, task=task, probe=probe, canary=self.canary)

    def dispatch(self, name, args, result, effect=None):
        self.fault.before_call(name, args)
        if effect is not None:
            self.text += effect
        return self.fault.after_call(name, args, result)

    def stale_refusal(self):
        return self.dispatch("select_text", {"operation_id": "stale-op"},
                             raw({"error": "Task resumed; fresh target observation required"}, error=True))

    def test_fault_schedule_is_seeded_and_bounded(self):
        self.assertEqual(fault_schedule(101), fault_schedule(101))
        self.assertNotEqual(fault_schedule(101), fault_schedule(102))
        self.assertEqual(3, len(fault_schedule(101)))
        self.assertTrue(all(2 <= row["after_call"] <= 8 for row in fault_schedule(101)))
        for seed in (-1, True, 2**63, 1.5):
            with self.assertRaises(ValueError):
                fault_schedule(seed)

    def test_pause_edit_resume_are_real_collaborator_calls(self):
        response = self.stale_refusal()
        self.assertTrue(response["isError"])
        self.assertEqual(self.tasks, ["pause", "resume"])
        self.assertEqual(self.text, original_note(self.seed) + external_note(self.seed))
        self.assertTrue(set(REQUIRED_NATIVE_FAULTS[:-1]) <= set(self.fault.fired))
        self.assertTrue(self.fault.native_receipts[-1]["known_guard_refusal"])

    def test_success_does_not_masquerade_as_refusal(self):
        self.dispatch("type_text", {"operation_id": "early-op"}, raw({"ok": True}), effect="oops")
        self.assertNotIn("native-stale-refusal", self.fault.fired)

    def test_unrelated_failure_does_not_masquerade_as_guard_refusal(self):
        self.dispatch("select_text", {}, raw({"error": "No such item"}, error=True))
        self.assertNotIn("native-stale-refusal", self.fault.fired)

    def test_lost_ack_suppresses_response_without_retrying_write(self):
        self.stale_refusal()
        success = raw({"ok": True, "operation_id": "write-op", "private_result": "not delivered"})
        result = self.dispatch("type_text", {"operation_id": "write-op", "text": "FINAL-SUMMARY"}, success, effect="FINAL-SUMMARY")
        self.assertTrue(result["isError"])
        self.assertNotIn("not delivered", json.dumps(result))
        self.assertEqual(self.text.count("FINAL-SUMMARY"), 1)
        self.assertEqual(self.fault.lost_operation, "write-op")
        self.assertEqual(len(self.fault.native_receipts), 2)
        self.assertTrue(self.fault.native_receipts[-1]["acknowledgement_suppressed"])
        self.assertFalse(self.fault.native_receipts[0]["acknowledgement_suppressed"])
        event = [row for row in self.fault.events if row["kind"] == "model_dispatch"][-1]
        self.assertEqual(event["runtime_result"], success)
        self.assertTrue(event["runtime_ok"])
        self.assertFalse(event["returned_ok"])

    def test_no_change_does_not_fire_ack_fault(self):
        self.stale_refusal()
        self.dispatch("select_text", {"operation_id": "select-op"}, raw({"ok": True}))
        self.assertIsNone(self.fault.lost_operation)

    def test_failure_with_effect_is_not_changed_to_success_or_guard_refusal(self):
        self.stale_refusal()
        result = self.dispatch("type_text", {"operation_id": "uncertain-op"},
                               raw({"outcome": {"state": "uncertain"}}), effect="possibly typed")
        self.assertIsNone(self.fault.lost_operation)
        self.assertFalse(self.fault.native_receipts[-1]["known_guard_refusal"])
        self.assertFalse(self.fault.native_receipts[-1]["runtime_ok"])
        self.assertEqual(json.loads(result["content"][0]["text"])["outcome"]["state"], "uncertain")

    def test_successful_content_write_needs_real_operation_identity(self):
        self.stale_refusal()
        with self.assertRaisesRegex(AssertionError, "operation ID"):
            self.dispatch("type_text", {}, raw({"ok": True}), effect="summary")

    def test_model_must_act_to_observe_uncertain_outcome(self):
        self.stale_refusal()
        self.dispatch("type_text", {"operation_id": "write-op"}, raw({"ok": True}), effect="summary")
        self.assertEqual(self.fault.reconciled_by, [])
        self.dispatch("operation_status", {"operation_id": "different-op"},
                      raw({"state": "completed", "outcome_known": True}))
        self.assertEqual(self.fault.reconciled_by, [])
        self.dispatch("operation_status", {"operation_id": "write-op"},
                      raw({"state": "completed", "outcome_known": True}))
        self.assertEqual(self.fault.reconciled_by[-1]["method"], "operation_status")

    def test_failed_read_is_not_reconciliation(self):
        self.stale_refusal()
        self.dispatch("type_text", {"operation_id": "write-op"}, raw({"ok": True}), effect="summary")
        self.dispatch("get_window_state", {}, raw({"error": "read failed"}, error=True))
        self.assertEqual(self.fault.reconciled_by, [])
        self.dispatch("inspect_element", {}, raw({"text": self.text}))
        self.assertEqual(self.fault.reconciled_by[-1]["method"], "inspect_element")

    def test_overlapping_hooks_fail_closed(self):
        self.fault.before_call("browser_snapshot", {"tab_id": 1})
        with self.assertRaisesRegex(RuntimeError, "Overlapping"):
            self.fault.before_call("browser_snapshot", {"tab_id": 1})
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.fault.after_call("browser_snapshot", {"tab_id": 2}, raw({"ok": True}))


class JudgeTests(unittest.TestCase):
    def fixture(self):
        original, external = original_note(101), external_note(101)
        summary = summary_line("INV-1234", 12345, "Standard", "RCPT-9876")
        prefix = original + external
        return dict(original=original, external=external, summary=summary,
            live=prefix + summary, saved=prefix + summary, receipts=[
                {"call": 1, "before": prefix, "after": prefix, "runtime_ok": False, "known_guard_refusal": True},
                {"call": 2, "before": prefix, "after": prefix + summary[:20], "runtime_ok": True,
                 "acknowledgement_suppressed": True},
                {"call": 4, "before": prefix + summary[:20], "after": prefix + summary, "runtime_ok": True}],
            required_faults=["pause", "lost"], fired_faults=["pause", "lost"],
            reconciled_by=[{"call": 3, "method": "get_window_state"}])

    def test_incremental_append_passes(self):
        self.assertTrue(all(judge_model_native(**self.fixture()).values()))

    def test_correcting_own_new_draft_preserves_user_notes(self):
        case = self.fixture()
        prefix = case["original"] + case["external"]
        case["receipts"] = [{"call": 2, "before": prefix, "after": prefix + "draft", "runtime_ok": True,
                             "acknowledgement_suppressed": True},
                            {"call": 4, "before": prefix + "draft", "after": case["live"], "runtime_ok": True}]
        self.assertTrue(all(judge_model_native(**case).values()))

    def test_write_before_reconcile_then_read_later_fails(self):
        case = self.fixture()
        case["reconciled_by"][0]["call"] = 5
        checks = judge_model_native(**case)
        self.assertTrue(checks["native_live_text_exact"])
        self.assertTrue(checks["model_observed_state_after_uncertain_native_write"])
        self.assertFalse(checks["model_reconciled_uncertainty_before_next_native_mutation"])

    def test_refused_mutation_before_reconciliation_still_fails_order(self):
        case = self.fixture()
        prefix = case["original"] + case["external"]
        case["receipts"].insert(2, {"call": 3, "before": prefix + case["summary"][:20],
            "after": prefix + case["summary"][:20], "runtime_ok": False})
        case["reconciled_by"][0]["call"] = 4
        self.assertFalse(judge_model_native(**case)["model_reconciled_uncertainty_before_next_native_mutation"])

    def test_pre_loss_or_non_observation_call_does_not_reconcile(self):
        for observation in ({"call": 1, "method": "get_window_state"},
                            {"call": 3, "method": "type_text"}, {"method": "get_window_state"}):
            case = self.fixture()
            case["reconciled_by"] = [observation]
            self.assertFalse(judge_model_native(**case)["model_observed_state_after_uncertain_native_write"])

    def test_missing_duplicate_or_unsuccessful_lost_receipt_fails(self):
        for mode in ("missing", "duplicate", "unsuccessful"):
            case = self.fixture()
            if mode == "missing":
                case["receipts"][1].pop("acknowledgement_suppressed")
            elif mode == "duplicate":
                case["receipts"][2]["acknowledgement_suppressed"] = True
            else:
                case["receipts"][1]["runtime_ok"] = False
            self.assertFalse(judge_model_native(**case)["native_lost_write_receipt_accounted_once"])

    def test_reconciliation_without_another_native_mutation_is_valid(self):
        case = self.fixture()
        case["receipts"] = [{"call": 2, "before": case["original"] + case["external"],
            "after": case["live"], "runtime_ok": True, "acknowledgement_suppressed": True}]
        self.assertTrue(all(judge_model_native(**case).values()))

    def test_repaired_note_corruption_still_fails(self):
        case = self.fixture()
        case["receipts"].insert(1, {"before": case["original"] + case["external"],
                                    "after": "CORRUPTED", "runtime_ok": True})
        self.assertFalse(judge_model_native(**case)["native_each_observed_mutation_preserves_notes"])

    def test_repaired_duplicate_summary_still_fails(self):
        case = self.fixture()
        case["receipts"].append({"before": case["live"], "after": case["live"] + case["summary"], "runtime_ok": True})
        self.assertFalse(judge_model_native(**case)["native_no_observed_duplicate_summary"])

    def test_unsaved_or_missing_fault_evidence_fails(self):
        case = self.fixture()
        case["saved"] = case["original"]
        case["fired_faults"].pop()
        case["reconciled_by"] = []
        checks = judge_model_native(**case)
        self.assertFalse(checks["native_saved_text_exact"])
        self.assertFalse(checks["all_required_faults_exercised_once"])
        self.assertFalse(checks["model_observed_state_after_uncertain_native_write"])

    def test_goal_has_format_and_targets_but_no_recovery_recipe(self):
        prompt = goal_prompt("Reconcile INV-123. Updates may arrive while you work. SECRET RECIPE",
                             tab_id=5, window_id=7, title="notes.txt")
        self.assertIn("Reconcile INV-123.", prompt)
        self.assertIn("<amount with two decimal places>", prompt)
        self.assertIn("<saved memo>.\n```", prompt)
        self.assertIn("The final period is part of the required document text", prompt)
        self.assertNotIn("SECRET RECIPE", prompt)
        for instruction in ("operation_status", "select_text", "pause", "resume"):
            self.assertNotIn(instruction, prompt)
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))


class HostEvidenceTests(unittest.TestCase):
    def fixture(self):
        events = []
        for i in range(3):
            base = {"call_id": str(i), "name": "type_text", "arguments": {"text": str(i)}}
            events.extend([{**base, "type": "tool_request", "start_ms": i * 100},
                {**base, "type": "tool", "start_ms": i * 100, "end_ms": i * 100 + 10,
                 "status": "error" if i < 2 else "completed"}])
        host = {"status": "completed", "model": "gpt-6.1-sol", "reasoning_effort": "high",
            "model_host_version": "pinned", "isolation": {"verified": True}, "tool_calls": 3,
            "tool_telemetry": {"coverage": "complete", "callback_calls": 3, "matched_calls": 3,
                "host_observed_attempts": 3, "errors": 2, "unmatched_host_items": 0,
                "unmatched_callback_calls": 0, "unknown_host_outcomes": 0, "host_only_errors": 0}}
        return host, events

    def checks(self, host, events):
        return host_evidence_checks(host, events, model="gpt-6.1-sol", reasoning_effort="high", host_version="pinned")

    def test_complete_correlated_evidence_passes(self):
        self.assertTrue(all(self.checks(*self.fixture()).values()))

    def test_missing_or_unknown_coverage_never_passes(self):
        host, events = self.fixture()
        for telemetry in ({}, {"coverage": "partial"}, None):
            host["tool_telemetry"] = telemetry
            self.assertFalse(all(self.checks(host, events).values()))

    def test_frozen_identity_is_required(self):
        host, events = self.fixture()
        host["reasoning_effort"] = "medium"
        self.assertFalse(self.checks(host, events)["model_identity_pinned"])

    def test_callback_argument_drift_is_rejected(self):
        host, events = self.fixture()
        events = copy.deepcopy(events)
        events[-1]["arguments"] = {"text": "different"}
        self.assertFalse(self.checks(host, events)["host_all_callbacks_accounted"])

    def test_missing_fault_errors_is_not_clean_success(self):
        host, events = self.fixture()
        for event in events:
            if event["type"] == "tool":
                event["status"] = "completed"
        self.assertFalse(self.checks(host, events)["host_fault_errors_present_in_trace"])


if __name__ == "__main__":
    unittest.main()
