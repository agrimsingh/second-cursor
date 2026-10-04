"""The convenience entry point must preserve case schedules and profile identity."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import workspace_workflows as runner


class WorkspaceCLITests(unittest.TestCase):
    def test_scripted_seed_schedule(self):
        args = runner.parser().parse_args(["--rounds", "3", "--seed", "12"])
        argv = runner.command(args)
        self.assertEqual([argv[i + 1] for i, value in enumerate(argv) if value == "--seed"],
                         ["12", "13", "14"])
        self.assertEqual(argv.count("--task"), 3)
        self.assertNotIn("--model", argv)

    def test_model_identity_and_scoped_driver(self):
        args = runner.parser().parse_args(["--model", "example-model", "--model-host", "cursor-sdk",
            "--reasoning-effort", "low", "--model-param", "effort=low", "--model-param", "fast=true",
            "--task", "workspace_recovery", "--seed", "9"])
        argv = runner.command(args)
        self.assertEqual(argv[argv.index("--engine") + 1], "second-cursor")
        self.assertEqual(argv[argv.index("--model") + 1], "example-model")
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "low")
        self.assertEqual(argv.count("--model-param"), 2)
        self.assertEqual(argv.count("--task"), 1)

    def test_invalid_schedule_and_ambiguous_profile_refuse(self):
        for flags in (["--rounds", "0"], ["--seed", "-1"], ["--model-param", "fast=true"],
                      ["--task", "workspace_recovery", "--task", "workspace_recovery"]):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                runner.command(runner.parser().parse_args(flags))


if __name__ == "__main__":
    unittest.main()
