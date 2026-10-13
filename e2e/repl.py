#!/usr/bin/env python3
"""browser_repl: a persistent JavaScriptCore REPL whose Playwright-shaped `page` drives the session's Chrome tabs,
driven over MCP like a client, against e2e/fixtures/repl.html in the isolated test browser.

Ways it can fail, written down before the code, each with the design decision that covers it:
 1. A synchronous runaway (`while(true){}`) holds the thread that runs the script, so the call never answers and
    later calls queue behind it. Each session's JavaScript runs on its own serial dispatch queue, never on the Swift
    concurrency pool, and JavaScriptCore's execution time limit (JSContextGroupSetExecutionTimeLimit, private, via
    dlsym) checks every 50 ms of script CPU time: a script that has run 5 s without yielding, or past the call's
    deadline, is terminated. The context survives termination, so earlier variables are kept.
 2. An async runaway with no browser calls (`for(;;) await null`, promise chains) never yields to the queue. The
    microtasks drain inside the same VM entry, so the same limit stops them.
 3. A runaway through browser calls (`for(;;) try { await page.waitForTimeout(10) } catch {}`) is a series of short
    entries that never trip the limit. The call's deadline ends it: pending browser calls are rejected, and calls made
    while no call is running are rejected synchronously, which turns any leftover loop into a synchronous one that
    the limit stops. No script runs between calls, so nothing from the old loop prints into the next call.
 4. Unbounded memory (an array or typed-array loop) swaps the machine. The time-limit callback and every browser
    call compare the process footprint with its value when the call started (so memory another session freed, but
    the allocator has not returned yet, is blamed on no one); 256 MB more terminates the script and discards the
    context, and the next call starts fresh with a [system] line saying variables were lost. At most 8 contexts are
    live across sessions; a ninth discards the least recently used idle one, whose next call says so, and when all 8
    are running code the call is refused rather than queued.
 5. A promise that never settles leaves the call waiting. The call returns at its deadline (timeout_ms, at most 55 s)
    with what was printed so far, up to 1.5 s before the router's own deadline so the output still arrives.
 6. Work outlives the MCP call: a browser action already sent cannot be recalled (as with every tool), but its result
    is dropped, and the script that awaited it gets an error instead of running on.
 7. Host calls after the session ended: closing the session tears the context down, cancels the call in flight, and
    the captured execution context refuses further dispatch ("Session is unknown or closed").
 8. Re-entrancy: one JSVirtualMachine and queue per session, so sessions share no variables or pages; calls within a
    session run one at a time in arrival order; the extension still checks each tab belongs to the session.
 9. Top-level await versus persistent declarations: a cell runs inside an async function, so top-level const/let/var
    and class declarations are rewritten to global assignments and function declarations are copied to the global
    object. Redeclaring a const in a later call works, as models do after an error. The last expression statement is
    returned as the cell's value; a trailing declaration returns nothing.
10. The rewrite misreads code (regex versus division, template literals, comments, ASI): if the rewritten cell does
    not compile, it runs without the return, then untransformed, and a real syntax error is reported against the
    code as written, with its line.
11. Syntax and runtime errors come back as `Name: message (line N)`, not a stack or a crash.
12. Huge output: console output and the value are capped at 40,000 characters, cut in JavaScript before they cross
    the bridge; object formatting is bounded (depth 4, 200 items, 10,000-character strings, cycles).
13. Exceptions from the bridge: host functions never throw into JavaScript. Browser failures reject the promise with
    the extension's message, turned into Playwright-style errors (TimeoutError naming the locator, strict mode
    violations listing the matches), and the REPL keeps working.
14. Escapes: a JavaScriptCore context has no process, require, filesystem, network, timers or module loader unless
    the host adds them. Constructor chains, Function and eval reach only that global. WebAssembly and
    FinalizationRegistry are removed (their callbacks would run outside a call). The host bridge is never a global,
    and even if reached, the Swift side allowlists the browser methods and operations (no cdp, claim, user_tabs,
    history, clipboard, upload, logs, export, assets) and pins the session, approvals and deadline.
15. Name and text matching differs from Playwright: case-insensitive substring unless exact, exact is case-sensitive
    and whole-string, whitespace is normalized, RegExp works; locator.filter({ hasText, hasNotText, has, visible }),
    chained locators and nth/first/last apply where they appear in the chain.
16. No auto-wait: actions wait up to 5 s (page.setDefaultTimeout, or { timeout }) for one attached, visible, enabled
    element that receives the click; strict mode violations fail at once; count() and isVisible() never wait.
    A click whose target is covered (an Undo toast over "Label: Spam") must fail naming what covers it, never land on
    the cover and report success.
22. Unsupported options or extra arguments are silently ignored (page.locator's { hasText } once was, and a loop
    relabeled one row 60 times): each one throws, naming it.
23. A script outlives the client: a call answers within 55 s by default and at most (under cursor-agent's 60 s tool
    timeout), and a client that closes its connection or sends notifications/cancelled ends the call at once.
24. page.url() goes stale after a click that navigates, and the next action runs on the old page: clicks and key
    presses wait for a load they start, and every browser call reports the tab's URL.
17. Dialogs freeze the page: a dialog an action opens is dismissed with a [system] line unless page.on('dialog')
    handles it.
18. The action log misses REPL work: each call is logged with its code's first 200 characters, length and SHA-256,
    and the whole code is stored by digest beside the log. Each input or navigation the script sends is its own line,
    named as the discrete tool would be (browser_click, browser_type, browser_navigate, ...), with the tab, locator,
    session_id and operation_id; typed text appears only as its length.
19. The page closes under the script: its calls fail with "Target page has been closed"; context.newPage() works.
20. Turn end loses work: agent tabs stay open at turn end, so the REPL does too; it ends with its session.
21. toolSurface "repl" advertises only browser_repl, and every tool stays callable; "discrete" leaves it out.

Usage:  SC_E2E_APP=build/SecondCursor.app python3 e2e/repl.py [scenario ...]
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, screenshots/)
"""
import base64
import http.client
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import run as harness
from run import FixtureServer, Run, SCENARIOS, Skip, TestInstance, scenario

