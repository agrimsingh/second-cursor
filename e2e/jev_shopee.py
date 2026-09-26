#!/usr/bin/env python3
"""A live-site version of e2e/jev_prune.py: a model finds multi-pack cat food on Shopee Singapore that arrives in
1-2 days, directly or through the Jev pruning proxy.

It always targets the user's installed app: Shopee refuses the isolated headless test browser, so the agent's tabs
open in the user's logged-in Chrome, in the Second Cursor tab group. Runs are read-only: the prompt forbids the cart,
purchases and settings, and every tab a run opened is closed when it ends.

A search result card's delivery badge ("2 Days") is what the user goes by, so the agent answers from search results
and the run scores what it reports: how many days each reported badge says, and whether each reported listing is a
cat food multi-pack (Jev judges, since listings phrase packs every way: "24x85g", "85g x 24", "Bundle of 24 Cans").

Ways the comparison can mislead:
  - The site changes between runs (stock, ranking, ads): the arms alternate in each round.
  - The score trusts what the agent reports; the call transcript in agent/ shows what it read.
  - The proxy knows the goal because the harness gives it GOAL.

Usage:  python3 e2e/jev_shopee.py --arms direct:M,pruned:M [--repeat N] [--timeout S]
Output: e2e/artifacts/shopee-<ts>-<arm>/ report.md, shopee.json (answer, scores, timing, tokens), agent/, and for
        pruned arms prune/ and jev.jsonl; with more than one run, e2e/artifacts/shopee-compare-<ts>.md
"""
import json
import re
import sys
import time
import urllib.request

import run
from jev_prune import Condenser, CondensingProxy, JevKeep, condensing_note, condensing_stats
from jev_triage import SERVER, Jev, jev_key, option, run_agent
from run import SCENARIOS, Skip

SCENARIOS.clear()

START = "https://shopee.sg/"
GOAL = """On Shopee Singapore, find cat food sold as multi-packs (for example 24 x 85g, or a carton of 24 cans) that \
will be delivered within 1-2 days, fastest delivery first. Up to 5 listings.
The delivery time on a search result card (for example "2 Days") is accurate, so there is no need to open product \
pages. Do not add anything to the cart, buy anything, or change any settings.
Reply with DONE and a JSON array of the listings, each {"name", "price", "pack", "delivery"}, with delivery as the \
card shows it."""

PROMPT = """Use only the tools of the MCP server named "{server}" (Second Cursor). Do not use the shell, files, or any \
other MCP server.
Open {url} with browser_open and work in that tab:

{goal}"""

MULTIPACK = ("This product listing sells cat food as a multi-pack: several cans, pouches or bags of cat food sold "
             "together in one purchase.\nListing: {name} (pack: {pack})")


def badge_days(delivery):
    text = str(delivery or "")
    if re.search(r"(?i)same day|today", text):
        return 0
    if re.search(r"(?i)next day|tomorrow", text):
        return 1
    match = re.search(r"(?i)(\d+)(?:\s*-\s*(\d+))?\s*days?\b", text)
    return int(match.group(2) or match.group(1)) if match else None


def listings(answer):
    try:
        data = json.loads(answer[answer.find("["):answer.rfind("]") + 1])
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def judge(found, jev):
    scored = [{"reported": item, "days": badge_days(item.get("delivery"))} for item in found]
    if scored:
        answers = jev.ask("Product listings on Shopee Singapore", {
            f"l{n}": {"type": "noul", "instructions": MULTIPACK.format(name=s["reported"].get("name"),
                                                                        pack=s["reported"].get("pack"))}
            for n, s in enumerate(scored)})
        for n, s in enumerate(scored):
            s["cat_multipack_p"] = round(answers[f"l{n}"]["noul"], 3)
    return scored


