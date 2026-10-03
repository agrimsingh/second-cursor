"""Tests for the judge and report semantics; deliberately cannot launch a desktop app."""
import json
import math
import pathlib
import sys
import os
import signal
import subprocess
import contextlib
import copy
import errno
import io
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import best_in_class as gate
from benchmarks.fixtures import judge
from benchmarks.tasks import adapter_metrics, off_space_conditions


class EvidenceTests(unittest.TestCase):
    def test_inconclusive_is_not_pass(self):
        self.assertEqual(gate.classify_scenario({"status": "PASS", "checks": [{"ok": True}],
                                               "invariants": [{"ok": True, "inconclusive": True}]}), "INCONCLUSIVE")

    def test_actual_failure_wins_over_interference(self):
        self.assertEqual(gate.classify_scenario({"status": "FAIL", "checks": [{"ok": False}],
                                               "invariants": [{"ok": True, "inconclusive": True}]}), "FAIL")

    def test_zero_checks_cannot_pass(self):
        self.assertEqual(gate.classify_scenario({"status": "PASS"}), "INCOMPLETE")
        self.assertEqual(gate.overall([]), "INCOMPLETE")

    def test_skips_and_missing_scenarios_block_gate(self):
        rows = gate.suite_rows({"results": [{"scenario": "present", "status": "SKIP"}]}, ["present", "absent"])
        self.assertEqual([r["status"] for r in rows], ["SKIP", "INCOMPLETE"])
        self.assertEqual(gate.overall(rows), "INCOMPLETE")

    def test_clean_evidence_passes(self):
        row = {"status": "PASS", "checks": [{"ok": True}], "invariants": [{"ok": True}]}
        self.assertEqual(gate.classify_scenario(row), "PASS")
        self.assertEqual(gate.overall([row]), "PASS")

    def test_unknown_usage_is_not_free(self):
        summary = gate.aggregate([{"task_id": "native_notes", "status": "PASS", "metrics": {"wall_ms": 4, "cost_usd": None}}])
        self.assertIsNone(summary["native_notes"]["cost_usd"]["median"])
        self.assertEqual(summary["native_notes"]["cost_usd"]["measured_trials"], 0)

    def test_failed_trials_remain_in_denominator_and_timings(self):
        summary = gate.aggregate([{"task_id": "t", "status": "PASS", "metrics": {"wall_ms": 2}},
                                  {"task_id": "t", "status": "FAIL", "metrics": {"wall_ms": 20}}])["t"]
        self.assertEqual((summary["completed"], summary["trials"]), (1, 2))
        self.assertEqual(summary["wall_ms"]["median"], 11)

    def test_parallel_intervals_do_not_double_count(self):
        self.assertEqual(gate.merge_intervals([(0, 10), (5, 20), (30, 40), (35, 38)]), 30)

    def test_invalid_metric_samples_are_not_measurements(self):
        rows = [{"task_id": "t", "status": "FAIL", "metrics": {"cost_usd": value}}
                for value in (None, True, math.nan, math.inf, -1, "0")]
        summary = gate.aggregate(rows)["t"]
        self.assertIsNone(summary["cost_usd"]["median"])
        self.assertEqual(summary["cost_usd"]["measured_trials"], 0)

    def test_correct_task_outcome_does_not_override_disturbed_execution(self):
        rows = [{"task_id": "t", "status": "INCONCLUSIVE", "independent_task_success": True},
                {"task_id": "t", "status": "FAIL", "independent_task_success": True},
                {"task_id": "t", "status": "FAIL", "independent_task_success": False}]
        summary = gate.aggregate(rows)["t"]
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(summary["independent_verified"], 2)
        self.assertEqual(summary["independent_measured_trials"], 3)
        self.assertEqual(summary["statuses"], {"INCONCLUSIVE": 1, "FAIL": 2})
        self.assertEqual(gate.overall(rows), "FAIL")
        self.assertEqual(gate.overall(rows[:1]), "INCOMPLETE")

    def test_unmeasured_task_outcomes_remain_unknown(self):
        rows = [{"task_id": "t", "status": "PASS"},
                *({"task_id": "t", "status": "FAIL", "independent_task_success": value}
                  for value in (None, 0, 1, "true"))]
        summary = gate.aggregate(rows)["t"]
        self.assertIsNone(summary["independent_verified"])
        self.assertEqual(summary["independent_measured_trials"], 0)
        rows.append({"task_id": "t", "status": "FAIL", "independent_task_success": False})
        summary = gate.aggregate(rows)["t"]
        self.assertEqual(summary["independent_verified"], 0)
        self.assertEqual(summary["independent_measured_trials"], 1)
        self.assertEqual(summary["trials"], 6)


