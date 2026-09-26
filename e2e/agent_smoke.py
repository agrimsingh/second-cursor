#!/usr/bin/env python3
"""Real-agent smoke test: a real model (the Cursor Agent CLI) drives Second Cursor through MCP.

The scripted suites prove the mechanics with fixed calls; this proves a model can find and use the tools.
It costs model usage, so it only runs with --run; without it, it prints the plan and exits.

Flow: fresh TestInstance -> temp project whose .cursor/mcp.json points at the instance (the user's global
~/.cursor/mcp.json is never touched) -> scripted fixtures (scratch TextEdit window, new Calculator) ->
`cursor-agent -p` with a timeout -> independent verification over MCP -> artifacts.

Usage:  python3 e2e/agent_smoke.py
        python3 e2e/agent_smoke.py --run [--model <model>] [--timeout <seconds>]
        python3 e2e/agent_smoke.py --run --models <a,b,...> [--repeat <n>]   compare models, n runs each
        --config key=value (repeatable) merges into the test instance's config, e.g. --config toolSurface=compact;
        values that parse as JSON (2, true, ["a"]) are used as such, anything else as a string
Output: e2e/artifacts/agent-<ts>/ report.md, results.json, transcript.jsonl (verification calls),
        agent/ (prompt, stream-json events, stderr, the project's mcp.json with the token redacted),
        agent/actions.jsonl (the instance's action log: every state-changing MCP call the agent made),
        agent/timing.json (agent loop split into model time and tool time, tool calls, token usage);
        with more than one run, also e2e/artifacts/agent-compare-<ts>.md
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import run
from run import SCENARIOS, Skip, ToolError, element_index, scenario

SCENARIOS.clear()

SERVER = "second-cursor-e2e"
TEXT = "Agent smoke test\nResult:"


def option(name, default=None):
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return default


def config_overrides():
    overrides = {}
    for flag, pair in zip(sys.argv, sys.argv[1:]):
        if flag != "--config":
            continue
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--config takes key=value, got {pair!r}")
        try:
            overrides[key] = json.loads(value)
        except json.JSONDecodeError:
            overrides[key] = value
    return overrides


def config_label(config):
    return ", ".join(f"{k}={v if isinstance(v, str) else json.dumps(v)}" for k, v in config.items()) or "default"


MODEL = option("--model")
TIMEOUT = float(option("--timeout", "300"))
CONFIG = config_overrides()

PROMPT = """Use only the tools of the MCP server named "{server}" (Second Cursor: it operates Mac apps in the \
background). Do not use the shell, AppleScript, files, a browser, or any other MCP server.
1. In the Calculator window with window_id {calc}, compute 7 × 6 by pressing its buttons.
2. Append the result on a new line at the end of the text in the TextEdit window with window_id {text}. Keep the \
existing text. Do not save or close anything.
Reply with only the result."""


def agent_command(project, prompt):
    command = ["cursor-agent", "-p", "--output-format", "stream-json", "--force", "--approve-mcps", "--trust",
               "--workspace", str(project)]
    if MODEL:
        command += ["--model", MODEL]
    return command + [prompt]


def full_text(ctx, window_id):
    tree = ctx.tree(window_id)
    area = element_index(tree, "AXTextArea")
    if area is None:
        return None
    try:
        return ctx.call("inspect_element", window_id=window_id, element_index=area, offset=0, length=10000)["text"]
    except (ToolError, KeyError):
        return run.value_of(tree)


def tool_events(events):
    """Tool calls from cursor-agent's stream-json events, as (kind, detail) pairs."""
    calls = []
    for event in events:
        if event.get("type") != "tool_call" or event.get("subtype") != "started":
            continue
        call = event.get("tool_call") or {}
        kind = next(iter(call), "unknown")
        calls.append((kind, json.dumps(call.get(kind, {}))[:300]))
    return calls


