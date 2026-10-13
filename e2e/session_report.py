#!/usr/bin/env python3
"""How many turns a client took, and which of them it could have saved, from the app's own logs.

Reads timings.jsonl (every call: tool, ms, ok, error, session_id) and actions.jsonl (every dispatched action with its
tab, caption and arguments), splits each session into runs at gaps longer than --gap seconds (a client can reuse one
MCP session for several conversations), and reports per run: wall time, calls, tool time, the gaps between calls
(client think time, which dominates for slow models), failures, and calls a better-informed client would not have made.

Usage:  e2e/session_report.py [--sc-home DIR | --logs DIR] [--session ID] [--since T] [--until T] [--gap SECONDS]
                              [--request-at T] [--answer-at T] [--run-id ID]
        T is ISO 8601; without a zone it is local time ("2026-10-13T22:36:43").
Output: printed, and e2e/artifacts/<run-id>/session-report.md and session-report.json

Timing lines written before session_id was logged are attributed to a session by matching each to the action line it
dispatched (same tool, dispatch inside the call), or, for reads that log no action, to the nearest attributed call.
Old lines have whole-second timestamps, so gaps and overlaps are only known to about a second there.
"""
import argparse
import bisect
import collections
import datetime
import json
import pathlib
import re
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent
USER_LOGS = pathlib.Path.home() / "Library/Logs/SecondCursor"

READS = {"browser_snapshot", "get_window_state"}
OBSERVING = {"click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action", "select_text", "paste",
             "browser_click", "browser_type", "browser_press_key", "browser_scroll", "browser_navigate", "batch", "act"}
MUTATING = {"paste", "select_text", "launch_app", "batch", "act", "click", "drag", "type_text", "set_value", "press_key",
            "scroll", "perform_action", "browser_open", "browser_navigate", "browser_click", "browser_type",
            "browser_press_key", "browser_scroll", "browser_close", "browser_repl"}
SINGLE_BROWSER_ACTIONS = {"browser_click", "browser_type", "browser_press_key", "browser_scroll"}
COMMAND_READS = {"capabilities", "user_tabs", "dom_snapshot", "inspect", "query", "wait", "screenshot", "history",
                 "logs", "downloads", "assets", "export", "cdp"}
PREFLIGHT = {"status", "browser_instances", "browser_status", "capabilities", "list_apps", "list_windows", "session",
             "browser_tabs", "operation_status"}
UNSIGNED_ARGS = {"operation_id", "caption", "session_id", "browser_id"}


def parse_time(text):
    value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.astimezone()
    return value.timestamp()


def fmt(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%H:%M:%S") + "Z"


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec="milliseconds")


def read_jsonl(paths):
    rows = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("time"), str):
                rows.append(row)
    return rows


def load_actions(logs):
    """Call-level action lines. Batch steps and browser_repl navigations are folded into the line that ran them."""
    calls = []
    for row in read_jsonl([logs / "actions.jsonl.1", logs / "actions.jsonl"]):
        raw = row.get("detail")
        refused = isinstance(raw, str) and raw.startswith("refused: ")
        try:
            detail = json.loads(raw) if isinstance(raw, str) and not refused else {}
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        target = row.get("target") or ""
        tab = re.fullmatch(r"tab_id=(\d+)", target)
        action = {"time": parse_time(row["time"]), "tool": row.get("tool"), "target": target,
                  "tab_id": detail.get("tab_id") or (int(tab[1]) if tab else None),
                  "session": detail.get("session_id"), "detail": detail,
                  "refused": raw[len("refused: "):] if refused else None, "steps": []}
        parent = calls[-1] if calls else None
        is_step = (parent is not None and parent["tool"] in {"batch", "act"} and "session_id" not in detail
                   and target == parent["target"] and action["time"] - parent["time"] <= 60
                   and not (parent["steps"] and parent["steps"][-1]["time"] > action["time"]))
        is_repl_child = action["tool"] == "browser_repl" and not detail and not refused and parent is not None \
            and parent["tool"] == "browser_repl"
        if is_step or is_repl_child:
            parent["steps"].append(action)
        else:
            calls.append(action)
    return calls


