"""Cursor selection and host integration contracts, with no model or desktop calls."""
import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import best_in_class as report
import model_benchmark as pilot
import model_efficiency as efficiency
from benchmarks import model_adapter as adapter
from benchmarks.test_model_adapter import FakeEngine, request, result


def cursor_config():
    return {"model": "grok-4.7", "reasoning_effort": "medium", "max_steps": 12,
            "timeout_seconds": 10, "model_host_version": "cursor-sdk 1.0.35:test-runtime",
            "model_host": "cursor-sdk", "model_params": {"reasoning_effort": "medium", "fast": "true", "context": "256k"}}


class CursorConfigTests(unittest.TestCase):
    def test_cursor_selection_requires_explicit_matching_effort_and_fast(self):
        self.assertEqual(report.validate_model_config(cursor_config()), cursor_config())
        for params in (None, {}, {"reasoning_effort": "high", "fast": "true"}, {"reasoning_effort": "medium"},
                       {"reasoning_effort": "medium", "fast": True}, {"reasoning_effort": "medium", "fast": "yes"},
                       {"reasoning_effort": "medium", "fast": "false", "context": 256}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                report.validate_model_config({**cursor_config(), "model_params": params})

    def test_grok_45_explicit_effort_and_fast_need_no_unsupported_context_parameter(self):
        config = {**cursor_config(), "model": "grok-4.5",
                  "model_params": {"effort": "medium", "fast": "true"}}
        self.assertEqual(report.validate_model_config(config), config)

    def test_reasoning_aliases_are_exclusive_and_must_match_the_pinned_effort(self):
        for params in ({"effort": "medium", "reasoning_effort": "medium", "fast": "true"},
                       {"effort": "high", "fast": "true"}, {"fast": "true"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                report.validate_model_config({**cursor_config(), "model": "grok-4.5", "model_params": params})

    def test_catalog_specific_parameters_are_preserved_for_the_runtime_catalog_guard(self):
        config = {**cursor_config(), "model": "grok-4.5",
                  "model_params": {"effort": "medium", "fast": "true", "unexpected": "catalog-must-reject"}}
        self.assertEqual(report.validate_model_config(config), config)

    def test_unknown_host_and_codex_params_are_rejected(self):
        legacy = request()["model_config"]
        self.assertEqual(report.validate_model_config(legacy), legacy)
        for changes in ({"model_host": "unknown"}, {"model_params": {}},
                        {"model_host": "codex", "model_params": {"fast": "true"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                report.validate_model_config({**legacy, **changes})

    def test_parameter_parser_rejects_duplicates_and_malformed_values(self):
        for values in (["effort"], ["=medium"], ["fast="], ["fast=true", "fast=false"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                report.parse_model_params(values)
        self.assertEqual(report.parse_model_params(["reasoning_effort=medium", "fast=true", "context=256k"]),
                         {"reasoning_effort": "medium", "fast": "true", "context": "256k"})


class CursorPlanTests(unittest.TestCase):
    def build(self, cli=pilot, extra=()):
        options = ["--model-host", "cursor-sdk", "--model", "grok-4.7", "--reasoning-effort", "medium",
                   "--model-param", "reasoning_effort=medium", "--model-param", "fast=true", "--model-param", "context=256k",
                   "--node-binary", "/pinned/node", "--cursor-sdk-root", "/pinned/sdk", "--rounds", "1"]
        if cli is pilot:
            options += ["--engine", "second-cursor", "--task", "native_notes"]
        args = cli.parser().parse_args([*options, *extra])
        with mock.patch.object(pilot, "cursor_host_version", return_value="cursor-sdk 1.0.35:test-runtime", create=True) as version, \
                mock.patch.object(pilot, "version", side_effect=AssertionError("Must not inspect Codex")), \
                mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(report, "digest", return_value="binary-hash"), \
                mock.patch.object(report, "metadata", return_value={"fixture_sha256": "fixture", "task_sha256": "task"}), \
                mock.patch.object(pilot, "runner_fingerprint", return_value="runner-hash"):
            plan = cli.build_plan(args)
        self.assertTrue(version.called)
        self.assertEqual(version.call_args.kwargs, {"sdk_root": "/pinned/sdk", "node_binary": "/pinned/node"})
        return plan

    def test_cursor_plan_pins_and_forwards_host_model_and_parameters(self):
        plan = self.build()
        self.assertEqual(plan["model_config"]["model_host"], "cursor-sdk")
        self.assertEqual(plan["model_config"]["model_params"], {"reasoning_effort": "medium", "fast": "true", "context": "256k"})
        for item in plan["runs"]:
            command = item["command"]
            self.assertEqual(command[command.index("--model-host") + 1], "cursor-sdk")
            self.assertIn("reasoning_effort=medium", command)
            self.assertIn("fast=true", command)
            bridge = json.loads(command[command.index("--adapter-command") + 1])
            self.assertEqual(bridge[bridge.index("--model-host") + 1], "cursor-sdk")
            self.assertEqual(bridge[bridge.index("--node-binary") + 1], "/pinned/node")
            self.assertEqual(bridge[bridge.index("--cursor-sdk-root") + 1], "/pinned/sdk")

    def test_efficiency_profiles_keep_same_cursor_selection(self):
        plan = self.build(efficiency)
        for profile in plan["profile_plans"].values():
            self.assertEqual(profile["model_config"], plan["model_config"])

    def test_cursor_cannot_read_configured_codex_model(self):
        args = pilot.parser().parse_args(["--model-host", "cursor-sdk", "--configured-model"])
        with mock.patch.object(pilot, "configured_model", side_effect=AssertionError("Must not read Codex config")), \
                self.assertRaisesRegex(ValueError, "configured-model"):
            pilot.build_plan(args)

    def test_codex_does_not_accept_cursor_parameters(self):
        args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high",
                                          "--model-param", "fast=true"])
        with mock.patch.object(pilot, "version", return_value="codex-cli 1.0"), self.assertRaisesRegex(ValueError, "param"):
            pilot.build_plan(args)

    def test_harness_reconstructs_identical_cursor_model_config(self):
        args = report.parser().parse_args(["tasks", "--model-host", "cursor-sdk", "--model", "grok-4.7",
            "--reasoning-effort", "medium", "--model-param", "reasoning_effort=medium", "--model-param", "fast=true", "--model-param", "context=256k",
            "--model-host-version", "cursor-sdk 1.0.35:test-runtime", "--max-steps", "12", "--timeout", "10"])
        self.assertEqual(report.model_config_for(args), cursor_config())


class CursorComparisonTests(unittest.TestCase):
    def artifact(self):
        return {"kind": "tasks", "execution_profile": "model", "model": "grok-4.7",
            "model_config": cursor_config(), "contract": report.CONTRACT, "track": "host-background",
            "native_placement": "desktop", "fixture_sha256": "fixture", "task_sha256": "task",
            "runner_sha256": "runner", "engine": "second-cursor-model", "engine_version": "sha256:binary",
            "interface_track": "native-tools", "browser_visibility": "headed", "browser_identity": None,
            "trials": [{"task_id": "native_notes", "trial": 1, "case_seed": 1}]}

    def test_generic_comparator_refuses_changed_cursor_selection_or_host(self):
        baseline = self.artifact()
        report.validate_comparison([baseline, copy.deepcopy(baseline)])
        for key, value in (("fast", "false"), ("context", "500k")):
            candidate = copy.deepcopy(baseline)
            candidate["model_config"]["model_params"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "model configurations"):
                report.validate_comparison([baseline, candidate])
        candidate = copy.deepcopy(baseline)
        candidate["model_config"].pop("model_host")
        candidate["model_config"].pop("model_params")
        candidate["model_config"]["model_host_version"] = "codex-cli test"
        with self.assertRaisesRegex(ValueError, "model configurations"):
            report.validate_comparison([baseline, candidate])

    def test_cursor_source_and_lock_changes_change_runner_fingerprint(self):
        for changed in ("benchmarks/cursor_model_host.py", "cursor_host/bridge.mjs",
                        "cursor_host/package.json", "cursor_host/package-lock.json"):
            with self.subTest(changed=changed), \
                    mock.patch.object(report, "extension_identity", return_value={"sha256": "test-extension-sha"}):
                with mock.patch.object(pathlib.Path, "read_bytes", return_value=b"baseline"):
                    baseline = pilot.runner_fingerprint()
                def content(path):
                    return b"changed" if path.relative_to(pilot.ROOT).as_posix() == changed else b"baseline"
                with mock.patch.object(pathlib.Path, "read_bytes", content):
                    self.assertNotEqual(baseline, pilot.runner_fingerprint())


class CursorAdapterTests(unittest.TestCase):
    def invoke(self, changes=None):
        req = request()
        req.update(model="grok-4.7", model_config=cursor_config())
        data = {**result(), **cursor_config()}
        data["isolation"].update({key: data.pop(key) for key in ("model_host", "model_params")})
        for key, value in (changes or {}).items():
            (data["isolation"] if key in {"model_host", "model_params"} else data)[key] = value
        captured = []

        class FakeCursorHost:
            def __init__(self, config, **kwargs):
                captured.append((config, kwargs))

            def run(self, prompt, tools, execute_tool, emit_event=None):
                return types.SimpleNamespace(as_dict=lambda: copy.deepcopy(data))

        fake_module = types.ModuleType("benchmarks.cursor_model_host")
        fake_module.CursorModelHost = FakeCursorHost
        engine = FakeEngine()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output), \
                mock.patch.dict(os.environ, {"SC_BENCH_TRACE_DIR": directory}), \
                mock.patch.dict(sys.modules, {"benchmarks.cursor_model_host": fake_module}), \
                mock.patch.object(adapter, "runner_fingerprint", return_value="test-runner"), \
                mock.patch.object(adapter, "create_engine", return_value=engine), \
                mock.patch.object(adapter, "CodexModelHost", side_effect=AssertionError("Wrong host")):
            code = adapter.run_adapter(req, types.SimpleNamespace(engine="second-cursor", cua_binary="unused",
                codex_binary="unused", model_host="cursor-sdk", cursor_sdk_root="/pinned/sdk", node_binary="/pinned/node"))
        final = json.loads(output.getvalue().splitlines()[-1])
        self.assertTrue(engine.closed)
        return code, final, captured

    def test_cursor_host_receives_explicit_selection_and_runtime(self):
        code, final, captured = self.invoke()
        self.assertEqual((code, final["status"]), (0, "completed"))
        self.assertEqual(captured[0][1], {"model_params": {"reasoning_effort": "medium", "fast": "true", "context": "256k"},
            "node_binary": "/pinned/node", "sdk_root": "/pinned/sdk"})

    def test_actual_host_and_selection_must_match_configuration(self):
        for change in ({"model_host": "codex"}, {"model_params": {"reasoning_effort": "medium", "fast": "false", "context": "256k"}},
                       {"model_params": None}, {"model": "auto"}):
            with self.subTest(change=change):
                code, final, _ = self.invoke(change)
                self.assertEqual((code, final["status"]), (1, "failed"))
                self.assertIn("identity", final["error"])

    def test_adapter_selector_must_match_requested_host(self):
        req = request()
        req.update(model="grok-4.7", model_config=cursor_config())
        with mock.patch.object(adapter, "runner_fingerprint", return_value="test-runner"), \
                self.assertRaisesRegex(ValueError, "host"):
            adapter.validate_request(req, "second-cursor", "codex")


if __name__ == "__main__":
    unittest.main()
