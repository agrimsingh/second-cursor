"""Scripted baselines for the flight search task, never a model reasoning score.

Three ways a client can drive the same page through Second Cursor's public browser tools, so their call counts can be
compared: one action per call (reading refs from each action's own observation), tab batches of the actions whose
refs are already known, and a single browser_repl script with locators. Each reads only what the page renders and
returns the reply a model would give, ending in an ANSWER: line.
"""
import json
import re
import time

DRIVERS = ("single", "batch", "repl")
DAY = "Wednesday, October 28, 2026"
RESULTS = "flights · SIN → MAA"


def ref(outlines, role, name):
    for outline in outlines:
        found = re.search(r"\[(e\d+)\] " + re.escape(role) + r' "' + re.escape(name), outline or "")
        if found:
            return found[1]
    return None


def answer(text):
    numbers = list(dict.fromkeys(re.findall(r"\b(?:SQ|6E|AI|TR) \d{2,4}\b", text)))
    if not numbers:
        raise AssertionError(f"No nonstop flights in the filtered results: {text[:800]}")
    return "Nonstop, in-policy one-way flights SIN → MAA on Wed, Oct 28:\nANSWER: " + ", ".join(numbers)


class Tab:
    def __init__(self, meter, tab):
        self.call, self.tab, self.seen = meter.call, tab, []

    def outline(self, result):
        observation = result.get("observation") or {}
        text = result.get("outline") or observation.get("outline") or ""
        self.seen.insert(0, text)
        return text

    def navigate(self, url):
        return self.outline(self.call("browser_navigate", tab_id=self.tab, url=url))

    def snapshot(self):
        return self.outline(self.call("browser_snapshot", tab_id=self.tab, full=True))

    def need(self, role, name):
        """The ref from the latest observation that has it; a fresh read only if none does, as a client would."""
        found = ref(self.seen, role, name)
        if found is None:
            found = ref([self.snapshot()], role, name)
        if found is None:
            raise AssertionError(f'No {role} "{name}" in the page: {self.seen[0][:1500]}')
        return found

    def click(self, role, name, **extra):
        return self.outline(self.call("browser_click", tab_id=self.tab, ref=self.need(role, name), **extra))

    def batch(self, steps, **extra):
        result = self.call("batch", tab_id=self.tab, steps=steps, **extra)
        failed = [step for step in result["steps"] if not step.get("ok")]
        if failed:
            raise AssertionError(f"Batch step failed: {failed}")
        return self.outline(result)

    def results_text(self):
        return self.call("browser_command", tab_id=self.tab, operation="inspect",
                         locator={"role": "list", "name": "Flights"})["text"]


def single(meter, tab, url):
    page = Tab(meter, tab)
    page.navigate(url)
    page.click("button", "Trip Round-trip")
    page.click("option", "One-way")
    page.outline(meter.call("browser_type", tab_id=tab, ref=page.need("combobox", "To"), text="MAA",
                            expect={"text": "Chennai International Airport (MAA)", "timeout_ms": 3000}))
    page.click("option", "Chennai International Airport (MAA)")
    page.click("button", "Depart Select date")
    page.click("button", DAY)
    page.click("button", "Confirm")
    page.click("button", "Search flights", expect={"text": RESULTS, "timeout_ms": 8000})
    page.click("checkbox", "Nonstop only")
    page.click("checkbox", "In policy only")
    return answer(page.results_text())


def batch(meter, tab, url):
    page = Tab(meter, tab)
    click = lambda role, name: {"tool": "browser_click", "ref": page.need(role, name)}
    page.navigate(url)
    page.click("button", "Trip Round-trip")
    page.batch([click("option", "One-way"), {"tool": "browser_type", "ref": page.need("combobox", "To"), "text": "MAA"}],
               expect={"text": "Chennai International Airport (MAA)", "timeout_ms": 3000})
    page.batch([click("option", "Chennai International Airport (MAA)"), click("button", "Depart Select date")])
    page.batch([click("button", DAY), click("button", "Confirm"), click("button", "Search flights")],
               expect={"text": RESULTS, "timeout_ms": 8000})
    page.batch([click("checkbox", "Nonstop only"), click("checkbox", "In policy only")])
    return answer(page.results_text())


REPL = """
await page.goto(%(url)s)
await page.getByRole('button', { name: /^Trip/ }).click()
await page.getByRole('option', { name: 'One-way' }).click()
await page.getByRole('combobox', { name: 'To' }).pressSequentially('MAA')
await page.getByRole('option', { name: /\\(MAA\\)/ }).click()
await page.getByRole('button', { name: /^Depart/ }).click()
await page.getByRole('button', { name: %(day)s }).click()
await page.getByRole('button', { name: 'Confirm' }).click()
await page.getByRole('button', { name: 'Search flights' }).click()
await page.getByText(%(results)s).waitFor({ timeout: 10000 })
await page.getByLabel('Nonstop only').check()
await page.getByLabel(/In policy only/).check()
await page.getByRole('list', { name: 'Flights' }).innerText()
"""


def repl(ctx, meter, url):
    code = REPL % {"url": json.dumps(url), "day": json.dumps(DAY), "results": json.dumps(RESULTS)}
    # browser_repl answers with printed text, not the JSON object ctx.call parses, so time the raw call here.
    start = (time.monotonic() - meter.start) * 1000
    result = ctx.mcp.rpc("tools/call", {"name": "browser_repl", "arguments": {"code": code}})
    meter.events.append({"type": "tool", "name": "browser_repl", "start_ms": start,
                         "end_ms": (time.monotonic() - meter.start) * 1000, "ok": not result.get("isError"),
                         "retry": False})
    text = "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        raise AssertionError(f"browser_repl failed: {text[:1500]}")
    return answer(text)


def execute(ctx, targets, meter, driver="single"):
    if driver == "repl":
        return repl(ctx, meter, targets["url"])
    return (single if driver == "single" else batch)(meter, targets["tab_id"], targets["url"])