def load_calls(logs):
    calls = []
    for index, row in enumerate(read_jsonl([logs / "timings.jsonl.1", logs / "timings.jsonl"])):
        precise = "." in row["time"]
        ms = float(row.get("ms") or 0)
        stamp = parse_time(row["time"])
        # A whole-second stamp truncates the end, so the start is only known to lie in [lo, hi].
        lo, hi = stamp - ms / 1000, stamp + (0 if precise else 1) - ms / 1000
        call = {"index": index, "tool": row.get("tool"), "ms": ms, "ok": row.get("ok") is not False,
                "error": row.get("error"), "phases": row.get("phases") or {}, "start_lo": lo, "start_hi": hi,
                "stamp": row["time"], "precise": precise, "session": row.get("session_id"),
                "session_source": "timing" if row.get("session_id") else None, "action": None}
        settle(call)
        calls.append(call)
    return calls


def settle(call):
    call["start"] = (call["start_lo"] + call["start_hi"]) / 2
    call["end"] = call["start"] + call["ms"] / 1000


def attach_actions(calls, actions):
    """Pair each call with the action line it wrote: same tool, dispatched (whole second) inside the call.

    Calls are taken in start order and each takes the earliest action it could have written, so two calls of one tool
    started in the same second keep their dispatch order."""
    free = sorted(actions, key=lambda a: a["time"])
    used = set()
    for call in sorted(calls, key=lambda c: c["start"]):
        for i, action in enumerate(free):
            if i in used or action["tool"] != call["tool"]:
                continue
            if call["session"] and action["session"] and call["session"] != action["session"]:
                continue
            if action["time"] + 1 <= call["start_lo"] or action["time"] > call["start_hi"] + call["ms"] / 1000:
                continue
            used.add(i)
            call["action"] = action
            if not call["session"] and action["session"]:
                call["session"], call["session_source"] = action["session"], "action"
            # The action line is written after resolve and gates (and any approval), so it bounds the start.
            before = sum(call["phases"].get(k, 0) for k in ("resolve", "gates", "approval")) / 1000
            lo, hi = max(call["start_lo"], action["time"] - before - 0.05), min(call["start_hi"], action["time"] + 1)
            if lo <= hi:
                call["start_lo"], call["start_hi"] = lo, hi
                settle(call)
            break
    return [action for i, action in enumerate(free) if i not in used]


def infer_sessions(calls, gap):
    """Reads log no action line: give each the session of the nearest attributed call, within one run gap."""
    known = sorted((c for c in calls if c["session"]), key=lambda c: c["start"])
    times = [c["start"] for c in known]
    for call in calls:
        if call["session"] or not known:
            continue
        i = bisect.bisect_left(times, call["start"])
        candidates = [known[j] for j in (i - 1, i) if 0 <= j < len(known)]
        distance = lambda other: max(0.0, other["start"] - call["end"], call["start"] - other["end"])
        nearest = min(candidates, key=distance)
        if distance(nearest) <= gap:
            call["session"], call["session_source"] = nearest["session"], "inferred"


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * fraction
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def merged_ms(calls):
    total, current = 0.0, None
    for start, end in sorted((c["start"], c["end"]) for c in calls):
        if current and start <= current[1]:
            current[1] = max(current[1], end)
        else:
            if current:
                total += current[1] - current[0]
            current = [start, end]
    if current:
        total += current[1] - current[0]
    return total * 1000


def operation(call):
    return (call["action"] or {}).get("detail", {}).get("operation")


def is_mutating(call):
    if call["tool"] == "browser_command":
        return operation(call) not in COMMAND_READS
    return call["tool"] in MUTATING


def tab_of(call):
    return (call["action"] or {}).get("tab_id")


