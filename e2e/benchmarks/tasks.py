"""Complete task execution. Imported only by the explicit `tasks` command."""
import collections
import json
import math
import os
import pathlib
import re
import time

import best_in_class as report
from benchmarks.fixtures import FixtureServer, judge
from benchmarks.flight_fixtures import FLIGHT_TASKS, FlightFixture
from benchmarks.workspace_fixtures import WORKSPACE_TASKS, WorkspaceFixture

NOTE = "Dispatch notes\nKeep this existing line."
PROMPTS = {
    "native_notes": "Use the provided Calculator window's buttons to compute 7 × 6. Read its result and append a newline followed by 'Total: ' and that result to the provided TextEdit window, preserving its original text exactly. Do not save or close either window.",
    "browser_work_order": "Open the provided URL. Create exactly one work order for Ada Lovelace, choose the Priority route, and confirm the details. Verify the page reports Work order created.",
    "canvas_route": "Open the provided URL. Draw one continuous pointer drag from the green start circle, through both gray gates, to the blue destination circle. Release at the destination and verify Route complete. Use real pointer input; no JavaScript evaluation, semantic substitutes, or DOM mutation.",
    "transient_dialog": "Open the provided URL. Click Review deletion, cancel the confirmation once, and verify the draft remains. Click Review deletion again, accept the confirmation once, and verify the draft was deleted.",
}
RULES = (" Use only your engine's normal computer-control tools. Do not use the shell, filesystem editing, "
         "JavaScript evaluation, or direct HTTP requests to complete the task. Do not change the human's focus, cursor, or Space. "
         "Do not touch any target outside this task. Close only browser tabs that you created when finished.")
# browser_repl runs JavaScript, so the unqualified rule reads as forbidding the very tool it is offered beside.
REPL_RULES = RULES.replace("JavaScript evaluation", "JavaScript evaluation in the page")
MODES = {"browser_work_order": "form", "canvas_route": "canvas", "transient_dialog": "dialog"}


def browser_task(task):
    return task in MODES or task in WORKSPACE_TASKS or task in FLIGHT_TASKS


def native_placement(args):
    return "textedit-off-space" if args.off_space else getattr(args, "native_placement", "agent-display")


def browser_identity(args):
    value = getattr(args, "browser_identity", None)
    return json.loads(value) if isinstance(value, str) else value


def off_space_conditions(window, active_space):
    spaces = window.get("space_ids")
    return {"off_screen": window.get("is_on_screen") is False,
            "not_on_agent_display": window.get("on_agent_display") is False,
            "on_inactive_space": isinstance(spaces, list) and bool(spaces) and active_space not in spaces,
            "not_minimized": window.get("is_minimized") is not True}


def require_off_space(ctx, window, active_space, when):
    conditions = off_space_conditions(window, active_space)
    ctx.require(f"{when}: TextEdit remains off screen, outside the agent display, on an inactive Space",
                all(conditions.values()), {"conditions": conditions, "active_space": active_space, "window": window})


def finite_number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def adapter_metrics(events, wall_ms, model):
    """Validate telemetry separately from independently judging its claimed completion."""
    finals = [event for event in events if event.get("type") == "result"]
    if len(finals) != 1:
        raise ValueError("Adapter must emit exactly one terminal result")
    final = finals[0]
    if final.get("status") not in ("completed", "unsupported", "failed"):
        raise ValueError("Adapter terminal status is invalid")
    if final.get("model") != model:
        raise ValueError("Adapter model does not match the explicitly selected model")
    calls = [event for event in events if event.get("type") == "tool"]
    intervals = []
    for call in calls:
        start, end = call.get("start_ms"), call.get("end_ms")
        if not finite_number(start) or not finite_number(end) or start > end or end > wall_ms + 100:
            raise ValueError("Adapter tool interval is missing or outside the measured trial")
        intervals.append((start, end))
    for key in ("model_ms", "cost_usd"):
        if final.get(key) is not None and not finite_number(final[key]):
            raise ValueError(f"Adapter {key} must be finite and nonnegative or absent")
    if final.get("model_ms") is not None and final["model_ms"] > wall_ms + 100:
        raise ValueError("Adapter model time exceeds measured trial wall time")
    if final.get("cost_usd") is not None and not final.get("pricing_source"):
        raise ValueError("Reported cost needs pricing_source")
    # Missing tool telemetry is unknown, not proof of zero tool time.
    tool_ms = report.merge_intervals(intervals) if calls else None
    return {"wall_ms": wall_ms, "tool_ms": tool_ms, "tool_calls": len(calls) if calls else None,
            "model_ms": final.get("model_ms"), "cost_usd": final.get("cost_usd"), "usage": final.get("usage"),
            "pricing_source": final.get("pricing_source"), "retries": sum(c.get("retry") is True for c in calls) if calls else None,
            "non_tool_ms": max(0, wall_ms - tool_ms) if tool_ms is not None else None,
            "source": "external adapter self-reported intervals, provider usage and cost; harness wall clock",
            "adapter_status": final["status"]}