SCENARIOS.clear()

# What a fresh JavaScriptCore context defines, plus the REPL's own globals.
BUILTINS = {
    "undefined", "NaN", "Infinity", "isNaN", "isFinite", "escape", "unescape", "decodeURI", "decodeURIComponent",
    "encodeURI", "encodeURIComponent", "eval", "globalThis", "parseInt", "parseFloat", "ArrayBuffer", "EvalError",
    "RangeError", "ReferenceError", "SyntaxError", "TypeError", "URIError", "AggregateError", "Proxy", "Reflect", "JSON",
    "Math", "Atomics", "console", "Int8Array", "Int16Array", "Int32Array", "Uint8Array", "Uint8ClampedArray",
    "Uint16Array", "Uint32Array", "Float16Array", "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
    "DataView", "Date", "Error", "Boolean", "Map", "Number", "Set", "WeakMap", "WeakSet", "Object", "Function", "Array",
    "RegExp", "Iterator", "String", "Promise", "BigInt", "Symbol", "WeakRef", "Intl", "page", "context",
}


def repl(ctx, code, **args):
    """(ok, text, images, seconds) of one browser_repl call; a failed call is returned, not raised."""
    started = time.time()
    result = ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {"code": code, **args}})
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    images = [c for c in result.get("content", []) if c.get("type") == "image"]
    for image in images:
        ctx.save_image(f"repl-{len(ctx.screenshots)}", image)
    return not result.get("isError"), texts[0] if texts else "", images, time.time() - started


def value(ctx, code, **args):
    ok, text, _, _ = repl(ctx, code, **args)
    return text if ok else f"ERROR {text}"


def in_thread(fn):
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=fn()))
    thread.start()
    return thread, box


def repl_lines(start):
    lines = harness.ACTION_LOG.read_text().splitlines()[start:] if harness.ACTION_LOG.exists() else []
    return [json.loads(line) for line in lines if '"browser_repl"' in line]


def via_repl_lines(start):
    """Lines for the input and navigation REPL scripts sent, with their detail decoded."""
    lines = harness.ACTION_LOG.read_text().splitlines()[start:] if harness.ACTION_LOG.exists() else []
    entries = [json.loads(line) for line in lines]
    for entry in entries:
        try:
            entry["detail"] = json.loads(entry.get("detail") or "{}")
        except ValueError:
            entry["detail"] = {}
    return [entry for entry in entries if isinstance(entry["detail"], dict) and entry["detail"].get("via") == "browser_repl"]


def log_length():
    return len(harness.ACTION_LOG.read_text().splitlines()) if harness.ACTION_LOG.exists() else 0


