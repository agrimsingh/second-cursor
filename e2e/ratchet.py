"""Ceilings for counts that repeat exactly between runs of the same build, unlike latency: the characters an agent reads
from tools/list and from fixed fixtures.

A count above its ceiling fails, and so does a gated count with no ceiling. Rerunning with --ratchet sets a missing
ceiling or lowers one, written when the run ends and only if no scenario failed, so a win cannot be given back unnoticed
and a broken read cannot become the bar. Raising a ceiling is a reviewed edit to that file. Counts come from
default-config test instances on this Mac: AX trees depend on its fonts and display.
"""
import atexit
import json
import pathlib
import sys

PATH = pathlib.Path(__file__).with_name("ratchets.json")
LOWER = "--ratchet" in sys.argv
proposed = {}


def check(ctx, name, samples, gated=True):
    """`samples` are the count from every round; they must agree, or the count is not one to gate on. `gated=False`
    (a non-default config) only reports it."""
    samples = list(samples) if isinstance(samples, (list, tuple, set)) else [samples]
    if ctx.run.instance is None or not gated:
        return ctx.check(f"{name}: {samples} (not gated: not a default-config test instance)", True, samples)
    if len(set(samples)) > 1:
        return ctx.check(f"ratchet {name}: differs between rounds, so it cannot be gated on", False, samples)
    value = samples[0]
    ceiling = (json.loads(PATH.read_text()) if PATH.exists() else {}).get(name)
    if ceiling is not None and value > ceiling:
        return ctx.check(f"ratchet {name}: {value}, above its ceiling {ceiling}", False, value)
    if ceiling == value:
        return ctx.check(f"ratchet {name}: {value} (at its ceiling)", True, value)
    action = "set it" if ceiling is None else f"lower it from {ceiling}"
    if not LOWER:
        return ctx.check(f"ratchet {name}: {value}; rerun with --ratchet to {action}", ceiling is not None, value)
    if not proposed:
        atexit.register(write, ctx.run)
    proposed[name] = value
    return ctx.check(f"ratchet {name}: {value}; --ratchet will {action} if the run passes", True, value)


def write(run):
    if any(r["status"] == "FAIL" for r in run.results):
        print(f"ratchets.json not written: the run failed ({len(proposed)} ceiling(s) proposed)")
        return
    ceilings = json.loads(PATH.read_text()) if PATH.exists() else {}
    for name, value in proposed.items():
        ceilings[name] = min(value, ceilings.get(name, value))
    PATH.write_text(json.dumps(ceilings, indent=2, sort_keys=True) + "\n")
    print(f"ratchets.json: {', '.join(f'{name} = {value}' for name, value in sorted(proposed.items()))}")
