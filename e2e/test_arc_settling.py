"""Verify timing bounds and fixture lifecycle without launching a GUI or model."""
import json
import pathlib
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from arc_settling import ArcFixtureServer, PollWatcher, summarize, timing_summary


class TimingTests(unittest.TestCase):
    def watcher(self, negative, positive=None):
        watcher = PollWatcher()
        watcher.accept({"visible": False}, negative, negative + 0.001)
        if positive is not None:
            watcher.accept({"visible": True}, positive - 0.001, positive)
        return watcher

    def test_return_inside_sample_interval_is_not_claimed_early(self):
        result = timing_summary(1, 2, self.watcher(1.99, 2.01))
        self.assertIsNone(result["returned_early"])
        self.assertEqual(result["effect_lower_ms"], 990)
        self.assertEqual(result["effect_upper_ms"], 1010)

    def test_positive_before_return_proves_effect_ready(self):
        result = timing_summary(1, 2.1, self.watcher(1.99, 2.01))
        self.assertIs(result["returned_early"], False)

    def test_negative_after_return_proves_early_return(self):
        result = timing_summary(1, 1.5, self.watcher(1.99, 2.01))
        self.assertIs(result["returned_early"], True)

    def test_never_effect_retains_missing_upper_bound(self):
        result = timing_summary(1, 1.5, self.watcher(1.6))
        self.assertIsNone(result["effect_upper_ms"])
        self.assertIs(result["returned_early"], True)

    def test_later_samples_do_not_overwrite_first_effect_interval(self):
        watcher = self.watcher(1.99, 2.01)
        watcher.accept({"visible": True}, 2.99, 3.01)
        watcher.accept({"visible": False}, 3.99, 4.01)
        self.assertEqual(watcher.first_visible_at, 2.01)
        self.assertEqual(watcher.last_absent_at, 1.99)

    def test_summary_preserves_misses_and_unknown_order(self):
        rows = [{"case": "slow", "return_ms": 1000, "ready_in_result": False,
                 "returned_early": True, "observer_error": None},
                {"case": "slow", "return_ms": 3000, "ready_in_result": True,
                 "returned_early": None, "observer_error": None}]
        summary = summarize(rows)["slow"]
        self.assertEqual(summary["median_return_ms"], 2000)
        self.assertEqual(summary["ready_in_result"], 1)
        self.assertEqual(summary["early_returns"], 1)
        self.assertEqual(summary["timing_boundary_inconclusive"], 1)


class FixtureServerTests(unittest.TestCase):
    def test_fixture_is_served_and_delay_is_real(self):
        with ArcFixtureServer() as fixture:
            with urllib.request.urlopen(fixture.base_url + "/arc-settling.html?case=immediate") as response:
                page = response.read().decode()
            self.assertIn('id="run"', page)
            self.assertIn("window.__arcActionCount", page)
            self.assertNotIn("dataset.actionCount", page)
            started = time.monotonic()
            with urllib.request.urlopen(fixture.base_url + "/delay?ms=60") as response:
                self.assertEqual(json.load(response), {"ok": True})
            self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_longpoll_is_observable_and_teardown_cancels_it(self):
        errors = []
        with ArcFixtureServer() as fixture:
            def pending():
                try:
                    urllib.request.urlopen(fixture.base_url + "/longpoll?case=test", timeout=5).read()
                except Exception as error:
                    errors.append(type(error).__name__)
            thread = threading.Thread(target=pending)
            thread.start()
            self.assertIsNotNone(fixture.wait_longpoll("test", 1))
            self.assertTrue(thread.is_alive())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_stream_exposes_headers_and_partial_bytes_before_completion(self):
        with ArcFixtureServer() as fixture:
            started = time.monotonic()
            with urllib.request.urlopen(fixture.base_url + "/stream?ms=250") as response:
                self.assertEqual(response.headers["Content-Encoding"], "identity")
                first = response.read(10)
                first_at = time.monotonic()
                self.assertLess(first_at - started, 0.2)
                self.assertLess(len(first), int(response.headers["Content-Length"]))
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(first)
                rest = response.read()
                self.assertGreaterEqual(time.monotonic() - first_at, 0.2)
                self.assertEqual(json.loads(first + rest), {"status": "ready"})
            events = [r for r in fixture.requests if r.get("event")]
            self.assertEqual([r["event"] for r in events], ["partial_body_sent", "complete_body_sent"])
            self.assertEqual(events[-1]["bytes"], events[-1]["content_length"])


if __name__ == "__main__":
    unittest.main()