def timing(events, elapsed):
    """Splits the agent loop into time inside tool calls (the union of their intervals, since a model may
    call tools in parallel) and everything else, which is the model's time."""
    init = next((e for e in events if e.get("type") == "system"), {})
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    starts, intervals, names = {}, [], []
    for e in events:
        if e.get("type") != "tool_call":
            continue
        if e.get("subtype") == "started":
            starts[e["call_id"]] = e["timestamp_ms"]
            call = e.get("tool_call") or {}
            kind = next(iter(call), "unknown")
            tool = (call.get(kind) or {}).get("args", {}).get("toolName")
            if kind.startswith("getMcpTools"):
                names.append(f"schema:{tool}" if tool else "schema")
            else:
                names.append(tool or kind.removesuffix("ToolCall"))
        elif e.get("subtype") == "completed" and e.get("call_id") in starts:
            intervals.append((starts[e["call_id"]], e["timestamp_ms"]))
    tool_ms, end = 0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            tool_ms += stop - start
            end = stop
        elif stop > end:
            tool_ms += stop - end
            end = stop
    loop_ms = final.get("duration_ms") or 0
    return {
        "model": init.get("model"), "requested_model": MODEL, "config": CONFIG, "loop_s": round(loop_ms / 1000, 1),
        "model_s": round((loop_ms - tool_ms) / 1000, 1), "tool_s": round(tool_ms / 1000, 1),
        "tool_calls": len(names), "used_batch": bool({"batch", "act"} & set(names)), "calls": names,
        "process_s": round(elapsed, 1), "usage": final.get("usage"),
    }