def tool_mix(events):
    """Which tools the model called: mixed means browser_repl and at least one other tool in the same trial."""
    names = collections.Counter(event.get("name") for event in events
                                if event.get("type") == "tool" and event.get("origin") == "model")
    repl = names.get("browser_repl", 0)
    return {"calls_by_tool": dict(sorted(names.items())), "repl_calls": repl,
            "mixed_tools": repl > 0 and sum(names.values()) > repl}


class Meter:
    def __init__(self, ctx):
        self.ctx = ctx
        self.events = []
        self.start = time.monotonic()

    def call(self, name, **kwargs):
        start = (time.monotonic() - self.start) * 1000
        ok = False
        try:
            result = self.ctx.call(name, **kwargs)
            ok = True
            return result
        finally:
            self.events.append({"type": "tool", "name": name, "start_ms": start,
                                "end_ms": (time.monotonic() - self.start) * 1000, "ok": ok, "retry": False})

    def finish(self):
        wall_ms = (time.monotonic() - self.start) * 1000
        tool_ms = report.merge_intervals([(e["start_ms"], e["end_ms"]) for e in self.events])
        return {"wall_ms": wall_ms, "tool_ms": tool_ms, "tool_calls": len(self.events), "model_ms": None,
                "cost_usd": None, "usage": None, "retries": 0, "non_tool_ms": max(0, wall_ms - tool_ms),
                "source": "harness-measured deterministic adapter; no model invoked"}


def execute_builtin(ctx, task, targets, meter):
    if task in WORKSPACE_TASKS:
        from benchmarks.workspace_driver import execute
        return execute(ctx, task, targets, meter)
    if task in FLIGHT_TASKS:
        from benchmarks.flight_driver import execute
        return execute(ctx, targets, meter, "single")
    from run import element_index
    call = meter.call
    if task == "native_notes":
        calc, notes = targets["calculator_window_id"], targets["notes_window_id"]
        tree = call("get_window_state", window_id=calc, full=True)["tree"]
        indexes = [element_index(tree, "AXButton", label) for label in ("7", "Multiply", "6", "Equals")]
        ctx.require("Calculator presents the required buttons", None not in indexes, tree[:600])
        call("batch", window_id=calc, steps=[{"tool": "click", "args": {"element_index": i}} for i in indexes])
        computed = call("get_window_state", window_id=calc, full=True)["tree"]
        ctx.require("adapter reads Calculator result before copying it", re.search(r'"Edit field" value="42"', computed), computed[:600])
        tree = call("get_window_state", window_id=notes, full=True)["tree"]
        area = element_index(tree, "AXTextArea")
        ctx.require("adapter locates the notes field", area is not None, tree[:600])
        call("select_text", window_id=notes, element_index=area, text="Keep this existing line.", selection_type="cursor_after")
        call("type_text", window_id=notes, text="\nTotal: 42")
        return
    tid = targets["tab_id"]
    command = lambda operation, **kw: call("browser_command", tab_id=tid, operation=operation, **kw)
    call("browser_snapshot", tab_id=tid, full=True)
    if task == "browser_work_order":
        command("fill", locator={"label": "Name"}, text="Ada Lovelace")
        command("select_option", locator={"label": "Route"}, value="priority")
        command("check", locator={"label": "Confirm details"}, checked=True)
        command("click", locator={"role": "button", "name": "Create work order"})
        command("wait", locator={"selector": "#form-status", "text": "Work order created", "exact": True},
                state="visible", timeout_ms=5000)
        text = command("inspect", locator={"selector": "#form-status"})["text"]
        ctx.require("adapter sees completed work order", text == "Work order created", text)
    elif task == "canvas_route":
        bounds = command("inspect", locator={"selector": "#route-canvas"})["bounds"]
        # Canvas has a 1px border; these are CSS-pixel coordinates from a current DOM observation.
        x = lambda value: bounds["x"] + 1 + value * (bounds["width"] - 2) / 640
        y = bounds["y"] + bounds["height"] / 2
        command("drag", from_x=x(80), from_y=y, to_x=x(560), to_y=y)
        # Drag promises dispatch, not completion of the fixture's asynchronous POST.
        # Wait for its terminal UI state instead of racing one immediate inspection.
        command("wait", locator={"selector": "#canvas-status", "text": "Route complete", "exact": True},
                state="visible", timeout_ms=5000)
        text = command("inspect", locator={"selector": "#canvas-status"})["text"]
        ctx.require("adapter sees completed route", text == "Route complete", text)
    elif task == "transient_dialog":
        for accept in (False, True):
            command("click", locator={"role": "button", "name": "Review deletion"})
            dialog = command("dialog").get("dialog")
            ctx.require("the expected confirmation is open", dialog and dialog.get("type") == "confirm", dialog)
            command("dialog", accept=accept)
            terminal = "Draft deleted" if accept else "Deletion cancelled. The temporary draft still exists."
            command("wait", locator={"selector": "#dialog-status", "text": terminal, "exact": True},
                    state="visible", timeout_ms=5000)


