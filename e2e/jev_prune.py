#!/usr/bin/env python3
"""Outline condensing: does cutting a big page's outline down make a frontier model driving Second Cursor faster,
without making it wrong, and does deciding what to cut need a model (Jev) and the goal, or do plain rules do?

Fixture: e2e/fixtures/console.html, an admin console. Every view carries several hundred lines of constant chrome (product
nav, a 35-item settings sidebar, a 120-entry activity feed, a footer) around a section that can be as long (80 team
members, 36 invoices). GOAL changes one setting in each of four sections and reads one invoice amount. Look-alikes
(Maya Cheng, the Push "Weekly digest", Jira Service Management) punish a model that stops reading closely.

Arms, each against a fresh test instance: cursor-agent -p with model M and MCP tools only.
  direct:M   The instance's MCP server as is. Browser actions return what changed, and browser_snapshot reads after
             the first are diffs.
  jev:M      Through CondensingProxy with JevKeep: every full outline browser_open or browser_snapshot returns is split
             into chunks of at most CHUNK_LINES lines under their containers, and one Jev call asks, per chunk, whether
             the goal needs it.
  rules:M    The same with RuleKeep: no model and no goal; footers and side panels hidden, long lists cut to their
             first chunk.
Each run of hidden chunks becomes one '… N lines hidden (expand "r3")' line; browser_snapshot with expand returns
those lines from the read they were hidden in, so the model can look inside one part without taking everything back.

Ways the comparison can mislead, and what the run does about each:
  - The Jev arm knows the goal because the harness gives it GOAL; a product would have to get the goal from the agent.
    The rules arm needs no goal.
  - Condensing hides something the goal needs: the model can expand it, and the run counts how often it does.
  - A model that skims picks a look-alike: the score counts every setting that changed besides the four in EXPECTED.
  - One run is noise: --repeat N, with the arms alternating in each round.

Usage:  python3 e2e/jev_prune.py --arms direct:M,jev:M,rules:M [--repeat N] [--timeout S]
Key:    TYPESAFE_API_KEY, or ~/.config/typesafe/key (jev arms)
Output: e2e/artifacts/prune-<ts>-<arm>/ report.md, transcript.jsonl, prune.json (score, timing, tokens), agent/ (as in
        jev_triage), and for condensed arms condensed/ (every outline before and after) and, for jev, jev.jsonl; with
        more than one run, e2e/artifacts/prune-compare-<ts>.md
"""
import http.server
import json
import sys
import threading
import time
import urllib.error
import urllib.request

import run
from jev_triage import SERVER, FixtureBackend, Jev, jev_key, option, run_agent
from run import SCENARIOS, Skip

SCENARIOS.clear()

GOAL = """In this admin console:
1. Change Maya Chen's role to Admin.
2. Turn off the Weekly digest email notification. Leave the push notification as it is.
3. Set the session timeout to 30 minutes.
4. Disconnect the Jira Software integration.
5. Find the amount of invoice INV-2026-03.
Change nothing else. Reply with DONE and the invoice amount when finished."""

PROMPT = """Use only the tools of the MCP server named "{server}" (Second Cursor). Do not use the shell, files, or any \
other MCP server.
Open {url} with browser_open and do this in that tab, operating the page through its controls as a person would:

{goal}"""

EXPECTED = {
    "role:Maya Chen": "Admin",
    "notify:Email notifications:Weekly digest": False,
    "security:Session timeout": "30 minutes",
    "integration:Jira Software": False,
}
INVOICE_AMOUNT = "1,284.50"

CHUNK_LINES = 24
KEEP_AT = 0.3
CONDENSE_FROM_LINES = 80
QUESTION = ("The goal needs this part of the page: it holds a control the goal requires using (including navigation to "
            "a section the goal names) or information the goal requires reading or checking.\n"
            "Where it is: {path}\nThe part:\n{text}")