class ComparisonTests(unittest.TestCase):
    def test_observation_modes_cannot_be_silently_compared(self):
        first = self.artifact(observation_mode="on-demand")
        second = self.artifact(observation_mode="upfront")
        with self.assertRaisesRegex(ValueError, "observation_mode"):
            gate.validate_comparison([first, second])
        with self.assertRaisesRegex(ValueError, "observation_mode"):
            gate.validate_comparison([self.artifact(observation_mode="unknown")])

    def artifact(self, **changes):
        config = {"model": "model-snapshot-2026-10-01", "reasoning_effort": "high", "max_steps": 40,
                  "timeout_seconds": 180, "model_host_version": "shared-runner-sha256:abcdef"}
        return {"kind": "tasks", "contract": gate.CONTRACT, "engine": "second-cursor-model",
                "engine_version": "binary-sha256:123456", "execution_profile": "model", "model": config["model"],
                "model_config": config, "track": "host-background", "native_placement": "agent-display",
                "interface_track": "native-tools", "fixture_sha256": "fixture-hash", "task_sha256": "task-hash",
                "browser_visibility": "headed",
                "runner_sha256": "runner-hash",
                "trials": [{"task_id": "native_notes", "trial": 1, "case_seed": 7, "status": "PASS",
                            "metrics": {"wall_ms": 50, "cost_usd": None}},
                           {"task_id": "native_notes", "trial": 2, "case_seed": 8, "status": "FAIL",
                            "metrics": {"wall_ms": 100, "cost_usd": .2}}], **changes}

    def test_matching_config_allows_different_pinned_engines(self):
        left, right = self.artifact(), self.artifact(engine="cua-model", engine_version="cua-0.33.0")
        right["trials"].reverse()  # Interleaved engines may record trials in a different order.
        gate.validate_comparison([left, right])

    def test_legacy_scripted_comparison_still_works(self):
        left = self.artifact(model=None)
        for key in ("execution_profile", "model_config", "interface_track", "engine_version"):
            left.pop(key)
        for trial in left["trials"]:
            trial.pop("case_seed")
        gate.validate_comparison([left, copy.deepcopy(left)])

    def test_old_model_artifact_cannot_bypass_pinning(self):
        old = self.artifact()
        old.pop("execution_profile")
        old.pop("model_config")
        with self.assertRaisesRegex(ValueError, "model_config"):
            gate.validate_comparison([old, copy.deepcopy(old)])

    def test_scripted_model_and_external_profiles_do_not_mix(self):
        model = self.artifact()
        scripted = self.artifact(execution_profile="scripted", model=None, model_config=None)
        external = self.artifact(execution_profile="external-scripted", model=None, model_config=None)
        for pair in ([scripted, model], [scripted, external]):
            with self.assertRaisesRegex(ValueError, "execution profiles"):
                gate.validate_comparison(pair)
        with self.assertRaisesRegex(ValueError, "scripted execution profile"):
            gate.validate_comparison([self.artifact(execution_profile="scripted")])

    def test_every_model_setting_is_required_and_must_match(self):
        for key in gate.MODEL_CONFIG_FIELDS:
            missing = self.artifact()
            missing["model_config"].pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                gate.validate_comparison([missing, copy.deepcopy(missing)])
        for key, value in (("reasoning_effort", "low"), ("max_steps", 20), ("timeout_seconds", 90),
                           ("model_host_version", "shared-runner-sha256:other"), ("temperature", .5)):
            right = self.artifact()
            right["model_config"][key] = value
            with self.subTest(changed=key), self.assertRaisesRegex(ValueError, "different model configurations"):
                gate.validate_comparison([self.artifact(), right])

    def test_missing_versions_and_tracks_cannot_be_compared(self):
        for key in ("engine_version", "interface_track", "fixture_sha256", "task_sha256", "runner_sha256", "native_placement", "browser_visibility"):
            missing = self.artifact()
            missing.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                gate.validate_comparison([missing, copy.deepcopy(missing)])
        for value in ("", "unknown", "latest", "HEAD", None):
            with self.subTest(version=value), self.assertRaises(ValueError):
                gate.validate_comparison([self.artifact(engine_version=value)])
        with self.assertRaisesRegex(ValueError, "different interface_track"):
            gate.validate_comparison([self.artifact(), self.artifact(interface_track="screenshot-actions")])
        with self.assertRaisesRegex(ValueError, "different runner_sha256"):
            gate.validate_comparison([self.artifact(), self.artifact(runner_sha256="other-runner-hash")])
        with self.assertRaisesRegex(ValueError, "different browser_visibility"):
            gate.validate_comparison([self.artifact(), self.artifact(browser_visibility="headless")])

    def test_model_identity_and_budgets_must_be_valid(self):
        for key, value in (("max_steps", True), ("max_steps", 1.5), ("max_steps", 0),
                           ("timeout_seconds", math.nan), ("timeout_seconds", math.inf), ("timeout_seconds", -1),
                           ("reasoning_effort", None), ("model_host_version", "unknown")):
            invalid = self.artifact()
            invalid["model_config"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                gate.validate_comparison([invalid])
        with self.assertRaisesRegex(ValueError, "disagrees"):
            gate.validate_comparison([self.artifact(model="different-model")])

    def test_model_browser_trials_pin_browser_build_and_visibility(self):
        payload = self.artifact(browser_identity={"binary": "/fixture/chrome", "version": "Chrome 140.1", "sha256": "a" * 64})
        for trial in payload["trials"]:
            trial["task_id"] = "browser_work_order"
        gate.validate_comparison([payload, copy.deepcopy(payload)])
        for key in ("binary", "version", "sha256"):
            invalid = copy.deepcopy(payload)
            invalid["browser_identity"].pop(key)
            with self.subTest(missing=key), self.assertRaisesRegex(ValueError, "browser_identity"):
                gate.validate_comparison([invalid])
        for key, changed in (("binary", "/other/chrome"), ("version", "Chrome 141.1"), ("sha256", "b" * 64)):
            other = copy.deepcopy(payload)
            other["browser_identity"][key] = changed
            with self.subTest(changed=key), self.assertRaisesRegex(ValueError, "different browser_identity"):
                gate.validate_comparison([payload, other])
        with self.assertRaisesRegex(ValueError, "Native-only"):
            gate.validate_comparison([self.artifact(browser_identity=payload["browser_identity"])])

    def test_case_schedule_is_paired_and_cannot_omit_failures(self):
        for mutate in (lambda rows: rows.pop(), lambda rows: rows[0].update(case_seed=9),
                       lambda rows: rows[0].pop("case_seed"), lambda rows: rows[0].update(case_seed=True),
                       lambda rows: rows.append(copy.deepcopy(rows[0])),
                       lambda rows: rows[1].update(case_seed=7), lambda rows: rows[1].update(trial=1)):
            right = self.artifact()
            mutate(right["trials"])
            with self.assertRaises(ValueError):
                gate.validate_comparison([self.artifact(), right])
        with self.assertRaisesRegex(ValueError, "empty"):
            gate.validate_comparison([self.artifact(trials=[])])

    def test_case_seed_is_consistent_across_tasks_in_a_round(self):
        invalid = self.artifact(browser_identity={"binary": "/fixture/chrome", "version": "Chrome 140.1", "sha256": "a" * 64})
        invalid["trials"].append({"task_id": "canvas_route", "trial": 1, "case_seed": 8, "status": "FAIL"})
        with self.assertRaisesRegex(ValueError, "same case_seed"):
            gate.validate_comparison([invalid])

    def test_model_cli_requires_explicit_effort_and_host_fingerprint(self):
        parser = gate.parser()
        self.assertIsNone(gate.model_config_for(parser.parse_args(["tasks"])))
        with self.assertRaises(ValueError):
            gate.model_config_for(parser.parse_args(["tasks", "--model", "model-snapshot"]))
        args = parser.parse_args(["tasks", "--model", "model-snapshot", "--reasoning-effort", "high",
                                  "--model-host-version", "runner-1.2.3", "--seed", "7", "--seed", "8"])
        self.assertEqual(args.seed, [7, 8])
        self.assertEqual(gate.model_config_for(args)["max_steps"], 40)
        self.assertEqual(args.interface_track, "native-tools")
        self.assertFalse(args.adapter_browser)
        self.assertTrue(parser.parse_args(["tasks", "--adapter-browser"]).adapter_browser)
        self.assertFalse(args.headed_browser)
        self.assertEqual(args.native_placement, "agent-display")
        visible = parser.parse_args(["tasks", "--headed-browser", "--native-placement", "desktop"])
        self.assertTrue(visible.headed_browser)
        self.assertEqual(visible.native_placement, "desktop")

    def test_partial_telemetry_is_visible_in_comparison(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "tasks.json"
            path.write_text(json.dumps(self.artifact()))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                gate.compare(types.SimpleNamespace(paths=[str(path)]))
        text = output.getvalue()
        self.assertIn("1/2", text)
        self.assertIn("0.200 (1/2 measured)", text)
        self.assertIn("unavailable", text)

    def test_comparison_separates_outcome_from_all_checks_and_shows_coverage(self):
        payload = self.artifact()
        payload["trials"][0].update(status="INCONCLUSIVE", independent_task_success=True)
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "tasks.json"
            path.write_text(json.dumps(payload))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                gate.compare(types.SimpleNamespace(paths=[str(path)]))
        text = output.getvalue()
        self.assertIn("Task outcome verified | All checks passed", text)
        self.assertIn("| 1/2 (1/2 judged) | 0/2 |", text)
        self.assertEqual(payload["trials"][0]["status"], "INCONCLUSIVE")


class SupervisorTests(unittest.TestCase):

    def test_normal_output_and_unreaped_zombie_group(self):
        result = gate.run_process([sys.executable, '-c', 'import sys; print("out"); print("err",file=sys.stderr)'], 3)
        self.assertEqual(result['stdout'], 'out\n')
        self.assertEqual(result['stderr'], 'err\n')
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['cleanup_errors'], [])
        self.assertEqual(result['cleanup']['signals'], [])
        self.assertEqual(result['cleanup']['status'], 'no_live_members')
        self.assertTrue(result['cleanup']['snapshots'][0][0]['state'].startswith('Z'))

    def test_timeout_kills_ignored_term_and_preserves_output(self):
        code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("partial",flush=True); time.sleep(30)'
        result = gate.run_process([sys.executable, '-c', code], 0.5)
        self.assertEqual(result['stdout'], 'partial\n')
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['returncode'], -signal.SIGKILL)
        self.assertEqual(result['cleanup_errors'], [])

    def helper(self, inherit):
        with tempfile.TemporaryDirectory() as folder:
            ready = str(pathlib.Path(folder) / 'ready')
            child = 'import signal,time,pathlib,os; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(' + repr(ready) + ').write_text(str(os.getpid())); time.sleep(30)'
            redirection = '' if inherit else ',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL'
            parent = 'import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,"-c",' + repr(child) + ']' + redirection + ');\nwhile not pathlib.Path(' + repr(ready) + ').exists(): time.sleep(.01)\nprint("parent-finished",flush=True)'
            result = gate.run_process([sys.executable, '-c', parent], 0.3 if inherit else 3)
            self.assertEqual(result['stdout'], 'parent-finished\n')
            self.assertEqual(result['cleanup_errors'], [])
            self.assertEqual(result['timed_out'], inherit)
            self.assertIn(signal.SIGKILL, result['cleanup']['signals'])
            pid = int(pathlib.Path(ready).read_text())
            state = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'state='], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'))

    def test_helper_inherits_stdout_and_ignores_term(self):
        self.helper(True)

    def test_helper_redirected_output_after_leader_exit_is_still_cleaned(self):
        self.helper(False)

    def test_cleanup_permission_failure_keeps_output_and_error(self):
        # Force an EPERM with live members: this must remain a cleanup failure.
        with patch.object(gate, 'group_members', return_value=[{'pid': 1, 'state': 'S'}]), patch.object(gate.os, 'killpg', side_effect=PermissionError(errno.EPERM, 'denied')):
            result = gate.run_process([sys.executable, '-c', 'print("retained")'], 3)
        self.assertEqual(result['stdout'], 'retained\n')
        self.assertEqual(result['returncode'], 0)
        self.assertTrue(result['cleanup_errors'])
        self.assertEqual(result['cleanup']['status'], 'failed')

    def test_lost_ownership_never_signals(self):

        class Lost:
            pid = 12345

            def observe_exit(self):
                raise ChildProcessError('already reaped')
        with patch.object(gate.os, 'killpg') as kill:
            result = gate.stop_group(Lost())
            kill.assert_not_called()
        self.assertTrue(result['errors'])
        self.assertEqual(result['status'], 'failed')

    def test_missing_command_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            gate.run_process(['/does-not-exist/sc-cleanup-test'], 1)

