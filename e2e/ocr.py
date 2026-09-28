#!/usr/bin/env python3
"""Text recognition merged into window outlines (`ocr` on get_window_state), driven over MCP/HTTP like a client.

Ways this can fail, and the scenarios that catch them:
- Recognized text leaks into plain reads, or is missing from `ocr` reads (canvas_words).
- Frames are off: image pixels taken for points on a Retina capture, Vision's bottom-left origin left unflipped, the
  title bar forgotten, or the 1024 px screenshot scaling applied to recognition. canvas_words compares every word's
  frame with where the fixture drew it.
- A `text` line's center is not on its word, so an x/y click there misses (canvas_click: the fixture records clicks).
- Text accessibility already exposes is repeated: a label, the window title, a document's lines (canvas_words,
  textedit_not_duplicated, large_textedit_timing). A dedupe that drops everything would pass that, so the TextEdit
  document carries one control line whose accessibility text differs from what is drawn (a zero-width space), which
  must come back.
- Text drawn on an element is not attached to that element's line as ocr="…", or text outside every element gets an
  [index] as if it could be pressed (canvas_words, textedit_not_duplicated).
- Diffs: a plain read after an `ocr` read reports the recognized lines as removed; an `ocr` read after a plain read or
  an action repeats unchanged text or misses changed text; an action's observation shows recognized text (ocr_diffs).
- ocr_ms or ocr_count missing, or ocr_count disagreeing with what was merged (canvas_words, textedit_not_duplicated).
- `scope` with `ocr` shows text from outside the scope (canvas_words).
- Recognition is too slow on a large window to be worth calling (large_textedit_timing records ocr_ms).
- The fixture takes focus or shows on the user's screen: it is a background accessory window that opens on the agent
  display (checked when it starts); the harness checks the frontmost app, Space and cursor after every scenario.
- The first recognition on a Mac waits minutes for Vision to compile its model: the read must still come back within
  its deadline, with the outline and ocr_error (wait_for_recognizer checks this whenever the model is not ready yet;
  on a Mac where it is, a failed recognition cannot be forced without revoking Screen Recording).

Usage:  e2e/ocr.py [scenario ...]   (same targeting flags as e2e/run.py, e.g. SC_E2E_APP=build/SecondCursor.app)
Output: e2e/artifacts/<run-id>/ (report.md, results.json, transcript.jsonl, ocr-timings.json)
"""
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run import Run, SCENARIOS, agent_origin, element_index, inside, probe_binary, scenario

SCENARIOS.clear()

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT_LINE = re.compile(r'^([+-]?)( *)text "((?:[^"\\]|\\.)*)" \((-?\d+),(-?\d+) (\d+)x(\d+)\)$', re.M)
OCR_ATTR = re.compile(r' ocr="((?:[^"\\]|\\.)*)"')
TIMINGS = {}


def require_on_agent_display(ctx, wid, what):
    """AppKit can give a fixture process other, helper windows; waits for the one it reported."""
    display = ctx.call("agent_display", action="create")["bounds"]
    deadline, window = time.time() + 15, None
    while window is None and time.time() < deadline:
        window = next((w for w in ctx.call("list_windows")["windows"] if w["id"] == wid), None)
        time.sleep(0.2)
    ctx.require(f"{what} opened on the agent display, not on the user's screen",
                window and inside(window["frame"], display), (window, display))


def text_lines(tree):
    """`text "…" (x,y wxh)` lines, with the diff sign (+, - or empty) and depth."""
    return [{"sign": m[1], "depth": len(m[2]) // 2, "text": json.loads(f'"{m[3]}"'),
             "x": int(m[4]), "y": int(m[5]), "width": int(m[6]), "height": int(m[7])}
            for m in TEXT_LINE.finditer(tree)]


def attached(tree):
    """(line, [texts]) for every element line carrying ocr="…"."""
    out = []
    for line in tree.splitlines():
        match = OCR_ATTR.search(line)
        if match:
            out.append((line, json.loads(f'"{match[1]}"').split(" | ")))
    return out


def recognized(tree):
    return [t["text"] for t in text_lines(tree)] + [text for _, texts in attached(tree) for text in texts]


def line_for(tree, word):
    return next((t for t in text_lines(tree) if t["text"].lower() == word.lower()), None)