class ConsoleBackend(FixtureBackend):
    """A key/value store of the console's settings. Each change carries `was`, so the first one records the original."""

    def __init__(self):
        self.values, self.originals = {}, {}

    def saved(self):
        return {"saved": self.values}

    def apply(self, action):
        self.originals.setdefault(action["key"], action.get("was"))
        self.values[action["key"]] = action.get("value")

    def changes(self):
        with self.lock:
            return {key: value for key, value in self.values.items() if value != self.originals[key]}


def role(line):
    text = line.strip()
    if text.startswith("[e"):
        text = text.split("] ", 1)[-1]
    return text.split(" ", 1)[0]


class Page:
    """An outline split so that every line is covered exactly once: a container too long to be one chunk keeps its own
    line as structure, and its children become chunks, consecutive small siblings grouped up to CHUNK_LINES lines.
    A chunk is (start, stop, ancestors), ancestors being the structure lines above it, outermost first."""

    def __init__(self, outline):
        self.lines = outline.split("\n")
        self.depth = [(len(line) - len(line.lstrip(" "))) // 2 for line in self.lines]
        self.end, stack = [len(self.lines)] * len(self.lines), []
        for i in range(len(self.lines)):
            while stack and self.depth[stack[-1]] >= self.depth[i]:
                self.end[stack.pop()] = i
            stack.append(i)
        self.structure, self.chunks = set(), []
        top = 0
        while top < len(self.lines):
            self._visit(top, [])
            top = self.end[top]

    def children(self, i):
        j = i + 1
        while j < self.end[i]:
            yield j
            j = self.end[j]

    def text(self, start, stop):
        return "\n".join(line[2 * self.depth[start]:] for line in self.lines[start:stop])

    def _visit(self, i, ancestors):
        if self.end[i] - i <= CHUNK_LINES:
            self.chunks.append((i, self.end[i], ancestors))
            return
        self.structure.add(i)
        inner, group = ancestors + [i], None
        for child in self.children(i):
            if self.end[child] - child > CHUNK_LINES:
                if group:
                    self.chunks.append((*group, inner))
                    group = None
                self._visit(child, inner)
            elif group and self.end[child] - group[0] <= CHUNK_LINES:
                group = (group[0], self.end[child])
            else:
                if group:
                    self.chunks.append((*group, inner))
                group = (child, self.end[child])
        if group:
            self.chunks.append((*group, inner))


class JevKeep:
    """Asks Jev, in one call, whether the goal needs each chunk."""

    def __init__(self, jev, goal):
        self.jev, self.goal = jev, goal

    def __call__(self, page):
        questions = {f"c{n}": {"type": "noul", "instructions": QUESTION.format(
            path=" > ".join(page.lines[a].strip() for a in ancestors) or "top of the page", text=page.text(start, stop))}
            for n, (start, stop, ancestors) in enumerate(page.chunks)}
        answers = self.jev.ask({"goal": self.goal}, questions)
        return {n for n in range(len(page.chunks)) if answers[f"c{n}"]["noul"] >= KEEP_AT}


class RuleKeep:
    """No model and no goal. Hides footers and side panels, and of a long list of like items (table rows, list items,
    cards) keeps the first chunk. Navigation is never cut: it is how an agent reaches everything else."""

    HIDDEN = {"contentinfo", "complementary"}
    NEVER = {"navigation", "banner", "menubar", "tablist", "toolbar", "dialog", "alertdialog"}

    def __call__(self, page):
        keep, cut = set(), set()
        for n, (start, stop, ancestors) in enumerate(page.chunks):
            roles = {role(page.lines[a]) for a in ancestors} | {role(page.lines[start])}
            if roles & self.HIDDEN:
                continue
            parent = ancestors[-1] if ancestors else None
            if parent is not None and not roles & self.NEVER and self.list_like(page, parent):
                if parent in cut:
                    continue
                cut.add(parent)
            keep.add(n)
        return keep

    @staticmethod
    def list_like(page, i):
        roles = [role(page.lines[c]) for c in page.children(i)]
        return len(roles) >= 6 and max(roles.count(r) for r in set(roles)) >= 0.8 * len(roles)


class Condenser:
    """Condenses full outlines with a keep strategy. Each run of hidden chunks becomes one line with an id, and expand()
    answers from what was hidden, so the agent can look inside one part without reading the whole page again."""

    def __init__(self, keep, out_dir):
        self.keep, self.out, self.stats, self.regions = keep, out_dir, [], {}
        self.out.mkdir(exist_ok=True)

    def condense(self, outline):
        page = Page(outline)
        if len(page.lines) < CONDENSE_FROM_LINES:
            return outline
        started = time.time()
        keep = self.keep(page)
        seconds = time.time() - started
        chunk_at = {start: (n, stop) for n, (start, stop, _) in enumerate(page.chunks)}
        out, region, i = [], None, 0
        while i < len(page.lines):
            if i in page.structure:
                out.append(page.lines[i])
                region, i = None, i + 1
                continue
            n, stop = chunk_at[i]
            if n in keep:
                out.extend(page.lines[i:stop])
                region = None
            else:
                if not region or region["depth"] != page.depth[i]:
                    region = {"id": f"r{len(self.regions) + 1}", "depth": page.depth[i], "at": len(out),
                              "first": page.lines[i].strip()[:70], "lines": []}
                    self.regions[region["id"]] = region["lines"]
                    out.append("")
                region["lines"].extend(page.lines[i:stop])
                out[region["at"]] = (f'{"  " * region["depth"]}… {len(region["lines"])} lines hidden '
                                     f'(expand "{region["id"]}"): {region["first"]}')
            i = stop
        shown = sum(not line.lstrip().startswith("… ") for line in out)
        condensed = "\n".join([f"(condensed: {shown} of {len(page.lines)} lines shown. To read a hidden part, call "
                               f"browser_snapshot with expand set to its id, or a list of ids.)"] + out)
        index = len(self.stats) + 1
        (self.out / f"{index:03}-full.txt").write_text(outline + "\n")
        (self.out / f"{index:03}-condensed.txt").write_text(condensed + "\n")
        self.stats.append({"lines": len(page.lines), "shown": shown, "chars": len(outline), "condensed_chars": len(condensed),
                           "chunks": len(page.chunks), "kept": len(keep), "keep_s": round(seconds, 3)})
        return condensed

    def expand(self, ids):
        missing = [i for i in ids if i not in self.regions]
        if missing:
            raise KeyError(f"No hidden part {', '.join(missing)}; ids come from the '… N lines hidden (expand \"r3\")' "
                           f"lines of a condensed read")
        return "\n".join(f"(hidden part {i}, as it was when read)\n" + "\n".join(self.regions[i]) for i in ids)


class CondensingProxy:
    """An MCP endpoint that forwards everything to Second Cursor, condenses full outlines on the way back, and answers
    browser_snapshot calls with expand itself."""

    OUTLINES = {"browser_snapshot": (), "browser_open": ("observation",)}
    EXPAND = {"type": "array", "items": {"type": "string"},
              "description": "Ids of hidden parts of a condensed read (its '… N lines hidden (expand \"r3\")' lines) to "
                             "return in full, as they were when read, without reading the page again."}

    def __init__(self, upstream, condenser):
        self.upstream, self.condenser, self.expand_requests = upstream, condenser, 0

    def __enter__(self):
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, headers, body):
                self.send_response(status)
                for name in ("Content-Type", "Mcp-Session-Id"):
                    if headers.get(name):
                        self.send_header(name, headers[name])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.respond(405, {}, b"")

            def do_DELETE(self):
                self.respond(*proxy.forward(self, "DELETE", b""))

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                request = json.loads(body) if body else {}
                params = request.get("params") or {}
                tool = params.get("name") if request.get("method") == "tools/call" else None
                if tool == "browser_snapshot":
                    ids = (params.get("arguments") or {}).pop("expand", None)
                    ids = [ids] if isinstance(ids, str) else list(ids or [])
                    if ids:
                        proxy.expand_requests += 1
                        reply = json.dumps(proxy.expanded(request.get("id"), ids)).encode()
                        return self.respond(200, {"Content-Type": "application/json"}, reply)
                    body = json.dumps(request).encode()
                status, headers, reply = proxy.forward(self, "POST", body)
                if request.get("method") == "tools/list" or tool in proxy.OUTLINES:
                    reply = proxy.rewrite(headers.get("Content-Type", ""), reply,
                                          lambda message: proxy.edit(request.get("method"), tool, message))
                self.respond(status, headers, reply)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()

    def forward(self, handler, method, body):
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in ("host", "content-length", "connection")}
        request = urllib.request.Request(self.upstream, data=body if method == "POST" else None, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def expanded(self, rpc_id, ids):
        try:
            text, error = json.dumps({"outline": self.condenser.expand(ids), "expanded": ids, "is_diff": False}), False
        except KeyError as e:
            text, error = e.args[0], True
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": text}], "isError": error}}

    @staticmethod
    def rewrite(content_type, body, edit):
        if "text/event-stream" in content_type:
            lines = body.decode().split("\n")
            for n, line in enumerate(lines):
                if line.startswith("data:") and line[5:].strip():
                    lines[n] = "data: " + json.dumps(edit(json.loads(line[5:])))
            return "\n".join(lines).encode()
        return json.dumps(edit(json.loads(body))).encode() if body else body

    def edit(self, method, tool, message):
        result = message.get("result") or {}
        if method == "tools/list":
            for entry in result.get("tools", []):
                if entry.get("name") == "browser_snapshot":
                    entry.setdefault("inputSchema", {}).setdefault("properties", {})["expand"] = self.EXPAND
            return message
        if result.get("isError"):
            return message
        for item in result.get("content", []):
            if item.get("type") != "text":
                continue
            data = json.loads(item["text"])
            holder = data
            for key in self.OUTLINES[tool]:
                holder = holder.get(key) or {}
            if isinstance(holder.get("outline"), str) and not holder.get("is_diff"):
                holder["outline"] = self.condenser.condense(holder["outline"])
                item["text"] = json.dumps(data)
            break
        return message


def condensing_stats(condenser, proxy):
    stats = condenser.stats
    return {"condensed_outlines": len(stats), "expand_requests": proxy.expand_requests,
            "outline_lines": sum(s["lines"] for s in stats), "shown_lines": sum(s["shown"] for s in stats),
            "keep_s": round(sum(s["keep_s"] for s in stats), 1)}


def score(changes, answer):
    done = {key: changes.get(key) == value for key, value in EXPECTED.items()}
    found_amount = INVOICE_AMOUNT in answer
    return {
        "goals": sum(done.values()) + found_amount, "of": len(EXPECTED) + 1,
        "missed": [key for key, ok in done.items() if not ok] + ([] if found_amount else ["invoice amount"]),
        "collateral": {key: value for key, value in changes.items() if key not in EXPECTED},
    }


def trial(kind, model, timeout):
    def scenario(ctx):
        if not ctx.call("browser_status").get("connected"):
            raise Skip("isolated test browser not connected")
        with ConsoleBackend() as backend:
            url = f"{backend.base}/console.html"
            urllib.request.urlopen(urllib.request.Request(
                ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=b"{}", method="POST",
                headers={"Authorization": f"Bearer {ctx.mcp.token}", "Content-Type": "application/json"}), timeout=15)
            prompt = PROMPT.format(server=SERVER, url=url, goal=GOAL)
            if kind == "direct":
                result = run_agent(ctx, model, prompt, url, timeout)
            else:
                if kind == "jev":
                    key = jev_key()
                    ctx.require("a TypeSafe API key is available", key,
                                "set TYPESAFE_API_KEY or write ~/.config/typesafe/key")
                    keep = JevKeep(Jev(key, ctx.run.dir / "jev.jsonl"), GOAL)
                else:
                    keep = RuleKeep()
                condenser = Condenser(keep, ctx.run.dir / "condensed")
                with CondensingProxy(ctx.run.instance.url, condenser) as proxy:
                    result = run_agent(ctx, model, prompt, url, timeout, mcp_url=proxy.url)
                result.update(condensing_stats(condenser, proxy))
            ctx.require("the page was loaded", backend.loads(), "the page never loaded its settings")
            ctx.check("nothing but the page read or changed the console's backend", not backend.bypass(),
                      backend.bypass())
            result.update(score(backend.changes(), result["answer"]), arm=f"{kind}:{model}", page_loads=backend.loads())
            usage = result.get("usage") or {}
            result["context_tokens"] = (usage.get("inputTokens") or 0) + (usage.get("cacheReadTokens") or 0)
            (ctx.run.dir / "prune.json").write_text(json.dumps(result, indent=2) + "\n")
            ctx.run.notes.append(summary(result))
            ctx.check(f"finished on its own ({result['finish']})", result["finish"] == "exit 0", result["finish"])
            ctx.check(f"did everything the goal asks ({result['goals']}/{result['of']})",
                      result["goals"] == result["of"], result["missed"])
            ctx.check("changed nothing else", not result["collateral"], result["collateral"])
            ctx.check("used only MCP tools", "shell" not in result["tools_used"], result["tools_used"])
    scenario.__name__ = "prune"
    return scenario


def condensing_note(r):
    if "condensed_outlines" not in r:
        return ""
    return (f", {r['condensed_outlines']} outlines condensed to {r['shown_lines']} of {r['outline_lines']} lines "
            f"({r['keep_s']}s deciding), {r['expand_requests']} expand requests")


def summary(r):
    return (f"{r['arm']}: {r['goals']}/{r['of']} goals, {len(r['collateral'])} other changes, finish {r['finish']}; "
            f"loop {r['loop_s']}s = model {r['model_s']}s + tools {r['tool_s']}s, {r['tool_calls']} tool calls, "
            f"{r['context_tokens']:,} context tokens{condensing_note(r)}")


def compare(rows):
    table = ["| Arm | Run | Goals | Other changes | Loop | Model | Tools | Tool calls | Context tokens | Lines shown | "
             "Expand asks |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        r = row["result"] or {}
        shown = f"{r['shown_lines']}/{r['outline_lines']}" if "shown_lines" in r else "all"
        table.append(f"| {row['arm']} | {row['dir']} | {r.get('goals', '?')}/{r.get('of', '?')} | "
                     f"{len(r.get('collateral', {})) if r else '?'} | {r.get('loop_s', '?')}s | {r.get('model_s', '?')}s | "
                     f"{r.get('tool_s', '?')}s | {r.get('tool_calls', '?')} | {r.get('context_tokens', 0):,} | {shown} | "
                     f"{r.get('expand_requests', '-')} |")
    return "\n".join(table) + "\n"


def parse_arm(spec):
    kind, _, model = spec.partition(":")
    if kind not in ("direct", "jev", "rules"):
        raise SystemExit(f"unknown arm {spec!r}: use direct:<model>, jev:<model> or rules:<model>")
    return kind, model or "grok-4.7-medium-fast"


if __name__ == "__main__":
    arms = [parse_arm(s.strip()) for s in (option("--arms") or "direct:,jev:,rules:").split(",") if s.strip()]
    repeat, timeout = int(option("--repeat", "1")), float(option("--timeout", "900"))
    rows = []
    for _ in range(repeat):
        for kind, model in arms:
            SCENARIOS[:] = [trial(kind, model, timeout)]
            attempt = run.Run(config_overrides={"approvals": "allow", "appApproval": "off"}, browser=True)
            renamed = attempt.dir.with_name(f"prune-{attempt.dir.name}-{kind}-{model}")
            attempt.dir.rename(renamed)
            attempt.dir = renamed
            passed = attempt.run(set())
            result = attempt.dir / "prune.json"
            rows.append({"arm": f"{kind}:{model}", "dir": attempt.dir.name, "passed": passed,
                         "result": json.loads(result.read_text()) if result.exists() else None})
    if len(rows) > 1:
        path = run.ROOT / "artifacts" / f"prune-compare-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(f"# Outline condensing comparison\n\n{compare(rows)}")
        print(f"\n{compare(rows)}\nSummary: {path}")
    sys.exit(0 if all(row["passed"] for row in rows) else 1)
