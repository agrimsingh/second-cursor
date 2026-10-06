"""Production browser route invariants against a deterministic transport; no GUI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class BrowserExpectationRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = ("SCShared", "MCP", "Logging", "EventSource", "SystemPackage", "CSystem")
        products = next((REPO / ".build" / mode for mode in ("debug", "release")
                         if all((REPO / ".build" / mode / (name + ".o")).is_file() for name in names)), None)
        if not products or not shutil.which("xcrun"):
            raise unittest.SkipTest("Build Swift dependencies once to run the browser router probe")
        cls.scratch = tempfile.TemporaryDirectory(prefix="sc-expect-router-")
        path = Path(cls.scratch.name)
        cls.binary = path / "probe"
        command = ["xcrun", "swiftc", "-parse-as-library", "-I", str(products),
                   "-module-cache-path", str(path / "module-cache")]
        command += [str(REPO / "Sources/SCServer" / name) for name in (
            "Arguments.swift", "ToolCatalog.swift", "Shapes.swift", "ToolRouter+Browser.swift", "ToolRouter+Outcomes.swift")]
        command += [str(REPO / "e2e/browser_expect_router.swift")]
        command += [str(products / (name + ".o")) for name in names]
        command += ["-o", str(cls.binary)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if built.returncode:
            cls.scratch.cleanup()
            raise AssertionError(built.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def run_cases(self, *cases):
        proc = subprocess.run([str(self.binary)], input="".join(json.dumps(case) + "\n" for case in cases),
                              capture_output=True, text=True, timeout=10,
                              env={**os.environ, "SC_HOME": str(Path(self.scratch.name) / "home")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(results), len(cases))
        return results

    def click(self, expect, **extra):
        return {"tool": "browser_click", "args": {"tab_id": 1, "ref": "e1", "expect": expect, **extra}}

    def batch(self, **extra):
        return {"tool": "batch", "args": {"tab_id": 1, "expect": {"text": "Saved"},
                "steps": [{"tool": "browser_click", "ref": "e1"}, {"tool": "browser_click", "ref": "e2"}], **extra}}

    def test_all_malformed_expectations_refuse_before_caption_or_input(self):
        malformed = ["not-json", [], {}, {"text": ""}, {"text": 2}, {"text": None}, {"ref": "e1"},
                     {"value": "x"}, {"ref": "", "value": "x"}, {"ref": "e1", "value": 1},
                     {"text": "Saved", "gone": "true"}, {"text": "Saved", "gone": None},
                     {"text": "Saved", "timeout_ms": -1}, {"text": "Saved", "timeout_ms": 10001},
                     {"text": "Saved", "timeout_ms": 1.5}, {"text": "Saved", "timeout_ms": "1"},
                     {"text": "Saved", "typo": True}, {"element_index": 1, "value": "x"}]
        for condition, result in zip(malformed, self.run_cases(*(self.click(item) for item in malformed))):
            with self.subTest(condition=condition):
                self.assertIn("expect", result["error"])
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["inputs"], 0)

    def test_json_string_and_none_screenshot_modes_preserve_verification(self):
        results = self.run_cases(self.click(json.dumps({"text": "Saved"}), observe="none"),
                                 self.click({"text": "Saved"}, observe="screenshot"))
        for result in results:
            self.assertEqual(result["result"]["expect"]["status"], "verified")
            self.assertEqual(result["result"]["outcome"]["state"], "verified")
            self.assertEqual(result["inputs"], 1)
        self.assertNotIn("observation", results[0]["result"])
        self.assertEqual(results[1]["images"], 1)

    def test_old_extension_refuses_before_caption(self):
        case = self.click({"text": "Saved"})
        case["fake"] = {"old_extension": True}
        result, = self.run_cases(case)
        self.assertIn("reload", result["error"])
        self.assertEqual(result["calls"], [])

    def test_unsupported_routes_refuse_before_caption_or_input(self):
        cases = [{"tool": tool, "args": {"tab_id": 1, "expect": {"text": "Saved"}}}
                 for tool in ("browser_navigate", "browser_command", "browser_repl", "browser_open", "browser_snapshot")]
        cases += [{"tool": "browser_command", "args": {"tab_id": 1, "operation": "batch",
                   "steps": [{"operation": "click"}, {"operation": "click", "expect": {"text": "Saved"}}]}}]
        for result in self.run_cases(*cases):
            self.assertIn("expect", result["error"])
            self.assertEqual(result["calls"], [])

    def test_invalid_later_step_and_full_flag_refuse_before_prepare(self):
        cases = [self.batch(steps=[{"tool": "browser_click", "ref": "e1"}, {"tool": "browser_click"}]),
                 self.batch(full=[]), self.batch(steps=[{"tool": "browser_click", "ref": "e1", "expect": {"text": "Saved"}}])]
        for result in self.run_cases(*cases):
            self.assertIn("error", result)
            self.assertEqual(result["calls"], [])

    def test_failed_step_never_verifies_even_if_later_steps_continue_and_finish_claims_success(self):
        cases = [self.batch(), self.batch(stop_on_error=False)]
        for case in cases:
            case["fake"] = {"fail_first": True}
        stopped, continued = self.run_cases(*cases)
        for result in (stopped, continued):
            body = result["result"]
            self.assertEqual(body["expect"]["status"], "failed")
            self.assertEqual(body["expect"]["reason"], "batch_incomplete")
            self.assertEqual(body["outcome"]["state"], "failed")
            finish = next(call for call in result["calls"] if call["method"] == "tab.expectFinish")
            self.assertFalse(finish["params"]["complete"])
        self.assertEqual(stopped["inputs"], 1)
        self.assertTrue(stopped["result"]["steps"][1]["skipped"])
        self.assertEqual(continued["inputs"], 2)
        self.assertEqual(continued["result"]["events"], ["input-1", "input-2"])

    def test_uncertain_step_is_carried_and_cannot_verify(self):
        case = self.batch()
        case["fake"] = {"uncertain_first": True}
        result, = self.run_cases(case)
        self.assertEqual(result["result"]["expect"]["status"], "failed")
        self.assertEqual(result["result"]["outcome"]["state"], "uncertain")
        self.assertEqual(result["inputs"], 1)

    def test_finish_failure_preserves_steps_without_a_second_failed_observation(self):
        case = self.batch()
        case["fake"] = {"finish_error": True}
        result, = self.run_cases(case)
        self.assertEqual(result["inputs"], 2)
        body = result["result"]
        self.assertEqual(body["completed"], 2)
        self.assertEqual(body["expect"]["status"], "failed")
        self.assertIn("disconnected", body["observation_error"])
        self.assertEqual(body["events"], ["input-1", "input-2"])
        self.assertNotIn("tab.snapshot", [call["method"] for call in result["calls"]])


if __name__ == "__main__":
    unittest.main()
