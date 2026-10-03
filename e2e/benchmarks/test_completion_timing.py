"""Conservative completion timing from paired traces; no app or model is launched."""
import copy
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks.completion_timing import verified_native_completion
from benchmarks.tasks import NOTE


def evidence():
    row = {"task_id": "native_notes", "status": "PASS", "independent_task_success": True,
           "fixture_environment": {"calculator_initial": {"id": 10, "pid": 100},
                                   "textedit_initial": {"id": 11, "pid": 101}}}
    final_note = NOTE + "\nTotal: 42"
    args = [{"window_id": 10, "steps": [{"tool": "click", "element_index": 3}]},
            {"window_id": 11, "steps": [{"tool": "type_text", "text": "\nTotal: 42"}],
             "expect": {"element_index": 1, "value": final_note}}]
    trees = ['+    AXStaticText "Edit field" value="42"',
             '+    [1] AXTextArea "" value=' + json.dumps(final_note)]
    events, trace = [], []
    for index, (arguments, tree) in enumerate(zip(args, trees)):
        start, end = (100, 120) if index == 0 else (300, 325)
        call_id = f"call-{index}"
        events.append({"type": "tool", "origin": "model", "call_id": call_id, "name": "batch",
                       "arguments": arguments, "start_ms": start, "end_ms": end, "ok": True})
        result = {"observation": {"tree": tree, "window": {"id": 10 + index, "pid": 100 + index}},
                  "window_id": 10 + index, "stopped_early": False,
                  "steps": [{"ok": True, "uncertain": False}]}
        if index:
            result.update(expect={"status": "verified"}, outcome={"state": "verified", "verified": True})
        trace.extend([
            {"source": "engine", "type": "tool", "phase": "model", "name": "batch",
             "start_ms": start + 1, "end_ms": end - 1, "ok": True},
            {"source": "model_host", "type": "tool", "call_id": call_id, "name": "batch",
             "arguments": arguments, "start_ms": start - 40, "end_ms": end - 40,
             "status": "completed", "result": {"isError": False,
                 "content": [{"type": "text", "text": json.dumps(result)}]}}])
    events.append({"type": "result", "status": "completed", "adapter_wall_ms": 400})
    return row, events, trace


def payload(trace, index=-1):
    return json.loads(trace[index]["result"]["content"][0]["text"])


def change_payload(trace, change, index=-1):
    value = payload(trace, index)
    change(value)
    trace[index]["result"]["content"][0]["text"] = json.dumps(value)


