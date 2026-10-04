"""Scripted UI baseline for workspace workflows, never a model reasoning score.

This adapter reads only rendered content through Second Cursor and writes through
the same public browser controls as the model. It cannot access fixture objects,
HTTP endpoints, the case generator, or the judge's expected state.
"""
import re
from decimal import Decimal


def match(pattern, text):
    found = re.search(pattern, text)
    if not found:
        raise AssertionError(f"Visible context did not contain {pattern!r}: {text[:1600]}")
    return found.group(1)


def fact(text, label):
    return match(r"(?:^|\n)" + re.escape(label) + r":\s*([^\n]+)", text).strip()


def cents(text):
    return int(Decimal(match(r"\$([\d,]+\.\d{2})", text).replace(",", "")) * 100)


class WorkspaceUI:
    def __init__(self, ctx, targets, meter):
        self.ctx, self.meter, self.tab = ctx, meter, targets["tab_id"]

    def command(self, operation, **args):
        return self.meter.call("browser_command", tab_id=self.tab, operation=operation, **args)

    def text(self):
        return self.command("inspect", locator={"selector": "#workspace"})["text"]

    def click(self, name):
        return self.command("click", locator={"role": "button", "name": name, "exact": True})

    def wait(self, text):
        return self.command("wait", locator={"text": text, "exact": False},
                            state="visible", timeout_ms=5000)

    def notice(self, text):
        return self.command("wait", locator={"text": text, "exact": True},
                            state="visible", timeout_ms=5000)

    def wait_button(self, name):
        return self.command("wait", locator={"role": "button", "name": name, "exact": True},
                            state="visible", timeout_ms=5000)

    def navigate(self, name):
        self.click(name)
        self.command("wait", locator={"text": "Loading records…", "exact": True},
                     state="hidden", timeout_ms=5000)
        return self.text()

    def detail(self):
        return self.command("inspect", locator={"selector": "#record-detail"})["text"]

    def buttons(self, prefix):
        outline = self.meter.call("browser_snapshot", tab_id=self.tab, full=True)["outline"]
        return list(dict.fromkeys(name for name in re.findall(r'button "([^"\n]+)"', outline)
                                 if name.startswith(prefix)))

    def latest_message(self, record):
        self.navigate("Inbox")
        names = [name for name in self.buttons("Open message ") if record in name]
        self.ctx.require("related messages can be discovered in the visible inbox", bool(names), record)
        messages = []
        for name in names:
            self.click(name)
            text = self.detail()
            self.ctx.require("message belongs to the requested record", fact(text, "Related record") == record)
            messages.append((fact(text, "Sent"), text))
        return max(messages, key=lambda item: item[0])[1]

    def fill(self, name, value):
        return self.command("fill", locator={"label": name, "exact": True}, text=str(value))

    def select(self, name, value):
        return self.command("select_option", locator={"label": name, "exact": True}, value=value)


def execute(ctx, task, targets, meter):
    try:
        return _execute(ctx, task, targets, meter)
    except Exception:
        # Keep the visible failure state before fixture cleanup; never mask the
        # original error if an observation also fails.
        try:
            meter.call("browser_screenshot", tab_id=targets["tab_id"])
            meter.call("browser_snapshot", tab_id=targets["tab_id"], full=True)
        except Exception:
            pass
        raise