def record_timing(ctx, label, value):
    TIMINGS[label] = value
    (ctx.run.dir / "ocr-timings.json").write_text(json.dumps(TIMINGS, indent=2))
    print(f"      {label}: {value}")


_recognizer_ready = False


def wait_for_recognizer(ctx, wid, minutes=8):
    """The first recognition for an app identity on a Mac (and again after an OS update) compiles Vision's model for the
    Neural Engine. That takes minutes and only completes while the app keeps running, so a short-lived test instance
    can keep missing it; until then an ocr read returns its outline with ocr_error. Waits it out once per run, in a
    throwaway session so the scenario's diff baselines stay untouched."""
    global _recognizer_ready
    if _recognizer_ready:
        return
    sid = ctx.call("session", action="create")["session_id"]
    started, errors = time.time(), 0
    try:
        while True:
            read = ctx.call("get_window_state", window_id=wid, session_id=sid, full=True, ocr=True)
            if "ocr_error" not in read:
                break
            errors += 1
            ctx.require("while the model is not ready, an ocr read returns its outline with ocr_error",
                        read["tree"].startswith("window ") and "did not finish" in read["ocr_error"], read)
            ctx.require(f"the on-device model was ready within {minutes} minutes", time.time() - started < minutes * 60)
            if errors == 1:
                print("      waiting for Vision to prepare its on-device model (first use for this app identity)")
    finally:
        ctx.mcp.call("session", action="close", session_id=sid)
    _recognizer_ready = True
    if errors:
        record_timing(ctx, "one-time model preparation before the first recognition (s)", round(time.time() - started))


