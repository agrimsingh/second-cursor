#!/usr/bin/env python3
"""Keys that reach the system clipboard need the user's clipboard approval, on every browser input route, driven over
MCP like a client against e2e/fixtures/clipboard.html in the isolated test browser.

Ways it can fail, written down before the code:
 1. Cmd+V sends Chrome's paste editing command, so an agent-built page receives the clipboard and the agent reads it
    back from a field. Without the clipboard approval the key is refused before any key event is sent.
 2. Cmd+C and Cmd+X send copy and cut, so the agent overwrites the user's clipboard (and cut deletes the selection).
    Refused the same way.
 3. Chrome's own key bindings reach the clipboard without an editing command: Shift+Insert pastes, Ctrl+Insert
    copies, Shift+Delete cuts (all observed on macOS Chrome through CDP). Refused too.
 4. A route forgets the gate: browser_press_key, browser_command operation=press_key, a tab batch step, browser_repl's
    page.keyboard.press and locator.press each refuse.
 5. The refusal is not actionable: it names browser_command operation=clipboard, which asks the user.
 6. Something slips through anyway: the fixture records copy/cut/paste events (their type, never their data); none
    fires, the target field stays empty, the source keeps its text, and the system pasteboard's change count stays
    put. Checks read emptiness through :placeholder-shown and never read a field the clipboard could have filled, so
    a regression cannot copy the user's clipboard into this run's artifacts.
 7. The gate catches ordinary keys: Cmd+A, Backspace, Ctrl+V/C/X (no clipboard on macOS) and typing still work.

SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_clipboard.py
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl)
"""
import json
import subprocess
import sys

from run import FixtureServer, Run, SCENARIOS, Skip, ToolError, scenario

SCENARIOS.clear()

READS = ["cmd+v", "Command+V", "shift+insert"]
WRITES = ["cmd+c", "cmd+x", "ctrl+insert", "shift+delete"]
HARMLESS = ["ctrl+v", "ctrl+c", "ctrl+x", "cmd+shift+v"]


def change_count():
    """The general pasteboard's change count: it moves on any write and reveals nothing about the contents."""
    out = subprocess.run(["osascript", "-l", "JavaScript", "-e",
                          'ObjC.import("AppKit"); $.NSPasteboard.generalPasteboard.changeCount'],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.lstrip("-").isdigit() else None


def refused(error):
    return "operation=clipboard" in error and "no key was sent" in error


@scenario
def clipboard_keys(ctx):
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    pasteboard = change_count()
    with FixtureServer() as base:
        tab = ctx.call("browser_open", url=f"{base}/clipboard.html", observe="none")["tab"]["id"]
        ctx.cleanups.append(lambda: ctx.mcp.call("browser_close", tab_id=tab))

        def command(operation, **fields):
            return ctx.call("browser_command", tab_id=tab, operation=operation, **fields)

        def count(selector):
            return command("query", locator={"selector": selector})["count"]

        def focus(selector, select_all=False):
            command("click", locator={"selector": selector})
            if select_all:
                ctx.call("browser_press_key", tab_id=tab, keys="cmd+a", observe="none")

        def attempt(route, chord):
            try:
                if route == "browser_press_key":
                    reply = ctx.call("browser_press_key", tab_id=tab, keys=chord, observe="none")
                elif route == "browser_command":
                    reply = command("press_key", keys=chord)
                else:
                    reply = ctx.call("batch", tab_id=tab, observe="none",
                                     steps=[{"tool": "browser_press_key", "keys": chord}])
                    step = reply["steps"][0]
                    return step.get("error", "") if not step.get("ok") else None
                return None if reply is not None else ""
            except ToolError as error:
                return str(error)

        for route in ("browser_press_key", "browser_command", "batch"):
            for chord in READS + WRITES:
                focus("#target" if chord in READS else "#source", select_all=chord in WRITES)
                error = attempt(route, chord)
                ctx.check(f"{route} {chord} is refused, naming operation=clipboard", error and refused(error), error)

        ctx.check("no copy, cut or paste event fired", count("#events") == 1
                  and command("inspect", locator={"selector": "#events"})["text"] == "none")
        ctx.check("the target field stays empty", count("#target:placeholder-shown") == 1)
        ctx.check("the source keeps its text (no cut)", count("#source:placeholder-shown") == 0)

        for chord in HARMLESS:
            focus("#target")
            error = attempt("browser_press_key", chord)
            ctx.check(f"{chord} is not a clipboard key on macOS and is sent", error is None, error)
        focus("#target")
        ctx.call("browser_type", tab_id=tab, text="typed", observe="none")
        ctx.check("typing still works", count("#target:placeholder-shown") == 0)
        ctx.call("browser_press_key", tab_id=tab, keys="cmd+a", observe="none")
        ctx.call("browser_press_key", tab_id=tab, keys="Backspace", observe="none")
        ctx.check("Cmd+A and Backspace still work", count("#target:placeholder-shown") == 1)

        for code in ("await page.locator('#target').press('Meta+V')", "await page.keyboard.press('ControlOrMeta+v')",
                     "await page.locator('#source').press('Meta+C')", "await page.keyboard.press('Shift+Insert')"):
            result = ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {
                "code": f"await page.goto('{base}/clipboard.html'); await page.locator('#target').click(); {code}"}})
            text = result["content"][0]["text"]
            ctx.check(f"browser_repl {code} is refused", result.get("isError") and refused(text), text)
        result = ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {"code":
            "[await page.locator('#target:placeholder-shown').count(), await page.locator('#events').innerText()]"}})
        ctx.check("and nothing reached the REPL's page", result["content"][0]["text"] == "[ 1, 'none' ]",
                  result["content"][0]["text"])

    after = change_count()
    ctx.check("the system pasteboard was never written", pasteboard is not None and after == pasteboard,
              json.dumps({"before": pasteboard, "after": after}))


if __name__ == "__main__":
    sys.exit(0 if Run(config_overrides={"toolSurface": "full"}).run(set(sys.argv[1:])) else 1)
