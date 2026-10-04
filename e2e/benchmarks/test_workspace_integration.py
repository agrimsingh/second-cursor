"""Workspace harness wiring and scope checks; no browser, driver, or model launches."""
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import best_in_class as report
import model_benchmark as pilot
import run as harness
from benchmarks import tasks
from benchmarks.engines import ScopeError, ScopedEngine
from benchmarks.test_engines import FakeMCP, content, sc_tools


class FakeFixture:
    def __init__(self):
        self.url = "http://127.0.0.1:8123/fixture.html"
        self.prompt = "Review the visible workspace and complete the handoff."
        self.metadata = {"task_id": "workspace_handoff", "seed": 9,
                         "case_id": "case-9", "fixture_version": "test-version"}
        self.evidence = {"case_metadata": self.metadata, "baseline": {"private_expected": "do not expose"},
                         "state": {"durable": "saved"}, "events": []}
        self.checks = {"target_updated": True, "unrelated_records_preserved": True}
        self.closed, self.judged = False, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def case_metadata(self):
        return copy.deepcopy(self.metadata)

    def read(self):
        return copy.deepcopy(self.evidence)

    def judge(self, evidence=None):
        self.judged.append(evidence)
        return copy.deepcopy(self.checks)


class FakeContext:
    def __init__(self):
        self.checks, self.cleanups = [], []
        self.run = types.SimpleNamespace(invariants=Mock(return_value=[]))

    def check(self, label, ok, detail=None):
        self.checks.append({"label": label, "ok": bool(ok), "detail": detail})
        return bool(ok)