@scenario
def repl_cells(ctx):
    """Failure modes 9-12 and 18: persistence, top-level await, the rewrite, readable errors, the output cap."""
    tool = next(t for t in ctx.mcp.rpc("tools/list")["tools"] if t["name"] == "browser_repl")
    ctx.check("browser_repl is listed with a description naming page.snapshot() and aria-ref",
              "page.snapshot()" in tool["description"] and "aria-ref=" in tool["description"], tool["description"])
    properties = tool["inputSchema"]["properties"]
    ctx.check("its schema takes code and timeout_ms, and no observation_id",
              {"code", "timeout_ms"} <= set(properties) and "observation_id" not in properties, sorted(properties))
    log_start = log_length()

    ctx.check("top-level const and function declarations persist",
              value(ctx, "const a = 1; function twice(n) { return n * 2 }") == "undefined"
              and value(ctx, "a + twice(2)") == "5")
    ctx.check("a const can be declared again in a later call", value(ctx, "const a = 10; a") == "10")
    ctx.check("top-level await works", value(ctx, "await Promise.resolve(3) * 2") == "6")
    value(ctx, "const { x, y = 2 } = { x: 1 }\nclass Box { get v() { return 7 } }")
    ctx.check("destructured names and classes persist", value(ctx, "x + y + new Box().v") == "10")
    ctx.check("names declared in loops stay local", value(ctx, "for (const i of [1]) {}\ntypeof i") == "undefined")
    ctx.check("printed output comes before the value", value(ctx, "console.log('hi', { n: 1 }); 42") == "hi { n: 1 }\n42")
    ctx.check("values are formatted like Node's inspect",
              value(ctx, "({ list: [1, 'two'], nested: { deep: { deeper: { deepest: { gone: { x: 1 } } } } } })")
              == "{\n  list: [ 1, 'two' ],\n  nested: { deep: { deeper: { deepest: { gone: [Object] } } } }\n}")
    tricky = value(ctx, "const re = /[{}]\\/(\\/)/g // const hidden = 1\n"
                        "const t = `a${ '}' }b${`c${ { k: 1 }.k }`}`\n"
                        "const url = 'http://x // y'\n"
                        "let n = 10\nn /\n2 + [re.source, t, url, typeof hidden].join('|')")
    ctx.check("regex literals, nested templates, comments and division survive the rewrite",
              tricky == "5[{}]\\/(\\/)|a}bc1|http://x // y|undefined", tricky)
    ctx.check("a block's closing brace ends its statement, as in JavaScript",
              value(ctx, "if (true) {\n  console.log('in')\n}\n(1 + 1)") == "in\n2")
    ctx.check("\"use strict\" in a cell does not break persistence", value(ctx, "'use strict'; const s = 5; s") == "5")

    ok, text, _, _ = repl(ctx, "const broken = ;")
    ctx.check("a syntax error is a failed call with a readable message and line",
              not ok and text.startswith("SyntaxError:") and "line 1" in text, text)
    ok, text, _, _ = repl(ctx, "const fine = 1\nnope.nope")
    ctx.check("a runtime error names its line", not ok and text.startswith("ReferenceError:") and "(line 2)" in text,
              text)
    ok, text, _, _ = repl(ctx, "throw { code: 7 }")
    ctx.check("a thrown non-error is shown", not ok and "Uncaught { code: 7 }" in text, text)
    text = value(ctx, "Promise.reject(new Error('floating')); 'after'")
    ctx.check("an unhandled rejection is printed as a [system] line",
              "[system] Unhandled promise rejection: Error: floating" in text and text.endswith("after"), text)

    ok, text, _, _ = repl(ctx, "console.log('x'.repeat(100000)); 'done'")
    ctx.check("output is capped at 40,000 characters with a note",
              ok and len(text) < 40500 and "more characters (output is capped at 40000 characters)" in text, len(text))
    text = value(ctx, "Array.from({ length: 5000 }, (_, i) => i)")
    ctx.check("large values are cut to 200 items", "... 4800 more items" in text and len(text) < 3000, text[-80:])
    text = value(ctx, "const loop = { name: 'l' }; loop.self = loop; loop")
    ctx.check("cycles are shown, not followed", text == "{ name: 'l', self: [Circular] }", text)

    refusals = {
        "page.locator('li', { hasText: 'x', exact: true })": "TypeError: page.locator does not support option exact",
        "await page.getByRole('button').click({ delay: 10 })": "TypeError: click does not support option delay",
        "await page.waitForTimeout(1, 2)": "TypeError: page.waitForTimeout takes at most 1 argument, not 2",
        "await page.getByRole('button').fill(42)": "TypeError: fill's value must be a string, not 42",
        "await page.goto('http://127.0.0.1:9/', { referer: 'x' })": "TypeError: goto does not support option referer",
        "await page.locator('body').innerHTML()": "Error: locator.innerHTML is not available in browser_repl",
    }
    for code, want in refusals.items():
        ok, text, _, _ = repl(ctx, code)
        ctx.check(f"{code} is refused: {want}", not ok and text.startswith(want), text)
    ctx.check("page.locator's hasText option is a filter",
              value(ctx, "String(page.locator('li', { hasText: 'x' }))") == "locator('li').filter({ hasText: 'x' })")

    lines = repl_lines(log_start)
    ctx.check("every call is in the action log with its code",
              len(lines) >= 25 and any("twice" in (line.get("detail") or "") for line in lines), len(lines))


