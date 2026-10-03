"""Adapter protocol/deadline tests: no model, subprocess, or desktop access."""
import contextlib
import copy
import io
import json
import os
import pathlib
import signal
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks import model_adapter as adapter


def request(timeout=10):
    config = {"model": "gpt-6.1-sol", "reasoning_effort": "high", "max_steps": 12,
              "timeout_seconds": timeout, "model_host_version": "codex-cli test-1"}
    return {"protocol": adapter.report.CONTRACT, "task_id": "browser_work_order",
            "prompt": "Complete the disposable fixture.", "track": "host-background",
            "interface_track": "native-tools", "engine": "second-cursor-model",
            "engine_version": "sha256:pinned-test-binary", "model": config["model"],
            "model_config": config, "timeout_seconds": timeout, "runner_sha256": "test-runner"}


def result():
    return {"status": "completed", "model": "gpt-6.1-sol", "reasoning_effort": "high",
            "model_host_version": "codex-cli test-1", "isolation": {"verified": True},
            "usage": {"inputTokens": 10}, "final_response": "Done", "error": None}


class FakeEngine:
    def __init__(self):
        self.identity = {"engine": "second-cursor", "interface_track": "native-tools",
                         "scope_policy": "fixture-only-v1", "engine_version": "sha256:pinned-test-binary",
                         "verified": True}
        self.context, self.cleanup_errors = {"targets": {"tab_id": 1}}, []
        self.started = self.closed = False

    def start(self):
        self.started = True

    def tools(self):
        return [{"name": "fixture_action", "description": "Test", "inputSchema": {"type": "object"}}]

    def call(self, name, args):
        raise AssertionError("These tests must never dispatch an action")

    def close(self):
        self.closed = True