class WorkspaceIntegrationTests(unittest.TestCase):
    def test_workspace_tasks_are_opt_in_to_both_clis(self):
        self.assertEqual(report.DEFAULT_TASKS, ("native_notes", "browser_work_order", "canvas_route", "transient_dialog"))
        self.assertEqual(report.TASKS, report.DEFAULT_TASKS + report.WORKSPACE_TASKS)
        self.assertIsNone(report.parser().parse_args(["tasks"]).task)
        self.assertIsNone(pilot.parser().parse_args([]).task)
        for task in report.WORKSPACE_TASKS:
            self.assertEqual(report.parser().parse_args(["tasks", "--task", task]).task, [task])
            self.assertEqual(pilot.parser().parse_args(["--task", task]).task, [task])
            self.assertTrue(tasks.browser_task(task))

    def plan(self, selected=None):
        options = ["--model", "gpt-6.1-sol", "--reasoning-effort", "high", "--rounds", "2", "--seed", "9"]
        for task in selected or []:
            options += ["--task", task]
        with patch.object(pilot, "version", return_value="pinned-version"), \
                patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                patch.object(report, "digest", return_value="binary-sha"), \
                patch.object(report, "metadata", return_value={"fixture_sha256": "fixture-sha", "task_sha256": "task-sha"}), \
                patch.object(pilot, "runner_fingerprint", return_value="runner-sha"), \
                patch.object(pilot, "browser_identity", return_value={"binary": "/fixture/browser", "version": "1", "sha256": "a" * 64}):
            return pilot.build_plan(pilot.parser().parse_args(options))

    def test_default_plan_does_not_silently_add_workspace_trials(self):
        plan = self.plan()
        self.assertEqual(plan["tasks"], list(report.DEFAULT_TASKS))
        self.assertEqual(plan["trial_count"], 16)
        self.assertIn("fixed fixtures", plan["case_seed_semantics"])

    def test_workspace_plan_pairs_real_case_seeds_and_browser_identity(self):
        plan = self.plan(["workspace_recovery"])
        self.assertEqual(plan["trial_count"], 4)
        self.assertEqual([item["case_seed"] for item in plan["runs"]], [9, 9, 10, 10])
        self.assertIn("generate reproducible case data", plan["case_seed_semantics"])
        self.assertIsNotNone(plan["browser_identity"])
        for item in plan["runs"]:
            command = item["command"]
            self.assertIn("--adapter-browser", command)
            self.assertEqual(command[command.index("--task") + 1], "workspace_recovery")

    def test_builtin_dispatch_uses_public_workspace_driver(self):
        driver = types.SimpleNamespace(execute=Mock())
        ctx, targets, meter = object(), {"url": "fixture", "tab_id": 1}, object()
        with patch.dict(sys.modules, {"benchmarks.workspace_driver": driver}):
            tasks.execute_builtin(ctx, "workspace_reconcile", targets, meter)
        driver.execute.assert_called_once_with(ctx, "workspace_reconcile", targets, meter)

    def test_adapter_failure_still_judges_and_keeps_goal_only_request(self):
        fixture, ctx, row = FakeFixture(), FakeContext(), {"case_seed": 9}
        args = report.parser().parse_args(["tasks", "--task", "workspace_handoff", "--adapter-command", '["fake"]',
                                           "--engine", "fake-scripted"])
        observed = {"frontmost": {"pid": 40}, "active_space": 1}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tasks, "WorkspaceFixture", return_value=fixture) as start, \
                patch.object(tasks, "execute_external", side_effect=RuntimeError("adapter failure")), \
                patch.object(harness, "observe", return_value=observed):
            output = pathlib.Path(directory)
            with self.assertRaisesRegex(RuntimeError, "adapter failure"):
                tasks.task_trial(ctx, "workspace_handoff", 1, args, row, output)
            trial_dir = output / "trials/workspace_handoff-1"
            start.assert_called_once_with("workspace_handoff", 9, trial_dir)
            request = json.loads((trial_dir / "request.json").read_text())
            self.assertEqual(request["prompt"], fixture.prompt + tasks.RULES)
            self.assertEqual(request["targets"], {"url": fixture.url})
            self.assertNotIn("private_expected", json.dumps(request))
            self.assertEqual(json.loads((trial_dir / "case-manifest.json").read_text()), fixture.metadata)
            persisted = json.loads((trial_dir / "independent-result.json").read_text())
            self.assertEqual(persisted["ledger"], fixture.evidence)
            self.assertEqual(fixture.judged[-1], persisted["ledger"])
            self.assertTrue(row["independent_task_success"])
            self.assertTrue(all(name in row["independent_checks"] for name in fixture.checks))
        for cleanup in ctx.cleanups:
            cleanup()
        self.assertTrue(fixture.closed)

    def test_failed_checks_all_remain_in_independent_result(self):
        fixture, ctx = FakeFixture(), FakeContext()
        fixture.checks = {"target_updated": False, "unrelated_preserved": True, "saved_durably": False}
        row = {"case_metadata": fixture.metadata}
        with patch.object(tasks.time, "monotonic", side_effect=[0, 3]):
            evidence = tasks.judge_workspace(ctx, fixture, row)
        self.assertFalse(row["independent_task_success"])
        self.assertTrue(all(evidence["checks"][key] is value for key, value in fixture.checks.items()))
        self.assertEqual(len(ctx.checks), len(fixture.checks) + 2)

    def test_builtin_receives_same_public_goal_without_changing_stored_targets(self):
        fixture, ctx, row = FakeFixture(), FakeContext(), {"case_seed": 9}
        ctx.call = Mock(return_value={"tab": {"id": 7}})
        ctx.mcp = Mock()
        args = report.parser().parse_args(["tasks", "--task", "workspace_handoff"])
        observed = {"frontmost": {"pid": 40}, "active_space": 1}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tasks, "WorkspaceFixture", return_value=fixture), \
                patch.object(tasks, "execute_builtin") as execute, \
                patch.object(harness, "observe", return_value=observed):
            output = pathlib.Path(directory)
            tasks.task_trial(ctx, "workspace_handoff", 1, args, row, output)
            request = json.loads((output / "trials/workspace_handoff-1/request.json").read_text())
            self.assertEqual(execute.call_args.args[2], {**request["targets"], "goal": request["prompt"]})
            self.assertNotIn("goal", request["targets"])
            self.assertNotIn("private_expected", json.dumps(execute.call_args.args[2]))
        for cleanup in ctx.cleanups:
            cleanup()

    def test_missing_corrupt_or_malformed_ledger_cannot_pass(self):
        for failure in (FileNotFoundError("state removed"), ValueError("corrupt ledger"), None):
            with self.subTest(failure=failure):
                fixture, ctx = FakeFixture(), FakeContext()
                if failure is None:
                    fixture.evidence = {"state": "malformed"}
                else:
                    fixture.read = Mock(side_effect=failure)
                row = {"case_metadata": fixture.metadata}
                evidence = tasks.judge_workspace(ctx, fixture, row)
                self.assertFalse(row["independent_task_success"])
                self.assertFalse(evidence["checks"]["durable_workspace_evidence_readable"])
                self.assertIn("error", evidence)

    def test_workspace_judge_cannot_return_truthy_non_boolean_checks(self):
        fixture, ctx = FakeFixture(), FakeContext()
        fixture.checks = {"saved": "yes"}
        row = {"case_metadata": fixture.metadata}
        tasks.judge_workspace(ctx, fixture, row)
        self.assertFalse(row["independent_task_success"])

    def test_api_evaluator_and_unrelated_urls_never_dispatch(self):
        url = "http://127.0.0.1:8123/fixture.html"
        for task in report.WORKSPACE_TASKS:
            client = FakeMCP(sc_tools(), {"browser_open": content({"tab": {"id": 4, "url": url}})})
            request = {"task_id": task, "targets": {"url": url}, "timeout_seconds": 5}
            with self.subTest(task=task), ScopedEngine("second-cursor", request, client) as engine:
                engine.call("browser_open", {"url": url})
                for forbidden in ("/api/state", "/api/action", "/judge", "/events", "/workspace.html"):
                    with self.assertRaises(ScopeError):
                        engine.call("browser_navigate", {"tab_id": 4, "url": url.replace("/fixture.html", forbidden)})
                with self.assertRaises(ScopeError):
                    engine.call("browser_navigate", {"tab_id": 4, "url": "https://example.com"})
                for operation in ("eval", "cdp", "upload"):
                    with self.assertRaises(ScopeError):
                        engine.call("browser_command", {"tab_id": 4, "operation": operation, "arguments": {}})
                self.assertEqual([name for name, _ in client.calls], ["browser_open"])
                for backend in ("second-cursor", "cua-driver"):
                    with self.assertRaises(ScopeError):
                        ScopedEngine(backend, {**request, "targets": {"url": url.replace("/fixture.html", "/api/state")}}, client)

    def test_scripted_workspace_comparison_requires_matched_case_seeds(self):
        artifact = {"kind": "tasks", "execution_profile": "scripted", "model": None,
                    "trials": [{"task_id": "workspace_reconcile", "trial": 1, "case_seed": 9}]}
        report.validate_comparison([artifact, copy.deepcopy(artifact)])
        different = copy.deepcopy(artifact)
        different["trials"][0]["case_seed"] = 10
        with self.assertRaisesRegex(ValueError, "case_seed schedules"):
            report.validate_comparison([artifact, different])

    def test_fingerprints_cover_workspace_frontend_backend_and_scripted_driver(self):
        for name in ("workspace.html", "workspace_fixtures.py", "workspace_driver.py"):
            self.assertIn(name, report.TASK_FILES)
            def bytes_for(path):
                return (str(path) + (" changed" if path.name == name else "")).encode()
            with patch.object(report, "extension_identity", return_value={"sha256": "extension-sha"}), \
                    patch.object(pathlib.Path, "read_bytes", lambda path: str(path).encode()):
                before = pilot.runner_fingerprint()
            with patch.object(report, "extension_identity", return_value={"sha256": "extension-sha"}), \
                    patch.object(pathlib.Path, "read_bytes", bytes_for):
                self.assertNotEqual(pilot.runner_fingerprint(), before)

    def test_optional_diagnostics_use_same_snapshot_without_affecting_success(self):
        fixture, ctx = FakeFixture(), FakeContext()
        details = {"write_attempts": 3, "commits": 2, "validation_refusals": 1, "stale_refusals": 0}
        fixture.diagnostics = Mock(return_value=details)
        row = {"case_metadata": fixture.metadata}
        evidence = tasks.judge_workspace(ctx, fixture, row)
        fixture.diagnostics.assert_called_once_with(evidence["ledger"])
        self.assertEqual(row["independent_diagnostics"], details)
        self.assertEqual(evidence["diagnostics"], details)
        self.assertTrue(row["independent_task_success"])
        fixture.diagnostics.side_effect = ValueError("optional diagnostics unavailable")
        row = {"case_metadata": fixture.metadata}
        evidence = tasks.judge_workspace(ctx, fixture, row)
        self.assertIn("diagnostics_error", evidence)
        self.assertTrue(row["independent_task_success"])

    def test_headless_builtin_runner_recognizes_workspace_browser_tasks(self):
        args = report.parser().parse_args(["tasks", "--task", "workspace_recovery", "--seed", "9"])
        with tempfile.TemporaryDirectory() as directory:
            output, existing = pathlib.Path(directory) / "output", pathlib.Path(directory) / "raw"
            output.mkdir()
            existing.mkdir()
            fake = types.SimpleNamespace(dir=existing, results=[], close=Mock())
            def run(*_):
                fake.results = [{"scenario": fn.__name__, "checks": [{"ok": True}],
                                 "invariants": [], "error": None, "status": "PASS"} for fn in harness.SCENARIOS]
            fake.run = run
            with patch.object(report, "artifact_dir", return_value=output), \
                    patch.object(report, "metadata", return_value={"commit": "sha", "fixture_sha256": "f", "task_sha256": "t"}), \
                    patch.object(harness, "SCENARIOS", []), patch.object(harness, "Run", return_value=fake) as create, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tasks.run_tasks(args), 0)
            self.assertTrue(create.call_args.kwargs["browser"])
            payload = json.loads((output / "tasks.json").read_text())
            self.assertEqual([(row["task_id"], row["case_seed"]) for row in payload["trials"]], [("workspace_recovery", 9)])
            self.assertIn("reproducible case data", payload["case_seed_semantics"])


if __name__ == "__main__":
    unittest.main()