@scenario
def repl_limits(ctx):
    """Failure modes 1-5 and 8: runaways, memory, promises that never settle, calls within one session."""
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    ctx.check("a variable is set", value(ctx, "const keep = 'kept'; keep", session_id=sid) == "kept")

    thread, box = in_thread(lambda: repl(ctx, "while (true) {}", session_id=sid))
    time.sleep(1)
    started = time.time()
    status = ctx.call("status")
    status_s = time.time() - started
    other = value(ctx, "'other session'")
    thread.join()
    ok, text, _, seconds = box["result"]
    ctx.check("while(true){} is stopped after about 5 s with a readable error",
              not ok and "ran for 5 s without yielding" in text and seconds < 8, (seconds, text))
    ctx.check("the server answered status while it ran", status_s < 2 and "paused" in status, round(status_s, 2))
    ctx.check("another session's REPL answered while it ran", other == "other session", other)
    ctx.check("the session's variables survive the stop", value(ctx, "keep", session_id=sid) == "kept")

    ok, text, _, seconds = repl(ctx, "for (;;) await null", session_id=sid)
    ctx.check("an await loop with no browser calls is stopped too",
              not ok and "without yielding" in text and seconds < 8, (seconds, text))
    ok, text, _, seconds = repl(ctx, "while (true) {}", session_id=sid, timeout_ms=2000)
    ctx.check("synchronous code stops at a shorter call deadline",
              not ok and "call's time limit" in text and seconds < 3, (seconds, text))

    ok, text, _, seconds = repl(ctx, "let spins = 0\nfor (;;) { try { await page.waitForTimeout(10) } catch {} spins++ }",
                                session_id=sid, timeout_ms=3000)
    ctx.check("a loop of host calls ends at the call deadline", not ok and "TimeoutError" in text and seconds < 4,
              (seconds, text))
    before = value(ctx, "spins", session_id=sid)
    time.sleep(1)
    after = value(ctx, "console.log('next'); spins", session_id=sid)
    ctx.check("nothing from that loop ran or printed after its call ended",
              after == f"next\n{before}" and before.isdigit(), (before, after))

    ok, text, _, seconds = repl(ctx, "console.log('before'); await new Promise(() => {})", session_id=sid,
                                timeout_ms=2000)
    ctx.check("a promise that never settles returns at the deadline with the output so far",
              not ok and text.startswith("before") and "TimeoutError" in text and seconds < 2.5, (seconds, text))

    first, first_box = in_thread(lambda: repl(ctx, "await page.waitForTimeout(1000); 'first'", session_id=sid))
    time.sleep(0.2)
    ok, text, _, seconds = repl(ctx, "'second'", session_id=sid)
    first.join()
    ctx.check("calls in one session run one at a time, in order",
              first_box["result"][1] == "first" and text == "second" and seconds > 0.6, (seconds, text))

    ok, text, _, seconds = repl(ctx, "const hog = []\nfor (;;) hog.push(new Float64Array(1e6).fill(1))", session_id=sid)
    ctx.check("unbounded memory is stopped", not ok and "memory grew by more than 256 MB" in text and seconds < 20,
              (seconds, text))
    text = value(ctx, "typeof keep", session_id=sid)
    ctx.check("and the next call starts from a fresh context, saying so",
              "[system] The REPL was reset" in text and text.endswith("undefined"), text)


@scenario
def repl_escapes(ctx):
    """Failure mode 14: nothing in the context reaches the process, the filesystem, the network or the bridge."""
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    probe = value(ctx, session_id=sid, code="""
const probe = {}
for (const name of ["process", "require", "module", "fetch", "XMLHttpRequest", "WebSocket", "setTimeout",
  "setInterval", "queueMicrotask", "WebAssembly", "FinalizationRegistry", "Deno", "Bun", "host", "$vm", "print",
  "load", "readFile", "run", "quit", "__repl_global__"]) probe[name] = typeof globalThis[name]
probe.constructorChain = page.constructor.constructor('return typeof process')()
probe.asyncConstructor = await page.goto.constructor('return typeof require')()
probe.functionThis = Function('return this')() === globalThis
probe.evalProcess = eval('typeof process')
probe.importFs = await import('fs').then(() => 'loaded', (e) => 'refused')
probe.pageKeys = Object.getOwnPropertyNames(page).concat(Object.getOwnPropertyNames(Object.getPrototypeOf(page)))
  .filter((k) => k.startsWith('_'))
probe.globals = Object.getOwnPropertyNames(globalThis)
JSON.stringify(probe)
""")
    try:
        found = json.loads(probe)
    except json.JSONDecodeError:
        return ctx.check("the probe ran", False, probe)
    reachable = {k: v for k, v in found.items() if k not in ("globals", "pageKeys") and v not in ("undefined", False, "refused")}
    ctx.check("no host object, timer, module loader or network API is reachable, by name, constructor chain, "
              "Function or eval", reachable == {"functionThis": True}, reachable)
    extra = sorted(set(found["globals"]) - BUILTINS - {"probe"})
    ctx.check("the global object holds only JavaScript builtins, page, context and console", not extra, extra)
    ctx.check("page has no underscore internals", found["pageKeys"] == [], found["pageKeys"])
    ok, text, _, _ = repl(ctx, "await page.evaluate(() => document.cookie)", session_id=sid)
    ctx.check("page.evaluate is refused with a way forward",
              not ok and "not available in browser_repl" in text and "locators" in text, text)


def fixture_page(ctx, base):
    ok, text, _, _ = repl(ctx, f"await page.goto('{base}/repl.html'); page.url()")
    ctx.require("page.goto opened the fixture", ok and text.endswith("/repl.html"), text)


