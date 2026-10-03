"""Catalog profile provenance tests; no model or native fixture is started."""
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import model_benchmark as pilot
from benchmarks import test_harness


class CatalogProfileTests(unittest.TestCase):
    def plan(self, *options):
        args = pilot.parser().parse_args(["--model", "gpt-6.1-sol", "--reasoning-effort", "high",
            "--engine", "second-cursor", "--task", "native_notes", *options])
        with mock.patch.object(pilot.pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(pilot, "version", return_value="codex-cli 1.0"), \
                mock.patch.object(pilot.report, "digest", return_value="binary-hash"), \
                mock.patch.object(pilot.report, "metadata", return_value={"fixture_sha256": "fixture-hash", "task_sha256": "task-hash"}):
            return pilot.build_plan(args)

    def test_catalog_mode_is_pinned_in_plan_adapter_and_harness(self):
        plan = self.plan("--catalog-mode", "compact")
        self.assertEqual(plan["catalog_mode"], "compact")
        self.assertEqual(pilot.planned_artifact(plan, "second-cursor")["catalog_mode"], "compact")
        for item in plan["runs"]:
            command = item["command"]
            self.assertEqual(command[command.index("--catalog-mode") + 1], "compact")
            adapter = json.loads(command[command.index("--adapter-command") + 1])
            self.assertEqual(adapter[adapter.index("--catalog-mode") + 1], "compact")

    def test_catalog_modes_cannot_be_silently_compared(self):
        fixture = test_harness.ComparisonTests().artifact
        with self.assertRaisesRegex(ValueError, "catalog_mode"):
            pilot.report.validate_comparison([fixture(catalog_mode="full"), fixture(catalog_mode="compact")])
        with self.assertRaisesRegex(ValueError, "catalog_mode"):
            pilot.report.validate_comparison([fixture(catalog_mode="unknown")])

    def test_full_is_the_default(self):
        self.assertEqual(self.plan()["catalog_mode"], "full")


if __name__ == "__main__":
    unittest.main()
