#!/usr/bin/env python3
"""Live E2E contract, specified before implementation. Run with signed app installed."""

import pathlib, sys, threading, http.server, functools, json, time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, Context, SCENARIOS, scenario, ToolError, element_index

SCENARIOS.clear()


def refuses(ctx, tool, **args):
    try:
        ctx.call(tool, **args)
    except ToolError:
        return True
    return False


@scenario
def sessions_native(ctx):
    a = ctx.call("session", action="create")["session_id"]
    b = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.extend(
        [
            lambda: ctx.call("session", action="close", session_id=a),
            lambda: ctx.call("session", action="close", session_id=b),
        ]
    )
    w = ctx.launch_scratch_textedit(
        "parity.txt",
        "first repeat then second repeat 🙂\n" + "long text " * 300 + " END",
    )["id"]
    sa = ctx.call("get_window_state", window_id=w, session_id=a, structured=True)
    sb = ctx.call("get_window_state", window_id=w, session_id=b)
    ctx.require(
        "first observations are independently full",
        not sa["is_diff"] and not sb["is_diff"],
    )
    area = element_index(sa["tree"], "AXTextArea")
    ctx.require(
        "structured elements expose supported actions and state",
        "actions" in next(x for x in sa["elements"] if x["index"] == area),
    )
    detail = ctx.call(
        "inspect_element",
        window_id=w,
        session_id=a,
        element_index=area,
        offset=0,
        length=10000,
    )
    ctx.require(
        "full text recovers beyond outline truncation", detail["text"].endswith(" END")
    )
    ctx.require(
        "ambiguous text refuses",
        refuses(
            ctx,
            "select_text",
            window_id=w,
            session_id=a,
            element_index=area,
            text="repeat",
        ),
    )
    ctx.call(
        "select_text",
        window_id=w,
        session_id=a,
        element_index=area,
        text="repeat",
        prefix="second ",
    )
    ctx.call(
        "type_text", window_id=w, session_id=a, element_index=area, text="replacement"
    )
    read = ctx.call(
        "inspect_element",
        window_id=w,
        session_id=a,
        element_index=area,
        offset=0,
        length=10000,
    )
    ctx.require(
        "selection replaced only intended occurrence",
        "first repeat then second replacement" in read["text"],
    )
    ctx.require(
        "other client cannot mutate claimed target",
        refuses(ctx, "press_key", window_id=w, session_id=b, keys="a"),
    )
    newer = ctx.call("get_window_state", window_id=w, session_id=a)
    ctx.require(
        "stale observation rejected",
        refuses(
            ctx,
            "press_key",
            window_id=w,
            session_id=a,
            observation_id=sa["snapshot_id"],
            keys="a",
        ),
    )
    ctx.call("paste", window_id=w, session_id=a, text="\nPasted parity", format="text")
    shot = ctx.call(
        "get_window_state",
        window_id=w,
        session_id=a,
        full=True,
        include_screenshot=True,
    )
    ctx.require(
        "paste is visible",
        "Pasted parity"
        in ctx.call(
            "inspect_element",
            window_id=w,
            session_id=a,
            element_index=area,
            length=10000,
        )["text"],
    )
    ctx.call("session", action="close", session_id=b)
    ctx.require(
        "closed session refused",
        refuses(ctx, "get_window_state", window_id=w, session_id=b),
    )