@scenario
def repl_playwright(ctx):
    """Failure modes 15-17, 19 and 20 against e2e/fixtures/repl.html."""
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    log_start = log_length()
    with FixtureServer() as base:
        fixture_page(ctx, base)
        text = value(ctx, "await page.getByRole('button', { name: 'Publish' }).click()\n"
                          "await page.locator('#status').textContent()")
        ctx.check("click waits for a disabled button to become enabled", text == "Clicked: Publish", text)
        ctx.check("page.title() reads the tab", value(ctx, "await page.title()") == "REPL fixture")

        snap = value(ctx, "const snap = await page.snapshot(); snap")
        ctx.check("page.snapshot() returns the outline with [e123] refs",
                  re.search(r'\[e\d+\] button "Save draft"', snap) and 'heading "Orders"' in snap, snap[:400])
        text = value(ctx, "const ref = /\\[(e\\d+)\\] button \"Save draft\"/.exec(snap)[1]\n"
                          "const byRef = page.locator('aria-ref=' + ref)\n"
                          "await byRef.click();\n"
                          "[await byRef.textContent(), await page.locator('#status').innerText(), await byRef.count()]")
        ctx.check("an aria-ref locator clicks and reads its element", text == "[ 'Save draft', 'Clicked: Save draft', 1 ]",
                  text)

        ok, text, _, _ = repl(ctx, "await page.getByRole('button', { name: 'save' }).click()")
        ctx.check("an ambiguous locator fails at once as a strict mode violation listing the matches",
                  not ok and "strict mode violation: getByRole('button', { name: 'save' }) resolved to 2 elements" in text
                  and '"Save draft"' in text, text)
        cases = {
            "await page.getByRole('button', { name: 'Save', exact: true }).click(); await page.locator('#status').textContent()":
                "Clicked: Save",
            "await page.getByRole('button', { name: 'DRAFT' }).click(); await page.locator('#status').textContent()":
                "Clicked: Save draft",
            "await page.getByRole('button', { name: /^save$/i }).count()": "1",
            "await page.getByText('total: 42 items').count()": "1",
            "await page.getByText('Total: 42 items', { exact: true }).count()": "1",
            "await page.getByText('total: 42 items', { exact: true }).count()": "0",
            "await page.getByRole('heading', { level: 1 }).textContent()": "Orders",
            "await page.getByRole('row').filter({ hasText: 'bob' }).getByRole('button', { name: 'Edit' }).click();"
            " await page.locator('#status').textContent()": "Clicked: Edit (Bob Jones)",
            "await page.locator('tr').filter({ hasText: 'Pending' }).count()": "2",
            "await page.locator('tr').filter({ hasNotText: 'Pending' }).locator('td').first().textContent()": "Bob Jones",
            "await page.locator('tr').filter({ has: page.getByText('Carol') }).getByRole('button').click();"
            " await page.locator('#status').textContent()": "Clicked: Edit (Carol White)",
            "await page.locator('tr').nth(1).locator('td').first().textContent()": "Bob Jones",
            "await page.locator('tr').last().locator('td').nth(1).innerText()": "Pending",
            "await page.locator('tr').filter({ hasText: 'Pending' }).first().getByRole('cell').first().textContent()":
                "Alice Smith",
            "(await page.getByRole('cell').allInnerTexts()).length": "9",
            "await page.getByRole('region', { name: 'Toolbar' }).getByRole('button').count()": "7",
            "await page.locator('section >> text=Delete all').count()": "1",
            "await page.getByTestId('badge').textContent()": "Beta",
            "await page.locator('tr', { hasText: 'carol' }).count()": "1",
            "await page.locator('tr', { hasNotText: 'Pending' }).locator('td').first().textContent()": "Bob Jones",
        }
        for code, want in cases.items():
            got = value(ctx, code)
            ctx.check(f"{code[:110]} → {want}", got == want, got)

        text = value(ctx, "await page.getByRole('button', { name: 'Load details' }).click()\n"
                          "const early = await page.getByText('Details loaded').isVisible()\n"
                          "await page.getByText('Details loaded').waitFor();\n"
                          "[early, await page.getByText('Details loaded').isVisible()]")
        ctx.check("isVisible() answers at once and waitFor() waits for visibility", text == "[ false, true ]", text)
        text = value(ctx, "await page.getByRole('button', { name: 'Add row' }).click()\n"
                          "await page.getByRole('button', { name: 'Late button' }).click()\n"
                          "await page.locator('#status').textContent()")
        ctx.check("click waits for an element that is not attached yet", text == "Clicked: Late button", text)
        text = value(ctx, "await page.getByRole('button', { name: 'Cover the page' }).click()\n"
                          "await page.getByRole('button', { name: 'Save', exact: true }).click()\n"
                          "await page.locator('#status').textContent()")
        ctx.check("click waits while another element covers the target", text == "Clicked: Save", text)
        ok, text, _, seconds = repl(ctx, "await page.getByRole('button', { name: 'Nope' }).click({ timeout: 500 })")
        ctx.check("a locator that never matches times out with Playwright's message",
                  not ok and text.startswith("TimeoutError: locator.click: Timeout 500ms exceeded waiting for "
                                             "getByRole('button', { name: 'Nope' }) (no element matches)")
                  and seconds < 3, (seconds, text))

        text = value(ctx, "await page.getByLabel('Email').fill('a@b.c')\n"
                          "await page.getByLabel('Email').pressSequentially('d')\n"
                          "await page.getByLabel('Email').press('Backspace')\n"
                          "await page.getByLabel('Subscribe').check()\n"
                          "await page.getByLabel('Size').selectOption('Large');\n"
                          "[await page.getByLabel('Email').inputValue(), await page.getByLabel('Subscribe').isChecked(),"
                          " await page.getByLabel('Size').inputValue()]")
        ctx.check("fill, pressSequentially, press, check and selectOption by label", text == "[ 'a@b.c', true, 'l' ]",
                  text)

        ok, text, _, _ = repl(ctx, "await page.getByRole('button', { name: 'Archive' }).click()\n"
                                   "await page.getByRole('button', { name: 'Label: Spam' }).click({ timeout: 1000 })")
        ctx.check("a click on a target the Undo toast covers fails, naming the toast",
                  not ok and text.startswith("TimeoutError: locator.click: Timeout 1000ms exceeded")
                  and 'is covered by status "Conversation archived. Undo"' in text,
                  text)
        text = value(ctx, "await page.locator('#status').textContent()")
        ctx.check("and the click did not land on the toast", text == "Archived", text)

        text = value(ctx, "await page.getByRole('button', { name: 'Delete all' }).click()\n"
                          "await page.locator('#status').textContent()")
        ctx.check("a dialog without a handler is dismissed with a [system] line",
                  "[system] Dismissed a confirm dialog saying 'Delete every order?'" in text
                  and text.endswith("Cancelled delete"), text)
        text = value(ctx, "page.once('dialog', (d) => d.accept())\n"
                          "await page.getByRole('button', { name: 'Delete all' }).click()\n"
                          "await page.locator('#status').textContent()")
        ctx.check("page.on('dialog') answers it", text == "Confirmed delete", text)

        ok, text, images, _ = repl(ctx, "await page.screenshot()")
        ctx.check("a screenshot returned as the value comes back as an image",
                  ok and len(images) == 1 and base64.b64decode(images[0]["data"])[:4] == b"\x89PNG", text)

        request = urllib.request.Request(ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=b"{}", method="POST",
                                         headers={"Authorization": f"Bearer {ctx.mcp.token}",
                                                  "Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=15).read()
        text = value(ctx, "[page.url().endsWith('/repl.html'), typeof snap, await page.title()]")
        ctx.check("the page and variables outlive the turn", text == "[ true, 'string', 'REPL fixture' ]", text)

        text = value(ctx, "await page.getByRole('link', { name: 'Slow next page' }).click();\n"
                          "[page.url().endsWith('/next.html?delay_ms=800'), await page.locator('#marker').count()]")
        ctx.check("a click that navigates waits for the new page, and page.url() follows it", text == "[ true, 1 ]", text)
        text = value(ctx, "await page.goBack(); page.url().endsWith('/repl.html')")
        ctx.check("page.url() follows goBack()", text == "true", text)

        text = value(ctx, f"const second = await context.newPage()\nawait second.goto('{base}/form.html')\n"
                          "const opened = context.pages().length\nawait second.close()\n"
                          "const after = await second.title().catch((e) => e.message);\n[opened, after, context.pages().length]")
        ctx.check("context.newPage() opens a page, and a closed page says so",
                  text == "[ 2, 'Target page has been closed', 1 ]", text)

    navigations = [line for line in via_repl_lines(log_start) if line["tool"] in ("browser_open", "browser_navigate")
                   and line["detail"].get("url", "").endswith("/repl.html")]
    ctx.check("navigations are in the action log with their URL", navigations, via_repl_lines(log_start)[:3])


@scenario
def repl_sessions(ctx):
    """Failure modes 7 and 8: sessions share nothing, and closing one ends its REPL and its call in flight."""
    a = ctx.call("session", action="create")["session_id"]
    b = ctx.call("session", action="create")["session_id"]
    c = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: [ctx.mcp.call("session", action="close", session_id=s) for s in (b, c)])
    value(ctx, "const who = 'A'", session_id=a)
    value(ctx, "const who = 'B'", session_id=b)
    ctx.check("each session keeps its own variables",
              value(ctx, "who", session_id=a) == "A" and value(ctx, "who", session_id=b) == "B")
    ctx.check("the legacy session sees neither", value(ctx, "typeof who") == "undefined")
    ctx.call("session", action="close", session_id=a)
    ok, text, _, _ = repl(ctx, "who", session_id=a)
    ctx.check("a closed session's REPL refuses calls", not ok and "unknown or closed" in text, text)

    thread, box = in_thread(lambda: repl(ctx, "await page.waitForTimeout(8000); 'finished'", session_id=c))
    time.sleep(0.5)
    ctx.call("session", action="close", session_id=c)
    thread.join()
    ok, text, _, seconds = box["result"]
    ctx.check("closing a session ends its call in flight", not ok and "session was closed" in text and seconds < 3,
              (seconds, text))

    thread, box = in_thread(lambda: repl(ctx, "while (true) {}", session_id=b, timeout_ms=10000))
    time.sleep(0.5)
    started = time.time()
    ctx.call("session", action="close", session_id=b)
    thread.join()
    ok, text, _, _ = box["result"]
    ctx.check("closing a session stops its runaway script within a moment",
              not ok and "session was closed" in text and time.time() - started < 2, (time.time() - started, text))


def raw_request(ctx, payload, client=None):
    """An open HTTP connection carrying one MCP request, so the test can hang up on it like a client that gives up.
    `client` is sent as the Mcp-Session-Id the server issued."""
    url = urllib.parse.urlsplit(ctx.mcp.url)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=90)
    connection.request("POST", url.path, body=json.dumps(payload), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": harness.PROTOCOL_VERSION, "Authorization": f"Bearer {ctx.mcp.token}",
        **({"Mcp-Session-Id": client} if client else {})})
    return connection