class TaskJudgeTests(unittest.TestCase):
    def test_off_space_fixture_requires_inactive_space_and_no_agent_display(self):
        window = {"is_on_screen": False, "on_agent_display": False, "space_ids": [4], "is_minimized": False}
        self.assertTrue(all(off_space_conditions(window, 8).values()))
        for changes in ({"is_on_screen": True}, {"on_agent_display": True}, {"space_ids": [8]},
                        {"space_ids": []}, {"space_ids": None}, {"is_minimized": True}):
            self.assertFalse(all(off_space_conditions({**window, **changes}, 8).values()))
        self.assertFalse(all(off_space_conditions({}, 8).values()))

    def test_default_task_profile_remains_on_agent_display(self):
        self.assertFalse(gate.parser().parse_args(["tasks"]).off_space)
        self.assertTrue(gate.parser().parse_args(["tasks", "--off-space"]).off_space)

    def test_form_exactly_once(self):
        event = {"kind": "work_order", "name": "Ada Lovelace", "route": "priority", "confirmed": True}
        self.assertTrue(judge("browser_work_order", [event]))
        self.assertFalse(judge("browser_work_order", [event, event]))
        self.assertFalse(judge("browser_work_order", [{**event, "confirmed": False}]))

    def test_dialog_cancellation_does_not_delete(self):
        self.assertTrue(judge("transient_dialog", [{"kind": "dialog_cancelled"}, {"kind": "draft_deleted"}]))
        self.assertFalse(judge("transient_dialog", [{"kind": "draft_deleted"}]))
        self.assertFalse(judge("transient_dialog", [{"kind": "draft_deleted"}, {"kind": "dialog_cancelled"}]))

    def test_canvas_requires_real_sampled_route(self):
        event = {"kind": "canvas_route", "trusted": True,
                 "points": [{"x": x, "y": 120} for x in range(80, 561, 40)]}
        self.assertTrue(judge("canvas_route", [event]))
        self.assertFalse(judge("canvas_route", [{**event, "trusted": False}]))
        self.assertFalse(judge("canvas_route", [{**event, "points": [event["points"][0], event["points"][-1]]}]))
        self.assertFalse(judge("canvas_route", [event, event]))

    def test_canvas_rejects_bad_coordinates(self):
        for value in (math.nan, math.inf, "240", True):
            points = [{"x": x, "y": 120} for x in range(80, 561, 40)]
            points[4]["x"] = value
            self.assertFalse(judge("canvas_route", [{"kind": "canvas_route", "trusted": True, "points": points}]))

    def test_coalesced_pointer_segments_cross_the_gates(self):
        event = {"kind": "canvas_route", "trusted": True,
                 "points": [{"x": x, "y": 120} for x in (80, 160, 300, 350, 460, 500, 560)]}
        self.assertTrue(judge("canvas_route", [event]))