def execute_external(ctx, request, output, args, row):
    command = json.loads(args.adapter_command)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError("--adapter-command must be a nonempty JSON array of strings")
    # The solver's deadline includes its setup; give the outer supervisor a short
    # additional cleanup window before terminating the complete process group.
    env = {**os.environ, "SC_BENCH_TRACE_DIR": str(output.resolve())}
    proc = report.run_process(command, args.timeout + 20, stdin=json.dumps(request) + "\n", env=env)
    row["metrics"] = {"wall_ms": proc["seconds"] * 1000, "tool_ms": None, "tool_calls": None,
                      "model_ms": None, "cost_usd": None, "retries": None,
                      "source": "harness wall clock; valid adapter telemetry unavailable"}
    (output / "adapter.jsonl").write_text(proc["stdout"])
    (output / "adapter.stderr.log").write_text(proc["stderr"])
    row["supervisor_cleanup"] = proc.get("cleanup")
    row["supervisor_cleanup_errors"] = proc.get("cleanup_errors", [])
    events = []
    for line in proc["stdout"].splitlines():
        if line.strip():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Each adapter event must be an object")
            events.append(event)
    metrics = adapter_metrics(events, proc["seconds"] * 1000, args.model)
    final = next(event for event in events if event.get("type") == "result")
    metrics["model_tool_calls"] = final.get("model_tool_calls")
    metrics.update(tool_mix(events))
    if final.get("repl_audit") is not None:
        row["repl_audit"] = final["repl_audit"]
    row["metrics"] = metrics
    if args.model:
        row["adapter_result"] = final
        row["adapter_tools"] = [event for event in events if event.get("type") == "tool"]
        row["adapter_identity"] = final.get("engine_identity")
        row["isolation"] = final.get("isolation")
        row["cleanup_errors"] = final.get("cleanup_errors", [])
        ctx.require("adapter used the pinned model configuration",
                    final.get("model_config") == request["model_config"], final.get("model_config"))
        ctx.require("adapter used the pinned observation mode",
                    final.get("observation_mode", "on-demand") == request.get("observation_mode", "on-demand"),
                    final.get("observation_mode"))
        ctx.require("adapter used the pinned catalog mode",
                    final.get("catalog_mode", "full") == request.get("catalog_mode", "full"),
                    final.get("catalog_mode"))
        ctx.require("adapter used the pinned browser surface",
                    final.get("browser_surface", "discrete") == request.get("browser_surface", "discrete"),
                    final.get("browser_surface"))
        if final["status"] == "completed":
            ctx.require("adapter confirms restricted computer-tool isolation",
                        final.get("isolation", {}).get("verified") is True, final.get("isolation"))
        ctx.require("adapter released its owned resources", not row["cleanup_errors"], row["cleanup_errors"])
    ctx.require("adapter exits successfully within its deadline", proc["returncode"] == 0 and not proc["timed_out"],
                {k: proc[k] for k in ("returncode", "timed_out", "seconds")})
    ctx.require("supervisor released its owned process group", not row["supervisor_cleanup_errors"], row["supervisor_cleanup_errors"])
    if metrics["adapter_status"] == "unsupported":
        from run import Skip
        raise Skip("The external adapter reports this task unsupported")
    ctx.require("adapter reports task completed", metrics["adapter_status"] == "completed", metrics)
    return metrics