def answer(connection):
    response = connection.getresponse()
    return response.status, response.getheader("Mcp-Session-Id"), harness.parse_body(
        response.getheader("Content-Type", ""), response.read().decode())


def repl_call(code, session, request_id, **args):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "browser_repl", "arguments": {"code": code, "session_id": session, **args}}}


@scenario
def repl_client_gives_up(ctx):
    """Failure mode 23: no script runs after the client stopped waiting."""
    tool = next(t for t in ctx.mcp.rpc("tools/list")["tools"] if t["name"] == "browser_repl")
    ctx.check("timeout_ms is documented as at most and by default 55000",
              "55000" in tool["inputSchema"]["properties"]["timeout_ms"]["description"])
    sessions = [ctx.call("session", action="create")["session_id"] for _ in range(4)]
    ctx.cleanups.append(lambda: [ctx.mcp.call("session", action="close", session_id=s) for s in sessions])
    hung, asked = sessions[0], sessions[1]
    default, default_box = in_thread(lambda: repl(ctx, "await new Promise(() => {})", session_id=hung))
    capped, capped_box = in_thread(lambda: repl(ctx, "await new Promise(() => {})", session_id=asked, timeout_ms=60000))

    loop = "globalThis.ticks = 0\nfor (;;) { try { await page.waitForTimeout(50) } catch {} ticks++ }"
    left = sessions[2]
    connection = raw_request(ctx, repl_call(loop, left, "hang-up-1"))
    time.sleep(1.5)
    connection.close()
    time.sleep(0.2)
    ok, text, _, seconds = repl(ctx, "const seen = ticks; await page.waitForTimeout(600); [seen > 0, seen === ticks]",
                                session_id=left)
    ctx.check("a client that hangs up ends its call: the next call runs at once and the loop has stopped",
              ok and text == "[ true, true ]" and seconds < 2, (seconds, text))

    _, issued, _ = answer(raw_request(ctx, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": harness.PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}))
    ctx.check("initialize issues an Mcp-Session-Id, which scopes the client's cancellations", bool(issued), issued)
    cancelled = sessions[3]
    box = {}
    started = time.time()
    thread = threading.Thread(target=lambda: box.update(result=answer(raw_request(
        ctx, repl_call("while (true) {}", cancelled, "cancel-me-1", timeout_ms=30000), client=issued))))
    thread.start()
    time.sleep(1)
    notice = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "cancel-me-1", "reason": "e2e"}}
    answer(raw_request(ctx, notice, client="another-client"))
    answer(raw_request(ctx, notice))
    time.sleep(1)
    ctx.check("a cancellation from another client, or from none, with the same request id leaves the call running",
              thread.is_alive())
    status, _, _ = answer(raw_request(ctx, notice, client=issued))
    thread.join()
    result = (box["result"][2] or {}).get("result", {})
    text = (result.get("content") or [{}])[0].get("text", "")
    ctx.check("its own client's notifications/cancelled stops the running call, even a synchronous loop",
              status < 300 and result.get("isError") and text.startswith("Cancelled: the client stopped waiting")
              and time.time() - started < 4, (status, round(time.time() - started, 2), text))
    ctx.check("and the session answers at once afterwards", value(ctx, "'after'", session_id=cancelled) == "after")

    for label, (thread, result) in {"by default": (default, default_box),
                                    "even when timeout_ms asks for 60000": (capped, capped_box)}.items():
        thread.join()
        ok, text, _, seconds = result["result"]
        ctx.check(f"a call that never settles answers within 55 s {label}", not ok and "TimeoutError" in text
                  and 52 < seconds < 55, (round(seconds, 2), text[:120]))