def _execute(ctx, task, targets, meter):
    ui = WorkspaceUI(ctx, targets, meter)
    goal = targets["goal"]
    ui.command("wait", locator={"text": "Loading workspace…", "exact": True},
               state="hidden", timeout_ms=5000)
    meter.call("browser_screenshot", tab_id=ui.tab)
    if task in {"workspace_reconcile", "workspace_recovery"}:
        invoice = match(r"invoice (INV-\d+)", goal)
        order = match(r"purchase order (PO-\d+)", goal)
        ui.navigate("Policy")
        ui.click("Open policy Invoice reconciliation policy")
        policy = ui.detail()
        threshold = cents(match(r"amounts of (\$[\d,.]+) or more", policy))
        ui.navigate("Orders")
        ui.click("Open order " + order)
        order_text = ui.detail()
        ctx.require("purchase order identity matches the assignment", fact(order_text, "Order ID") == order)
        price, cap = cents(fact(order_text, "Unit price")), cents(fact(order_text, "Delivery cap"))

        def decide():
            message = ui.latest_message(order)
            accepted = int(match(r"Receiving confirmed (\d+) units", message))
            credit = cents(match(r"credit of (\$[\d,.]+)", message))
            memo = match(r"Reconciliation memo: ([A-Z0-9-]+)\.", message)
            ui.navigate("Invoices")
            ui.click("Open invoice " + invoice.removeprefix("INV-"))
            detail = ui.detail()
            ctx.require("invoice and purchase order match the assignment",
                        fact(detail, "Invoice ID") == invoice and fact(detail, "Order ID") == order)
            amount = accepted * price + min(cents(fact(detail, "Shipping")), cap) - credit
            route = "Finance review" if amount >= threshold else "Standard"
            ui.fill("Approved amount ($)", f"{amount // 100}.{amount % 100:02}")
            ui.select("Decision", route)
            ui.fill("Review note", memo)
            ui.click("Review invoice")
            ui.wait_button("Save invoice")
            ui.click("Save invoice")
            return amount, route, memo

        expected = decide()
        if task == "workspace_recovery":
            ui.wait("This record changed.")
            expected = decide()
            ui.wait("Save confirmation is unavailable.")
            ui.navigate("Activity")
            activity = ui.text()
            ctx.require("activity exposes a lost-response save to reconcile", "response_lost" in activity, activity)
            ui.navigate("Invoices")
            ui.click("Open invoice " + invoice.removeprefix("INV-"))
        else:
            ui.notice("Saved " + invoice + ".")
            ui.click("Reload invoice")
            ui.wait("Invoice reloaded from saved records.")
        detail = ui.detail()
        actual = (cents(fact(detail, "Saved approved amount")), fact(detail, "Saved decision"), fact(detail, "Saved note"))
        ctx.require("fresh invoice view confirms the derived result", actual == expected, {"actual": actual, "expected": expected})
    elif task == "workspace_handoff":
        tickets = list(dict.fromkeys(re.findall(r"TKT-\d+", goal)))
        ctx.require("the assignment identifies two support records", len(tickets) == 2, tickets)
        ui.navigate("Policy")
        ui.click("Open policy Release handoff policy")
        policy = ui.detail()
        owners = {
            "ready-for-verification": match(r"Ready for verification and owner ([^.]+)\.", policy),
            "scheduled onboarding": match(r"Scheduled and owner ([^.]+)\.", policy),
        }
        ui.navigate("Release notes")
        stable = []
        for name in ui.buttons("Open release "):
            ui.click(name)
            detail = ui.detail()
            if fact(detail, "Stage") == "Stable" and fact(detail, "Rollout") == "Current":
                stable.append(fact(detail, "Version"))
        ctx.require("release notes identify one current Stable release", len(stable) == 1, stable)
        for ticket in tickets:
            message = ui.latest_message(ticket)
            disposition = match(r"Latest disposition: ([^.]+)\.", message)
            memo = match(r"Required handoff memo: ([A-Z0-9-]+)\.", message)
            status = {"ready-for-verification": "Ready for verification", "scheduled onboarding": "Scheduled"}[disposition]
            ui.navigate("Support")
            ui.click("Open ticket " + ticket)
            ctx.require("ticket identity matches the assignment", fact(ui.detail(), "Ticket ID") == ticket)
            ui.select("Status", status)
            ui.select("Owner", owners[disposition])
            ui.select("Release", stable[0])
            ui.fill("Handoff note", memo)
            ui.click("Review support update")
            ui.wait_button("Save support update")
            ui.click("Save support update")
            ui.notice("Saved " + ticket + ".")
            ui.click("Reload ticket")
            ui.wait("Ticket reloaded from saved records.")
            detail = ui.detail()
            actual = tuple(fact(detail, key) for key in ("Saved status", "Saved owner", "Saved release", "Saved note"))
            expected = status, owners[disposition], stable[0], memo
            ctx.require("fresh ticket view confirms the handoff", actual == expected, {"actual": actual, "expected": expected})
    else:
        raise ValueError("Unknown workspace workflow")
    meter.call("browser_screenshot", tab_id=ui.tab)