class AdapterTests(unittest.TestCase):
    def final(self, **kwargs):
        return {"type": "result", "status": "completed", "model": None, **kwargs}

    def test_missing_telemetry_is_unknown(self):
        metrics = adapter_metrics([self.final()], 100, None)
        self.assertIsNone(metrics["tool_ms"])
        self.assertIsNone(metrics["model_ms"])
        self.assertIsNone(metrics["cost_usd"])

    def test_terminal_result_required_exactly_once(self):
        for events in ([], [self.final(), self.final()]):
            with self.assertRaises(ValueError):
                adapter_metrics(events, 100, None)

    def test_interval_and_model_validation(self):
        for call in ({"start_ms": -1, "end_ms": 10}, {"start_ms": 20, "end_ms": 10},
                     {"start_ms": 0, "end_ms": 1000}, {"start_ms": math.nan, "end_ms": 20}):
            with self.assertRaises(ValueError):
                adapter_metrics([{"type": "tool", **call}, self.final()], 100, None)
        with self.assertRaises(ValueError):
            adapter_metrics([self.final(model="different")], 100, "requested")

    def test_cost_needs_provenance(self):
        with self.assertRaises(ValueError):
            adapter_metrics([self.final(cost_usd=.1)], 100, None)
        measured = adapter_metrics([self.final(cost_usd=.1, pricing_source="provider invoice")], 100, None)
        self.assertEqual(measured["cost_usd"], .1)

    def test_non_tool_time_is_not_model_time(self):
        metrics = adapter_metrics([{"type": "tool", "start_ms": 0, "end_ms": 10}, self.final()], 100, None)
        self.assertEqual(metrics["non_tool_ms"], 90)
        self.assertIsNone(metrics["model_ms"])


if __name__ == "__main__":
    unittest.main()