@scenario
def repl_audit(ctx):
    """Failure mode 18: a script's clicks, typing and key presses are each in the action log, without typed text, and
    the call's whole code is stored beside the log."""
    if not ctx.call("browser_status").get("connected"):
        raise Skip("Chrome extension not connected")
    sid = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=sid))
    secret = "audit-typed@example.com"
    operation_id = f"repl-audit-{int(time.time())}"
    log_start = log_length()
    with FixtureServer() as base:
        code = ("// " + "padding past the 200-character preview " * 6 + "\n"
                f"await page.goto('{base}/repl.html')\n"
                f"await page.getByLabel('Email').fill('{secret}')\n"
                "await page.getByRole('button', { name: 'Save', exact: true }).click()\n"
                "await page.keyboard.press('Tab')\n"
                "await page.locator('#status').textContent()")
        ok, text, _, _ = repl(ctx, code, session_id=sid, operation_id=operation_id)
        ctx.require("the script ran", ok and text == "Clicked: Save", text)
    lines = via_repl_lines(log_start)
    by_tool = {}
    for line in lines:
        by_tool.setdefault(line["tool"], []).append(line)
    ctx.check("the navigation, fill, click and key press are separate lines named as the discrete tools",
              all(by_tool.get(tool) for tool in ("browser_open", "browser_type", "browser_click", "browser_press_key")),
              [(line["tool"], line["detail"].get("operation")) for line in lines])
    fill = (by_tool.get("browser_type") or [{}])[0]
    ctx.check("the fill line has the tab, locator and text length, not the text",
              (fill.get("target") or "").startswith("tab_id=") and fill["detail"].get("operation") == "fill"
              and fill["detail"].get("text_length") == len(secret) and "locator" in fill["detail"]
              and all(secret not in json.dumps(line) for line in lines), fill)
    click = (by_tool.get("browser_click") or [{}])[0]
    ctx.check("the click line names its locator", "Save" in json.dumps(click.get("detail", {}).get("locator")), click)
    press = (by_tool.get("browser_press_key") or [{}])[0]
    ctx.check("the key press line names its keys", press.get("detail", {}).get("keys") == "Tab", press)
    ctx.check("every line carries the session and operation ids",
              lines and all(line["detail"].get("session_id") == sid and line["detail"].get("operation_id") == operation_id
                            for line in lines), [line["detail"] for line in lines][:2])
    ctx.check("lines stay bounded", all(len(json.dumps(line)) < 2500 for line in lines),
              max((len(json.dumps(line)) for line in lines), default=0))

    calls = [json.loads(line["detail"]) for line in repl_lines(log_start) if line.get("detail", "").startswith("{")]
    call = next((c for c in calls if c.get("operation_id") == operation_id), {})
    stored = harness.ACTION_LOG.parent / call.get("code_file", "missing")
    ctx.check("the call's line has the code's preview, length and SHA-256",
              call.get("code_chars") == len(code) and len(call.get("code_sha256", "")) == 64
              and len(call.get("code", "")) <= 201, {k: v for k, v in call.items() if k != "code"})
    ctx.check("the whole code is stored by its digest, readable only by the user",
              stored.is_file() and stored.read_text() == code and (stored.stat().st_mode & 0o077) == 0, str(stored))


