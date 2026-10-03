"""Controlled efficiency experiment contracts; no desktop or model is launched."""
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
import model_efficiency as efficiency
from benchmarks.tool_surface import MUTATING_TOOLS, NATIVE_TOOLS, READ_TOOLS


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def plan(self, rounds=2, experiment="observations"):
        args = efficiency.parser().parse_args([
            "--model", "gpt-6.1-sol", "--reasoning-effort", "high", "--rounds", str(rounds),
            "--experiment", experiment])
        with mock.patch.object(efficiency.pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(efficiency.pilot, "version", return_value="codex-cli 1.0"), \
                mock.patch.object(efficiency.report, "digest", return_value="binary-hash"), \
                mock.patch.object(efficiency.report, "metadata", return_value={
                    "fixture_sha256": "fixture-hash", "task_sha256": "task-hash"}), \
                mock.patch.object(efficiency.pilot, "runner_fingerprint", return_value="runner-hash"), \
                mock.patch.object(efficiency.pilot, "browser_identity", side_effect=AssertionError("Native only")):
            return efficiency.build_plan(args)

    def payload(self, plan, item):
        profile_plan = plan["profile_plans"][item["profile"]]
        prefetched = 2 if item["observation_mode"] == "upfront" else 0
        model_calls = 2 if prefetched else 4
        tools = [{"type": "tool", "origin": "host-prefetch" if n < prefetched else "model",
                  "name": "get_window_state" if n < prefetched else "batch",
                  "start_ms": n * 10, "end_ms": n * 10 + 5, "ok": True} for n in range(4)]
        compact = item["catalog_mode"] == "compact"
        names = sorted(READ_TOOLS | {"batch"} if compact else NATIVE_TOOLS)
        catalog = {"catalog_mode": item["catalog_mode"], "advertised_tool_names": names,
                   "advertised_tool_count": len(names), "original_tool_count": len(NATIVE_TOOLS),
                   "hidden_tool_names": sorted(MUTATING_TOOLS) if compact else [],
                   "catalog_sha256": "b" * 64 if compact else "a" * 64,
                   "original_catalog_sha256": "a" * 64, "original_schema_bytes": 10000,
                   "advertised_schema_bytes": 6000 if compact else 10000,
                   "schema_bytes_saved": 4000 if compact else 0}
        return {**efficiency.pilot.planned_artifact(profile_plan, "second-cursor"), "rounds": 1,
                "observation_mode": item["observation_mode"], "catalog_mode": item["catalog_mode"],
                "status": "PASS", "trials": [{
                    "task_id": "native_notes", "trial": 1, "case_seed": item["case_seed"],
                    "runner_sha256": plan["runner_sha256"], "status": "PASS", "independent_task_success": True,
                    "metrics": {"wall_ms": 90 if prefetched else 100, "tool_ms": 20, "tool_calls": 4,
                                "verified_completion_ms": 70 if prefetched else 80, "post_verification_tail_ms": 20},
                    "adapter_result": {"status": "completed", "observation_mode": item["observation_mode"],
                                       "catalog_mode": item["catalog_mode"], "tool_catalog": catalog,
                                       "prefetch_tool_calls": prefetched, "model_tool_calls": model_calls,
                                       "driver_tool_calls": 4}, "adapter_tools": tools}]}

    def execute(self, change=None, result_change=None, process_error=None, experiment="observations"):
        plan = self.plan(experiment=experiment)
        output = self.root / "experiment"
        output.mkdir()
        results = []
        for index, item in enumerate(plan["runs"]):
            payload = self.payload(plan, item)
            if index == 0 and change:
                change(payload)
            artifact = self.root / f"child-{index}.json"
            artifact.write_text(json.dumps(payload))
            result = {"stdout": f"Benchmark: {artifact}\n", "stderr": "", "timed_out": False,
                      "returncode": efficiency.report.exit_code(payload["status"]), "seconds": .2}
            if index == 0 and result_change:
                result_change(result)
            results.append(process_error if index == 0 and process_error else result)
        with mock.patch.object(efficiency.report, "artifact_dir", return_value=output), \
                mock.patch.object(efficiency.report, "run_process", side_effect=results) as processes, \
                contextlib.redirect_stdout(io.StringIO()):
            code = efficiency.run_plan(plan)
        return code, json.loads((output / "results.json").read_text()), output, processes

    def test_defaults_are_five_pairs_and_model_choice_is_explicit(self):
        args = efficiency.parser().parse_args([])
        self.assertEqual(args.rounds, 5)
        self.assertIsNone(args.model)
        self.assertIsNone(args.reasoning_effort)
        self.assertEqual(args.experiment, "observations")

    def test_plan_is_fixed_native_same_engine_and_alternates_order(self):
        plan = self.plan()
        self.assertEqual(plan["trial_count"], 4)
        self.assertEqual(plan["tasks"], ["native_notes"])
        self.assertEqual(plan["selected_engines"], ["second-cursor"])
        self.assertEqual([(r["profile"], r["round"], r["case_seed"]) for r in plan["runs"]],
                         [("baseline", 1, 1), ("upfront", 1, 1), ("upfront", 2, 2), ("baseline", 2, 2)])
        for item in plan["runs"]:
            command = item["command"]
            adapter = json.loads(command[command.index("--adapter-command") + 1])
            self.assertEqual(adapter[adapter.index("--observation-mode") + 1], item["observation_mode"])
            self.assertEqual(command[command.index("--observation-mode") + 1], item["observation_mode"])
            self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
            self.assertEqual(command[command.index("--reasoning-effort") + 1], "high")
            self.assertEqual(item["catalog_mode"], "full")

    def test_tool_surface_holds_prefetch_fixed_and_alternates_full_compact(self):
        plan = self.plan(experiment="tool-surface")
        self.assertEqual(plan["experiment"], "tool-surface")
        self.assertEqual(plan["reference_profile"], "upfront-full")
        self.assertEqual(plan["candidate_profile"], "upfront-compact")
        self.assertEqual([(r["profile"], r["round"]) for r in plan["runs"]],
                         [("upfront-full", 1), ("upfront-compact", 1), ("upfront-compact", 2), ("upfront-full", 2)])
        for item in plan["runs"]:
            self.assertEqual(item["observation_mode"], "upfront")
            command = item["command"]
            adapter = json.loads(command[command.index("--adapter-command") + 1])
            self.assertEqual(command[command.index("--catalog-mode") + 1], item["catalog_mode"])
            self.assertEqual(adapter[adapter.index("--catalog-mode") + 1], item["catalog_mode"])

    def test_tool_surface_report_uses_explicit_profiles_and_preserves_success_gate(self):
        code, result, output, processes = self.execute(experiment="tool-surface")
        self.assertEqual(code, 0)
        self.assertEqual(result["experiment"], "tool-surface")
        self.assertEqual(result["paired"]["reference_profile"], "upfront-full")
        self.assertEqual(result["paired"]["candidate_profile"], "upfront-compact")
        self.assertEqual(result["paired"]["wall_saved_ms"]["median"], 0)
        compact = result["profiles"]["upfront-compact"]["summary"]
        self.assertEqual(compact["advertised_tool_count"]["median"], 6)
        self.assertEqual(compact["advertised_schema_bytes"]["median"], 6000)
        text = (output / "report.md").read_text()
        self.assertIn("upfront-compact versus upfront-full", text)
        self.assertIn("not provider token counts", text)

    def test_catalog_cannot_be_claimed_without_complete_adapter_proof(self):
        changes = [lambda p: p.update(catalog_mode="compact"),
                   lambda p: p["trials"][0]["adapter_result"].pop("catalog_mode"),
                   lambda p: p["trials"][0]["adapter_result"].pop("tool_catalog"),
                   lambda p: p["trials"][0]["adapter_result"]["tool_catalog"].update(catalog_mode="compact"),
                   lambda p: p["trials"][0]["adapter_result"]["tool_catalog"].update(advertised_tool_names=["batch"]),
                   lambda p: p["trials"][0]["adapter_result"]["tool_catalog"].update(advertised_tool_count=6),
                   lambda p: p["trials"][0]["adapter_result"]["tool_catalog"].update(catalog_sha256="unversioned"),
                   lambda p: p["trials"][0]["adapter_result"]["tool_catalog"].update(schema_bytes_saved=300),
                   lambda p: p["trials"][0]["adapter_result"]["tool_catalog"].update(hidden_tool_names=["screenshot"])]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                previous = self.root
                self.root = previous / str(index)
                self.root.mkdir()
                code, result, output, processes = self.execute(change=change, experiment="tool-surface")
                self.root = previous
                self.assertEqual(code, 2)
                self.assertEqual(processes.call_count, 4)
                self.assertFalse(result["evidence_complete"])
                self.assertEqual(result["profiles"]["upfront-full"]["summary"]["scheduled"], 2)
                self.assertEqual(result["profiles"]["upfront-full"]["summary"]["passed"], 1)

    def test_profiles_cannot_pin_different_runtime_or_runner(self):
        real = efficiency.pilot.build_plan
        for key in ("runner_sha256", "engines", "model_config", "task_sha256", "fixture_sha256"):
            count = 0
            def changed(args):
                nonlocal count
                result = real(args)
                count += 1
                if count == 2:
                    result[key] = "changed"
                return result
            with self.subTest(key=key), mock.patch.object(efficiency.pilot, "build_plan", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "profile"):
                    self.plan()

    def test_complete_run_reports_full_cost_and_both_call_counts(self):
        code, result, output, processes = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(processes.call_count, 4)
        self.assertTrue(result["evidence_complete"])
        self.assertEqual(result["paired"]["eligible_pairs"], 2)
        self.assertEqual(result["paired"]["wall_saved_ms"]["median"], 10)
        upfront = result["profiles"]["upfront"]["summary"]
        self.assertEqual((upfront["passed"], upfront["scheduled"]), (2, 2))
        self.assertEqual(upfront["model_tool_calls"]["median"], 2)
        self.assertEqual(upfront["driver_tool_calls"]["median"], 4)
        self.assertEqual(upfront["prefetch_ms"]["median"], 10)
        self.assertEqual(upfront["verified_completion_ms"]["median"], 70)
        self.assertEqual(upfront["post_verification_tail_ms"]["median"], 20)
        self.assertIn("not randomized", (output / "report.md").read_text())

    def test_profile_cannot_be_claimed_without_adapter_proof(self):
        changes = [lambda p: p["trials"][0].pop("adapter_result"),
                   lambda p: p["trials"][0]["adapter_result"].update(observation_mode="upfront"),
                   lambda p: p["trials"][0]["adapter_result"].update(driver_tool_calls=3),
                   lambda p: p["trials"][0]["adapter_result"].update(prefetch_tool_calls=True),
                   lambda p: p["trials"][0]["adapter_tools"][0].update(origin="host-prefetch")]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                # Each execution persists into its own artifact directory.
                previous = self.root
                self.root = previous / str(index)
                self.root.mkdir()
                code, result, output, processes = self.execute(change=change)
                self.root = previous
                self.assertEqual(code, 2)
                self.assertEqual(processes.call_count, 4)
                self.assertFalse(result["evidence_complete"])
                self.assertEqual(result["profiles"]["baseline"]["summary"]["scheduled"], 2)
                self.assertEqual(result["profiles"]["baseline"]["summary"]["passed"], 1)
                self.assertIn("no comparative conclusion", (output / "report.md").read_text())

    def test_failed_judge_cannot_contribute_verified_completion_or_paired_speedup(self):
        def fail(payload):
            payload["status"] = "FAIL"
            payload["trials"][0].update(status="FAIL", independent_task_success=False)
            payload["trials"][0]["metrics"]["wall_ms"] = 150
        code, result, output, processes = self.execute(change=fail)
        self.assertEqual(code, 2)
        self.assertTrue(result["evidence_complete"])
        baseline = result["profiles"]["baseline"]["summary"]
        self.assertEqual(baseline["wall_ms"]["median"], 125)
        self.assertEqual(baseline["verified_completion_ms"]["measured_trials"], 1)
        self.assertEqual(baseline["post_verification_tail_ms"]["measured_trials"], 1)
        self.assertEqual(result["paired"]["eligible_pairs"], 1)

    def test_missing_verified_metric_stays_unknown_not_zero(self):
        code, result, output, processes = self.execute(change=lambda p: p["trials"][0]["metrics"].pop("verified_completion_ms"))
        self.assertEqual(code, 0)
        self.assertEqual(result["profiles"]["baseline"]["summary"]["verified_completion_ms"]["measured_trials"], 1)

    def test_timeout_retains_artifact_and_rest_of_schedule(self):
        code, result, output, processes = self.execute(result_change=lambda p: p.update(timed_out=True))
        self.assertEqual(code, 2)
        self.assertEqual(processes.call_count, 4)
        self.assertEqual(sum(len(p["trials"]) for p in result["profiles"].values()), 4)
        rejected = result["profiles"]["baseline"]["trials"][0]
        self.assertEqual(rejected["status"], "INCOMPLETE")
        self.assertTrue(pathlib.Path(rejected["child_artifact"]).is_file())

    def test_interrupt_preserves_entire_planned_denominator_and_writes_report(self):
        code, result, output, processes = self.execute(process_error=KeyboardInterrupt())
        self.assertEqual(code, 2)
        self.assertEqual(processes.call_count, 1)
        self.assertEqual(sum(p["summary"]["scheduled"] for p in result["profiles"].values()), 4)
        self.assertFalse(result["evidence_complete"])
        self.assertTrue((output / "report.md").is_file())
        self.assertTrue(all(row["status"] == "INCOMPLETE" for p in result["profiles"].values() for row in p["trials"]))

    def test_out_of_bounds_completion_claim_cannot_enter_report(self):
        code, result, output, processes = self.execute(change=lambda p: p["trials"][0]["metrics"].update(verified_completion_ms=1000))
        self.assertEqual(code, 2)
        self.assertFalse(result["evidence_complete"])


if __name__ == "__main__":
    unittest.main()