@scenario
def native_rich_editing(ctx):
    import subprocess, hashlib

    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    path = ctx.scratch / "rich.rtf"
    path.write_text(r"{\rtf1\ansi initial rich text}")
    launched = ctx.call(
        "launch_app", app="TextEdit", urls=[str(path)], new_instance=True, on_agent_display=True
    )
    ctx.kill_on_exit(launched["app"]["pid"])
    w = ctx.wait_for_window("TextEdit", pid=launched["app"]["pid"], title="rich")["id"]
    snap = ctx.call("get_window_state", window_id=w, session_id=sid)
    area = element_index(snap["tree"], "AXTextArea")
    ctx.call(
        "select_text",
        window_id=w,
        session_id=sid,
        element_index=area,
        text="initial rich text",
    )
    before = hashlib.sha256(
        subprocess.run(["pbpaste"], capture_output=True).stdout
    ).hexdigest()
    ctx.call(
        "paste",
        window_id=w,
        session_id=sid,
        text="<b>Bold parity</b> and plain",
        format="html",
    )
    after = hashlib.sha256(
        subprocess.run(["pbpaste"], capture_output=True).stdout
    ).hexdigest()
    ctx.require("native clipboard restored", before == after)
    ctx.require(
        "HTML paste inserted rendered text",
        ctx.call("inspect_element", window_id=w, session_id=sid, element_index=area)[
            "text"
        ]
        == "Bold parity and plain",
    )
    ctx.call("press_key", window_id=w, session_id=sid, keys="cmd+s")
    deadline = time.time() + 3
    while "Bold parity" not in path.read_text() and time.time() < deadline:
        time.sleep(0.1)
    rtf = path.read_text()
    ctx.require(
        "saved document retains bold formatting",
        "Bold parity" in rtf and r"\b" in rtf,
        rtf,
    )
    (ctx.run.dir / "native-rich.rtf").write_text(rtf)
    ctx.call(
        "select_text",
        window_id=w,
        session_id=sid,
        element_index=area,
        text="plain",
        selection_type="cursor_after",
    )
    ctx.call(
        "paste", window_id=w, session_id=sid, text=" **markdown source**", format="md"
    )
    ctx.require(
        "Markdown source semantics preserved",
        "**markdown source**"
        in ctx.call("inspect_element", window_id=w, session_id=sid, element_index=area)[
            "text"
        ],
    )
    ctx.call("get_window_state", window_id=w, session_id=sid, include_screenshot=True)


@scenario
def browser_parity(ctx):
    instances = ctx.call("browser_instances")["browsers"]
    ctx.require("extension browser connected", bool(instances))
    sid = ctx.call("session", action="create")["session_id"]
    bid = instances[0]["id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(pathlib.Path(__file__).parent / "fixtures/parity"),
        ),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.cleanups.append(server.shutdown)
    tab = ctx.call(
        "browser_open",
        session_id=sid,
        browser_id=bid,
        url=f"http://127.0.0.1:{server.server_port}/index.html",
    )["tab"]
    tid = tab["id"]

    def command(op, **kw):
        return ctx.call(
            "browser_command",
            session_id=sid,
            browser_id=bid,
            tab_id=tid,
            operation=op,
            **kw,
        )

    dom = command("dom_snapshot")
    ctx.require("DOM observation contains fixture", "Parity fixture" in json.dumps(dom))
    command("fill", locator={"label": "Name"}, text="Ada")
    command("check", locator={"label": "Enabled"}, checked=True)
    command("select_option", locator={"label": "Choice"}, value="b")
    command(
        "fill",
        locator={"frame": 'iframe[title="Nested form"]', "label": "Frame field"},
        text="Framed",
    )
    command("click", locator={"role": "button", "name": "Save"})
    ctx.require(
        "form result observed",
        "Saved Ada" in command("inspect", locator={"selector": "#out"})["text"],
    )
    ctx.require(
        "ambiguous locator rejected",
        refuses(
            ctx,
            "browser_command",
            session_id=sid,
            browser_id=bid,
            tab_id=tid,
            operation="click",
            locator={"role": "button"},
        ),
    )
    command(
        "select_text", locator={"selector": "#rich"}, text="repeat", prefix="second "
    )
    command("type", text="changed")
    command(
        "paste", locator={"selector": "#rich"}, text="<b>formatted</b>", format="html"
    )
    ctx.require(
        "HTML paste persists",
        "formatted" in command("inspect", locator={"selector": "#rich"})["text"],
    )
    command("click", locator={"role": "button", "name": "Confirm"})
    ctx.require("dialog observed", command("dialog")["dialog"]["type"] == "confirm")
    command("dialog", accept=True)
    command(
        "upload",
        locator={"selector": "#upload"},
        files=[str(pathlib.Path(__file__).parent / "fixtures/parity/download.txt")],
    )
    ctx.require(
        "upload visible",
        "download.txt" in command("inspect", locator={"selector": "#out"})["text"],
    )
    command("click", locator={"text": "Download fixture"})
    downloads = command("wait", event="download", timeout_ms=10000)
    ctx.require("download event captured", bool(downloads))
    command("screenshot", full_page=True)
    command("screenshot", clip={"x": 0, "y": 0, "width": 320, "height": 240})
    ctx.require(
        "console captured",
        any("parity fixture" in x["message"] for x in command("logs")["logs"]),
    )
    exported = command("export", format="html")
    ctx.require("content export usable", "Parity fixture" in exported["content"])
    command("viewport", width=800, height=600)
    command("viewport", reset=True)
    command("click", locator={"text": "Next page"})
    command("wait", url="next.html")
    command("back")
    command("forward")
    command("reload")
    ctx.require(
        "navigation verified",
        "Navigation complete" in json.dumps(command("dom_snapshot")),
    )
    command("retain", mode="handoff")
    command("retain", mode="ephemeral")