class AdapterTests(unittest.TestCase):
    def invoke(self, *, engine=None, data=None, host_run=None, req=None, cleanup_timeout=5):
        engine = engine or FakeEngine()
        data = result() if data is None else data
        received = []

        class FakeHost:
            def __init__(self, config):
                received.append(config)

            def run(self, prompt, tools, execute_tool, emit_event=None):
                self_test.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
                if host_run:
                    host_run(emit_event)
                return types.SimpleNamespace(as_dict=lambda: copy.deepcopy(data))

        self_test = self
        stdout = io.StringIO()
        previous_alarm, previous_term = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(stdout), \
                patch.dict(os.environ, {"SC_BENCH_TRACE_DIR": directory}), \
                patch.object(adapter, "runner_fingerprint", return_value="test-runner"), \
                patch.object(adapter, "create_engine", return_value=engine), \
                patch.object(adapter, "CodexModelHost", FakeHost), \
                patch.object(adapter, "CLEANUP_TIMEOUT_SECONDS", cleanup_timeout):
            code = adapter.run_adapter(req or request(), types.SimpleNamespace(
                engine="second-cursor", cua_binary="unused", codex_binary="unused"))
            trace = [json.loads(line) for line in (pathlib.Path(directory) / "model-trace.jsonl").read_text().splitlines()]
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous_alarm)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_term)
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(sum(item["type"] == "result" for item in events), 1)
        self.assertEqual(events[-1]["type"], "result")
        return code, events[-1], events, trace, engine, received

    def test_complete_requires_identity_isolation_and_cleanup(self):
        code, final, _, _, engine, configs = self.invoke()
        self.assertEqual((code, final["status"]), (0, "completed"))
        self.assertTrue(engine.closed)
        self.assertLess(configs[0].timeout_seconds, request()["timeout_seconds"])
        self.assertEqual(configs[0].model, "gpt-6.1-sol")
        self.assertEqual(configs[0].reasoning_effort, "high")
        self.assertIsNone(final["model_ms"])
        self.assertIsNone(final["cost_usd"])

    def test_unverified_or_inconsistent_completion_fails(self):
        for changes in ({"isolation": {"verified": 1}}, {"isolation": {}}, {"error": "failed"},
                        {"model": "wrong"}, {"model_host_version": "wrong"}, {"reasoning_effort": "ultra"},
                        {"status": "invented"}):
            with self.subTest(changes=changes):
                code, final, _, _, engine, _ = self.invoke(data={**result(), **changes})
                self.assertEqual((code, final["status"]), (1, "failed"))
                self.assertTrue(engine.closed)

    def test_unverified_engine_does_not_start_model(self):
        for changes in ({"verified": False}, {"engine_version": "wrong"}, {"engine": "wrong"}):
            with self.subTest(changes=changes):
                engine = FakeEngine()
                engine.identity.update(changes)
                code, final, _, _, _, configs = self.invoke(engine=engine)
                self.assertEqual((code, final["status"], configs), (1, "failed", []))
                self.assertTrue(engine.closed)

    def test_failed_host_stays_failed(self):
        for status in ("failed", "timeout", "step_limit", "isolation_error"):
            with self.subTest(status=status):
                code, final, _, _, _, _ = self.invoke(data={**result(), "status": status})
                self.assertEqual((code, final["status"], final["host_status"]), (1, "failed", status))

    def test_failed_host_without_model_preserves_root_cause(self):
        message = "Received an event for another trial thread"
        code, final, _, _, _, _ = self.invoke(data={**result(), "status": "isolation_error",
                                                   "model": None, "error": message})
        self.assertEqual((code, final["status"], final["error"]), (1, "failed", message))
        self.assertNotIn("identity_error", final)

    def test_failed_host_reports_wrong_model_without_hiding_root_cause(self):
        code, final, _, _, _, _ = self.invoke(data={**result(), "status": "isolation_error",
                                                   "model": "wrong", "error": "Provider rerouted model"})
        self.assertEqual((code, final["status"], final["error"]), (1, "failed", "Provider rerouted model"))
        self.assertIn("identity differs", final["identity_error"])

    def test_cleanup_errors_prevent_success(self):
        engine = FakeEngine()
        engine.cleanup_errors.append("Could not release owned fixture")
        code, final, _, _, _, _ = self.invoke(engine=engine)
        self.assertEqual((code, final["status"]), (1, "failed"))
        self.assertEqual(final["cleanup_errors"], engine.cleanup_errors)

    def test_malformed_callback_cannot_be_swallowed_into_success(self):
        def run(emit):
            try:
                emit({"type": "tool", "name": "fixture_action", "start_ms": 4, "end_ms": 2,
                      "status": "completed"})
            except ValueError:
                pass
        code, final, _, _, _, _ = self.invoke(host_run=run)
        self.assertEqual((code, final["status"]), (1, "failed"))
        self.assertIn("callback failed", final["error"])

    def test_tool_measurements_keep_failed_attempts_and_unknown_cost(self):
        def run(emit):
            for status in ("completed", "error", "timeout"):
                emit({"type": "tool", "name": "fixture_action", "start_ms": 1, "end_ms": 2,
                      "status": status})
        _, final, events, _, _, _ = self.invoke(host_run=run)
        self.assertEqual([item["ok"] for item in events[:-1]], [True, False, False])
        self.assertTrue(all(item["end_ms"] >= item["start_ms"] for item in events[:-1]))
        self.assertIsNone(final["cost_usd"])

    def test_setup_deadline_interrupts_and_cleans(self):
        engine = FakeEngine()
        engine.start = lambda: time.sleep(1)
        began = time.monotonic()
        code, final, _, _, _, configs = self.invoke(engine=engine, req=request(.035))
        self.assertLess(time.monotonic() - began, .8)
        self.assertEqual((code, final["status"], configs), (1, "failed", []))
        self.assertTrue(engine.closed)
        self.assertIn("AdapterDeadline", final["error"])

    def test_cleanup_deadline_is_bounded_and_fails(self):
        engine = FakeEngine()
        engine.close = lambda: time.sleep(1)
        began = time.monotonic()
        code, final, _, _, _, _ = self.invoke(engine=engine, cleanup_timeout=.035)
        self.assertLess(time.monotonic() - began, .8)
        self.assertEqual((code, final["status"]), (1, "failed"))
        self.assertIn("AdapterDeadline", final["cleanup_errors"][0])

    def test_sigterm_during_model_executes_cleanup(self):
        def run(emit):
            os.kill(os.getpid(), signal.SIGTERM)
        code, final, _, _, engine, _ = self.invoke(host_run=run)
        self.assertEqual((code, final["status"]), (1, "failed"))
        self.assertTrue(engine.closed)
        self.assertIn("SIGTERM", final["error"])

    def test_trace_redacts_keys(self):
        def run(emit):
            emit({"type": "host", "authorization": "private", "nested": {"token": "private"}})
        _, _, _, trace, _, _ = self.invoke(host_run=run)
        event = next(item for item in trace if item["source"] == "model_host")
        self.assertEqual((event["authorization"], event["nested"]["token"]), ("[redacted]", "[redacted]"))

    def test_stale_request_refused_before_resources(self):
        with patch.object(adapter, "runner_fingerprint", return_value="different"), \
                patch.object(adapter, "create_engine") as create, self.assertRaises(ValueError):
            adapter.run_adapter(request(), types.SimpleNamespace(engine="second-cursor"))
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
