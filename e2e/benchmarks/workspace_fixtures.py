"""Seeded, synthetic business workflows with durable server-owned outcome evidence.

The browser can read business records and submit bounded application actions. It
cannot read expected answers, evaluator state, evidence files, or a judge endpoint.
"""
from __future__ import annotations

import copy
import hashlib
import http.server
import json
import os
import pathlib
import random
import threading
import urllib.parse

WORKSPACE_TASKS = ("workspace_reconcile", "workspace_handoff", "workspace_recovery")
FIXTURE_VERSION = "workspace-workflows/1"
SECTIONS = ("home", "invoices", "orders", "policy", "messages", "tickets", "releases", "activity")
ROUTES = [{"value": "standard", "label": "Standard"},
          {"value": "finance_review", "label": "Finance review"}, {"value": "hold", "label": "Hold"}]
STATUSES = ["Open", "Needs investigation", "Ready for verification", "Scheduled"]
OWNERS = ("Maya Chen", "Leo Brooks", "Amira Shah", "Noah Reyes", "Iris Cole")
MAX_BODY = 16384


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def money(cents):
    return f"${cents / 100:.2f}"


def integer(value, minimum=0, maximum=100_000_000):
    return type(value) is int and minimum <= value <= maximum


def _case(task, seed):
    rng = random.Random(f"{FIXTURE_VERSION}:{task}:{seed}")
    serials = rng.sample(range(1000, 9999), 40)
    vendor = rng.choice(["Northstar Labs", "Harbor Studio", "Cedar Works", "Juniper Supply"])
    owners = rng.sample(list(OWNERS), 2)
    inv, order = f"INV-{serials[0]}", f"PO-{serials[1]}"
    qty, price, shipping, cap, credit = rng.randint(7, 13), rng.randint(18, 47) * 100, rng.randint(12, 24) * 100, rng.randint(6, 10) * 100, rng.randint(3, 12) * 100
    accepted = qty - 1
    amount = accepted * price + min(shipping, cap) - credit
    threshold = amount + rng.choice([-1500, 1500])
    memo = f"RCPT-{serials[2]}"
    stable = f"{rng.randint(3, 8)}.{rng.randint(1, 8)}.{rng.randint(1, 8)}"
    preview = stable + "-preview"
    records = {section: [] for section in SECTIONS if section not in {"home", "activity"}}
    records["orders"] = [{"id": order, "vendor": vendor, "product": "Workshop equipment", "quantity": qty,
                           "unit_price_cents": price, "delivery_cap_cents": cap, "notes": "Pay only for confirmed accepted units."}]
    invoice = {"id": inv, "vendor": vendor, "order_id": order, "invoice_number": str(serials[0]), "quantity": qty,
               "unit_price_cents": price + 200, "shipping_cents": shipping,
               "invoice_amount_cents": qty * (price + 200) + shipping,
               "approved_amount_cents": None, "route": "unreviewed", "note": "", "revision": 1, "status": "Needs review"}
    records["invoices"] = [invoice]
    records["policy"] = [{"id": "POL-INVOICE", "title": "Invoice reconciliation policy",
        "body": f"Approve accepted units at the purchase-order unit price, plus shipping up to the order's delivery cap, less the latest credit. Route amounts of {money(threshold)} or more to Finance review; lower amounts to Standard. Copy the latest reconciliation memo exactly."},
        {"id": "POL-HANDOFF", "title": "Release handoff policy",
         "body": f"For ready-for-verification tickets set status Ready for verification and owner {owners[0]}. For scheduled onboarding set status Scheduled and owner {owners[1]}. Assign the current Stable release, never a Preview or Retired release. Copy each ticket's newest required handoff memo exactly."}]
    records["messages"] = [
        {"id": f"MSG-{serials[3]}", "topic": "Billing", "record_id": order, "author": "Receiving desk",
         "sent_at": "2026-01-05T09:00:00Z", "subject": f"Earlier receiving advice for {order}",
         "body": f"Receiving confirmed {qty} units for {order}. Apply a credit of $0.00. Reconciliation memo: OLD-{serials[2]}."},
        {"id": f"MSG-{serials[4]}", "topic": "Billing", "record_id": order, "author": "Receiving desk",
         "sent_at": "2026-01-05T11:00:00Z", "subject": f"Updated receiving advice for {order}",
         "body": f"Receiving confirmed {accepted} units for {order}. Apply a credit of {money(credit)}. Reconciliation memo: {memo}. This replaces earlier receiving advice."}]
    records["releases"] = [
        {"id": f"REL-{serials[5]}", "version": stable, "stage": "Stable", "rollout": "Current", "notes": "Approved for customer handoffs."},
        {"id": f"REL-{serials[6]}", "version": preview, "stage": "Preview", "rollout": "Internal only", "notes": "Not approved for customer commitments."},
        {"id": f"REL-{serials[7]}", "version": f"{stable.split('.')[0]}.0.0", "stage": "Retired", "rollout": "Ended", "notes": "Do not assign new handoffs."}]
    ticket_ids = [f"TKT-{serials[8]}", f"TKT-{serials[9]}"]
    memos = [f"VERIFY-{serials[10]}", f"ONBOARD-{serials[11]}"]
    for index, tid in enumerate(ticket_ids):
        records["tickets"].append({"id": tid, "customer": vendor, "subject": "Export regression" if index == 0 else "Team onboarding",
            "status": "Open", "owner": "Unassigned", "release": "", "note": "", "revision": 1})
        records["messages"].extend([
            {"id": f"MSG-{serials[12 + index]}", "topic": "Support", "record_id": tid, "author": "Customer team",
             "sent_at": "2026-01-05T08:30:00Z", "subject": f"Earlier plan for {tid}",
             "body": f"Investigate this ticket first. The proposed release was {preview}. Required handoff memo: OLD-{memos[index]}."},
            {"id": f"MSG-{serials[14 + index]}", "topic": "Support", "record_id": tid, "author": "Customer team",
             "sent_at": "2026-01-05T12:00:00Z", "subject": f"Latest handoff for {tid}",
             "body": (f"The customer confirmed the export fix. Latest disposition: ready-for-verification. Required handoff memo: {memos[index]}."
                      if index == 0 else f"The customer agreed to the next onboarding session. Latest disposition: scheduled onboarding. Required handoff memo: {memos[index]}.")}])
    # Similar names, adjacent-looking references and irrelevant records punish broad edits.
    for index in range(3):
        oid, iid, tid = f"PO-{serials[16 + index]}", f"INV-{serials[19 + index]}", f"TKT-{serials[22 + index]}"
        other = vendor + (" East" if index == 0 else " Services" if index == 1 else " Archive")
        records["orders"].append({"id": oid, "vendor": other, "product": "Replacement supplies", "quantity": qty + index,
            "unit_price_cents": price + 100, "delivery_cap_cents": cap + 100, "notes": "Separate order; no changes requested."})
        records["invoices"].append({**invoice, "id": iid, "invoice_number": str(serials[19 + index]), "vendor": other, "order_id": oid})
        records["tickets"].append({"id": tid, "customer": other, "subject": "Export regression", "status": "Needs investigation",
            "owner": owners[1], "release": preview, "note": "Preserve the existing investigation.", "revision": 1})
        records["messages"].append({"id": f"MSG-{serials[25 + index]}", "topic": "Billing", "record_id": oid,
            "author": "Receiving desk", "sent_at": "2026-01-05T14:00:00Z", "subject": f"Receipt for separate order {oid}",
            "body": f"Receiving confirmed {qty + 2} units for {oid}. Apply a credit of $0.00. Reconciliation memo: OTHER-{serials[25 + index]}."})
    for rows in records.values():
        rng.shuffle(rows)
    baseline = {"revision": 1, "sections": records}
    private = {"invoice_id": inv, "order_id": order, "ticket_ids": ticket_ids, "owners": owners, "stable": stable,
               "memos": memos, "amount": amount, "memo": memo, "threshold": threshold,
               "accepted": accepted, "credit": credit, "price": price, "shipping": shipping, "cap": cap,
               "late_message_id": f"MSG-{serials[30]}", "late_memo": f"RCPT-{serials[31]}"}
    if task == "workspace_handoff":
        prompt = (f"Prepare the handoff for {vendor}'s tickets {ticket_ids[0]} and {ticket_ids[1]}. Use the latest conversations "
                  "and current release handoff policy to update each ticket's status, owner, release and handoff note. Preserve every other record.")
    else:
        prompt = (f"Reconcile invoice {inv} from {vendor} against purchase order {order}, the latest receiving messages and the invoice policy. "
                  "Save the approved amount, routing decision and required reconciliation memo. Preserve every other record.")
        if task == "workspace_recovery":
            prompt += " Updates may arrive while you work. Resolve any conflict using current records; if a save response is lost, inspect the persisted invoice before deciding whether another submission is needed."
    return baseline, private, prompt


