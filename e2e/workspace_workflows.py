#!/usr/bin/env python3
"""Run realistic, seeded workspace workflows through Second Cursor.

Defaults to a scripted control-path check; an explicit --model selects the existing
isolated model host. --list prints the command without launching apps or a model.
"""
import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def parser():
    from benchmarks.workspace_fixtures import WORKSPACE_TASKS
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--task", action="append", choices=WORKSPACE_TASKS)
    cli.add_argument("--rounds", type=int, default=1)
    cli.add_argument("--seed", type=int, default=1,
                     help="First generated case seed; each round increments it")
    cli.add_argument("--headed-browser", action="store_true",
                     help="Show scripted fixtures; model runs always use a visible browser")
    cli.add_argument("--model", help="Omit for scripted UI verification; set for agent reasoning trials")
    cli.add_argument("--model-host", choices=("codex", "cursor-sdk"), default="codex")
    cli.add_argument("--reasoning-effort", default="high")
    cli.add_argument("--model-param", action="append", default=[])
    cli.add_argument("--max-steps", type=int, default=100)
    cli.add_argument("--timeout", type=float, default=300)
    cli.add_argument("--list", action="store_true")
    return cli


def command(args):
    from benchmarks.workspace_fixtures import WORKSPACE_TASKS
    if args.rounds < 1 or args.seed < 0:
        raise ValueError("rounds must be positive and seed must be nonnegative")
    tasks = args.task or list(WORKSPACE_TASKS)
    if len(tasks) != len(set(tasks)):
        raise ValueError("Duplicate task selection")
    if args.model:
        argv = [sys.executable, str(ROOT / "model_benchmark.py"), "--engine", "second-cursor",
                "--model", args.model, "--model-host", args.model_host,
                "--reasoning-effort", args.reasoning_effort, "--max-steps", str(args.max_steps),
                "--rounds", str(args.rounds), "--seed", str(args.seed), "--timeout", str(args.timeout)]
        for param in args.model_param:
            argv.extend(["--model-param", param])
    else:
        if args.model_param or args.model_host != "codex":
            raise ValueError("A model host or model parameters require --model")
        argv = [sys.executable, str(ROOT / "best_in_class.py"), "tasks", "--rounds", str(args.rounds),
                "--timeout", str(args.timeout)]
        for seed in range(args.seed, args.seed + args.rounds):
            argv.extend(["--seed", str(seed)])
        if args.headed_browser:
            argv.append("--headed-browser")
    for task in tasks:
        argv.extend(["--task", task])
    return argv


def main():
    cli = parser()
    args = cli.parse_args()
    try:
        argv = command(args)
    except ValueError as error:
        cli.error(str(error))
    if args.list:
        print(json.dumps({"profile": "model" if args.model else "scripted", "command": argv,
                          "note": "Generated cases vary by seed. Scripted runs verify the control path; "
                                  "model runs additionally test context gathering and reasoning. "
                                  "Neither profile alone establishes shared-desktop coexistence."}, indent=2))
        return 0
    return subprocess.run(argv, cwd=ROOT.parent).returncode


if __name__ == "__main__":
    sys.exit(main())
