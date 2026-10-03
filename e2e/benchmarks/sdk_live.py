#!/usr/bin/env python3
"""Run SDK process and real-app checks inside the same disposable runtime contract."""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import best_in_class as benchmark
from run import Run, SCENARIOS, scenario

SCENARIOS.clear()


def invoke(ctx, command, label, live=False):
    result = benchmark.run_process(command, 180, env=ctx.run.instance.env)
    (ctx.run.dir / f"{label}.stdout.log").write_text(result["stdout"])
    (ctx.run.dir / f"{label}.stderr.log").write_text(result["stderr"])
    ctx.require(f"{label} exits successfully", result["returncode"] == 0 and not result["timed_out"], result["stderr"][-500:])
    if live:
        paths = [pathlib.Path(line.strip()) for line in result["stdout"].splitlines() if line.strip().startswith("/")]
        ctx.require(f"{label} records an artifact directory", paths)
        artifact = paths[-1].resolve()
        artifact.relative_to(benchmark.ROOT / "artifacts")
        text = (artifact / "report.md").read_text()
        ctx.check(f"{label} independently reports its assertions passing", text.startswith("PASS:"), text)
        ctx.check(f"{label} records the live protocol transcript", (artifact / "transcript.json").is_file())
        ctx.run.notes.append(f"{label} evidence: {artifact / 'report.md'}")
    return result


@scenario
def sdk_recovery_contract(ctx):
    node = shutil.which("node")
    ctx.require("Node is available", node)
    result = invoke(ctx, [node, str(benchmark.REPO / "sdk/recovery-smoke.mjs")], "sdk-recovery")
    for transport in ("http", "mcp"):
        ctx.check(f"{transport} process contract assertions completed",
                  f"{transport}: sessions, disconnect reconciliation, parsing, timeout/abort, observations, tasks, callbacks passed" in result["stdout"])


@scenario
def sdk_live_transports(ctx):
    node = shutil.which("node")
    ctx.require("Node is available", node)
    invoke(ctx, [node, str(benchmark.REPO / "sdk/smoke.mjs")], "sdk-live", live=True)


@scenario
def sdk_live_stdio(ctx):
    node = shutil.which("node")
    ctx.require("Node is available", node)
    invoke(ctx, [node, str(benchmark.REPO / "sdk/stdio-smoke.mjs")], "sdk-stdio", live=True)


if __name__ == "__main__":
    sys.exit(0 if Run(browser=True, installed=False).run(sys.argv[1:]) else 1)
