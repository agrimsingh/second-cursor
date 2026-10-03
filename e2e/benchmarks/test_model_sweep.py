"""Model/host sweep contracts, entirely offline."""
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import model_sweep as sweep


class SweepTests(unittest.TestCase):
    def plan(self, *extra):
        args = sweep.parser().parse_args(["--rounds", "2", *extra])
        with mock.patch.object(sweep.pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(sweep.pilot, "version", return_value="codex-cli audited"), \
                mock.patch.object(sweep.pilot, "cursor_host_version", return_value="cursor-sdk audited"), \
                mock.patch.object(sweep.report, "digest", return_value="binary-hash"), \
                mock.patch.object(sweep.report, "metadata", return_value={"fixture_sha256": "fixture", "task_sha256": "tasks"}), \
                mock.patch.object(sweep.pilot, "runner_fingerprint", return_value="runner"), \
                mock.patch.object(sweep.pilot, "browser_identity", return_value={"binary": "/test/browser", "version": "pinned", "sha256": "a" * 64}):
            return sweep.build_plan(args)

    def payload(self, plan, item):
        return {**sweep.pilot.planned_artifact(plan["profile_plans"][item["profile"]], "second-cursor"),
                "rounds": 1, "status": "PASS", "trials": [
                    {"task_id": task, "trial": 1, "case_seed": item["case_seed"], "runner_sha256": "runner",
                     "status": "PASS", "independent_task_success": True,
                     "metrics": {"wall_ms": 100, "verified_completion_ms": 80}}
                    for task in plan["tasks"]]}

    def test_schedule_interleaves_same_seed_and_alternates_order(self):
        scheduled = sweep.build_schedule(["a", "b", "c"], 2, 7)
        self.assertEqual([(r["profile"], r["round"], r["case_seed"]) for r in scheduled],
                         [("a", 1, 7), ("b", 1, 7), ("c", 1, 7), ("c", 2, 8), ("b", 2, 8), ("a", 2, 8)])
        for rounds, seed in [(0, 0), (1, -1), (True, 0)]:
            with self.assertRaises(ValueError):
                sweep.build_schedule(["a"], rounds, seed)
        with self.assertRaises(ValueError):
            sweep.build_schedule(["a", "a"], 1, 0)

    def test_defaults_pin_requested_profiles_and_full_native_tools(self):
        args = sweep.parser().parse_args([])
        self.assertEqual((args.rounds, args.timeout, args.max_steps), (3, 120, 40))
        plan = self.plan()
        self.assertEqual(plan["trial_count"], 10)
        self.assertEqual(plan["tasks"], ["native_notes"])
        for profile, child in plan["profile_plans"].items():
            self.assertEqual(child["observation_mode"], "upfront")
            self.assertEqual(child["catalog_mode"], "full")
            self.assertEqual(child["selected_engines"], ["second-cursor"])
            declared = sweep.PROFILES[profile]
            self.assertEqual(child["model_config"]["model"], declared["model"])
            self.assertEqual(child["model_config"].get("model_params", {}), declared.get("model_params", {}))

    def test_browser_selection_uses_on_demand_for_every_profile(self):
        plan = self.plan("--task", "native_notes", "--task", "browser_work_order")
        self.assertEqual(plan["trial_count"], 20)
        self.assertTrue(all(p["observation_mode"] == "on-demand" for p in plan["profile_plans"].values()))

    def test_preflight_rejects_undeclared_model_or_shared_identity_change(self):
        original = sweep.pilot.build_plan
        for key in ("runner_sha256", "engines", "fixture_sha256", "task_sha256", "model", "max_steps", "extra_sampling", "model_host_version"):
            count = 0
            def altered(args):
                nonlocal count
                result = original(args)
                count += 1
                if count == 2:
                    if key in ("model", "max_steps", "extra_sampling", "model_host_version"):
                        result["model_config"][key] = "changed"
                    else:
                        result[key] = "changed"
                return result
            with self.subTest(key=key), mock.patch.object(sweep.pilot, "build_plan", side_effect=altered):
                with self.assertRaises(ValueError):
                    self.plan()

    def test_child_cannot_change_model_or_runner(self):
        plan = self.plan()
        item = plan["runs"][0]
        for key in ("model", "runner_sha256"):
            payload = self.payload(plan, item)
            payload[key] = "changed"
            with self.subTest(key=key), self.assertRaises(ValueError):
                sweep.validate_child(plan, item, payload, {"returncode": 0, "timed_out": False})

    def test_summary_counts_failed_missing_and_independent_success_separately(self):
        rows = [{"status": "PASS", "independent_task_success": True, "metrics": {"wall_ms": 100}},
                {"status": "FAIL", "independent_task_success": False, "metrics": {"wall_ms": 1}},
                {"status": "INCONCLUSIVE", "independent_task_success": True, "metrics": {"wall_ms": 2}},
                {"status": "NOT_RUN", "metrics": {}}]
        result = sweep.summarize(rows)
        self.assertEqual((result["scheduled"], result["passed"], result["independent_verified"]), (4, 1, 2))
        self.assertEqual(result["wall_ms"], {"median": 100, "measured_trials": 1})
        self.assertEqual(result["statuses"]["NOT_RUN"], 1)

    def test_interrupt_retains_full_denominator_and_incremental_artifact(self):
        plan = self.plan()
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary)
            def interrupted(*args):
                initial = json.loads((output / "results.json").read_text())
                self.assertEqual(initial["scheduled"], 10)
                self.assertEqual(sum(p["summary"]["statuses"]["NOT_RUN"] for p in initial["profiles"].values()), 10)
                raise KeyboardInterrupt
            with mock.patch.object(sweep.report, "artifact_dir", return_value=output), \
                    mock.patch.object(sweep.report, "run_process", side_effect=interrupted) as process, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sweep.run_plan(plan), 2)
            result = json.loads((output / "results.json").read_text())
            self.assertEqual(process.call_count, 1)
            self.assertEqual(result["scheduled"], 10)
            self.assertFalse(result["evidence_complete"])
            self.assertEqual(sum(p["summary"]["statuses"].get("NOT_RUN", 0) for p in result["profiles"].values()), 9)
            self.assertNotIn("winner", result)

    def test_failed_child_stays_failed_and_missing_child_is_incomplete(self):
        plan = self.plan("--task", "native_notes", "--task", "browser_work_order")
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary)
            processes = []
            for index, item in enumerate(plan["runs"]):
                payload = self.payload(plan, item)
                if index == 0:
                    payload["trials"][0]["status"] = "FAIL"
                    payload["trials"][0]["independent_task_success"] = False
                    payload["status"] = "FAIL"
                path = output / f"child-{index}.json"
                path.write_text(json.dumps(payload))
                processes.append({"stdout": "" if index == 1 else f"Benchmark: {path}\n", "stderr": "",
                                  "returncode": sweep.report.exit_code(payload["status"]), "timed_out": False})
            with mock.patch.object(sweep.report, "artifact_dir", return_value=output), \
                    mock.patch.object(sweep.report, "run_process", side_effect=processes), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sweep.run_plan(plan), 2)
            result = json.loads((output / "results.json").read_text())
            self.assertEqual(result["scheduled"], 20)
            statuses = [row["status"] for p in result["profiles"].values() for row in p["trials"]]
            self.assertEqual(statuses.count("FAIL"), 1)
            self.assertEqual(statuses.count("INCOMPLETE"), 2)
            self.assertEqual(statuses.count("PASS"), 17)
            self.assertFalse(result["evidence_complete"])


if __name__ == "__main__":
    unittest.main()