class Canvas:
    """The custom-drawn fixture (sc-focus-probe --canvas): a background window that opens on the agent display, so it
    never shows on the user's screen."""

    def __init__(self, ctx, word="Hello Canvas"):
        self.state_path = ctx.scratch / "canvas-state.json"
        self.process = subprocess.Popen([str(probe_binary()), "--canvas", word, "--state", str(self.state_path),
                                         "--origin", agent_origin(ctx)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ctx.cleanups.append(self.stop)
        deadline = time.time() + 15
        while self.read() is None and time.time() < deadline:
            time.sleep(0.2)
        state = self.read()
        ctx.require("the canvas fixture started and wrote its state", state is not None)
        self.wid = state["window"]
        require_on_agent_display(ctx, self.wid, "the canvas")
        wait_for_recognizer(ctx, self.wid)

    def read(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def word(self, text):
        return next(w for w in self.read()["words"] if w["text"] == text)

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def offsets(found, drawn):
    """Center and width offsets, in points, of a recognized frame from the drawn one."""
    return {"dx": round(found["x"] + found["width"] / 2 - drawn["x"] - drawn["width"] / 2, 1),
            "dy": round(found["y"] + found["height"] / 2 - drawn["y"] - drawn["height"] / 2, 1),
            "dw": round(found["width"] - drawn["width"], 1)}


@scenario
def canvas_words(ctx):
    canvas = Canvas(ctx)
    state = canvas.read()
    words = [w["text"] for w in state["words"]]
    schema = next(t for t in ctx.mcp.rpc("tools/list")["tools"] if t["name"] == "get_window_state")["inputSchema"]
    described = schema["properties"].get("ocr", {}).get("description", "")
    ctx.check("get_window_state advertises ocr, and says text lines are clicked with x/y at their center",
              "x/y" in described and "center" in described, described)

    plain = ctx.call("get_window_state", window_id=canvas.wid, full=True)
    ctx.check("a plain read shows none of the drawn text and no ocr fields",
              not any(w in plain["tree"] for w in words + ["Loop"]) and not {"ocr_ms", "ocr_count"} & plain.keys(),
              plain["tree"])

    read = ctx.call("get_window_state", window_id=canvas.wid, full=True, ocr=True)
    tree = read["tree"]
    ctx.require("the ocr read reports ocr_ms and ocr_count",
                isinstance(read.get("ocr_ms"), int) and isinstance(read.get("ocr_count"), int), read)
    record_timing(ctx, "canvas ocr_ms (460x358 pt window)", read["ocr_ms"])
    for drawn in state["words"]:
        found = line_for(tree, drawn["text"])
        if not ctx.check(f"\"{drawn['text']}\" is a text line directly under the window", found and found["depth"] == 1,
                         tree):
            continue
        off = offsets(found, drawn)
        ctx.check(f"\"{drawn['text']}\" frame is where the fixture drew it (center within 4 pt, width within 8 pt)",
                  abs(off["dx"]) <= 4 and abs(off["dy"]) <= 4 and abs(off["dw"]) <= 8, {**off, "found": found, "drawn": drawn})
    ctx.check("no text line carries an index", not re.search(r"\[\d+\] text ", tree))
    ctx.check("the caption drawn on the Transport button is on its line as ocr=\"Loop\"",
              re.search(r'\[\d+\] AXButton "Transport".* ocr="Loop"', tree), tree)
    repeats = [t for t in recognized(tree) if t.lower() in ("tempo 128", "sc canvas probe")]
    ctx.check("the label and the window title, already in the outline, are not repeated", not repeats, repeats)
    ctx.check("ocr_count counts every merged text", read["ocr_count"] == len(recognized(tree)),
              (read["ocr_count"], recognized(tree)))

    button = element_index(tree, "AXButton", "Transport")
    scoped = ctx.call("get_window_state", window_id=canvas.wid, scope=button, ocr=True)
    ctx.check("a scoped ocr read keeps only the text inside the scope",
              'ocr="Loop"' in scoped["tree"] and not any(w in scoped["tree"] for w in words) and scoped["ocr_count"] == 1,
              scoped)


@scenario
def canvas_click(ctx):
    canvas = Canvas(ctx)
    tree = ctx.call("get_window_state", window_id=canvas.wid, full=True, ocr=True)["tree"]
    line = line_for(tree, "Render")
    ctx.require("\"Render\" is a text line", line, tree)
    x, y = line["x"] + line["width"] / 2, line["y"] + line["height"] / 2
    result = ctx.call("click", window_id=canvas.wid, x=x, y=y)
    deadline = time.time() + 5
    while not canvas.read()["clicks"] and time.time() < deadline:
        time.sleep(0.1)
    target, clicks = canvas.word("Render"), canvas.read()["clicks"]
    ctx.check("a pixel click at the text line's center landed on the drawn word",
              any(target["x"] <= c["x"] <= target["x"] + target["width"]
                  and target["y"] <= c["y"] <= target["y"] + target["height"] for c in clicks),
              {"clicked": (x, y), "received": clicks, "word": target})
    ctx.check("the click went through the pixel path", result.get("route") == "skylight_pointer", result)
    time.sleep(0.5)
    ctx.check("one pixel click reached the canvas view exactly once (a second copy would flip a toggle back)",
              len(canvas.read()["clicks"]) == 1, canvas.read()["clicks"])


@scenario
def ocr_diffs(ctx):
    canvas = Canvas(ctx)
    wid = canvas.wid
    ctx.call("get_window_state", window_id=wid, full=True)
    first = ctx.call("get_window_state", window_id=wid, ocr=True)
    added = [t["text"] for t in text_lines(first["tree"]) if t["sign"] in ("+", "")]
    ctx.check("the first ocr read after a plain read brings the drawn words",
              all(w["text"] in added for w in canvas.read()["words"]), first["tree"])
    ctx.check("and removes nothing", not [t for t in text_lines(first["tree"]) if t["sign"] == "-"], first["tree"])

    again = ctx.call("get_window_state", window_id=wid, ocr=True)
    ctx.check("an unchanged ocr read is (no changes)", again["is_diff"] and again["tree"] == "(no changes)", again["tree"])
    plain = ctx.call("get_window_state", window_id=wid)
    ctx.check("a plain read after an ocr read does not report the recognized lines as removed",
              plain["is_diff"] and plain["tree"] == "(no changes)", plain["tree"])
    back = ctx.call("get_window_state", window_id=wid, ocr=True)
    ctx.check("an ocr read after that plain read is still (no changes)",
              back["is_diff"] and back["tree"] == "(no changes)", back["tree"])

    stopped = canvas.word("Stopped")
    clicked = ctx.call("click", window_id=wid, x=stopped["x"] + stopped["width"] / 2, y=stopped["y"] + stopped["height"] / 2)
    observation = clicked["observation"]["tree"]
    ctx.check("the click's observation (no ocr) shows no recognized text", not text_lines(observation)
              and "ocr=" not in observation, observation)
    ctx.require("the fixture toggled its drawn status", canvas.read()["words"][3]["text"] == "Playing", canvas.read())
    changed = ctx.call("get_window_state", window_id=wid, ocr=True)
    lines = text_lines(changed["tree"])
    ctx.check("the next ocr read shows exactly the drawn change: -Stopped +Playing",
              [(t["sign"], t["text"]) for t in lines] == [("-", "Stopped"), ("+", "Playing")]
              and not re.search(r"^[+-]\s*\[", changed["tree"], re.M), changed["tree"])
    ctx.check("its diff_base is the click's observation", changed.get("diff_base") == clicked["observation"]["snapshot_id"],
              (changed.get("diff_base"), clicked["observation"].get("snapshot_id")))


CONTROL = "Plan\u200bning grid"
DOCUMENT = ["Quarterly planning notes", "Ship the recognizer before Friday", "Review the canvas fixture twice", CONTROL]


@scenario
def textedit_not_duplicated(ctx):
    window = ctx.launch_scratch_textedit("dedupe.txt", "\n".join(DOCUMENT), on_agent_display=True)
    wait_for_recognizer(ctx, window["id"])
    read = ctx.call("get_window_state", window_id=window["id"], full=True, ocr=True)
    tree = read["tree"]
    record_timing(ctx, f"TextEdit ocr_ms ({int(window['frame']['width'])}x{int(window['frame']['height'])} pt)",
                  read.get("ocr_ms"))
    texts = recognized(tree)
    repeated = [t for t in texts if any(line.lower() in t.lower() for line in DOCUMENT[:3] + ["dedupe.txt"])]
    ctx.check("the document's lines and the window title are not repeated as recognized text", not repeated, texts)
    ctx.check("each document line is in the outline once", all(tree.count(line) == 1 for line in DOCUMENT[:3]), tree)
    area = next((line for line, texts in attached(tree) if "] AXTextArea " in line), "")
    ctx.check("the control line, drawn as \"Planning grid\" but read by accessibility with a zero-width space, is "
              "attached to the text area (so recognition did read the document)", "planning grid" in area.lower(), tree)
    ctx.check("ocr_count counts every merged text", read.get("ocr_count") == len(texts), (read.get("ocr_count"), texts))


def rtf(lines, width, height):
    """A TextEdit document that opens at about width x height points (\\vieww/\\viewh are in twips)."""
    body = "\\\n".join(lines)
    return ("{\\rtf1\\ansi\\ansicpg1252\n{\\fonttbl\\f0\\fswiss\\fcharset0 Helvetica;}\n"
            f"\\paperw12240\\paperh15840\\vieww{width * 20}\\viewh{height * 20}\\viewkind0\n"
            f"\\pard\\f0\\fs28 {body}}}\n")


@scenario
def large_textedit_timing(ctx):
    lines = [f"Line {i:02d} of the long report: every row names a different city, like Lisbon, Osaka and Quito {i * 7}"
             for i in range(1, 46)]
    window = ctx.launch_scratch_textedit("large.rtf", rtf(lines, 1300, 800), on_agent_display=True)
    wait_for_recognizer(ctx, window["id"])
    reads = [ctx.call("get_window_state", window_id=window["id"], full=True, ocr=True) for _ in range(3)]
    frame = reads[-1]["window"]["frame"]
    size = f"{int(frame['width'])}x{int(frame['height'])} pt"
    samples = [r.get("ocr_ms") for r in reads]
    ctx.require("every ocr read reports ocr_ms", all(isinstance(s, int) for s in samples), samples)
    record_timing(ctx, f"large TextEdit ocr_ms, first read ({size})", samples[0])
    record_timing(ctx, f"large TextEdit ocr_ms, median of later reads ({size})", statistics.median(samples[1:]))
    texts = recognized(reads[-1]["tree"])
    leaked = [t for t in texts if re.match(r"line \d+ of the long report", t.lower())]
    record_timing(ctx, "large TextEdit: document lines that came back as recognized text", f"{len(leaked)} of {len(lines)}")
    ctx.check(f"the large window is large ({size})", frame["width"] >= 1000 and frame["height"] >= 600, frame)
    ctx.check("at most a few misread document lines come back as recognized text (dedupe needs an exact read)",
              len(leaked) <= len(lines) // 10, leaked)


if __name__ == "__main__":
    sys.exit(0 if Run().run(set(sys.argv[1:])) else 1)