def full_text(ctx, window):
    from run import element_index
    tree = ctx.tree(window, include_screenshot=False)
    area = element_index(tree, "AXTextArea")
    ctx.require("independent judge locates the note", area is not None, tree[:600])
    return ctx.call("inspect_element", window_id=window, element_index=area, offset=0, length=10000)["text"]


def require_desktop_window(ctx, window, name):
    ctx.require(f"{name} fixture is on screen on the ordinary desktop",
                window.get("is_on_screen") is True and window.get("on_agent_display") is False
                and window.get("is_minimized") is False, window)


def start_fixture_browser(ctx, row, trial_dir):
    """Register teardown before readiness, including a failed startup's browser."""
    instance = ctx.run.instance
    ctx.require("browser trial starts without a previous fixture browser", instance.browser is None)
    owned = {"browser": None}

    def close_fixture_browser():
        browser = owned["browser"]
        evidence = {"pid": browser.pid if browser is not None else None,
                    "errors": [], "verified": browser is None, "status": "not_launched"}
        row["fixture_cleanup"] = evidence
        if browser is not None:
            try:
                browser.stop()
            except Exception as error:
                evidence["errors"].append(f"stop: {type(error).__name__}: {error}")
            try:
                deadline = time.monotonic() + 2
                while True:
                    evidence["returncode"] = browser.process.poll()
                    evidence["process_group_members"] = report.group_members(browser.pid)
                    live = [member for member in evidence["process_group_members"]
                            if not member["state"].startswith("Z")]
                    absent = evidence["returncode"] is not None and not live
                    if absent or time.monotonic() >= deadline:
                        break
                    time.sleep(.05)
                if not absent:
                    evidence["errors"].append("Browser or live process-group members remain after teardown")
                evidence["verified"] = absent and not evidence["errors"]
            except Exception as error:
                evidence["errors"].append(f"verify: {type(error).__name__}: {error}")
            evidence["status"] = "no_live_processes" if evidence["verified"] else "failed"
            # A failed teardown remains owned by the instance for its final retry;
            # the next trial must fail the fresh-browser check rather than reuse it.
            if evidence["verified"] and instance.browser is browser:
                instance.browser = None
        ctx.check("harness released its fixture browser and live helper processes", evidence["verified"], evidence)
        report.write_json(trial_dir / "fixture-cleanup.json", evidence)

    ctx.cleanups.append(close_fixture_browser)
    try:
        instance.start_browser(headed=True)
    finally:
        owned["browser"] = instance.browser
    return owned["browser"]


def judge_workspace(ctx, server, row):
    """Read the durable ledger independently and retain every predicate, even on failure."""
    checks, evidence = {}, {}
    row["independent_task_success"] = False
    try:
        deadline = time.monotonic() + 2
        while True:
            durable = server.read()
            if not isinstance(durable, dict):
                raise ValueError("Workspace durable evidence is missing or malformed")
            evidence["ledger"] = durable
            judged = server.judge(durable)
            if (not isinstance(judged, dict) or not judged or any(not isinstance(key, str) or not key
                    or type(value) is not bool for key, value in judged.items())):
                raise ValueError("Workspace judge must return nonempty named boolean checks")
            checks = dict(judged)
            if (not isinstance(durable.get("case_metadata"), dict) or not isinstance(durable.get("baseline"), dict)
                    or not isinstance(durable.get("state"), dict) or not isinstance(durable.get("events"), list)):
                raise ValueError("Workspace durable evidence is missing or malformed")
            checks["durable_workspace_evidence_readable"] = True
            checks["durable_workspace_case_matches_manifest"] = durable["case_metadata"] == row["case_metadata"]
            if all(checks.values()) or time.monotonic() >= deadline:
                break
            time.sleep(.05)
    except Exception as error:
        checks["durable_workspace_evidence_readable"] = False
        evidence["error"] = f"{type(error).__name__}: {error}"
    evidence["checks"] = checks
    row["independent_checks"] = checks
    row["independent_task_success"] = bool(checks) and all(checks.values())
    diagnostics = getattr(server, "diagnostics", None)
    if callable(diagnostics) and "ledger" in evidence:
        try:
            detail = diagnostics(evidence["ledger"])
            if not isinstance(detail, dict):
                raise ValueError("Workspace diagnostics must be an object")
            json.dumps(detail, allow_nan=False)
            evidence["diagnostics"] = detail
            row["independent_diagnostics"] = detail
        except Exception as error:
            # Auxiliary counts cannot replace or invalidate the exact predicates.
            evidence["diagnostics_error"] = f"{type(error).__name__}: {error}"
            row["independent_diagnostics_error"] = evidence["diagnostics_error"]
    for name, passed in checks.items():
        ctx.check("independent workspace: " + name, passed, evidence.get("error"))
    return evidence


