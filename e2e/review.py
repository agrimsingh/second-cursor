#!/usr/bin/env python3
"""Live regression cases for review corrections; no mocks or unit tests."""

import json, sys, time, uuid, subprocess
from pathlib import Path
from run import Run, SCENARIOS, scenario, element_index, ToolError, observe

SCENARIOS.clear()


@scenario
def compact_observations(ctx):
    import re

    w = ctx.launch_scratch_textedit("compact.txt", "initial")["id"]
    full = ctx.call("get_window_state", window_id=w, full=True, structured=True)
    i = element_index(full["tree"], "AXTextArea")
    ctx.require("full read has structured elements", bool(full["elements"]))
    obvious = {"AXPress", "AXConfirm", "AXShowMenu", "AXScrollToVisible", "AXShowDefaultUI", "AXShowAlternateUI", "AXRaise"}
    listed = {a.strip() for group in re.findall(r"actions=\[([^\]]*)\]", full["tree"]) for a in group.split(",")}
    ctx.require(
        "outline omits common annotations",
        "enabled=true" not in full["tree"]
        and "editable=false" not in full["tree"]
        and not listed & obvious,
        sorted(listed & obvious),
    )
    diff = ctx.call("set_value", window_id=w, element_index=i, value="changed")["observation"]
    ctx.require(
        "diff omits full element list", diff["is_diff"] and "elements" not in diff
    )
    ctx.require(
        "diff payload substantially smaller",
        len(json.dumps(diff)) < len(json.dumps(full)) * 0.65,
    )
    try:
        ctx.call("press_key", window_id=w, keys="impossible-key")
    except ToolError as e:
        ctx.require("invalid input has no reconcile hint", "reconcile" not in str(e))
    else:
        ctx.require("invalid key refused", False)
    ctx.call("get_window_state", window_id=w, include_screenshot=True)


@scenario
def legacy_leases(ctx):
    w = ctx.launch_scratch_textedit("leases.txt", "initial")["id"]
    a = ctx.call("session", action="create")["session_id"]
    b = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.extend(
        [
            lambda: ctx.call("session", action="close", session_id=a),
            lambda: ctx.call("session", action="close", session_id=b),
        ]
    )
    for sid in ["legacy", a, b]:
        ctx.call("get_window_state", window_id=w, session_id=sid)
    ctx.call("press_key", window_id=w, keys="right")
    ctx.call("press_key", window_id=w, session_id=a, keys="right")
    ctx.check("legacy does not lock explicit clients out", True)
    try:
        ctx.call("press_key", window_id=w, session_id=b, keys="right")
    except ToolError:
        ctx.check("active explicit lease refuses competitors", True)
    else:
        ctx.require("active explicit lease refuses competitors", False)
    ctx.call("bind_target", session_id=a, target=f"window:{w}", action="release")
    ctx.call("press_key", window_id=w, session_id=b, keys="right")


@scenario
def private_browser_access(ctx):
    if "--approvals" not in sys.argv:
        from run import Skip

        raise Skip("Interactive approval tests require --approvals; no prompts opened")
    browsers = ctx.call("browser_instances")["browsers"]
    ctx.require("isolated browser connected", bool(browsers))
    bid = browsers[0]["id"]
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    tid = ctx.call(
        "browser_open",
        browser_id=bid,
        session_id=sid,
        url='data:text/html,<title>Approval fixture</title><input value="unchanged">',
    )["tab"]["id"]

    def call(**args):
        return ctx.call(
            "browser_command", browser_id=bid, session_id=sid, tab_id=tid, **args
        )

    for method in [
        "Network.getAllCookies",
        "Network.getCookies",
        "Runtime.evaluate",
        "Browser.getVersion",
        "Target.getTargets",
    ]:
        began = time.monotonic()
        try:
            call(
                operation="cdp",
                method=method,
                params={"expression": "document.title"},
                timeout_ms=400,
            )
        except ToolError as e:
            ctx.require(
                "unsafe CDP refused without a prompt: " + method,
                "allowlist" in str(e).lower() and time.monotonic() - began < 2,
            )
        else:
            ctx.require("unsafe CDP refused: " + method, False)
    call(operation="release")
    for args in [
        dict(operation="user_tabs"),
        dict(operation="claim"),
        dict(operation="history", **{"from": 0}),
        dict(operation="clipboard", action="read"),
        dict(operation="cdp", method="Page.getLayoutMetrics"),
    ]:
        began = time.monotonic()
        try:
            call(**args, timeout_ms=150, approved=True)
        except ToolError as e:
            ctx.require(
                "local approval expires: " + args["operation"],
                "approval" in str(e).lower() and time.monotonic() - began < 3,
                str(e),
            )
        else:
            ctx.require(
                "sensitive operation requires approval: " + args["operation"], False
            )
    try:
        call(
            operation="batch",
            steps=[{"operation": "history", "from": 0}],
            timeout_ms=150,
        )
    except ToolError as e:
        ctx.require("batch cannot carry a sensitive step", "call history on its own" in str(e), str(e))
    else:
        ctx.require("batch cannot carry a sensitive step", False)
    try:
        ctx.call("browser_snapshot", session_id=sid, browser_id=bid, tab_id=tid)
    except ToolError:
        ctx.check("expired claim did not take ownership", True)
    else:
        ctx.require("expired claim did not take ownership", False)
    # Released fixture tab remains open; the isolated profile is deleted by the test operator.


