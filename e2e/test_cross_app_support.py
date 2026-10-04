"""Negative controls for replay inputs and independent native outcome checks."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from cross_app_support import MODES, activity_overlapped, judge_native, original_note, schedule, summary_line


class CrossAppSupportTests(unittest.TestCase):
    def test_schedules_are_replayable_and_seed_changes_interleaving(self):
        self.assertEqual(schedule(41, "interleaved"), schedule(41, "interleaved"))
        self.assertNotEqual([item.get("after_call") for item in schedule(41, "interleaved")],
                            [item.get("after_call") for item in schedule(42, "interleaved")])
        for mode in MODES:
            events = schedule(9, mode)
            self.assertEqual(len({event["id"] for event in events}), len(events))
            self.assertEqual([event["after_call"] for event in events if event["phase"] == "browser"],
                             sorted(event["after_call"] for event in events if event["phase"] == "browser"))

    def test_bad_replay_inputs_refused(self):
        for seed in (-1, True, 2 ** 63, 1.5):
            with self.assertRaises(ValueError):
                schedule(seed, "interleaved")
        with self.assertRaises(ValueError):
            schedule(1, "unknown")

    def test_activity_must_deliver_during_the_phase(self):
        before = {"activity_running": True, "activity_count": 10}
        self.assertTrue(activity_overlapped(before, {"activity_running": True, "activity_count": 11}))
        for after in ({"activity_running": True, "activity_count": 10},
                      {"activity_running": False, "activity_count": 11},
                      {"activity_running": True, "activity_count": 9}, None):
            self.assertFalse(activity_overlapped(before, after))

    def fixture(self):
        note = original_note(41)
        external = "\nNewer user note."
        summary = summary_line("INV-1234", 12005, "Standard", "RCPT-9012")
        return dict(original=note, external=external, summary=summary,
                    live=note + external + summary, saved=note + external + summary,
                    events=[{"kind": "native_summary_dispatch"}], required_events=["one", "two"], fired_events=["one", "two"])

    def test_exact_live_and_saved_result_pass(self):
        self.assertTrue(all(judge_native(**self.fixture()).values()))

    def test_repaired_duplicate_dispatch_still_fails(self):
        value = self.fixture()
        value["events"].append({"kind": "native_summary_dispatch"})
        self.assertFalse(judge_native(**value)["native_summary_dispatched_once"])

    def test_lost_external_edit_is_not_hidden_by_correct_summary(self):
        value = self.fixture()
        value["live"] = value["original"] + value["summary"]
        checks = judge_native(**value)
        self.assertFalse(checks["native_live_text_exact"])
        self.assertFalse(checks["native_original_prefix_preserved"])

    def test_unsaved_result_fails(self):
        value = self.fixture()
        value["saved"] = value["original"]
        self.assertFalse(judge_native(**value)["native_saved_text_exact"])

    def test_duplicate_summary_and_wrong_result_fail(self):
        value = self.fixture()
        value["live"] += value["summary"]
        self.assertFalse(judge_native(**value)["native_summary_present_exactly_once"])
        value = self.fixture()
        value["live"] = value["live"].replace("$120.05", "$120.50")
        self.assertFalse(judge_native(**value)["native_live_text_exact"])

    def test_missing_or_reordered_disruption_fails(self):
        for fired in (["one"], ["two", "one"], ["one", "two", "one"]):
            value = self.fixture()
            value["fired_events"] = fired
            self.assertFalse(judge_native(**value)["all_planned_disruptions_exercised"])


if __name__ == "__main__":
    unittest.main()