@scenario
def agent_calculator_to_textedit(ctx):
    inst = ctx.run.instance
    if inst is None:
        raise Skip("needs a disposable test instance (not the installed app)")
    agent = shutil.which("cursor-agent")
    ctx.require("cursor-agent is installed", agent, "install the Cursor Agent CLI")

    # Off the user's screen: a fixture window the user can see is one they can close mid-run, and Calculator
    # quits with its last window.
    text_window = ctx.launch_scratch_textedit("agent-notes.txt", TEXT, on_agent_display=True)["id"]
    launched = ctx.call("launch_app", app="Calculator", new_instance=True, on_agent_display=True)
    ctx.kill_on_exit(launched["app"]["pid"])
    calc = ctx.wait_for_window("Calculator", pid=launched["app"]["pid"])["id"]
    run.clear_calculator(ctx, calc)
    ctx.require("fixture text is in place", full_text(ctx, text_window) == TEXT)
    # The harness shares the agent's default session; a new turn makes the agent's first read a full outline,
    # as it would be for a real agent, instead of a diff against the harness's reads.
    ended = urllib.request.urlopen(urllib.request.Request(
        ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {ctx.mcp.token}", "Content-Type": "application/json"}), timeout=15)
    ctx.require("the harness's turn ended before the agent starts", json.loads(ended.read()).get("ok") is True)

    project = pathlib.Path(tempfile.mkdtemp(prefix="sc-agent-"))
    ctx.cleanups.append(lambda: shutil.rmtree(project, ignore_errors=True))
    mcp_config = {"mcpServers": {SERVER: {"url": inst.url, "headers": {"Authorization": f"Bearer {inst.token}"}}}}
    (project / ".cursor").mkdir()
    (project / ".cursor/mcp.json").write_text(json.dumps(mcp_config, indent=2))

    out_dir = ctx.run.dir / "agent"
    out_dir.mkdir(exist_ok=True)
    redacted = json.dumps(mcp_config, indent=2).replace(inst.token, "<token>")
    (out_dir / "mcp.json").write_text(redacted + "\n")
    prompt = PROMPT.format(server=SERVER, calc=calc, text=text_window)
    (out_dir / "prompt.txt").write_text(prompt + "\n")
    log_start = len(inst.action_log.read_text().splitlines()) if inst.action_log.exists() else 0

    command = agent_command(project, prompt)
    started = time.time()
    try:
        proc = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=TIMEOUT)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        decode = lambda b: b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        code, stdout, stderr = None, decode(e.stdout), decode(e.stderr)
    elapsed = time.time() - started
    (out_dir / "agent.jsonl").write_text(stdout)
    (out_dir / "agent-stderr.txt").write_text(f"$ {' '.join(command[:-1])} <prompt>\nexit: {code} ({elapsed:.1f}s)\n{stderr}")
    actions = inst.action_log.read_text().splitlines()[log_start:] if inst.action_log.exists() else []
    (out_dir / "actions.jsonl").write_text("\n".join(actions) + ("\n" if actions else ""))

    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    calls = tool_events(events)
    (out_dir / "tool-calls.txt").write_text("\n".join(f"{kind}: {detail}" for kind, detail in calls) + "\n")
    split = timing(events, elapsed)
    (out_dir / "timing.json").write_text(json.dumps(split, indent=2) + "\n")
    ctx.run.notes.append(
        f"{split['model']} (config: {config_label(CONFIG)}): agent loop {split['loop_s']}s = model {split['model_s']}s + tools {split['tool_s']}s, "
        f"{split['tool_calls']} tool calls, batch {'used' if split['used_batch'] else 'not used'} "
        f"({split['process_s']}s including CLI startup)")

    ctx.check(f"cursor-agent finished within {TIMEOUT:.0f}s and exited 0", code == 0, f"exit {code} after {elapsed:.0f}s; {stderr[-300:]}")
    ctx.check("the agent reported success", final and not final.get("is_error"), final)
    ctx.check("the agent's answer is 42", re.search(r"\b42\b", str(final.get("result", ""))), final.get("result"))

    calc_tree = ctx.tree(calc, include_screenshot=True)
    ctx.check("independent read: Calculator shows 42", re.search(r"\b42\b", calc_tree), calc_tree[:500])
    text = full_text(ctx, text_window) or ""
    ctx.call("screenshot", window_id=text_window)
    ctx.check("independent read: TextEdit kept its text and gained 42 on a new line",
              re.fullmatch(re.escape(TEXT) + r"\n42\s*", text), repr(text[-200:]))

    logged = [json.loads(line) for line in actions if line.strip()]
    touched = {entry.get("target") for entry in logged}
    ctx.check("the agent's actions went through the test instance (its action log names both windows)",
              any(str(calc) in str(t) for t in touched) and any(str(text_window) in str(t) for t in touched),
              sorted(map(str, touched))[:10])
    shell = [detail for kind, detail in calls if "shell" in kind.lower()]
    ctx.check("the agent used no shell commands", not shell, shell[:3])


def plan():
    print(__doc__.strip())
    print("\nWould run, in a temp project with only a project-level .cursor/mcp.json for the test instance:")
    print("  " + " ".join(agent_command("<temp project>", "<prompt>")))
    print(f"\nTest instance config: {json.dumps(instance_config())}")
    print(f"\nPrompt ({SERVER}, window ids filled in at run time):\n")
    print(PROMPT.format(server=SERVER, calc="<calculator window>", text="<textedit window>"))
    print("\nThis spends model usage. Re-run with --run to execute.")


def seconds(value):
    return "?" if value is None else f"{value}s"


def median(values):
    return values[len(values) // 2] if values else "?"


def instance_config():
    """A model may reach for gated tools; the instance must grant or refuse without ever showing a dialog."""
    return {"approvals": "allow", "appApproval": "off", **CONFIG}


def compare(rows):
    lines = ["| Model | Config | Run | Result | Loop | Model time | Tool time | Tool calls | Batch |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        t = row["timing"] or {}
        lines.append(f"| {row['model'] or 'default'} | {config_label(row['config'])} | {row['dir']} | "
                     f"{'PASS' if row['passed'] else 'FAIL'} | "
                     f"{seconds(t.get('loop_s'))} | {seconds(t.get('model_s'))} | {seconds(t.get('tool_s'))} | "
                     f"{t.get('tool_calls', '?')} | {'yes' if t.get('used_batch') else 'no'} |")
    lines += ["", "| Model | Passed | Median loop | Median tool calls |", "| --- | --- | --- | --- |"]
    for model in dict.fromkeys(row["model"] for row in rows):
        mine = [row for row in rows if row["model"] == model]
        loops = sorted(row["timing"]["loop_s"] for row in mine if row["timing"])
        counts = sorted(row["timing"]["tool_calls"] for row in mine if row["timing"])
        lines.append(f"| {model or 'default'} | {sum(row['passed'] for row in mine)}/{len(mine)} | "
                     f"{median(loops)}s | {median(counts)} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if "--run" not in sys.argv:
        plan()
        sys.exit(0)
    models = [m.strip() or None for m in (option("--models") or MODEL or "").split(",")]
    repeat = int(option("--repeat", "1"))
    rows = []
    for model in models:
        for _ in range(repeat):
            MODEL = model
            smoke = run.Run(config_overrides=instance_config(), browser=False)
            renamed = smoke.dir.with_name(f"agent-{smoke.dir.name}")
            smoke.dir.rename(renamed)
            smoke.dir = renamed
            passed = smoke.run(set())
            split = smoke.dir / "agent/timing.json"
            rows.append({"model": model, "config": CONFIG, "dir": smoke.dir.name, "passed": passed,
                         "timing": json.loads(split.read_text()) if split.exists() else None})
    if len(rows) > 1:
        summary = run.ROOT / "artifacts" / f"agent-compare-{time.strftime('%Y%m%d-%H%M%S')}.md"
        summary.write_text(f"# Agent smoke comparison\n\n{compare(rows)}")
        print(f"\n{compare(rows)}\nSummary: {summary}")
    sys.exit(0 if all(row["passed"] for row in rows) else 1)
