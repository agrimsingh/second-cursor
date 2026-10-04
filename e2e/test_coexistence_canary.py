"""Replay negative controls for the continuous foreground canary oracle.

These traces describe observed outcomes, including bad outcomes which are later
repaired. They never launch a GUI or generate keyboard events.
"""
import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from coexistence_canary import analyze_canary_events


class Trace:
    """Small event recorder with explicit outcome changes for oracle testing."""

    def __init__(self):
        self.commands = []
        self.events = []
        self.state = {
            "active": True,
            "key_window": "A",
            "responders": {"A": "A.editor", "B": "B.editor"},
            "documents": {"A": "Protected canary A.\n", "B": "Protected canary B.\n"},
            "sequence": 0,
            "counters": {},
            "activity_count": 0,
            "activity_running": False,
            "write_error": "",
        }
        self.emit("ready")

    def emit(self, kind, detail=None, boundary=-1):
        self.state["sequence"] += 1
        if kind in {"app_resign_active", "app_become_active", "window_resign_key",
                    "window_become_key", "responder_changed", "text_changed"}:
            counters = self.state["counters"]
            counters[kind] = counters.get(kind, 0) + 1
        sequence = self.state["sequence"]
        self.events.append({**copy.deepcopy(self.state), "kind": kind,
                            "detail": detail or {}, "boundary_id": boundary,
                            "timestamp": 1_700_000_000 + sequence / 100,
                            "uptime": 300 + sequence / 100})

    def command(self, action, **fields):
        command = {"id": len(self.commands) + 1, "action": action, **fields}
        self.commands.append(command)
        if action == "start_activity":
            self.state["activity_running"] = True
        if action == "stop_activity":
            self.state["activity_running"] = False
        self.emit("command_received", command)
        return command["id"]

    def ack(self, command_id):
        self.emit("command_ack", {"id": command_id, "ok": True,
                                  "error": "", "sequence": self.state["sequence"] + 1})

    def edit(self, window, text):
        self.state["documents"][window] = text
        self.emit("text_changed", {"window": window, "text": text})

    def input(self, text, source):
        window = self.state["key_window"]
        detail = {**source, "target": window, "text": text}
        self.emit("input_intent", detail)
        self.edit(window, self.state["documents"][window] + text)
        self.emit("input_result", {**detail, "delivered": True})

    def tick(self):
        self.state["activity_count"] += 1
        count = self.state["activity_count"]
        self.input(f"<{self.state['key_window']}:{count:06d}>", {"activity_tick": count})

    def switch(self, window, *, extra_transitions=False):
        command_id = self.command("focus", window=window)
        previous = self.state["key_window"]
        self.emit("focus_begin", {"id": command_id, "window": window}, command_id)
        if previous != window:
            self.state["key_window"] = "none"
            self.emit("window_resign_key", {"window": previous}, command_id)
            self.state["key_window"] = window
            self.emit("window_become_key", {"window": window}, command_id)
        if extra_transitions:
            self.state["key_window"] = "none"
            self.emit("window_resign_key", {"window": window}, command_id)
            self.state["key_window"] = window
            self.emit("window_become_key", {"window": window}, command_id)
        self.emit("focus_settled", {"id": command_id, "window": window})
        self.ack(command_id)

    def judge(self):
        return analyze_canary_events(self.events, self.commands, self.state)


