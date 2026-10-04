"""Pure scheduling and outcome checks for the cross-app interruption fixture.

The solver never receives this module's expected-state arguments. The schedule is
public replay input; expected native contents belong only to the final judge.
"""
import random

VERSION = "cross-app-interruptions/1"
MODES = ("recovery", "pause-resume", "stale-plan", "interleaved")


def schedule(seed, mode):
    if type(seed) is not int or not 0 <= seed < 2 ** 63:
        raise ValueError("seed must be a nonnegative 63-bit integer")
    if mode not in MODES:
        raise ValueError("unknown disruption mode")
    rng = random.Random(f"{VERSION}:{seed}:{mode}")
    events = []
    if mode == "interleaved":
        # Early boundaries are guaranteed to occur in every supported workspace
        # workflow, while seed changes timing and the interleaving of both inputs.
        boundaries = sorted(rng.sample(range(3, 20), 5))
        window = "A"
        for index, boundary in enumerate(boundaries):
            if index in (0, 2, 4):
                window = "B" if window == "A" else "A"
                events.append({"id": f"browser-{index}", "phase": "browser", "after_call": boundary,
                               "action": "focus", "window": window})
            else:
                events.append({"id": f"browser-{index}", "phase": "browser", "after_call": boundary,
                               "action": "type", "text": f"[manual-{seed}-{index}]"})
    if mode in ("pause-resume", "stale-plan", "interleaved"):
        events.append({"id": "native-pause", "phase": "native", "action": "pause-resume"})
    if mode in ("stale-plan", "interleaved"):
        events.append({"id": "native-external-edit", "phase": "native", "action": "external-edit",
                       "text": f"\nUser correction {seed}: preserve this newer note."})
    if mode == "interleaved":
        events.append({"id": "native-lost-ack", "phase": "native", "action": "lost-acknowledgement"})
    return events


def original_note(seed):
    return (f"Operations handoff — case {seed}\n"
            "Keep this existing note: café ☕.\n"
            "An older invoice summary is only a reference; preserve it.\n"
            "End of existing notes.")


def summary_line(invoice, amount_cents, route, memo):
    return f"\nReconciled {invoice}: ${amount_cents // 100}.{amount_cents % 100:02}; {route}; {memo}."


def activity_overlapped(before, after):
    return bool(before and after and before.get("activity_running") is True
                and after.get("activity_running") is True
                and type(before.get("activity_count")) is int
                and type(after.get("activity_count")) is int
                and after["activity_count"] > before["activity_count"] >= 0)


def judge_native(*, original, external, summary, live, saved, events, required_events, fired_events):
    expected = original + external + summary
    appends = [event for event in events if event.get("kind") == "native_summary_dispatch"]
    return {
        "native_live_text_exact": live == expected,
        "native_saved_text_exact": saved == expected,
        "native_original_prefix_preserved": isinstance(live, str) and live.startswith(original + external),
        "native_summary_present_exactly_once": isinstance(live, str) and live.count(summary) == 1,
        # This is dispatch accounting, not a continuous TextEdit mutation monitor.
        "native_summary_dispatched_once": len(appends) == 1,
        "all_planned_disruptions_exercised": list(fired_events) == list(required_events),
    }
