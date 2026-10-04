"""Codex host-only refusal accounting, using fake RPC and no model or desktop."""
import copy
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks import model_host as host
import test_model_host as protocol
import test_model_adapter as adapter_protocol


def dynamic(phase="completed", item_id="call-1", **changes):
    item = {"type": "dynamicToolCall", "id": item_id, "tool": "fixture_probe",
            "arguments": {"value": 1}, "namespace": None,
            "status": "inProgress" if phase == "started" else "completed",
            "success": None if phase == "started" else True,
            "contentItems": None if phase == "started" else [{"type": "inputText", "text": "ok"}],
            "durationMs": None if phase == "started" else 10}
    item.update(changes)
    return {"method": "item/" + phase, "params": {
        "threadId": "thread-1", "turnId": "turn-1", "item": item}}


class HostTelemetryTests(unittest.TestCase):
    def run_host(self, events, execute=None, **kwargs):
        return protocol.HostProtocolTests().run_host(events, execute, **kwargs)

    def test_host_only_schema_refusal_is_retained_without_fabricating_callback(self):
        callback = mock.Mock()
        rejected = dynamic(status="failed", success=False, contentItems=[
            {"type": "inputText", "text": "Invalid tool arguments: required field command is missing"}])
        result, _, events = self.run_host([dynamic("started"), rejected, protocol.completed()], callback)
        self.assertEqual(result.status, "completed")
        callback.assert_not_called()
        self.assertEqual(result.tool_calls, 0)
        self.assertFalse(any(event["type"] in {"tool", "tool_request"} for event in events))
        terminal = next(event for event in events if event["type"] == "host_tool" and event["phase"] == "completed")
        self.assertIn("required field command", terminal["output_text"][0])
        metrics = result.tool_telemetry
        self.assertEqual((metrics["coverage"], metrics["host_only_errors"], metrics["errors"]), ("complete", 1, 1))
        self.assertEqual((metrics["callback_calls"], metrics["host_observed_attempts"]), (0, 1))

    def test_started_callback_terminal_and_final_replay_are_one_call(self):
        terminal = dynamic()
        result, _, events = self.run_host([
            dynamic("started"), protocol.tool_call(), terminal, copy.deepcopy(terminal),
            protocol.completed(items=[terminal["params"]["item"]])])
        metrics = result.tool_telemetry
        self.assertEqual((result.tool_calls, metrics["host_observed_attempts"], metrics["matched_calls"]), (1, 1, 1))
        self.assertEqual((metrics["coverage"], metrics["errors"], metrics["host_only_errors"]), ("complete", 0, 0))
        self.assertEqual(sum(event["type"] == "tool" for event in events), 1)
        self.assertEqual(sum(event["type"] == "host_tool" and event["phase"] == "completed" for event in events), 1)

    def test_callback_failure_and_host_failure_are_not_two_errors(self):
        result, _, _ = self.run_host([
            dynamic("started"), protocol.tool_call(), dynamic(status="completed", success=False), protocol.completed()],
            lambda *_: {"isError": True, "content": [{"type": "text", "text": "Fixture action refused"}]})
        metrics = result.tool_telemetry
        self.assertEqual((metrics["callback_errors"], metrics["host_observed_errors"], metrics["errors"]), (1, 1, 1))
        self.assertEqual(metrics["host_only_errors"], 0)

    def test_host_only_refusal_and_callback_failure_count_separately(self):
        result, _, _ = self.run_host([
            dynamic(item_id="refusal", status="failed", success=False),
            protocol.tool_call(), dynamic(success=False), protocol.completed()],
            lambda *_: {"isError": True, "content": [{"type": "text", "text": "failed"}]})
        metrics = result.tool_telemetry
        self.assertEqual((metrics["host_only_errors"], metrics["errors"], metrics["callback_calls"]), (1, 2, 1))

    def test_missing_dynamic_stream_cannot_turn_callback_zero_errors_into_total_zero(self):
        result, _, _ = self.run_host([protocol.tool_call(), protocol.completed()])
        metrics = result.tool_telemetry
        self.assertEqual(metrics["coverage"], "unobserved")
        self.assertEqual(metrics["callback_errors"], 0)
        self.assertIsNone(metrics["errors"])
        self.assertIsNone(metrics["host_only_errors"])
        self.assertIn("unmatched_callback_identity", metrics["coverage_reasons"])
        empty, _, _ = self.run_host([protocol.completed()])
        self.assertIsNone(empty.tool_telemetry["errors"])
        self.assertIsNone(empty.tool_telemetry["host_only_errors"])

    def test_unmatched_ids_are_unknown_not_assumed_distinct_refusals(self):
        result, _, _ = self.run_host([
            protocol.tool_call(), dynamic(item_id="different-item-id", status="failed", success=False),
            protocol.completed()], lambda *_: {"isError": True, "content": []})
        metrics = result.tool_telemetry
        self.assertEqual((metrics["matched_calls"], metrics["known_error_lower_bound"]), (0, 1))
        self.assertIsNone(metrics["host_only_errors"])
        self.assertIsNone(metrics["errors"])
        self.assertEqual(metrics["coverage"], "partial")

    def test_same_id_requires_same_tool_and_arguments_for_correlation(self):
        for changes in ({"tool": "different"}, {"arguments": {"value": 2}},
                        {"arguments": {"value": True}}, {"namespace": "foreign"}):
            with self.subTest(changes=changes):
                result, _, _ = self.run_host([protocol.tool_call(), dynamic(**changes), protocol.completed()])
                self.assertEqual(result.tool_telemetry["matched_calls"], 0)
                self.assertIsNone(result.tool_telemetry["errors"])

    def test_callback_argument_mutation_does_not_change_original_correlation(self):
        def execute(name, arguments):
            arguments["value"] = "sanitized by scoped engine"
            return {"content": [{"type": "text", "text": "ok"}]}
        result, _, _ = self.run_host([
            dynamic("started"), protocol.tool_call(), dynamic(), protocol.completed()], execute)
        self.assertEqual((result.tool_telemetry["matched_calls"], result.tool_telemetry["coverage"]), (1, "complete"))

    def test_string_arguments_correlate_only_after_valid_json_parsing(self):
        result, _, _ = self.run_host([protocol.tool_call(arguments='{"value":1}'),
                                    dynamic(arguments='{"value":1}'), protocol.completed()])
        self.assertEqual(result.tool_telemetry["matched_calls"], 1)

    def test_missing_or_unrecognized_terminal_preserves_unknown(self):
        for notification in (dynamic("started"), dynamic(success=None),
                             dynamic(status="surprise"), dynamic(status=["completed"]),
                             dynamic(id=None), dynamic(status="failed", success=1),
                             dynamic(status="failed", success=False, namespace=False)):
            with self.subTest(notification=notification):
                result, _, _ = self.run_host([notification, protocol.completed()])
                self.assertEqual(result.status, "completed")
                self.assertIsNone(result.tool_telemetry["errors"])

    def test_timeout_after_start_is_unknown_and_keeps_observed_attempt(self):
        result, _, _ = self.run_host([dynamic("started")])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.tool_telemetry["host_observed_attempts"], 1)
        self.assertEqual(result.tool_telemetry["unknown_host_outcomes"], 1)
        self.assertIn("turn_not_completed", result.tool_telemetry["coverage_reasons"])
        self.assertIsNone(result.tool_telemetry["errors"])

    def test_contradictory_terminal_or_callback_is_never_complete_coverage(self):
        sequences = ([dynamic(status="failed", success=True)],
                     [dynamic(), dynamic(status="failed", success=False)],
                     [dynamic(status="failed", success=False), dynamic()],
                     [protocol.tool_call(), dynamic(status="failed", success=False)])
        for sequence in sequences:
            with self.subTest(sequence=sequence):
                result, _, _ = self.run_host([*sequence, protocol.completed()])
                self.assertIsNone(result.tool_telemetry["errors"])
                self.assertEqual(result.tool_telemetry["coverage"], "partial")
                self.assertGreaterEqual(result.tool_telemetry["known_error_lower_bound"], 1)

    def test_cross_trial_dynamic_notification_is_rejected_before_telemetry(self):
        for field in ("threadId", "turnId"):
            notification = dynamic()
            notification["params"][field] = "foreign"
            result, _, events = self.run_host([notification, protocol.completed()])
            self.assertEqual(result.status, "isolation_error")
            self.assertFalse(any(event["type"] == "host_tool" for event in events))

    def test_host_events_whitelist_tool_fields_and_bound_text_without_hidden_data(self):
        notification = dynamic(status="failed", success=False, hiddenReasoning="SECRET_REASONING",
                               auth={"token": "SECRET_AUTH"}, contentItems=[
                                   {"type": "inputText", "text": "x" * 5000},
                                   {"type": "inputImage", "imageUrl": "SECRET_IMAGE"}])
        result, _, events = self.run_host([notification, protocol.completed()])
        event = next(event for event in events if event["type"] == "host_tool")
        self.assertNotIn("SECRET", str(event))
        self.assertEqual(len(event["output_text"][0]), 4000)
        self.assertEqual(event["output_content_types"], ["inputText", "inputImage"])
        self.assertEqual(result.as_dict()["tool_telemetry"], result.tool_telemetry)


