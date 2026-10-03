"""Derive conservative native completion timing from independently accepted trials.

This does not run a judge during execution or measure the first instant a goal
became true. It timestamps the final model tool's exact postcondition check,
only after the existing independent judge and strict trial gate both pass.
"""
import json
import math
import re

from benchmarks.tasks import NOTE


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _payload(event):
    result = event.get("result")
    if (not isinstance(result, dict) or result.get("isError") is True
            or "isError" in result and type(result["isError"]) is not bool):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    values = []
    for block in content:
        if not isinstance(block, dict):
            return None
        if block.get("type") != "text":
            continue
        try:
            value = json.loads(block.get("text", ""))
        except (ValueError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        values.append(value)
    return values[0] if len(values) == 1 else None


def _observation(payload, target):
    if not isinstance(payload, dict):
        return None
    value = payload.get("observation", payload)
    if not isinstance(value, dict) or not isinstance(value.get("tree"), str):
        return None
    window = value.get("window")
    if (not isinstance(window, dict) or window.get("id") != target["id"]
            or window.get("pid") != target["pid"]):
        return None
    return value


def _values(tree, role, *, index=None, label=None):
    """Read current/full or added AX values, never removed lines from a diff."""
    values = []
    pattern = re.compile(r'^\s*(?:\[(\d+)\]\s+)?' + re.escape(role)
                         + r'\s+("(?:\\.|[^"\\])*").*?\bvalue=("(?:\\.|[^"\\])*")')
    for line in tree.splitlines():
        if line.startswith("-"):
            continue
        if line.startswith("+"):
            line = line[1:]
        match = pattern.match(line)
        if not match or index is not None and match[1] != str(index):
            continue
        try:
            actual_label, value = json.loads(match[2]), json.loads(match[3])
        except ValueError:
            continue
        if label is None or actual_label == label:
            values.append(value)
    return values


def verified_native_completion(row, events, trace_events):
    """Return adapter-clock timing or explicit unknown values, never a guess.

    The adapter must retain call IDs and arguments when normalizing model tool
    events. Full trace results establish the postcondition, and an underlying
    engine interval confirms that the tool was executed. The host clock's
    normalization has up to 1 ms tolerance; no positional/fuzzy call matching is
    permitted. A later call without the final exact-note check makes timing
    unknown even when the final independent outcome passes.
    """
    basis = {"clock_origin": "adapter", "first_goal_achievement_measured": False,
             "method": "final_tool_exact_postcondition_then_independent_judge",
             "clock_pairing_tolerance_ms": 1}

    def unknown(reason):
        return {"verified_completion_ms": None, "post_verification_tail_ms": None,
                "verified_completion_basis": {**basis, "unavailable_reason": reason}}

    if (not isinstance(row, dict) or row.get("task_id") != "native_notes" or row.get("status") != "PASS"
            or row.get("independent_task_success") is not True):
        return unknown("A clean native trial and independent task success are required")
    if (not isinstance(events, list) or not isinstance(trace_events, list)
            or any(not isinstance(event, dict) for event in events + trace_events)):
        return unknown("Malformed adapter or trace events")
    environment = row.get("fixture_environment", {})
    if not isinstance(environment, dict):
        return unknown("Missing fixture identity")
    calculator, notes = (environment.get(key) for key in ("calculator_initial", "textedit_initial"))
    if (any(not isinstance(target, dict) or any(type(target.get(key)) is not int or target[key] <= 0
                                               for key in ("id", "pid")) for target in (calculator, notes))
            or calculator["id"] == notes["id"]):
        return unknown("Missing exact fixture window/process identity")
    terminal = [event for event in events if event.get("type") == "result"]
    if (len(terminal) != 1 or terminal[0].get("status") != "completed"
            or not _number(terminal[0].get("adapter_wall_ms"))):
        return unknown("Missing successful adapter duration")
    wall_ms = terminal[0]["adapter_wall_ms"]
    calls = [event for event in events if event.get("type") == "tool" and event.get("origin") == "model"]
    traced = [event for event in trace_events if event.get("source") == "model_host" and event.get("type") == "tool"]
    if not calls or len(calls) != len(traced):
        return unknown("Model tool events are missing or unpaired")
    for group in (calls, traced):
        ids = [event.get("call_id") for event in group]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            return unknown("Missing or duplicate model call IDs")
    by_id = {event["call_id"]: event for event in traced}
    if set(by_id) != {event["call_id"] for event in calls}:
        return unknown("Adapter and trace call IDs disagree")
    paired = []
    for call in calls:
        trace = by_id[call["call_id"]]
        if (trace.get("name") != call.get("name") or trace.get("arguments") != call.get("arguments")
                or not isinstance(call.get("arguments"), dict) or call.get("ok") is not True
                or trace.get("status") != "completed"
                or not _number(call.get("start_ms")) or not _number(call.get("end_ms"))
                or not 0 <= call["start_ms"] <= call["end_ms"] <= wall_ms):
            return unknown("Call identity, success, or normalized timing is inconsistent")
        driver = [event for event in trace_events if event.get("source") == "engine"
                  and event.get("type") == "tool" and event.get("phase") == "model"
                  and event.get("name") == call["name"] and event.get("ok") is True
                  and _number(event.get("start_ms")) and _number(event.get("end_ms"))
                  and call["start_ms"] - 1 <= event["start_ms"] <= event["end_ms"] <= call["end_ms"] + 1]
        if len(driver) != 1:
            return unknown("A unique successful driver execution cannot be matched")
        paired.append((call, _payload(trace)))
    paired.sort(key=lambda pair: pair[0]["start_ms"])
    if any(left[0]["end_ms"] > right[0]["start_ms"] for left, right in zip(paired, paired[1:])):
        return unknown("Model tool intervals overlap")
    last_call, last_payload = paired[-1]
    arguments = last_call["arguments"]
    expected = arguments.get("expect")
    final_note = NOTE + "\nTotal: 42"
    if (last_call["name"] not in {"batch", "act", "type_text", "set_value", "press_key", "select_text"}
            or arguments.get("window_id") != notes["id"] or not isinstance(expected, dict)
            or type(expected.get("element_index")) is not int or expected["element_index"] < 0
            or expected.get("value") != final_note):
        return unknown("The final model tool lacks the exact whole-note postcondition")
    note_observation = _observation(last_payload, notes)
    observed_expect = last_payload.get("expect") if isinstance(last_payload, dict) else None
    observed_outcome = last_payload.get("outcome") if isinstance(last_payload, dict) else None
    if (note_observation is None or not isinstance(observed_expect, dict)
            or observed_expect.get("status") != "verified" or not isinstance(observed_outcome, dict)
            or observed_outcome.get("verified") is not True or observed_outcome.get("state") != "verified"
            or last_payload.get("stopped_early") is True
            or _values(note_observation["tree"], "AXTextArea", index=expected["element_index"]) != [final_note]):
        return unknown("The final result does not verify and observe the complete note")
    if last_call["name"] in {"batch", "act"}:
        steps = last_payload.get("steps")
        if (not isinstance(steps, list) or not steps
                or any(not isinstance(step, dict) or step.get("ok") is not True
                       or step.get("uncertain") is not False for step in steps)):
            return unknown("Final batch contains a failed, uncertain, or missing step")
    calculator_calls = [(call, payload) for call, payload in paired[:-1]
                        if call["arguments"].get("window_id") == calculator["id"]]
    if not calculator_calls:
        return unknown("No model tool observed the Calculator result")
    calc_observation = _observation(calculator_calls[-1][1], calculator)
    if calc_observation is None or _values(calc_observation["tree"], "AXStaticText", label="Edit field") != ["42"]:
        return unknown("Latest Calculator tool did not observe 42")
    completion = last_call["end_ms"]
    return {"verified_completion_ms": completion, "post_verification_tail_ms": wall_ms - completion,
            "verified_completion_basis": {**basis, "call_id": last_call["call_id"],
                                          "independent_confirmation": True,
                                          "tail_includes": "final model response, host shutdown, and adapter cleanup"}}
