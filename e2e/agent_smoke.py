#!/usr/bin/env python3
"""Real-agent smoke test: a real model (the Cursor Agent CLI) drives Second Cursor through MCP.

The scripted suites prove the mechanics with fixed calls; this proves a model can find and use the tools.
It costs model usage, so it only runs with --run; without it, it prints the plan and exits.

Flow: fresh TestInstance -> temp project whose .cursor/mcp.json points at the instance (the user's global
~/.cursor/mcp.json is never touched) -> scripted fixtures (scratch TextEdit window, new Calculator) ->
`cursor-agent -p` with a timeout -> independent verification over MCP -> artifacts.

Usage:  python3 e2e/agent_smoke.py
        python3 e2e/agent_smoke.py --run [--model <model>] [--timeout <seconds>]
Output: e2e/artifacts/agent-<ts>/ report.md, results.json, transcript.jsonl (verification calls),
        agent/ (prompt, stream-json events, stderr, the project's mcp.json with the token redacted),
        agent/actions.jsonl (the instance's action log: every state-changing MCP call the agent made)
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

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


MODEL = option("--model")
TIMEOUT = float(option("--timeout", "300"))

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


@scenario
def agent_calculator_to_textedit(ctx):
    inst = ctx.run.instance
    if inst is None:
        raise Skip("needs a disposable test instance (not the installed app)")
    agent = shutil.which("cursor-agent")
    ctx.require("cursor-agent is installed", agent, "install the Cursor Agent CLI")

    text_window = ctx.launch_scratch_textedit("agent-notes.txt", TEXT)["id"]
    launched = ctx.call("launch_app", app="Calculator", new_instance=True)
    ctx.kill_on_exit(launched["app"]["pid"])
    calc = ctx.wait_for_window("Calculator", pid=launched["app"]["pid"])["id"]
    ctx.require("fixture text is in place", full_text(ctx, text_window) == TEXT)

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
    print(f"\nPrompt ({SERVER}, window ids filled in at run time):\n")
    print(PROMPT.format(server=SERVER, calc="<calculator window>", text="<textedit window>"))
    print("\nThis spends model usage. Re-run with --run to execute.")


if __name__ == "__main__":
    if "--run" not in sys.argv:
        plan()
        sys.exit(0)
    # A model may reach for gated tools; the instance must grant or refuse without ever showing a dialog.
    smoke = run.Run(config_overrides={"approvals": "allow", "appApproval": "off"}, browser=False)
    renamed = smoke.dir.with_name(f"agent-{smoke.dir.name}")
    smoke.dir.rename(renamed)
    smoke.dir = renamed
    sys.exit(0 if smoke.run(set()) else 1)