@scenario
def browser_extended(ctx):
    import shutil

    instances = ctx.call("browser_instances")["browsers"]
    ctx.require("browser present", bool(instances))
    bid = instances[0]["id"]
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(pathlib.Path(__file__).parent / "fixtures/parity"),
        ),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.cleanups.append(server.shutdown)
    tid = ctx.call(
        "browser_open",
        session_id=sid,
        browser_id=bid,
        url=f"http://127.0.0.1:{server.server_port}/index.html",
    )["tab"]["id"]

    def c(op, **kw):
        return ctx.call(
            "browser_command",
            session_id=sid,
            browser_id=bid,
            tab_id=tid,
            operation=op,
            **kw,
        )

    c(
        "fill",
        locator={
            "frame": ['iframe[title="Nested form"]', "iframe"],
            "label": "Inner field",
        },
        text="Nested",
    )
    ctx.require(
        "nested frame fill observed",
        c(
            "inspect",
            locator={
                "frame": ['iframe[title="Nested form"]', "iframe"],
                "label": "Inner field",
            },
        )["text"]
        == "Nested",
    )
    c(
        "fill",
        locator={"frame": 'iframe[title="Cross-origin form"]', "label": "Frame field"},
        text="Cross origin",
    )
    ctx.require(
        "cross-origin frame fill observed",
        c(
            "inspect",
            locator={
                "frame": 'iframe[title="Cross-origin form"]',
                "label": "Frame field",
            },
        )["text"]
        == "Cross origin",
    )
    c("perform_action", locator={"label": "Quantity"}, action="increment")
    ctx.require(
        "secondary action applied",
        c("inspect", locator={"label": "Quantity"})["text"] == "3",
    )
    c("click", locator={"label": "Name"}, button="right")
    ctx.require(
        "right click delivered",
        c("inspect", locator={"selector": "#out"})["text"] == "right clicked",
    )
    c("scroll", locator={"selector": "#scroll"}, direction="down", amount=3)
    ctx.require(
        "targeted scroll moved nested region",
        c("scroll", locator={"selector": "#scroll"}, direction="down", amount=1)[
            "scroll_top"
        ]
        > 0,
    )
    c(
        "batch",
        steps=[
            {"operation": "fill", "locator": {"label": "Name"}, "text": "Batch"},
            {"operation": "press_key", "locator": {"label": "Name"}, "keys": "End"},
            {"operation": "type", "text": " value"},
        ],
    )
    ctx.require(
        "browser batch preserves order",
        c("inspect", locator={"label": "Name"})["text"] == "Batch value",
    )
    c("perform_action", locator={"selector": "#drop"}, action="scroll_into_view")
    start = c("inspect", locator={"selector": "#drag"})["bounds"]
    end = c("inspect", locator={"selector": "#drop"})["bounds"]
    c(
        "drag",
        from_x=start["x"] + start["width"] / 2,
        from_y=start["y"] + start["height"] / 2,
        to_x=end["x"] + end["width"] / 2,
        to_y=end["y"] + end["height"] / 2,
    )
    ctx.require(
        "browser drag-and-drop delivered",
        c("inspect", locator={"selector": "#drop"})["text"] == "Dropped",
    )
    c("click", locator={"role": "button", "name": "Prompt"})
    ctx.require("prompt observed", c("dialog")["dialog"]["type"] == "prompt")
    c("dialog", accept=True, prompt_text="answer")
    ctx.require(
        "prompt response applied",
        c("inspect", locator={"selector": "#out"})["text"] == "answer",
    )
    c("click", locator={"role": "button", "name": "Alert"})
    c("dialog", accept=False)
    c("click", locator={"selector": "#upload"})
    chooser = c("wait", event="filechooser", timeout_ms=2000)
    ctx.require("file chooser event observed", "filechooser" in chooser)
    c(
        "upload",
        files=[str(pathlib.Path(__file__).parent / "fixtures/parity/download.txt")],
    )
    ctx.require(
        "chooser assignment visible",
        "download.txt" in c("inspect", locator={"selector": "#out"})["text"],
    )
    ctx.require(
        "missing upload path rejected",
        refuses(
            ctx,
            "browser_command",
            session_id=sid,
            browser_id=bid,
            tab_id=tid,
            operation="upload",
            locator={"selector": "#upload"},
            files=["/tmp/second-cursor-does-not-exist"],
        ),
    )
    c("click", locator={"text": "Download fixture"})
    download = c("wait", event="download", timeout_ms=5000)["download"]
    if download.get("filename"):
        path = pathlib.Path(download["filename"])
        deadline = time.time() + 5
        while not path.exists() and time.time() < deadline:
            time.sleep(0.1)
        ctx.require("download file exists", path.exists())
        shutil.copyfile(path, ctx.run.dir / "download.txt")
    for fmt in ["text", "html", "md", "pdf"]:
        result = c("export", format=fmt)
        if "content_base64" in result:
            import base64

            data = base64.b64decode(result["content_base64"])
            ctx.require("PDF signature", data.startswith(b"%PDF"))
            (ctx.run.dir / "export.pdf").write_bytes(data)
        else:
            ctx.require(
                "export has fixture content", "Parity fixture" in result["content"]
            )
            (ctx.run.dir / f"export.{fmt}").write_text(result["content"])
    assets = c("assets")["assets"]
    ctx.require("network assets observed", bool(assets))
    selected = [a["id"] for a in assets if a["url"].endswith("index.html")]
    if selected:
        bundle = c("assets", action="bundle", asset_ids=selected[:1])
        (ctx.run.dir / "assets.json").write_text(json.dumps(bundle, indent=2))
        ctx.require("asset bodies included", "body" in bundle["assets"][0])
    capabilities = c("capabilities")
    ctx.require(
        "history explicitly requires local approval",
        "history" in capabilities["local_approval_required"],
    )
    c("viewport", width=640, height=480)
    shot = c("screenshot")
    ctx.require(
        "viewport screenshot dimensions",
        shot["width"] == 640 and shot["height"] == 480,
        shot,
    )
    c("viewport", reset=True)
    ctx.require(
        "read-only CDP allowlist advertised",
        "Page.getLayoutMetrics" in capabilities["cdp_allowlist"],
    )
    ctx.require(
        "browser-global CDP refused",
        refuses(
            ctx,
            "browser_command",
            session_id=sid,
            browser_id=bid,
            tab_id=tid,
            operation="cdp",
            method="Target.getTargets",
        ),
    )
    snapshot = ctx.call(
        "browser_snapshot", session_id=sid, browser_id=bid, tab_id=tid, full=True, structured=True
    )
    ctx.require("browser structured state present", bool(snapshot["elements"]))
    diff = ctx.call(
        "browser_snapshot", session_id=sid, browser_id=bid, tab_id=tid, full=False
    )
    ctx.require(
        "browser diff uses own observation",
        diff["is_diff"] and diff["diff_base"] == snapshot["snapshot_id"],
    )


