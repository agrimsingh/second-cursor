"""Pure pilot orchestration tests. No desktop process or model can be launched."""
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import model_benchmark as pilot


class PlanTests(unittest.TestCase):
    def test_upfront_mode_is_pinned_in_plan_adapter_and_harness(self):
        args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high",
            "--engine", "second-cursor", "--task", "native_notes", "--observation-mode", "upfront"])
        with mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(pilot, "version", return_value="codex-cli 1.0"), \
                mock.patch.object(pilot.report, "digest", return_value="binary-hash"), \
                mock.patch.object(pilot.report, "metadata", return_value={"fixture_sha256": "fixture-hash", "task_sha256": "task-hash"}):
            plan = pilot.build_plan(args)
        self.assertEqual(plan["observation_mode"], "upfront")
        self.assertEqual(pilot.planned_artifact(plan, "second-cursor")["observation_mode"], "upfront")
        for item in plan["runs"]:
            command = item["command"]
            self.assertEqual(command[command.index("--observation-mode") + 1], "upfront")
            adapter = json.loads(command[command.index("--adapter-command") + 1])
            self.assertEqual(adapter[adapter.index("--observation-mode") + 1], "upfront")

    def test_upfront_mode_rejects_unmatched_driver_or_task(self):
        for options in (["--engine", "both", "--task", "native_notes"],
                        ["--engine", "second-cursor", "--task", "browser_work_order"]):
            args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high",
                                              "--observation-mode", "upfront", *options])
            with mock.patch.object(pilot, "version", return_value="codex-cli 1.0"), \
                    mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                    mock.patch.object(pilot.report, "digest", return_value="binary-hash"), \
                    self.assertRaisesRegex(ValueError, "upfront"):
                pilot.build_plan(args)

    def test_explicit_model_and_source_identity_are_pinned_before_trials(self):
        args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high",
                                          "--rounds", "2", "--task", "native_notes"])
        with mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(pilot, "version", side_effect=["codex-cli 1.0", "cua-driver 0.33.0"]), \
                mock.patch.object(pilot.report, "digest", return_value="binary-hash"), \
                mock.patch.object(pilot.report, "metadata", return_value={"fixture_sha256": "fixture-hash",
                                                                        "task_sha256": "task-hash"}), \
                mock.patch.object(pilot, "runner_fingerprint", return_value="runner-hash"), \
                mock.patch.object(pilot, "configured_model", side_effect=AssertionError("Must not read defaults")):
            plan = pilot.build_plan(args)
        self.assertEqual(plan["model_config"]["model"], "gpt-6.1-sol")
        self.assertEqual(plan["model_config"]["reasoning_effort"], "high")
        self.assertEqual(plan["task_sha256"], "task-hash")
        self.assertEqual(plan["fixture_sha256"], "fixture-hash")
        self.assertEqual(plan["trial_count"], 4)
        self.assertEqual(plan["native_placement"], "desktop")
        self.assertEqual(plan["browser_visibility"], "headed")
        self.assertIsNone(plan["browser_identity"])
        self.assertEqual([(run["engine"], run["round"], run["case_seed"]) for run in plan["runs"]],
                         [("second-cursor", 1, 1), ("cua-driver", 1, 1),
                          ("cua-driver", 2, 2), ("second-cursor", 2, 2)])
        for run in plan["runs"]:
            self.assertEqual(run["command"][run["command"].index("--model") + 1], "gpt-6.1-sol")
            self.assertEqual(run["command"][run["command"].index("--reasoning-effort") + 1], "high")
            self.assertIn("--headed-browser", run["command"])
            self.assertIn("--adapter-browser", run["command"])
            self.assertEqual(run["command"][run["command"].index("--native-placement") + 1], "desktop")
            self.assertEqual(json.loads(run["command"][run["command"].index("--browser-identity") + 1]), None)

    def test_explicit_placement_and_off_space_alias_are_pinned(self):
        for options, expected in ((["--native-placement", "agent-display"], "agent-display"),
                                  (["--native-placement", "textedit-off-space"], "textedit-off-space"),
                                  (["--off-space"], "textedit-off-space")):
            with self.subTest(options=options):
                args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high",
                                                  "--rounds", "3", "--task", "native_notes", *options])
                with mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                        mock.patch.object(pilot, "version", side_effect=["codex-cli 1.0", "cua-driver 0.33.0"]), \
                        mock.patch.object(pilot.report, "digest", return_value="binary-hash"), \
                        mock.patch.object(pilot.report, "metadata", return_value={"fixture_sha256": "fixture-hash",
                                                                                "task_sha256": "task-hash"}), \
                        mock.patch.object(pilot, "runner_fingerprint", return_value="runner-hash"), \
                        mock.patch.object(pilot, "browser_identity", side_effect=AssertionError("Native-only cohort")):
                    plan = pilot.build_plan(args)
                self.assertEqual(plan["native_placement"], expected)
                self.assertEqual(plan["trial_count"], 6)
                self.assertTrue(all(run["command"][run["command"].index("--native-placement") + 1] == expected
                                    for run in plan["runs"]))
                self.assertTrue(all(pilot.planned_artifact(plan, engine)["native_placement"] == expected
                                    for engine in plan["selected_engines"]))

    def test_shared_actual_browser_identity_is_pinned_and_forwarded_to_both_drivers(self):
        identity = {"binary": "/fixture/Chrome for Testing", "version": "Chrome for Testing 149.0.1",
                    "sha256": "a" * 64}
        args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high", "--rounds", "1"])
        with mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(pilot, "version", side_effect=["codex-cli 1.0", "cua-driver 0.33.0"]), \
                mock.patch.object(pilot.report, "digest", return_value="binary-hash"), \
                mock.patch.object(pilot.report, "metadata", return_value={"fixture_sha256": "fixture-hash",
                                                                        "task_sha256": "task-hash"}), \
                mock.patch.object(pilot, "runner_fingerprint", return_value="runner-hash"), \
                mock.patch.object(pilot, "browser_identity", return_value=identity) as inspect_browser:
            plan = pilot.build_plan(args)
        inspect_browser.assert_called_once_with()
        self.assertEqual(plan["browser_identity"], identity)
        for run in plan["runs"]:
            self.assertIn("--adapter-browser", run["command"])
            self.assertIn("--headed-browser", run["command"])
            self.assertEqual(json.loads(run["command"][run["command"].index("--browser-identity") + 1]), identity)

    def test_explicit_override_cannot_silently_use_configured_model(self):
        args = pilot.parser().parse_args(["--configured-model", "--model", "gpt-6.1-sol"])
        with self.assertRaisesRegex(ValueError, "either"):
            pilot.build_plan(args)


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.iteration = 0
        self.plan = {
            "contract": pilot.report.CONTRACT, "track": "host-background", "native_placement": "desktop",
            "fixture_sha256": "fixture-hash", "task_sha256": "task-hash", "runner_sha256": "runner-hash",
            "browser_visibility": "headed", "browser_identity": {"binary": "/fixture/Chrome for Testing",
                                                                       "version": "Chrome for Testing 149.0.1",
                                                                       "sha256": "a" * 64},
            "interface_track": "native-tools", "selected_engines": ["second-cursor", "cua-driver"],
            "engines": {"second-cursor": "sha256:binary-hash", "cua-driver": "cua-driver 0.33.0"},
            "model_config": {"model": "gpt-6.1-sol", "reasoning_effort": "high", "max_steps": 20,
                             "timeout_seconds": 90, "model_host_version": "codex-cli 1.0"},
            "tasks": ["native_notes", "browser_work_order"], "trial_count": 8, "note": "Fixed fixture pilot.",
            "runs": [{"engine": engine, "round": round_number, "case_seed": round_number + 40,
                      "command": ["mock-trial", engine, str(round_number)]}
                     for round_number in (1, 2)
                     for engine in (["second-cursor", "cua-driver"] if round_number == 1
                                    else ["cua-driver", "second-cursor"])],
        }

    def payload(self, item):
        payload = {**pilot.planned_artifact(self.plan, item["engine"]), "rounds": 1,
                   "status": "PASS", "trials": []}
        for task in self.plan["tasks"]:
            payload["trials"].append({"task_id": task, "trial": 1, "case_seed": item["case_seed"],
                                      "status": "PASS", "runner_sha256": self.plan["runner_sha256"],
                                      "metrics": {"wall_ms": 10}})
        return payload

    def execute(self, payload_change=None, result_change=None, raw=None, broken_index=0, process_error=None,
                comparison_error=None):
        self.iteration += 1
        folder = self.root / str(self.iteration)
        folder.mkdir()
        output = folder / "pilot"
        output.mkdir()
        results = []
        for index, item in enumerate(self.plan["runs"]):
            payload = self.payload(item)
            if index == broken_index and payload_change:
                payload_change(payload)
            artifact = folder / f"child-{index}.json"
            artifact.write_text(raw if raw is not None and index == broken_index else json.dumps(payload))
            result = {"stdout": f"Benchmark: {artifact}\n", "stderr": "", "timed_out": False,
                      "returncode": pilot.report.exit_code(payload.get("status")), "seconds": 1}
            if index == broken_index and result_change:
                result_change(result)
            results.append(process_error if index == broken_index and process_error else result)
        comparison = types.SimpleNamespace(returncode=0, stdout="Comparison complete", stderr="")
        with mock.patch.object(pilot.report, "artifact_dir", return_value=output), \
                mock.patch.object(pilot.report, "run_process", side_effect=results) as processes, \
                mock.patch.object(pilot.subprocess, "run", side_effect=comparison_error,
                                  return_value=comparison) as compare, contextlib.redirect_stdout(io.StringIO()):
            code = pilot.run_plan(copy.deepcopy(self.plan))
        engines = {engine: json.loads((output / f"{engine}.tasks.json").read_text())
                   for engine in self.plan["selected_engines"]}
        runs = json.loads((output / "runs.json").read_text())
        return code, engines, runs, output, processes, compare

    def assert_incomplete(self, outcome):
        code, engines, runs, output, processes, compare = outcome
        self.assertEqual(code, 2)
        self.assertEqual(processes.call_count, 4, "A bad child must not erase remaining planned trials")
        compare.assert_not_called()
        self.assertEqual(sum(len(payload["trials"]) for payload in engines.values()), 8)
        self.assertEqual(sum(row["status"] == "INCOMPLETE" for payload in engines.values()
                             for row in payload["trials"]), 2)
        self.assertEqual(sum(row["status"] == "PASS" for payload in engines.values()
                             for row in payload["trials"]), 6)
        self.assertEqual(sum("error" in entry for entry in runs), 1)
        self.assertIn("no comparative conclusion", (output / "report.md").read_text())
        self.assertIn("Passed / scheduled", (output / "report.md").read_text())

    def test_matched_schedule_aggregates_rounds_and_retains_child_links(self):
        code, engines, runs, output, processes, compare = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(processes.call_count, 4)
        compare.assert_called_once()
        for engine in engines.values():
            self.assertTrue(engine["evidence_complete"])
            self.assertEqual(engine["rounds"], 2)
            self.assertEqual(engine["status"], "PASS")
            self.assertEqual(len(engine["child_artifacts"]), 2)
            self.assertEqual(sorted((row["task_id"], row["trial"], row["case_seed"]) for row in engine["trials"]),
                             sorted((task, round_number, round_number + 40)
                                    for task in self.plan["tasks"] for round_number in (1, 2)))
            self.assertTrue(all(row["child_artifact"] for row in engine["trials"]))

    def test_every_metadata_invariant_is_pinned_before_first_child_and_between_rounds(self):
        changes = {"engine": "different-engine", "engine_version": "cua-driver 0.1.0",
                   "task_sha256": "changed-task", "fixture_sha256": "changed-fixture",
                   "track": "different-track", "native_placement": "textedit-off-space",
                   "interface_track": "screenshot-actions", "runner_sha256": "changed-runner",
                   "browser_visibility": "headless",
                   "contract": "different-contract", "model": "different-model", "execution_profile": "scripted"}
        for key, value in changes.items():
            for index in (0, 3):
                with self.subTest(field=key, run=index):
                    self.assert_incomplete(self.execute(payload_change=lambda p: p.update({key: value}), broken_index=index))

    def test_model_settings_and_trial_runner_are_pinned(self):
        for change in (lambda p: p["model_config"].update(reasoning_effort="low"),
                       lambda p: p["model_config"].update(max_steps=100),
                       lambda p: p["trials"][0].update(runner_sha256="different-runner")):
            with self.subTest(change=change):
                self.assert_incomplete(self.execute(payload_change=change))

    def test_exact_schedule_is_checked_even_with_same_row_count(self):
        for change in (lambda p: p["trials"][0].update(task_id="canvas_route"),
                       lambda p: p["trials"][0].update(case_seed=99),
                       lambda p: p["trials"][0].update(trial=2),
                       lambda p: p["trials"].pop(),
                       lambda p: p["trials"].append(copy.deepcopy(p["trials"][0])),
                       lambda p: p.update(rounds=2)):
            with self.subTest(change=change):
                self.assert_incomplete(self.execute(payload_change=change))

    def test_browser_binary_version_and_hash_cannot_drift_between_children(self):
        for key, value in (("binary", "/different/Chrome for Testing"), ("version", "Chrome for Testing 150.0.1"),
                           ("sha256", "b" * 64)):
            for index in (0, 3):
                with self.subTest(field=key, index=index):
                    self.assert_incomplete(self.execute(payload_change=lambda p: p["browser_identity"].update({key: value}),
                                                         broken_index=index))

    def test_valid_failed_trials_stay_in_denominator_and_comparison(self):
        def fail(payload):
            payload["trials"][0]["status"] = "FAIL"
            payload["trials"][0]["metrics"]["wall_ms"] = 30
            payload["status"] = "FAIL"
        code, engines, runs, output, processes, compare = self.execute(payload_change=fail)
        self.assertEqual(code, 2)
        compare.assert_called_once()
        self.assertTrue(engines["second-cursor"]["evidence_complete"])
        task = engines["second-cursor"]["summary"]["native_notes"]
        self.assertEqual((task["completed"], task["trials"], task["wall_ms"]["median"]), (1, 2, 20))

    def test_status_and_process_exit_must_agree(self):
        for code in (1, 2, -15):
            with self.subTest(exit_code=code):
                self.assert_incomplete(self.execute(result_change=lambda r: r.update(returncode=code)))
        self.assert_incomplete(self.execute(payload_change=lambda p: p.update(status="FAIL")))
        self.assert_incomplete(self.execute(payload_change=lambda p: p["trials"][0].update(status="FAIL")))
        self.assert_incomplete(self.execute(payload_change=lambda p: p["trials"][0].update(status="unverified")))

    def test_deadline_cannot_accept_an_earlier_pass_artifact(self):
        self.assert_incomplete(self.execute(result_change=lambda r: r.update(timed_out=True)))

    def test_cleanup_failure_cannot_accept_a_pass_artifact_and_keeps_evidence(self):
        outcome = self.execute(result_change=lambda r: r.update(cleanup_errors=["owned process remains"],
                                                                 cleanup={"verified": False}))
        self.assert_incomplete(outcome)
        self.assertEqual(outcome[2][0]["cleanup_errors"], ["owned process remains"])
        self.assertEqual(outcome[2][0]["cleanup"], {"verified": False})

    def test_malformed_missing_or_ambiguous_artifact_does_not_stop_remaining_trials(self):
        for raw in ("{", "[]", "null", "{}"):
            with self.subTest(raw=raw):
                self.assert_incomplete(self.execute(raw=raw))
        for stdout in ("No artifact", "Benchmark: /missing/fixture/artifact.json\n",
                       "Benchmark: /first\nBenchmark: /second\n"):
            with self.subTest(stdout=stdout):
                self.assert_incomplete(self.execute(result_change=lambda r: r.update(stdout=stdout)))

    def test_process_spawn_failure_is_persisted_and_remaining_trials_continue(self):
        outcome = self.execute(process_error=OSError("child unavailable"))
        self.assert_incomplete(outcome)
        self.assertIn("child unavailable", outcome[2][0]["error"])

    def test_raw_rejected_child_evidence_and_logs_remain_available(self):
        outcome = self.execute(payload_change=lambda p: p.update(task_sha256="wrong-task"),
                               result_change=lambda r: r.update(stderr="diagnostic output"))
        self.assert_incomplete(outcome)
        run, output = outcome[2][0], outcome[3]
        self.assertEqual(json.loads(pathlib.Path(run["artifact"]).read_text())["task_sha256"], "wrong-task")
        self.assertEqual((output / run["stderr_log"]).read_text(), "diagnostic output")
        incomplete = [row for row in outcome[1]["second-cursor"]["trials"] if row["status"] == "INCOMPLETE"]
        self.assertTrue(all(row["child_artifact"] == run["artifact"] for row in incomplete))

    def test_harness_error_cannot_leave_all_passing_rows_in_aggregate(self):
        self.assert_incomplete(self.execute(payload_change=lambda p: p.update(status="FAIL", error="cleanup failed")))

    def test_comparison_failure_still_writes_report(self):
        outcome = self.execute(comparison_error=OSError("compare unavailable"))
        self.assertEqual(outcome[0], 2)
        self.assertIn("Comparison failed", (outcome[3] / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