def judge_flight(ctx, server, row):
    """The server's ledger and expected flights, against the reply the adapter returned; never the page's state."""
    time.sleep(.2)
    events = server.read()
    answer = (row.get("adapter_result") or {}).get("final_response")
    checks = server.judge(events, answer)
    evidence = {"events": events, "answer": answer, "checks": checks, "diagnostics": server.diagnostics(events, answer)}
    row["independent_checks"] = checks
    row["independent_diagnostics"] = evidence["diagnostics"]
    row["independent_task_success"] = all(checks.values())
    for name, passed in checks.items():
        ctx.check("independent flight search: " + name, passed, evidence["diagnostics"])
    return evidence


def task_trial(ctx, task, round_number, args, row, output):
    import run as harness
    trial_dir = output / "trials" / f"{task}-{round_number}"
    trial_dir.mkdir(parents=True)
    row["evidence_dir"] = str(trial_dir)
    server, targets, meter = None, {}, None
    if task == "native_notes":
        placement = native_placement(args)
        note_filename = f"benchmark-notes-{round_number}.txt"
        note_window = ctx.launch_scratch_textedit(note_filename, NOTE, on_agent_display=placement == "agent-display")
        notes = note_window["id"]
        row["fixture_environment"] = {"native_placement": native_placement(args), "textedit_initial": note_window,
                                      "calculator_placement_requested": "desktop" if placement == "desktop" else "agent-display"}
        if placement == "desktop":
            if harness.observe()["main_space_full_screen"]:
                raise harness.Skip("Visible desktop fixtures require an ordinary desktop Space")
            require_desktop_window(ctx, note_window, "TextEdit")
        if placement == "textedit-off-space":
            active_space = harness.observe()["active_space"]
            row["fixture_environment"]["initial_active_space"] = active_space
            require_off_space(ctx, note_window, active_space, "Before task")
            ctx.run.notes.append("Explicit --off-space native task: TextEdit stays on its ordinary inactive Space; Calculator uses the existing agent-display setup. The default benchmark profile is unchanged.")
        launched = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=placement != "desktop")
        ctx.kill_on_exit(launched["app"]["pid"])
        calc_window = ctx.wait_for_window("Calculator", pid=launched["app"]["pid"])
        calc = calc_window["id"]
        row["fixture_environment"]["calculator_initial"] = calc_window
        if placement == "desktop":
            require_desktop_window(ctx, calc_window, "Calculator")
        harness.clear_calculator(ctx, calc)
        ctx.require("original note is intact at task start", full_text(ctx, notes) == NOTE)
        # Setup input belongs to the verifier session. Release its leases before a
        # distinct engine/client begins, so all adapters start with identical ownership.
        for window in (calc, notes):
            ctx.call("bind_target", action="release", target=f"window:{window}")
        targets = {"calculator_window_id": calc, "calculator_pid": launched["app"]["pid"],
                   "notes_window_id": notes, "notes_pid": note_window["pid"], "original_note": NOTE}
    else:
        if task in WORKSPACE_TASKS:
            server = WorkspaceFixture(task, row["case_seed"], trial_dir).__enter__()
            ctx.cleanups.append(lambda: server.__exit__(None, None, None))
            row["case_metadata"] = server.case_metadata()
            report.write_json(trial_dir / "case-manifest.json", row["case_metadata"])
            targets["url"] = server.url
        elif task in FLIGHT_TASKS:
            server = FlightFixture(row["case_seed"], trial_dir).__enter__()
            ctx.cleanups.append(lambda: server.__exit__(None, None, None))
            row["case_metadata"] = server.case_metadata()
            report.write_json(trial_dir / "case-manifest.json", row["case_metadata"])
            targets["url"] = server.url
        else:
            server = FixtureServer().__enter__()
            ctx.cleanups.append(lambda: server.__exit__(None, None, None))
            targets["url"] = server.url + "?task=" + MODES[task]
        if args.model and getattr(args, "headed_browser", False):
            # Each solver gets the same fresh, active blank tab. Reusing a
            # profile would leave the previous task visible and break binding.
            browser = start_fixture_browser(ctx, row, trial_dir)
            row["fixture_environment"] = {"browser": browser.metadata, "browser_pid": browser.pid}
            ctx.require("fixture browser matches the pinned visible browser build",
                        browser.headed is True and browser.identity == browser_identity(args), browser.metadata)
            window = ctx.wait_for_window("Google Chrome for Testing", pid=browser.pid, title="about:blank")
            row["fixture_environment"]["browser_window"] = window
            require_desktop_window(ctx, window, "Browser")
            targets.update(browser_pid=browser.pid, browser_window_id=window["id"])
        if not args.adapter_command:
            # Workspace smoke tests exercise the same initial navigation as the
            # model's claimed blank tab, including the server's document boundary.
            blank = task in WORKSPACE_TASKS or task in FLIGHT_TASKS
            tid = ctx.call("browser_open", url="about:blank" if blank else targets["url"])["tab"]["id"]
            targets["tab_id"] = tid
            ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tid))
            if task in WORKSPACE_TASKS:
                ctx.call("browser_navigate", tab_id=tid, url=targets["url"])
    request = {"protocol": report.CONTRACT, "task_id": task, "trial": round_number, "track": args.track,
               "native_placement": native_placement(args),
               "model": args.model, "prompt": (server.prompt if task in WORKSPACE_TASKS or task in FLIGHT_TASKS else PROMPTS[task])
               + (REPL_RULES if getattr(args, "browser_surface", "discrete") == "repl" else RULES),
               "targets": targets, "timeout_seconds": args.timeout}
    if args.model:
        request.update(model_config=report.model_config_for(args), case_seed=row["case_seed"],
                       observation_mode=getattr(args, "observation_mode", "on-demand"),
                       catalog_mode=getattr(args, "catalog_mode", "full"),
                       browser_surface=getattr(args, "browser_surface", "discrete"),
                       interface_track=args.interface_track, engine=args.engine, engine_version=args.engine_version,
                       runner_sha256=row["runner_sha256"], browser_visibility="headed" if args.headed_browser else "headless",
                       browser_identity=browser_identity(args))
    report.write_json(trial_dir / "request.json", request)
    before, started = harness.observe(), time.time()
    target_pids = sorted({value for key, value in targets.items() if key.endswith("_pid")})
    initial_foreground = before["frontmost"]
    row["execution_environment"] = {
        "initial_frontmost": initial_foreground, "initial_active_space": before["active_space"],
        "target_pids": target_pids,
        "fixture_initially_foreground": initial_foreground["pid"] in target_pids if target_pids else None,
        "background_start_observed": initial_foreground["pid"] not in target_pids if target_pids else None,
        "background_execution_verified": False,
        "note": "Foreground state is sampled at solver endpoints only. A foreground fixture or unchanged endpoints do not establish background execution.",
    }
    report.write_json(trial_dir / "execution-environment.json", row["execution_environment"])
    adapter_error = None
    try:
        if args.adapter_command:
            row["metrics"] = execute_external(ctx, request, trial_dir, args, row)
        else:
            meter = Meter(ctx)
            builtin_targets = {**targets, "goal": request["prompt"]} if task in WORKSPACE_TASKS else targets
            answer = execute_builtin(ctx, task, builtin_targets, meter)
            if task in FLIGHT_TASKS:
                row["adapter_result"] = {"final_response": answer}
    except Exception as error:
        adapter_error = error
    finally:
        if meter is not None:
            row["metrics"] = meter.finish()
            report.write_json(trial_dir / "tool-events.json", meter.events)
        try:
            after = harness.observe()
            row["execution_environment"].update(final_frontmost=after["frontmost"], final_active_space=after["active_space"])
            report.write_json(trial_dir / "execution-environment.json", row["execution_environment"])
            row["adapter_invariants"] = ctx.run.invariants(before, after, time.time() - started)
            report.write_json(trial_dir / "adapter-invariants.json", row["adapter_invariants"])
        except Exception as error:
            if task not in WORKSPACE_TASKS and task not in FLIGHT_TASKS:
                raise
            row["post_adapter_error"] = f"{type(error).__name__}: {error}"
            adapter_error = adapter_error or error
    if task == "native_notes":
        actual = full_text(ctx, notes)
        tree = ctx.tree(calc, include_screenshot=True)
        ctx.call("screenshot", window_id=notes)
        saved_note = (ctx.scratch / note_filename).read_text()
        evidence = {"note": actual, "calculator_tree": tree, "saved_note": saved_note}
        row["independent_task_success"] = (actual == NOTE + "\nTotal: 42"
            and bool(re.search(r'"Edit field" value="42"', tree)) and saved_note == NOTE)
        ctx.check("independent read: note preserved and result appended exactly once", actual == NOTE + "\nTotal: 42", actual)
        ctx.check("independent read: Calculator still shows 42", bool(re.search(r'"Edit field" value="42"', tree)), tree[:500])
        ctx.check("independent file read: the task did not save or overwrite the original fixture", saved_note == NOTE, saved_note)
        if native_placement(args) == "textedit-off-space":
            windows = ctx.call("list_windows", app="TextEdit")["windows"]
            final_window = next((window for window in windows if window["id"] == notes), None)
            ctx.require("off-Space fixture still exists after the task", final_window is not None)
            active_space = harness.observe()["active_space"]
            row["fixture_environment"].update(textedit_final=final_window, final_active_space=active_space)
            require_off_space(ctx, final_window, active_space, "After task")
    elif task in WORKSPACE_TASKS:
        evidence = judge_workspace(ctx, server, row)
    elif task in FLIGHT_TASKS:
        evidence = judge_flight(ctx, server, row)
    else:
        # Judge persistence after the adapter stops, not merely its own final observation.
        deadline = time.monotonic() + 2
        while not judge(task, server.read()) and time.monotonic() < deadline:
            time.sleep(.05)
        time.sleep(.2)
        evidence = {"events": server.read()}
        row["independent_task_success"] = judge(task, evidence["events"])
        ctx.check("independent event ledger verifies exact task result with no duplicate final action", row["independent_task_success"], evidence)
    report.write_json(trial_dir / "independent-result.json", evidence)
    if adapter_error is not None:
        raise adapter_error


