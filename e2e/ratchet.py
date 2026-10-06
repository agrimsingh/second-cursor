"""Ceilings for counts that repeat exactly between runs of the same build, unlike latency: the characters an agent reads
from tools/list and from fixed fixtures. Every one of them is model input on every step that reads it.

A count above its ceiling fails. A lower count, or one without a ceiling yet, passes and says so; rerunning with
--ratchet writes it to ratchets.json, so a win cannot be given back unnoticed. Raising a ceiling is a reviewed edit to
that file. Counts come from default-config test instances on this Mac: AX trees depend on its fonts and display.
"""
import json
import pathlib
import sys

PATH = pathlib.Path(__file__).with_name("ratchets.json")
LOWER = "--ratchet" in sys.argv


def check(ctx, name, samples, gated=True):
    """`samples` are the count from every round; they must agree, or the count is not one to gate on. `gated=False`
    (a non-default config) only reports it."""
    samples = list(samples) if isinstance(samples, (list, tuple, set)) else [samples]
    if ctx.run.instance is None or not gated:
        return ctx.check(f"{name}: {samples} (not gated: not a default-config test instance)", True, samples)
    if len(set(samples)) > 1:
        return ctx.check(f"ratchet {name}: differs between rounds, so it cannot be gated on", False, samples)
    value = samples[0]
    ceilings = json.loads(PATH.read_text()) if PATH.exists() else {}
    ceiling = ceilings.get(name)
    if ceiling is not None and value > ceiling:
        return ctx.check(f"ratchet {name}: {value}, above its ceiling {ceiling}", False, value)
    if ceiling is not None and value == ceiling:
        return ctx.check(f"ratchet {name}: {value} (at its ceiling)", True, value)
    if LOWER:
        ceilings[name] = value
        PATH.write_text(json.dumps(ceilings, indent=2, sort_keys=True) + "\n")
        return ctx.check(f"ratchet {name}: {value}, ceiling {'set' if ceiling is None else f'lowered from {ceiling}'}",
                         True, value)
    action = "set" if ceiling is None else f"lower it from {ceiling}"
    return ctx.check(f"ratchet {name}: {value}; rerun with --ratchet to {action}", True, value)