@scenario
def repl_context_cap(ctx):
    """Failure mode 4: at most 8 live contexts; a ninth session discards the least recently used idle one."""
    sids = [ctx.call("session", action="create")["session_id"] for _ in range(9)]
    for sid in sids:
        ctx.cleanups.append(lambda sid=sid: ctx.mcp.call("session", action="close", session_id=sid))
    for index, sid in enumerate(sids):
        value(ctx, f"const mine = {index}", session_id=sid)
    second = value(ctx, "mine", session_id=sids[1])
    ctx.check("a recent session keeps its variables", second == "1", second)
    first = value(ctx, "typeof mine", session_id=sids[0])
    ctx.check("the least recently used session was reset, and its next call says so",
              "[system] The REPL was reset because more than 8 browser_repl sessions were open" in first
              and first.endswith("undefined"), first)


@scenario
def repl_surface(ctx):
    """Failure mode 21: toolSurface "repl" lists only browser_repl; every tool stays callable. The default surface
    ("full" too) lists browser_repl beside the discrete browser tools, "discrete" leaves it out, and it answers on
    every surface."""
    with TestInstance({"toolSurface": "repl"}, browser=True) as inst:
        init = inst.mcp.initialize()
        names = [t["name"] for t in inst.mcp.rpc("tools/list")["tools"]]
        ctx.check("tools/list advertises only browser_repl", names == ["browser_repl"], names)
        ctx.check("and the server sends no desktop instructions", not init.get("instructions"), init.get("instructions"))
        ctx.check("other tools stay callable", "paused" in inst.mcp.call("status")[0])
        result = inst.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {"code": "1 + 1"}})
        ctx.check("browser_repl answers", result["content"][0]["text"] == "2", result)
    with TestInstance() as inst:
        inst.mcp.initialize()
        names = [t["name"] for t in inst.mcp.rpc("tools/list")["tools"]]
        ctx.check("the default surface lists browser_repl next to the discrete browser tools",
                  "browser_repl" in names and "browser_click" in names and "act" not in names, names)
        default = names
    with TestInstance({"toolSurface": "discrete"}, browser=True) as inst:
        inst.mcp.initialize()
        names = [t["name"] for t in inst.mcp.rpc("tools/list")["tools"]]
        ctx.check('toolSurface "discrete" lists the default surface without browser_repl',
                  names == [n for n in default if n != "browser_repl"], names)
        result = inst.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {"code": "2 + 2"}})
        ctx.check("browser_repl still answers there", result["content"][0]["text"] == "4", result)
    names = [t["name"] for t in ctx.mcp.rpc("tools/list")["tools"]]
    ctx.check('toolSurface "full" lists what the default surface does', names == default, names)


if __name__ == "__main__":
    sys.exit(0 if Run(config_overrides={"toolSurface": "full"}).run(set(sys.argv[1:])) else 1)
