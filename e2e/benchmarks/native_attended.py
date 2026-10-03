#!/usr/bin/env python3
"""Exercise the actual native input-ownership policy exposed by this Mac."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import native_safety
from run import Run, SCENARIOS, scenario

SCENARIOS.clear()


@scenario
def native_input_ownership(ctx):
    profile = ctx.call("status").get("native_capabilities", {})
    available = profile.get("input_monitoring_available")
    ctx.require("input monitoring capability is explicit", isinstance(available, bool), profile)
    if available:
        ctx.run.notes.append("Input monitor available: exercised two-window same-process key-window ownership, including transient focus counters. Recent physical typing still needs attended human evidence.")
        native_safety.native_same_process_refusal(ctx)
    else:
        ctx.run.notes.append("Input monitor unavailable: exercised conservative foreground refusal. This is fallback evidence, not proof of simultaneous foreground interaction.")
        native_safety.native_foreground_refusal(ctx)


if __name__ == "__main__":
    sys.exit(0 if Run(browser=False, installed=False).run(sys.argv[1:]) else 1)