@scenario
def lifetime_expiration(ctx):
    if "--short-retention" not in sys.argv:
        from run import Skip

        raise Skip("Run with --short-retention to exercise real configured expiry")
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    w = ctx.launch_scratch_textedit("expiry.txt", "initial")["id"]
    ctx.call("get_window_state", session_id=sid, window_id=w)
    op = str(uuid.uuid4())
    args = dict(session_id=sid, window_id=w, keys="right", operation_id=op)
    first = ctx.call("press_key", **args)
    ctx.require(
        "retry inside retention is deduplicated", first == ctx.call("press_key", **args)
    )
    time.sleep(3.3)
    ctx.call("get_window_state", window_id=w)
    ctx.call("press_key", window_id=w, keys="right")
    ctx.check("legacy recovers expired explicit lease", True)
    try:
        ctx.call("operation_status", session_id=sid, operation_id=op)
    except ToolError as e:
        ctx.require("completed operation expires", "expired" in str(e).lower())
    else:
        ctx.require("completed operation expires", False)


@scenario
def bounded_bookkeeping(ctx):
    before = ctx.call("status")["runtime"]
    for _ in range(150):
        ctx.call("status")
    after = ctx.call("status")["runtime"]
    ctx.require(
        "closed HTTP connections are forgotten",
        after["http_connections"] <= before["http_connections"] + 2,
        after,
    )
    ctx.require(
        "implicit calls do not retain history",
        after["operations"] <= before["operations"],
        after,
    )
    tools = ctx.mcp.rpc("tools/list")["tools"]
    byname = {t["name"]: t for t in tools}
    ctx.require(
        "full appears only on meaningful browser snapshot",
        "full" not in byname["browser_click"]["inputSchema"]["properties"],
    )
    ctx.require(
        "read-only discovery omits action metadata",
        "operation_id" not in byname["list_apps"]["inputSchema"]["properties"],
    )
    ctx.require(
        "extended schema loads detailed arguments on demand",
        len(byname["browser_command"]["inputSchema"]["properties"]) < 12,
    )


@scenario
def ordered_browser_diff(ctx):
    bid = ctx.call("browser_instances")["browsers"][0]["id"]
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    from urllib.parse import quote

    html = (
        '<title>Duplicate fixture</title><button onclick="document.getElementsByClassName(&quot;repeat&quot;)[0].remove()">Remove</button>'
        + "".join('<p class="repeat">Identical item</p>' for _ in range(12))
    )
    tid = ctx.call(
        "browser_open",
        browser_id=bid,
        session_id=sid,
        url="data:text/html," + quote(html),
    )["tab"]["id"]
    ctx.call(
        "browser_command",
        browser_id=bid,
        session_id=sid,
        tab_id=tid,
        operation="click",
        arguments={"locator": {"role": "button", "name": "Remove"}},
    )
    full = ctx.call(
        "browser_snapshot", browser_id=bid, session_id=sid, tab_id=tid, full=True
    )
    ctx.call(
        "browser_command",
        browser_id=bid,
        session_id=sid,
        tab_id=tid,
        operation="click",
        arguments={"locator": {"role": "button", "name": "Remove"}},
    )
    ctx.call(
        "browser_command",
        browser_id=bid,
        session_id=sid,
        tab_id=tid,
        operation="wait",
        arguments={"locator": {"selector": ".repeat", "nth": 10}, "state": "detached"},
        timeout_ms=2000,
    )
    time.sleep(0.15)
    diff = ctx.call(
        "browser_snapshot", browser_id=bid, session_id=sid, tab_id=tid, full=False
    )
    ctx.require(
        "duplicate removal is not silently dropped",
        "Identical item" in diff["outline"] and diff["outline"] != "(no changes)",
    )
    if diff["is_diff"]:
        ctx.require(
            "diff preserves a deleted occurrence",
            any(
                line.startswith("-") and "Identical item" in line
                for line in diff["outline"].splitlines()
            ),
        )
        ctx.require(
            "browser diff omits repeated structured elements", "elements" not in diff
        )
    else:
        ctx.require(
            "full fallback preserves remaining duplicates",
            diff["outline"].count("Identical item")
            < full["outline"].count("Identical item"),
        )