def describe(call):
    action = call["action"] or {}
    detail = action.get("detail", {})
    parts = [fmt(call["start"]), call["tool"] + (f".{detail['operation']}" if detail.get("operation") else ""),
             "ok" if call["ok"] else "FAILED"]
    if detail.get("caption"):
        parts.append(repr(detail["caption"]))
    for key in ("ref", "text", "keys", "key", "url", "locator"):
        if key in detail:
            parts.append(f"{key}={json.dumps(detail[key], ensure_ascii=False)[:80]}")
    if action.get("steps"):
        parts.append(f"{len(action['steps'])} steps")
    parts.append(f"({call['ms']:.0f} ms)")
    if not call["ok"] and call["error"]:
        parts.append("— " + normalize_error(call["error"])[:110])
    return " ".join(parts)


def normalize_error(text):
    text = (text or "").strip().splitlines()[0] if text else ""
    text = re.sub(r"\s*\[operation_id=[^\]]*\]", "", text)
    text = re.sub(r"\b(Ref|ref) e\d+\b", r"\1 eN", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", text, flags=re.I)
    return text


def signature(call):
    detail = (call["action"] or {}).get("detail", {})
    args = {k: v for k, v in detail.items() if k not in UNSIGNED_ARGS}
    return call["tool"] + " " + json.dumps(args, sort_keys=True, ensure_ascii=False)


def split_runs(calls, gap):
    sessions = collections.defaultdict(list)
    for call in calls:
        sessions[call["session"] or "(unattributed)"].append(call)
    runs = []
    for session, group in sessions.items():
        group.sort(key=lambda c: (c["start"], c["index"]))
        current, last_end = [], None
        for call in group:
            if current and call["start"] - last_end > gap:
                runs.append((session, current))
                current = []
            current.append(call)
            last_end = call["end"] if len(current) == 1 else max(last_end, call["end"])
        if current:
            runs.append((session, current))
    return sorted(runs, key=lambda item: item[1][0]["start"])


def gaps_of(calls):
    """Idle time before each call: its start minus the latest end so far. Negative means it overlapped."""
    gaps, latest = [], None
    for prev, call in zip(calls, calls[1:]):
        latest = prev["end"] if latest is None else max(latest, prev["end"])
        gaps.append({"before": prev, "after": call, "seconds": call["start"] - latest})
    return gaps


def anti_patterns(calls, gaps):
    found = collections.defaultdict(list)
    first_real = next((i for i, c in enumerate(calls) if c["tool"] not in PREFLIGHT), len(calls))
    preflight = calls[:first_real]
    if preflight and first_real < len(calls):
        found["preflight_before_first_action"].append({
            "calls": [describe(c) for c in preflight], "avoidable": [c["index"] for c in preflight],
            "seconds_before_first_action": round(calls[first_real]["start"] - calls[0]["start"], 1)})

    for prev, call in zip(calls, calls[1:]):
        if call["tool"] not in READS or not prev["ok"]:
            continue
        if prev["tool"] in OBSERVING:
            reason = "the previous action already returned an observation of the page"
        elif prev["tool"] == call["tool"]:
            reason = "nothing acted between this read and the previous one"
        else:
            continue
        found["read_after_observation"].append({"call": describe(call), "previous": describe(prev),
                                                 "reason": reason, "avoidable": [call["index"]]})

    after = {id(g["before"]): g["seconds"] for g in gaps}
    for call in calls:
        if call["tool"] == "browser_screenshot" or (call["tool"] == "browser_command" and operation(call) == "screenshot"):
            found["screenshot_on_agent_tab"].append({
                "call": describe(call), "gap_after_seconds": round(after.get(id(call), 0), 1),
                "avoidable": [call["index"]],
                "note": "the snapshot outline or a text read has the same content as text; the image costs the model a slow read"})

    mutations = [c for c in calls if is_mutating(c) and tab_of(c) is not None]
    for i, a in enumerate(mutations):
        for b in mutations[i + 1:]:
            if b["start"] > a["end"] + 1:
                break
            if tab_of(a) != tab_of(b):
                continue
            same_second = a["action"]["time"] == b["action"]["time"] and not (a["precise"] and b["precise"])
            certain = b["start_hi"] < a["start_lo"] + a["ms"] / 1000
            likely = b["start"] < a["end"]
            if same_second or certain or likely:
                evidence = ("intervals overlap" if certain else "dispatched in the same second" if same_second
                            else "intervals likely overlap (whole-second stamps)")
                found["concurrent_mutations_same_tab"].append({
                    "first": describe(a), "second": describe(b), "tab_id": tab_of(a), "evidence": evidence,
                    "avoidable": [c["index"] for c in (b, a) if not c["ok"]][:1]})

    failures = collections.defaultdict(list)
    for call in calls:
        if not call["ok"]:
            failures[(signature(call), normalize_error(call["error"]))].append(call)
    for (sig, error), group in failures.items():
        if len(group) > 1:
            found["identical_failing_calls_repeated"].append({
                "signature": sig, "error": error, "count": len(group), "times": [fmt(c["start"]) for c in group],
                "avoidable": [c["index"] for c in group[1:]]})

    streak = []
    def close_streak():
        if len(streak) >= 2:
            found["batchable_single_actions"].append({
                "tab_id": tab_of(streak[0]), "calls": [describe(c) for c in streak],
                "avoidable": [c["index"] for c in streak[1:]],
                "note": "consecutive single actions on one tab with no read between; one batch with tab_id or one browser_repl call could send them"})
    for call in calls:
        single = (call["tool"] in SINGLE_BROWSER_ACTIONS
                  or (call["tool"] == "browser_command" and is_mutating(call))) and tab_of(call) is not None \
            and call["ok"]
        if single and streak and tab_of(call) == tab_of(streak[-1]):
            streak.append(call)
        else:
            close_streak()
            streak = [call] if single else []
    close_streak()
    return dict(found)


def summarize(session, calls, args):
    gaps = gaps_of(calls)
    idle = [max(0.0, g["seconds"]) for g in gaps]
    by_tool = collections.defaultdict(lambda: {"calls": 0, "ms": 0.0, "failures": 0})
    for call in calls:
        row = by_tool[call["tool"]]
        row["calls"] += 1
        row["ms"] += call["ms"]
        row["failures"] += not call["ok"]
    failures = collections.defaultdict(list)
    for call in calls:
        if not call["ok"]:
            failures[normalize_error(call["error"])].append(call)
    patterns = anti_patterns(calls, gaps)
    start, end = calls[0]["start"], max(c["end"] for c in calls)
    attribution = collections.Counter(c["session_source"] or "none" for c in calls)
    single_tabs = collections.Counter(tab_of(c) for c in calls if c["tool"] in SINGLE_BROWSER_ACTIONS and tab_of(c))
    run = {
        "session_id": session, "start": iso(start), "end": iso(end), "wall_seconds": round(end - start, 1),
        "calls": len(calls), "failed_calls": sum(not c["ok"] for c in calls),
        "tool_ms_sum": round(sum(c["ms"] for c in calls)), "tool_ms_merged": round(merged_ms(calls)),
        "tool_ms_by_tool": {k: {**v, "ms": round(v["ms"])} for k, v in
                            sorted(by_tool.items(), key=lambda item: -item[1]["ms"])},
        "gaps": {"count": len(idle), "mean": rnd(statistics.mean(idle)) if idle else None,
                 "median": rnd(percentile(idle, .5)), "p90": rnd(percentile(idle, .9)),
                 "max": rnd(max(idle)) if idle else None, "total": rnd(sum(idle)),
                 "overlapping_calls": sum(g["seconds"] < 0 for g in gaps)},
        "top_gaps": [{"seconds": rnd(g["seconds"]), "before": describe(g["before"]), "after": describe(g["after"])}
                     for g in sorted(gaps, key=lambda g: -g["seconds"])[:args.top]],
        "failures": [{"error": error, "count": len(group), "tools": sorted({c["tool"] for c in group}),
                      "times": [fmt(c["start"]) for c in group]}
                     for error, group in sorted(failures.items(), key=lambda item: -len(item[1]))],
        "single_actions_by_tab": {str(k): v for k, v in single_tabs.items()},
        "anti_patterns": patterns,
        "avoidable_calls_upper_bound": len({i for items in patterns.values() for item in items for i in item["avoidable"]}),
        "attribution": dict(attribution),
        "timestamp_precision": "milliseconds" if all(c["precise"] for c in calls) else "whole seconds (±1 s on gaps)",
        "call_log": [describe(c) for c in calls],
    }
    median_gap = percentile(idle, .5) or 0
    run["avoidable_client_seconds_estimate"] = rnd(run["avoidable_calls_upper_bound"] * median_gap)
    if args.request_at:
        run["request_to_first_call_seconds"] = rnd(start - parse_time(args.request_at))
    if args.answer_at:
        run["last_call_to_answer_seconds"] = rnd(parse_time(args.answer_at) - end)
    if args.request_at and args.answer_at:
        run["request_to_answer_seconds"] = rnd(parse_time(args.answer_at) - parse_time(args.request_at))
    return run


def rnd(value):
    return None if value is None else round(value, 1)


TITLES = {
    "preflight_before_first_action": "Status/instance calls before the first real action",
    "read_after_observation": "Snapshot right after a call that already observed the page",
    "screenshot_on_agent_tab": "Screenshots on agent tabs",
    "concurrent_mutations_same_tab": "Order-dependent actions on one tab dispatched concurrently",
    "identical_failing_calls_repeated": "Identical failing calls repeated",
    "batchable_single_actions": "Consecutive single actions on one tab (one batch/browser_repl call)",
}


def markdown(report):
    lines = [f"# Session report — {len(report['runs'])} run(s)", "",
             f"Logs: `{report['logs']}`. Filters: `{json.dumps(report['filters'])}`. "
             f"Runs split at gaps over {report['filters']['gap']} s.", ""]
    for n, run in enumerate(report["runs"], 1):
        g = run["gaps"]
        lines += [f"## Run {n}: session `{run['session_id']}`", "",
                  f"- {run['start']} → {run['end']}: **{run['wall_seconds']} s wall, {run['calls']} calls "
                  f"({run['failed_calls']} failed), {run['tool_ms_sum'] / 1000:.1f} s tool time** "
                  f"({run['tool_ms_merged'] / 1000:.1f} s with overlaps merged)"]
        if g["count"]:
            lines.append(f"- Gaps between calls (client time): median {g['median']} s, p90 {g['p90']} s, max {g['max']} s, "
                         f"mean {g['mean']} s, total {g['total']} s; {g['overlapping_calls']} call(s) overlapped the previous")
        lines += [f"- Avoidable calls (upper bound): **{run['avoidable_calls_upper_bound']}**, "
                  f"≈ {run['avoidable_client_seconds_estimate']} s at the median gap",
                  f"- Timestamps: {run['timestamp_precision']}; session attribution: {run['attribution']}"]
        for key, label in (("request_to_first_call_seconds", "Request → first call"),
                           ("last_call_to_answer_seconds", "Last call → answer"),
                           ("request_to_answer_seconds", "Request → answer")):
            if key in run:
                lines.append(f"- {label}: {run[key]} s")
        lines += ["", "| Tool | Calls | Failed | Tool ms |", "| --- | ---: | ---: | ---: |"]
        lines += [f"| {tool} | {row['calls']} | {row['failures']} | {row['ms']} |" for tool, row in run["tool_ms_by_tool"].items()]
        lines += ["", "### Longest gaps", ""]
        for gap in run["top_gaps"]:
            lines += [f"- **{gap['seconds']} s** after `{gap['before']}`", f"  - then `{gap['after']}`"]
        if run["failures"]:
            lines += ["", "### Failures", ""]
            lines += [f"- ×{f['count']} {', '.join(f['tools'])}: {f['error']} ({', '.join(f['times'])})" for f in run["failures"]]
        lines += ["", "### Anti-patterns", ""]
        if not run["anti_patterns"]:
            lines.append("None detected.")
        for key, items in run["anti_patterns"].items():
            lines.append(f"- **{TITLES[key]}**: {len(items)} (avoidable calls: {sum(len(i['avoidable']) for i in items)})")
            for item in items:
                if "call" in item:
                    extra = f" — previous `{item['previous']}` ({item['reason']})" if "previous" in item else \
                        f" — then {item['gap_after_seconds']} s idle" if "gap_after_seconds" in item else ""
                    lines.append(f"  - `{item['call']}`{extra}")
                elif "first" in item:
                    lines.append(f"  - {item['evidence']}: `{item['first']}` and `{item['second']}`")
                elif "signature" in item:
                    lines.append(f"  - ×{item['count']} at {', '.join(item['times'])}: `{item['signature'][:160]}` — {item['error']}")
                else:
                    extra = f" ({item['seconds_before_first_action']} s before the first action)" \
                        if "seconds_before_first_action" in item else ""
                    lines.append(f"  - {len(item['calls'])} calls{extra}:")
                    lines += [f"    - `{c}`" for c in item["calls"]]
        lines += ["", "<details><summary>All calls</summary>", "", *[f"- `{c}`" for c in run["call_log"]], "", "</details>", ""]
    return "\n".join(lines) + "\n"


def build(args):
    logs = pathlib.Path(args.logs).expanduser() if args.logs else \
        pathlib.Path(args.sc_home).expanduser() / "logs" if args.sc_home else USER_LOGS
    calls = load_calls(logs)
    actions = load_actions(logs)
    attach_actions(calls, actions)
    infer_sessions(calls, args.gap)
    since = parse_time(args.since) if args.since else None
    until = parse_time(args.until) if args.until else None
    selected = [c for c in calls if (since is None or c["end"] >= since) and (until is None or c["start"] <= until)
                and (not args.session or c["session"] == args.session)]
    runs = [summarize(session, group, args) for session, group in split_runs(selected, args.gap)
            if len(group) >= args.min_calls]
    return {"logs": str(logs), "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "filters": {"session": args.session, "since": args.since, "until": args.until, "gap": args.gap},
            "runs": runs}


def main():
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = cli.add_mutually_exclusive_group()
    where.add_argument("--sc-home", help="An isolated instance's SC_HOME; its logs are in SC_HOME/logs")
    where.add_argument("--logs", help="A directory holding timings.jsonl and actions.jsonl")
    cli.add_argument("--session")
    cli.add_argument("--since")
    cli.add_argument("--until")
    cli.add_argument("--gap", type=float, default=180, help="Split a session into runs at gaps over this (s)")
    cli.add_argument("--min-calls", type=int, default=1)
    cli.add_argument("--top", type=int, default=5, help="Longest gaps to show per run")
    cli.add_argument("--request-at", help="When the user asked (client side), for lead time")
    cli.add_argument("--answer-at", help="When the client answered, for tail time")
    cli.add_argument("--run-id")
    cli.add_argument("--quiet", action="store_true", help="Write the artifacts without printing the report")
    args = cli.parse_args()
    report = build(args)
    run_id = args.run_id or "session-report-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "artifacts" / run_id
    out.mkdir(parents=True, exist_ok=True)
    text = markdown(report)
    (out / "session-report.md").write_text(text)
    (out / "session-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if not args.quiet:
        print(text)
    print(f"Report: {out / 'session-report.md'}", file=sys.stderr)
    return 0 if report["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