@scenario
def execution_safety(ctx):
    import uuid, subprocess
    from run import APP_BINARY

    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.call("session", action="close", session_id=sid))
    w = ctx.launch_scratch_textedit("operations.txt", "initial")["id"]
    snap = ctx.call("get_window_state", window_id=w, session_id=sid)
    area = element_index(snap["tree"], "AXTextArea")
    op = str(uuid.uuid4())
    kwargs = dict(
        window_id=w, session_id=sid, element_index=area, value="once", operation_id=op
    )
    first = ctx.call("set_value", **kwargs)
    second = ctx.call("set_value", **kwargs)
    ctx.require("same operation ID returns same result", first == second)
    ctx.require(
        "operation ID cannot be reused for changed payload",
        refuses(ctx, "set_value", **{**kwargs, "value": "twice"}),
    )
    errors = []

    def hold():
        try:
            ctx.call(
                "batch",
                window_id=w,
                session_id=sid,
                steps=[{"tool": "wait", "args": {"ms": 1600}}],
                observe="none",
            )
        except Exception as e:
            errors.append(str(e))

    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.2)
    expired = str(uuid.uuid4())
    ctx.require(
        "queued action expires",
        refuses(
            ctx,
            "set_value",
            window_id=w,
            session_id=sid,
            element_index=area,
            value="EXPIRED",
            timeout_ms=100,
            operation_id=expired,
        ),
    )
    thread.join()
    time.sleep(0.1)
    ctx.require(
        "expired action did not execute",
        ctx.call("inspect_element", window_id=w, session_id=sid, element_index=area)[
            "text"
        ]
        == "once",
    )
    status = ctx.call("operation_status", session_id=sid, operation_id=expired)
    ctx.require(
        "expired operation has reconcilable result",
        status["outcome_known"] and status["state"] == "failed",
        status,
    )
    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.2)
    cancelled = str(uuid.uuid4())

    def enqueue():
        try:
            ctx.call(
                "set_value",
                window_id=w,
                session_id=sid,
                element_index=area,
                value="CANCELLED",
                operation_id=cancelled,
            )
        except ToolError:
            pass

    queued = threading.Thread(target=enqueue)
    queued.start()
    time.sleep(0.2)
    ctx.call("cancel_operation", session_id=sid, operation_id=cancelled)
    thread.join()
    queued.join()
    ctx.require(
        "cancelled action did not execute",
        ctx.call("inspect_element", window_id=w, session_id=sid, element_index=area)[
            "text"
        ]
        == "once",
    )
    thread = threading.Thread(target=hold)
    thread.start()
    time.sleep(0.2)
    paused = str(uuid.uuid4())

    def enqueue_paused():
        try:
            ctx.call(
                "set_value",
                window_id=w,
                session_id=sid,
                element_index=area,
                value="PAUSED",
                operation_id=paused,
            )
        except ToolError:
            pass

    queued = threading.Thread(target=enqueue_paused)
    queued.start()
    time.sleep(0.2)
    subprocess.run([str(APP_BINARY), "--pause"], check=True, capture_output=True)
    try:
        thread.join()
        queued.join()
    finally:
        subprocess.run([str(APP_BINARY), "--resume"], check=True, capture_output=True)
    ctx.require(
        "pause checked at actual dispatch",
        ctx.call("inspect_element", window_id=w, session_id=sid, element_index=area)[
            "text"
        ]
        == "once",
    )
    other = ctx.launch_scratch_textedit("other-process.txt", "other")["id"]
    ctx.require(
        "cross-process batch refused",
        refuses(
            ctx,
            "batch",
            window_id=w,
            session_id=sid,
            steps=[{"tool": "press_key", "args": {"window_id": other, "keys": "a"}}],
        ),
    )
    ctx.call("get_window_state", window_id=w, session_id=sid, include_screenshot=True)