class WorkspaceFixture:
    def __init__(self, task, seed, artifact_dir):
        if task not in WORKSPACE_TASKS or not integer(seed, 0, 2 ** 63 - 1):
            raise ValueError("A supported workspace task and nonnegative integer seed are required")
        self.task, self.seed = task, seed
        self.artifact_dir = pathlib.Path(artifact_dir)
        self.baseline, self._private, self.prompt = _case(task, seed)
        self.state = copy.deepcopy(self.baseline)
        self.lock = threading.RLock()
        self.events, self._late_published, self._lost_response = [], False, False
        self.httpd = self.thread = None
        self.url = self.origin = None
        self._paths = {name: self.artifact_dir / f"workspace-{name}.json" for name in ("baseline", "state", "case")}
        self._ledger = self.artifact_dir / "workspace-ledger.jsonl"

    def case_metadata(self):
        fingerprint = hashlib.sha256(canonical({"task": self.task, "seed": self.seed, "baseline": self.baseline}).encode()).hexdigest()
        return {"task_id": self.task, "seed": self.seed, "case_id": fingerprint[:20], "fixture_version": FIXTURE_VERSION}

    def _write(self, path, value):
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _event(self, kind, **fields):
        event = {"seq": len(self.events) + 1, "kind": kind, **fields,
                 "previous_hash": self.events[-1]["hash"] if self.events else "0" * 64}
        event["hash"] = hashlib.sha256(canonical(event).encode()).hexdigest()
        with self._ledger.open("a") as stream:
            stream.write(canonical(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)
        return event

    def _transition(self, kind, next_state, **fields):
        self._event(kind, before=self.state, after=next_state, **fields)
        self.state = copy.deepcopy(next_state)
        self._write(self._paths["state"], self.state)

    def _options(self):
        return {"routes": copy.deepcopy(ROUTES), "statuses": list(STATUSES),
                "owners": ["Unassigned", *sorted(OWNERS)],
                "releases": [row["version"] for row in self.state["sections"]["releases"]]}

    def view(self, section):
        if section not in SECTIONS:
            raise ValueError("Unknown workspace section")
        with self.lock:
            self._event("view", section=section, revision=self.state["revision"])
            if section == "home":
                items = [{"id": "OVERVIEW", "title": "Operations workspace", "body": "Use Inbox, Orders, Invoices, Support and Release notes to resolve the assigned work. Activity records saved changes and conflicts."}]
            elif section == "activity":
                items = [{"id": f"ACT-{event['seq']}", "record_id": event.get("record_id", ""),
                          "action": event.get("action", event["kind"]), "outcome": event.get("outcome", "published"),
                          "message": event.get("message", "A newer receiving update was published.")}
                         for event in self.events if event["kind"] in {"result", "system_revision"}]
            else:
                items = self.state["sections"][section]
            return copy.deepcopy({"task_id": self.task, "title": "Operations workspace", "revision": self.state["revision"],
                                  "section": section, "items": items, "options": self._options()})

    def _late_state(self):
        next_state = copy.deepcopy(self.baseline)
        next_state["revision"] += 1
        for row in next_state["sections"]["invoices"]:
            if row["id"] == self._private["invoice_id"]:
                row.update(revision=2, status="Needs re-review")
        next_state["sections"]["messages"].append({"id": self._private["late_message_id"], "topic": "Billing",
            "record_id": self._private["order_id"], "author": "Receiving desk", "sent_at": "2026-01-05T15:00:00Z",
            "subject": f"Late correction for {self._private['order_id']}",
            "body": f"Receiving confirmed {self._private['accepted'] - 1} units for {self._private['order_id']}. Apply a credit of {money(self._private['credit'] + 200)}. Reconciliation memo: {self._private['late_memo']}. This replaces all earlier receiving advice."})
        return next_state

    def action(self, request):
        """Application behavior, deliberately independent of the expected task answer."""
        with self.lock:
            request = copy.deepcopy(request)
            action = request.get("action") if isinstance(request, dict) else None
            record_id = request.get("id") if isinstance(request, dict) else None
            kind = "review" if action == "review_invoice" else "attempt"
            self._event(kind, action=action, record_id=record_id, request=request)

            def reject(error, message, status=400, **extra):
                self._event("result", action=action, record_id=record_id, outcome=error, message=message)
                return status, {"ok": False, "error": error, "message": message, **extra}

            if not isinstance(request, dict) or not isinstance(action, str) or action not in {"review_invoice", "save_invoice", "update_ticket"}:
                return reject("validation_error", "Choose a supported application action.")
            invoice_action = action in {"review_invoice", "save_invoice"}
            allowed = {"action", "id", "revision"} | ({"amount_cents", "route", "note"} if action == "save_invoice" else {"status", "owner", "release", "note"} if action == "update_ticket" else set())
            if set(request) != allowed or not isinstance(record_id, str) or not integer(request.get("revision"), 1):
                return reject("validation_error", "Supply the displayed record ID, revision and all form fields.")
            section = "invoices" if invoice_action else "tickets"
            row = next((item for item in self.state["sections"][section] if item["id"] == record_id), None)
            if row is None:
                return reject("unknown_record", "This record does not exist.", 404)
            if action != "review_invoice":
                note = request.get("note")
                if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
                    return reject("validation_error", "Enter a note between 1 and 500 characters.")
                if action == "save_invoice" and (not integer(request.get("amount_cents")) or not isinstance(request.get("route"), str) or request.get("route") not in {route["value"] for route in ROUTES}):
                    return reject("validation_error", "Enter a nonnegative amount and choose a routing decision.")
                if action == "update_ticket" and (request.get("status") not in STATUSES or request.get("owner") not in self._options()["owners"] or request.get("release") not in self._options()["releases"]):
                    return reject("validation_error", "Choose a listed status, owner and release.")
            if action == "save_invoice" and self.task == "workspace_recovery" and record_id == self._private["invoice_id"] and not self._late_published:
                # Apply only the external correction, preserving any previous unrelated
                # writes so the judge can detect collateral edits rather than erase them.
                late = self._late_state()
                next_state = copy.deepcopy(self.state)
                next_state["revision"] += 1
                next_state["sections"]["messages"].append(late["sections"]["messages"][-1])
                for item in next_state["sections"]["invoices"]:
                    if item["id"] == record_id:
                        item.update(revision=item["revision"] + 1, status="Needs re-review")
                self._transition("system_revision", next_state, record_id=record_id)
                self._late_published = True
                return reject("stale_revision", "A newer receiving update arrived. Reload the invoice and latest Inbox message before saving.", 409, revision=row["revision"] + 1)
            if request["revision"] != row["revision"]:
                return reject("stale_revision", "This record changed. Reload it before saving.", 409, revision=row["revision"])
            if action == "review_invoice":
                self._event("result", action=action, record_id=record_id, outcome="reviewed", message="Invoice review is ready; no change has been saved.")
                return 200, {"ok": True, "record": copy.deepcopy(row), "review_revision": row["revision"], "message": "Review ready. Save only after checking the supporting records."}
            next_state = copy.deepcopy(self.state)
            target = next(item for item in next_state["sections"][section] if item["id"] == record_id)
            if action == "save_invoice":
                target.update(approved_amount_cents=request["amount_cents"], route=request["route"], note=request["note"].strip(), status="Reconciled")
            else:
                target.update({key: request[key].strip() for key in ("status", "owner", "release", "note")})
            target["revision"] += 1
            next_state["revision"] += 1
            lose = self.task == "workspace_recovery" and action == "save_invoice" and record_id == self._private["invoice_id"] and not self._lost_response
            outcome = "committed_response_lost" if lose else "committed"
            self._transition("commit", next_state, action=action, record_id=record_id, request=request, outcome=outcome)
            self._event("result", action=action, record_id=record_id, outcome=outcome, message=f"Saved {record_id} at revision {target['revision']}.")
            if lose:
                self._lost_response = True
                return 503, {"ok": False, "error": "response_lost", "uncertain": True,
                             "message": "The save response was lost. Check the saved invoice and Activity before another submission."}
            return 200, {"ok": True, "record": copy.deepcopy(target), "message": f"Saved {record_id}."}

    def read(self):
        """Reread persisted evidence rather than trust the page or in-memory state."""
        with self.lock:
            try:
                return {"case_metadata": json.loads(self._paths["case"].read_text()),
                        "baseline": json.loads(self._paths["baseline"].read_text()),
                        "state": json.loads(self._paths["state"].read_text()),
                        "events": [json.loads(line) for line in self._ledger.read_text().splitlines()],
                        "receipt": {"event_count": len(self.events), "head": self.events[-1]["hash"] if self.events else "0" * 64}}
            except (OSError, ValueError) as error:
                return {"evidence_error": f"{type(error).__name__}: {error}"}

    def _expected_state(self):
        expected = self._late_state() if self.task == "workspace_recovery" else copy.deepcopy(self.baseline)
        if self.task == "workspace_handoff":
            for index, tid in enumerate(self._private["ticket_ids"]):
                row = next(item for item in expected["sections"]["tickets"] if item["id"] == tid)
                row.update(status=["Ready for verification", "Scheduled"][index], owner=self._private["owners"][index],
                           release=self._private["stable"], note=self._private["memos"][index], revision=2)
                expected["revision"] += 1
        else:
            amount = self._private["amount"] - (self._private["price"] + 200 if self.task == "workspace_recovery" else 0)
            row = next(item for item in expected["sections"]["invoices"] if item["id"] == self._private["invoice_id"])
            row.update(approved_amount_cents=amount, route="finance_review" if amount >= self._private["threshold"] else "standard",
                       note=self._private["late_memo"] if self.task == "workspace_recovery" else self._private["memo"],
                       revision=row["revision"] + 1, status="Reconciled")
            expected["revision"] += 1
        return expected

    def judge(self, evidence=None):
        evidence = self.read() if evidence is None else evidence
        checks = dict.fromkeys(("durable_evidence_valid", "ledger_replays_to_saved_state", "exact_final_state",
                               "exact_committed_effects", "unrelated_records_untouched", "no_duplicate_submissions",
                               "recovery_conflict_exercised", "recovery_persisted_state_rechecked"), False)
        try:
            if evidence.get("case_metadata") != self.case_metadata() or evidence.get("baseline") != self.baseline:
                return checks
            events = evidence["events"]
            receipt = evidence["receipt"]
            if receipt != {"event_count": len(events), "head": events[-1]["hash"] if events else "0" * 64}:
                return checks
            state, previous = copy.deepcopy(self.baseline), "0" * 64
            for index, event in enumerate(events, 1):
                unsigned = {key: value for key, value in event.items() if key != "hash"}
                if event.get("seq") != index or event.get("previous_hash") != previous or event.get("hash") != hashlib.sha256(canonical(unsigned).encode()).hexdigest():
                    return checks
                previous = event["hash"]
                if event["kind"] in {"commit", "system_revision"}:
                    if event["before"] != state:
                        return checks
                    state = copy.deepcopy(event["after"])
            # Detect deleted tails/recomputed corruption using the server's retained
            # durable receipt. The supplied snapshot may legitimately predate later events.
            if events and (len(events) > len(self.events) or events[-1]["hash"] != self.events[len(events) - 1]["hash"]):
                return checks
            checks["durable_evidence_valid"] = True
            checks["ledger_replays_to_saved_state"] = state == evidence["state"]
            checks["exact_final_state"] = evidence["state"] == self._expected_state()
            target_ids = set(self._private["ticket_ids"] if self.task == "workspace_handoff" else [self._private["invoice_id"]])
            commits = [event for event in events if event["kind"] == "commit"]
            checks["exact_committed_effects"] = len(commits) == len(target_ids) and {event["record_id"] for event in commits} == target_ids
            attempts = [event for event in events if event["kind"] == "attempt"]
            checks["unrelated_records_untouched"] = all(event.get("record_id") in target_ids or event.get("record_id") is None for event in attempts)
            first_commit = {}
            for event in commits:
                first_commit.setdefault(event["record_id"], event["seq"])
            checks["no_duplicate_submissions"] = all(event.get("record_id") not in first_commit or event["seq"] < first_commit[event["record_id"]] for event in attempts)
            if self.task != "workspace_recovery":
                checks["recovery_conflict_exercised"] = checks["recovery_persisted_state_rechecked"] = True
            else:
                revisions = [event for event in events if event["kind"] == "system_revision"]
                stale = [event for event in events if event["kind"] == "result" and event.get("action") == "save_invoice" and event.get("outcome") == "stale_revision" and event.get("record_id") in target_ids]
                lost = [event for event in commits if event.get("outcome") == "committed_response_lost"]
                checks["recovery_conflict_exercised"] = (len(revisions) == 1 and revisions[0]["after"] == self._late_state()
                    and len(lost) == 1 and any(revisions[0]["seq"] < event["seq"] < lost[0]["seq"] for event in stale)
                    and lost[0]["request"]["revision"] == 2)
                checks["recovery_persisted_state_rechecked"] = bool(lost) and any(event["kind"] == "view" and event.get("section") == "invoices" and event["seq"] > lost[0]["seq"] and event.get("revision") == evidence["state"]["revision"] for event in events)
            return checks
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return checks

    def diagnostics(self, evidence=None):
        evidence = self.read() if evidence is None else evidence
        events = evidence.get("events", [])
        return {"write_attempts": sum(event.get("kind") == "attempt" for event in events),
                "commits": sum(event.get("kind") == "commit" for event in events),
                "validation_refusals": sum(event.get("kind") == "result" and event.get("outcome") == "validation_error" for event in events),
                "stale_refusals": sum(event.get("kind") == "result" and event.get("outcome") == "stale_revision" for event in events)}

    def __enter__(self):
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if any(path.exists() for path in [*self._paths.values(), self._ledger]):
            raise ValueError("Workspace evidence directory already contains a case; use a fresh trial directory")
        self._write(self._paths["baseline"], self.baseline)
        self._write(self._paths["state"], self.state)
        self._write(self._paths["case"], self.case_metadata())
        self._ledger.touch()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else canonical(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(body)

            def boundary(self, mutation=False, public_document=False):
                if self.headers.get("Host") != urllib.parse.urlsplit(owner.origin).netloc:
                    return False
                if mutation and self.headers.get("Origin") != owner.origin:
                    return False
                # Opening the static app shell from about:blank or an extension
                # is a cross-site navigation; its subsequent API calls are not.
                if (public_document and not mutation
                        and self.headers.get("Sec-Fetch-Mode") == "navigate"
                        and self.headers.get("Sec-Fetch-Dest") == "document"):
                    return True
                return self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")

            def do_GET(self):
                if not self.boundary(public_document=self.path == "/fixture.html"):
                    return self.reply(403, {"error": "same_origin_required"})
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/fixture.html" and not parsed.query:
                    try:
                        html = pathlib.Path(__file__).with_name("workspace.html").read_bytes()
                    except OSError:
                        return self.reply(503, {"error": "workspace_ui_unavailable"})
                    return self.reply(200, html, "text/html; charset=utf-8")
                if parsed.path == "/api/view":
                    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) == {"section"} and len(query["section"]) == 1 and query["section"][0] in SECTIONS:
                        return self.reply(200, owner.view(query["section"][0]))
                return self.reply(404, {"error": "not_found"})

            def do_POST(self):
                if self.path != "/api/action":
                    return self.reply(404, {"error": "not_found"})
                if not self.boundary(mutation=True):
                    return self.reply(403, {"error": "same_origin_required"})
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json" or self.headers.get("Transfer-Encoding"):
                    return self.reply(415, {"error": "json_required"})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BODY:
                        return self.reply(413, {"error": "body_size_invalid"})
                    self.connection.settimeout(3)
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError("Incomplete request")
                    request = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                except (ValueError, OSError, RecursionError):
                    with owner.lock:
                        owner._event("attempt", action=None, record_id=None, request=None)
                        owner._event("result", action=None, record_id=None, outcome="validation_error", message="Invalid JSON request.")
                    return self.reply(400, {"error": "invalid_json"})
                status, payload = owner.action(request)
                return self.reply(status, payload)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = self.origin + "/fixture.html"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=3)