class AdapterHostTelemetryTests(unittest.TestCase):
    def test_semantic_action_error_stays_separate_from_host_transport_success(self):
        raw = {"content": [], "structuredContent": {"outcome": {"state": "failed", "verified": False}}}
        outcome, _, host_events = protocol.HostProtocolTests().run_host([
            dynamic("started"), protocol.tool_call(), dynamic(), protocol.completed()], lambda *_: raw)
        self.assertEqual(outcome.tool_telemetry["errors"], 0)
        self.assertIn("semantic action failures", outcome.tool_telemetry["error_scope"])
        tool_event = next(event for event in host_events if event["type"] == "tool")
        data = {**adapter_protocol.result(), "tool_telemetry": outcome.tool_telemetry}
        _, result, events, _, _, _ = adapter_protocol.AdapterTests().invoke(
            data=data, host_run=lambda emit: emit(tool_event))
        self.assertFalse(next(event for event in events if event["type"] == "tool")["ok"])
        self.assertEqual(result["tool_telemetry"]["errors"], 0)

    def test_new_host_evidence_reaches_trace_and_result_without_driver_events(self):
        telemetry = {"schema_version": 1, "coverage": "partial", "errors": None,
                     "host_observed_errors": 1, "host_only_errors": None}
        host_data = {**adapter_protocol.result(), "tool_telemetry": telemetry}
        runner = adapter_protocol.AdapterTests()
        code, result, events, trace, _, _ = runner.invoke(data=host_data, host_run=lambda emit: emit({
            "type": "host_tool", "item_id": "host-refusal", "name": "fixture_action",
            "phase": "completed", "status": "failed", "success": False, "at_ms": 1}))
        self.assertEqual((code, result["status"]), (0, "completed"))
        self.assertEqual(result["tool_telemetry"], telemetry)
        self.assertEqual((result["model_tool_calls"], result["driver_tool_calls"]), (0, 0))
        self.assertFalse(any(event["type"] == "tool" for event in events))
        self.assertEqual(next(item for item in trace if item.get("source") == "host_result")["tool_telemetry"], telemetry)
        self.assertEqual(next(item for item in trace if item.get("source") == "adapter_result")["tool_telemetry"], telemetry)
        self.assertTrue(any(item.get("type") == "host_tool" for item in trace))

    def test_legacy_or_other_host_missing_telemetry_remains_unknown(self):
        _, result, _, _, _, _ = adapter_protocol.AdapterTests().invoke()
        self.assertIsNone(result["tool_telemetry"])


if __name__ == "__main__":
    unittest.main()