@scenario
def browser_ownership(ctx):
    instances = ctx.call("browser_instances")["browsers"]
    ctx.require("browser present", bool(instances))
    sid = ctx.call("session", action="create")["session_id"]
    other = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.extend(
        [
            lambda: ctx.call("session", action="close", session_id=sid),
            lambda: ctx.call("session", action="close", session_id=other),
        ]
    )
    bid = instances[0]["id"]
    tab = ctx.call("browser_open", session_id=sid, browser_id=bid, url="about:blank")[
        "tab"
    ]["id"]
    ctx.require(
        "other session cannot inspect claimed tab",
        refuses(ctx, "browser_snapshot", session_id=other, browser_id=bid, tab_id=tab),
    )
    ctx.call(
        "browser_command",
        session_id=sid,
        browser_id=bid,
        tab_id=tab,
        operation="release",
    )
    policy = ctx.call(
        "browser_command", session_id=other, browser_id=bid, operation="capabilities"
    )
    ctx.require(
        "claim declares mandatory local approval",
        "claim" in policy["local_approval_required"],
    )
    ctx.call("session", action="close", session_id=other)
    ctx.require(
        "released tab cannot be driven without approved reclaim",
        refuses(ctx, "browser_snapshot", session_id=sid, browser_id=bid, tab_id=tab),
    )
    # Released fixture tab remains in the isolated test profile; cleanup must not close user tabs.
    if len(instances) > 1:
        tabs = []
        for browser in instances:
            tab = ctx.call(
                "browser_open",
                session_id=sid,
                browser_id=browser["id"],
                url="about:blank",
            )["tab"]["id"]
            tabs.append((browser["id"], tab))
        ctx.require(
            "multiple browser instances operate concurrently",
            len(tabs) == len(instances),
        )
        ctx.call("session", action="close", session_id=sid)


if __name__ == "__main__":
    run = Run()
    run.run(sys.argv[1:])
    sys.exit(1 if any(x["status"] == "FAIL" for x in run.results) else 0)