def run_tasks(args):
    import run as harness
    model_config = report.model_config_for(args)
    if args.model:
        from model_benchmark import runner_fingerprint
        runner_sha256 = runner_fingerprint()
    else:
        runner_sha256 = None
    seeds = getattr(args, "seed", None) or list(range(1, args.rounds + 1))
    if len(seeds) != args.rounds or len(set(seeds)) != len(seeds) or any(seed < 0 for seed in seeds):
        raise ValueError("Provide exactly one unique nonnegative --seed per round")
    if args.model and not report.pinned_identifier(args.engine_version):
        raise ValueError("Model runs require an explicit pinned --engine-version")
    if args.adapter_command:
        command = json.loads(args.adapter_command)
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError("--adapter-command must be a nonempty JSON array of strings")
        if args.engine == "second-cursor-scripted":
            raise ValueError("An external adapter needs an explicit --engine name")
    elif args.model:
        raise ValueError("The built-in adapter does not invoke a model; supply --adapter-command for model runs")
    elif args.engine != "second-cursor-scripted":
        raise ValueError("The built-in adapter must retain the engine name second-cursor-scripted")
    tasks = args.task or report.DEFAULT_TASKS
    output = report.artifact_dir("best-in-class-tasks")
    meta = report.metadata()
    result = {"kind": "tasks", "contract": report.CONTRACT, "engine": args.engine,
              "engine_version": args.engine_version or (meta["commit"] if not args.adapter_command else None),
              "track": args.track, "native_placement": native_placement(args), "model": args.model,
              "fixture_sha256": meta["fixture_sha256"], "task_sha256": meta["task_sha256"],
              "metadata": meta, "rounds": args.rounds, "trials": [], "status": "INCOMPLETE"}
    result.update(execution_profile="model" if args.model else "external-scripted" if args.adapter_command else "scripted",
                  observation_mode=getattr(args, "observation_mode", "on-demand"),
                  catalog_mode=getattr(args, "catalog_mode", "full"),
                  browser_surface=getattr(args, "browser_surface", "discrete"),
                  model_config=model_config, interface_track=getattr(args, "interface_track", "native-tools"),
                  runner_sha256=runner_sha256,
                  browser_visibility="headed" if getattr(args, "headed_browser", False) else "headless",
                  browser_identity=browser_identity(args),
                  case_seed_semantics=report.case_seed_semantics(tasks))
    result["coexistence_scope"] = {"endpoint_invariants": True, "transient_interference_verified": False,
                                   "background_execution_verified": False,
                                   "note": "The host-background track requests background actions; it does not prove the fixture started or remained in the background. Each trial records its initial foreground app and target PIDs. Task trials compare before/after focus, cursor and Space and cannot detect a transient change restored between reads. Use attended focus and same-app suites for event-counter evidence."}
    harness.SCENARIOS.clear()
    for round_number in range(1, args.rounds + 1):
        for task in tasks:
            row = {"task_id": task, "trial": round_number, "case_seed": seeds[round_number - 1],
                   "status": "INCOMPLETE", "metrics": {}, "runner_sha256": runner_sha256}
            result["trials"].append(row)
            def trial(ctx, task=task, round_number=round_number, row=row):
                task_trial(ctx, task, round_number, args, row, output)
            trial.__name__ = f"{task}_{round_number}"
            harness.SCENARIOS.append(trial)
    run = None
    try:
        run = harness.Run(browser=(not (args.model and getattr(args, "headed_browser", False)))
                          and (not args.adapter_command or getattr(args, "adapter_browser", False))
                          and any(browser_task(t) for t in tasks), installed=False,
                          headed_browser=getattr(args, "headed_browser", False))
        run.dir.rename(output / "e2e")
        run.dir = output / "e2e"
        run.run([])
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        if run is not None:
            run.close()
    finally:
        outcomes = {r["scenario"]: r for r in (run.results if run else [])}
        for row in result["trials"]:
            observed = outcomes.get(f"{row['task_id']}_{row['trial']}")
            if observed:
                row.update(checks=observed["checks"], invariants=observed["invariants"], error=observed["error"])
                row["status"] = report.classify_scenario({**observed, "invariants": observed["invariants"] + row.get("adapter_invariants", [])})
            else:
                row["error"] = result.get("error", "Trial did not run")
            if args.model and row["task_id"] == "native_notes":
                from benchmarks.completion_timing import verified_native_completion
                events = [*row.get("adapter_tools", []), row.get("adapter_result", {})]
                trace_events = []
                try:
                    trace_path = pathlib.Path(row["evidence_dir"]) / "model-trace.jsonl"
                    trace_events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
                except (KeyError, OSError, ValueError):
                    # Missing or malformed timing evidence cannot establish an
                    # earlier verified completion; ordinary outcome judging stands.
                    pass
                row["metrics"].update(verified_native_completion(row, events, trace_events))
        result["status"] = report.overall(result["trials"])
        if result.get("error"):
            result["status"] = "FAIL"
        result["summary"] = report.aggregate(result["trials"])
        report.write_json(output / "tasks.json", result)
        report.write_json(output / "results.json", result)
        lines = [f"# Complete task benchmark — {result['status']}", "",
                 f"Engine: `{args.engine}`. Model: `{args.model}`. Track: `{args.track}`. Native placement: `{native_placement(args)}`.", "",
                 f"Execution profile: `{result['execution_profile']}`. Unknown model usage and cost remain unavailable. Every trial stays in the denominator.", "",
                 f"Model configuration: `{json.dumps(model_config, sort_keys=True)}`. {result['case_seed_semantics']}", "",
                 "Coexistence is checked at task endpoints only. The host-background track requests background actions, but a passing task does not verify background execution or absence of transient focus, cursor or Space changes. Each trial records whether its fixture initially had foreground focus; attended focus suites provide separate evidence.", "",
                 "| Task | Independent outcome correct | All checks passed | Median / p95 wall ms | Tool median ms |", "| --- | ---: | ---: | ---: | ---: |"]
        for task, summary in result["summary"].items():
            wall = summary["wall_ms"]
            verified = summary.get("independent_verified")
            outcome = "unavailable" if verified is None else f"{verified}/{summary['trials']} ({summary['independent_measured_trials']} judged)"
            lines.append(f"| {task} | {outcome} | {summary['completed']}/{summary['trials']} | {wall['median']} / {wall['p95']} | {summary['tool_ms']['median']} |")
        lines.extend(["", f"Full evidence: [tasks.json]({output / 'tasks.json'}). Independent checks and screenshots: [E2E report]({output / 'e2e/report.md'})."])
        (output / "report.md").write_text("\n".join(lines) + "\n")
    print(f"Benchmark: {output / 'tasks.json'}")
    return report.exit_code(result["status"])