@scenario
def approval_lifecycle(ctx):
    if "--approvals" not in sys.argv:
        from run import Skip

        raise Skip("Interactive approval tests require --approvals; no prompts opened")
    import threading

    bid = ctx.call("browser_instances")["browsers"][0]["id"]
    op = str(uuid.uuid4())
    out = []
    w = ctx.launch_scratch_textedit("approval-paste.txt", "initial")["id"]
    snap = ctx.call("get_window_state", window_id=w)
    i = element_index(snap["tree"], "AXTextArea")
    ctx.call("select_text", window_id=w, element_index=i, text="initial")

    def pending():
        try:
            ctx.call(
                "browser_command",
                browser_id=bid,
                operation="history",
                operation_id=op,
                timeout_ms=10000,
            )
            out.append("locally approved")
        except ToolError as e:
            out.append(str(e))

    thread = threading.Thread(target=pending)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        windows = []
        while time.monotonic() < deadline:
            windows = observe()["second_cursor_windows"]
            if windows:
                break
            time.sleep(0.05)
        ctx.require("approval window is visible", bool(windows))
        ctx.require(
            "approval activates visibly",
            observe()["frontmost"]["name"] in ["SecondCursor", "Second Cursor"],
        )
        try:
            ctx.call("get_window_state", window_id=windows[0]["id"])
        except ToolError:
            ctx.check("runtime refuses its own approval window", True)
        else:
            ctx.require("runtime refuses its own approval window", False)
        shot = ctx.run.dir / "approval-prompt.png"
        subprocess.run(
            ["screencapture", "-x", "-l", str(windows[0]["id"]), str(shot)], check=True
        )
        ctx.call(
            "paste",
            window_id=w,
            text="<b>main actor remains responsive</b>",
            format="html",
            timeout_ms=3000,
        )
        ctx.require(
            "HTML paste works while approval is pending",
            "main actor remains responsive"
            in ctx.call("inspect_element", window_id=w, element_index=i)["text"],
        )
        if out == ["locally approved"]:
            ctx.check("local Allow once path completed during test", True)
            op = str(uuid.uuid4())
            out.clear()
            thread = threading.Thread(target=pending)
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    info = ctx.call("operation_status", operation_id=op)
                    if info["state"] == "queued":
                        break
                except ToolError:
                    pass
                time.sleep(0.02)
        ctx.call("cancel_operation", operation_id=op)
        thread.join(2)
        ctx.require(
            "cancel dismisses pending approval",
            not thread.is_alive() and bool(out) and "approval" in out[0].lower(),
        )
        deadline = time.monotonic() + 2
        while observe()["second_cursor_windows"] and time.monotonic() < deadline:
            time.sleep(0.05)
        ctx.require(
            "cancelled prompt no longer visible", not observe()["second_cursor_windows"]
        )
    finally:
        ctx.call("cancel_operation", operation_id=op)
        thread.join(12)


if __name__ == "__main__":
    # Short retention lives only in the disposable test instance's config.
    short = "--short-retention" in sys.argv
    run = Run(config_overrides={"claimIdleSeconds": 2, "operationRetentionSeconds": 3} if short else None)
    run.run(sys.argv[1:])
    sys.exit(1 if any(r["status"] == "FAIL" for r in run.results) else 0)