def trial(kind, model, timeout):
    def scenario(ctx):
        if ctx.run.instance is not None:
            raise Skip("needs the user's logged-in Chrome, so it must target the installed app")
        if not ctx.call("browser_status").get("connected"):
            raise Skip("Chrome extension not connected")
        key = jev_key()
        ctx.require("a TypeSafe API key is available", key, "set TYPESAFE_API_KEY or write ~/.config/typesafe/key")
        before = {tab["id"] for tab in ctx.call("browser_tabs")["tabs"]}

        def close_new_tabs():
            for tab in ctx.mcp.call("browser_tabs")[0]["tabs"]:
                if tab["id"] not in before:
                    ctx.mcp.call("browser_close", tab_id=tab["id"])
        ctx.cleanups.append(close_new_tabs)
        urllib.request.urlopen(urllib.request.Request(
            ctx.mcp.url.rsplit("/", 1)[0] + "/turn-ended", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {ctx.mcp.token}", "Content-Type": "application/json"}), timeout=15)

        prompt = PROMPT.format(server=SERVER, url=START, goal=GOAL)
        if kind == "pruned":
            condenser = Condenser(JevKeep(Jev(key, ctx.run.dir / "jev.jsonl"), GOAL), ctx.run.dir / "condensed")
            with CondensingProxy(ctx.mcp.url, condenser) as proxy:
                result = run_agent(ctx, model, prompt, START, timeout, mcp_url=proxy.url)
            result.update(condensing_stats(condenser, proxy))
        else:
            result = run_agent(ctx, model, prompt, START, timeout)

        scored = judge(listings(result["answer"]), Jev(key, ctx.run.dir / "jev-judge.jsonl"))
        usage = result.get("usage") or {}
        result.update(arm=f"{kind}:{model}", listings=len(scored), scored=scored,
                      cat_multipacks=sum(s["cat_multipack_p"] >= 0.5 for s in scored),
                      within_2_days=sum(s["days"] is not None and s["days"] <= 2 for s in scored),
                      context_tokens=(usage.get("inputTokens") or 0) + (usage.get("cacheReadTokens") or 0))
        (ctx.run.dir / "shopee.json").write_text(json.dumps(result, indent=2) + "\n")
        ctx.run.notes.append(summary(result))
        ctx.check(f"finished on its own ({result['finish']})", result["finish"] == "exit 0", result["finish"])
        ctx.check(f"reported listings as JSON ({len(scored)})", scored, result["answer"][-300:])
        ctx.check("used only MCP tools", "shell" not in result["tools_used"], result["tools_used"])
    scenario.__name__ = "shopee"
    return scenario


def summary(r):
    return (f"{r['arm']}: {r['listings']} listings, {r['cat_multipacks']} cat food multi-packs, {r['within_2_days']} "
            f"within 2 days; loop {r['loop_s']}s = model {r['model_s']}s + tools {r['tool_s']}s, {r['tool_calls']} tool "
            f"calls, {r['context_tokens']:,} context tokens{condensing_note(r)}")


def compare(rows):
    table = ["| Arm | Run | Listings | Cat multi-packs | Within 2 days | Loop | Model | Tools | Tool calls | "
             "Context tokens | Lines shown | Expand asks |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        r = row["result"] or {}
        shown = f"{r['shown_lines']}/{r['outline_lines']}" if "shown_lines" in r else "all"
        table.append(f"| {row['arm']} | {row['dir']} | {r.get('listings', '?')} | {r.get('cat_multipacks', '?')} | "
                     f"{r.get('within_2_days', '?')} | {r.get('loop_s', '?')}s | {r.get('model_s', '?')}s | "
                     f"{r.get('tool_s', '?')}s | {r.get('tool_calls', '?')} | {r.get('context_tokens', 0):,} | {shown} | "
                     f"{r.get('expand_requests', '-')} |")
    return "\n".join(table) + "\n"


def parse_arm(spec):
    kind, _, model = spec.partition(":")
    if kind not in ("direct", "pruned"):
        raise SystemExit(f"unknown arm {spec!r}: use direct:<model> or pruned:<model>")
    return kind, model or "grok-4.7-medium-fast"


if __name__ == "__main__":
    arms = [parse_arm(s.strip()) for s in (option("--arms") or "direct:,pruned:").split(",") if s.strip()]
    repeat, timeout = int(option("--repeat", "1")), float(option("--timeout", "900"))
    rows = []
    for _ in range(repeat):
        for kind, model in arms:
            SCENARIOS[:] = [trial(kind, model, timeout)]
            attempt = run.Run(installed=True)
            renamed = attempt.dir.with_name(f"shopee-{attempt.dir.name}-{kind}-{model}")
            attempt.dir.rename(renamed)
            attempt.dir = renamed
            passed = attempt.run(set())
            result = attempt.dir / "shopee.json"
            rows.append({"arm": f"{kind}:{model}", "dir": attempt.dir.name, "passed": passed,
                         "result": json.loads(result.read_text()) if result.exists() else None})
    if len(rows) > 1:
        path = run.ROOT / "artifacts" / f"shopee-compare-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(f"# Shopee cat food comparison\n\n{compare(rows)}")
        print(f"\n{compare(rows)}\nSummary: {path}")
    sys.exit(0 if all(row["passed"] for row in rows) else 1)