class CoexistenceCanaryOracleTests(unittest.TestCase):
    def assert_rejected(self, trace, reason):
        result = trace.judge()
        self.assertFalse(result["ok"], result)
        self.assertTrue(any(reason in error for error in result["errors"]), result["errors"])

    def good_trace(self):
        trace = Trace()
        trace.ack(trace.command("start_activity", interval_ms=150))
        trace.tick()
        trace.switch("B")
        command_id = trace.command("type", text="Operator note café ☕.")
        trace.input("Operator note café ☕.", {"command_id": command_id})
        trace.ack(command_id)
        trace.tick()
        trace.ack(trace.command("stop_activity"))
        trace.ack(trace.command("mark", label="final"))
        return trace

    def test_complete_activity_and_commanded_window_switch_pass(self):
        trace = self.good_trace()
        result = trace.judge()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["activity_count"], 2)
        self.assertEqual(result["expected_window"], "B")
        self.assertIn("Operator note café ☕.", result["expected_documents"]["B"])

    def test_wrong_text_then_exact_repair_still_fails(self):
        trace = self.good_trace()
        original = trace.state["documents"]["A"]
        trace.edit("A", original + "WRONG AGENT INPUT")
        trace.edit("A", original)
        self.assert_rejected(trace, "protected text was corrupted")

    def test_foreground_loss_then_restore_still_fails(self):
        trace = self.good_trace()
        trace.state["active"] = False
        trace.emit("app_resign_active")
        trace.state["active"] = True
        trace.emit("app_become_active")
        self.assert_rejected(trace, "foreground application resigned active")

    def test_key_window_loss_then_restore_still_fails(self):
        trace = self.good_trace()
        trace.state["key_window"] = "A"
        trace.emit("window_resign_key", {"window": "B"})
        trace.emit("window_become_key", {"window": "A"})
        trace.emit("window_resign_key", {"window": "A"})
        trace.state["key_window"] = "B"
        trace.emit("window_become_key", {"window": "B"})
        self.assert_rejected(trace, "unrequested focus/responder transition")

    def test_responder_loss_then_restore_still_fails(self):
        trace = self.good_trace()
        trace.state["responders"]["B"] = "foreign.editor"
        trace.emit("responder_changed", {"window": "B"})
        trace.state["responders"]["B"] = "B.editor"
        trace.emit("responder_changed", {"window": "B"})
        self.assert_rejected(trace, "unrequested focus/responder transition")

    def test_extra_focus_loss_inside_valid_command_boundary_fails(self):
        trace = Trace()
        trace.switch("B", extra_transitions=True)
        self.assert_rejected(trace, "focus transitions")

    def test_acknowledged_focus_without_any_transition_fails(self):
        trace = Trace()
        trace.ack(trace.command("focus", window="B"))
        self.assert_rejected(trace, "focus")

    def test_unexercised_issued_command_fails(self):
        trace = self.good_trace()
        trace.commands.append({"id": len(trace.commands) + 1, "action": "type", "text": "missing"})
        self.assert_rejected(trace, "lack execution/acknowledgement")

    def test_missing_ack_fails_even_when_state_and_sequence_are_consistent(self):
        trace = self.good_trace()
        self.assertEqual(trace.events[-1]["kind"], "command_ack")
        trace.events.pop()
        trace.state["sequence"] -= 1
        self.assert_rejected(trace, "lack execution/acknowledgement")

    def test_truncated_ledger_with_newer_atomic_state_fails(self):
        trace = self.good_trace()
        trace.events.pop()
        self.assert_rejected(trace, "Ledger ended at")

    def test_deleted_middle_event_fails(self):
        trace = self.good_trace()
        del trace.events[4]
        self.assert_rejected(trace, "missing, duplicated, or reordered ledger sequence")

    def test_duplicate_command_log_entry_fails(self):
        trace = self.good_trace()
        trace.commands.append(dict(trace.commands[0]))
        self.assert_rejected(trace, "command IDs are not unique")

    def test_unrecorded_transition_counter_fails(self):
        trace = self.good_trace()
        trace.state["counters"]["app_resign_active"] = 1
        self.assert_rejected(trace, "cumulative transition counters")

    def test_missing_input_result_fails(self):
        trace = Trace()
        trace.ack(trace.command("start_activity", interval_ms=150))
        trace.tick()
        trace.events.pop()
        trace.state["sequence"] -= 1
        self.assert_rejected(trace, "text insertion completed")

    def test_misdirected_input_then_repair_still_fails(self):
        trace = Trace()
        command_id = trace.command("type", text="expected A")
        detail = {"command_id": command_id, "target": "A", "text": "expected A"}
        trace.emit("input_intent", detail)
        before = trace.state["documents"]["B"]
        trace.edit("B", before + "expected A")
        trace.edit("B", before)
        trace.edit("A", trace.state["documents"]["A"] + "expected A")
        trace.emit("input_result", {**detail, "delivered": True})
        trace.ack(command_id)
        self.assert_rejected(trace, "protected text was corrupted")

    def test_lost_synthetic_input_fails(self):
        trace = self.good_trace()
        next(event for event in trace.events if event["kind"] == "input_result")["detail"]["delivered"] = False
        self.assert_rejected(trace, "synthetic foreground input was lost")

    def test_ledger_write_failure_fails(self):
        trace = self.good_trace()
        trace.state["write_error"] = "disk full"
        self.assert_rejected(trace, "could not preserve evidence")


if __name__ == "__main__":
    unittest.main()