class VerifiedCompletionTests(unittest.TestCase):
    def assert_unavailable(self, row, events, trace):
        result = verified_native_completion(row, events, trace)
        self.assertIsNone(result["verified_completion_ms"])
        self.assertIsNone(result["post_verification_tail_ms"])
        self.assertTrue(result["verified_completion_basis"]["unavailable_reason"])

    def test_uses_normalized_adapter_clock_and_later_independent_judgment(self):
        row, events, trace = evidence()
        result = verified_native_completion(row, events, trace)
        self.assertEqual(result["verified_completion_ms"], 325)
        self.assertEqual(result["post_verification_tail_ms"], 75)
        self.assertEqual(result["verified_completion_basis"]["clock_origin"], "adapter")
        self.assertEqual(result["verified_completion_basis"]["call_id"], "call-1")
        self.assertIs(result["verified_completion_basis"]["first_goal_achievement_measured"], False)

    def test_requires_clean_native_independent_success(self):
        for key, values in (("task_id", ["browser_work_order"]), ("status", ["FAIL", "INCONCLUSIVE", None]),
                            ("independent_task_success", [False, None, 1])):
            for value in values:
                with self.subTest(key=key, value=value):
                    row, events, trace = evidence()
                    row[key] = value
                    self.assert_unavailable(row, events, trace)

    def test_refuses_missing_duplicate_or_mismatched_call_pairing(self):
        for mutation in (lambda e, t: e[1].pop("call_id"), lambda e, t: t.append(copy.deepcopy(t[-1])),
                         lambda e, t: t[-1].update(call_id="other"),
                         lambda e, t: t[-1].update(arguments={"window_id": 12}),
                         lambda e, t: t[-1].update(name="click"),
                         lambda e, t: e.insert(0, copy.deepcopy(e[0]))):
            row, events, trace = evidence()
            mutation(events, trace)
            self.assert_unavailable(row, events, trace)

    def test_refuses_failed_uncertain_and_unverified_results(self):
        changes = [lambda p: p.update(stopped_early=True),
                   lambda p: p["steps"][0].update(uncertain=True),
                   lambda p: p["steps"][0].update(ok=False),
                   lambda p: p["expect"].update(status="timeout"),
                   lambda p: p["outcome"].update(verified=False),
                   lambda p: p.pop("expect")]
        for change in changes:
            row, events, trace = evidence()
            change_payload(trace, change)
            self.assert_unavailable(row, events, trace)
        row, events, trace = evidence()
        trace[-1]["result"]["isError"] = True
        self.assert_unavailable(row, events, trace)

    def test_requires_the_exact_note_and_observed_calculator_value(self):
        for tree, index in [('AXStaticText "Edit field" value="0"', 1),
                            ('- AXStaticText "Edit field" value="42"', 1),
                            ('[1] AXTextArea "" value="Total: 42"', -1),
                            ('- [1] AXTextArea "" value=' + json.dumps(NOTE + '\nTotal: 42'), -1)]:
            row, events, trace = evidence()
            change_payload(trace, lambda p: p["observation"].update(tree=tree), index)
            self.assert_unavailable(row, events, trace)

    def test_requires_exact_requested_postcondition_not_generic_verified_claim(self):
        row, events, trace = evidence()
        events[1]["arguments"]["expect"] = {"text": "Total: 42"}
        self.assert_unavailable(row, events, trace)

    def test_requires_exact_fixture_window_and_process(self):
        for field, value in (("id", 99), ("pid", 999)):
            row, events, trace = evidence()
            change_payload(trace, lambda p: p["observation"]["window"].update({field: value}))
            self.assert_unavailable(row, events, trace)

    def test_requires_actual_driver_execution_and_valid_normalized_time(self):
        for mutation in (lambda e, t: t.pop(2), lambda e, t: t[2].update(ok=False),
                         lambda e, t: e[1].update(end_ms=401),
                         lambda e, t: e[1].update(start_ms=True),
                         lambda e, t: e[-1].update(adapter_wall_ms=float("nan"))):
            row, events, trace = evidence()
            mutation(events, trace)
            self.assert_unavailable(row, events, trace)

    def test_later_model_call_prevents_claiming_earlier_goal_as_final(self):
        row, events, trace = evidence()
        extra = copy.deepcopy(events[0])
        extra.update(call_id="call-2", start_ms=330, end_ms=340)
        events.insert(-1, extra)
        extra_trace = copy.deepcopy(trace[1])
        extra_trace.update(call_id="call-2")
        trace.append(extra_trace)
        self.assert_unavailable(row, events, trace)

    def test_prefetch_is_not_mistaken_for_a_model_call(self):
        row, events, trace = evidence()
        events.insert(0, {"type": "tool", "origin": "host_prefetch", "name": "get_window_state",
                          "start_ms": 1, "end_ms": 10, "ok": True})
        self.assertEqual(verified_native_completion(row, events, trace)["verified_completion_ms"], 325)

    def test_malformed_native_results_produce_unknown_instead_of_raising(self):
        for change in (lambda p: p.update(expect="verified"), lambda p: p.update(outcome=None),
                       lambda p: p.update(observation=[]), lambda p: p.update(steps=[None]),
                       lambda p: p["observation"].update(tree=None)):
            row, events, trace = evidence()
            change_payload(trace, change)
            self.assert_unavailable(row, events, trace)
        for malformed in ("false", 0, [], None):
            row, events, trace = evidence()
            trace[-1]["result"]["isError"] = malformed
            self.assert_unavailable(row, events, trace)


if __name__ == "__main__":
    unittest.main()
