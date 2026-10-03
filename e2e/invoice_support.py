"""Synthetic invoice fixture, independent judge, and SDK response fault injection."""
import copy
import http.server
import json
import os
import pathlib
import random
import threading
import time
import urllib.error
import urllib.request

TARGETS = ("INV-003", "INV-011", "INV-018")


def make_invoices(seed=4102026):
    rng = random.Random(seed)
    suppliers = ("Helio Analytics", "Cedar Office", "Northstar Studio", "Ridge Hosting")
    accounts = ("Software", "Office supplies", "Design services", "Infrastructure")
    return {f"INV-{i:03d}": {
        "id": f"INV-{i:03d}", "supplier": suppliers[(i - 1) % 4],
        "account": accounts[(i - 1) % 4], "status": "draft",
        "tax_percent": (0, 7, 9)[i % 3],
        "lines": [{"description": f"Service line {j}", "quantity": rng.randint(1, 6),
                   "unit_cents": rng.randint(115, 9876)} for j in (1, 2)],
    } for i in range(1, 21)}


def expected_entry(invoice):
    net = sum(line["quantity"] * line["unit_cents"] for line in invoice["lines"])
    tax = (net * invoice["tax_percent"] + 50) // 100
    return {"net_cents": net, "tax_cents": tax, "gross_cents": net + tax,
            "account": invoice["account"], "note": f"Reconciled {invoice['id']}"}


def judge(initial, saved):
    records, attempts = saved.get("records", {}), saved.get("attempts", [])
    return {
        "all_records_preserved": set(records) == set(initial),
        "exact_entries": all(records.get(key, {}).get("entry") == expected_entry(initial[key])
                             and records.get(key, {}).get("status") == "submitted" for key in TARGETS),
        "exactly_one_attempt": (len(attempts) == len(TARGETS)
                                 and all(sum(a.get("invoice_id") == key for a in attempts) == 1 for key in TARGETS)
                                 and all(a.get("accepted") is True and a.get("duplicate") is False for a in attempts)),
        "unrelated_unchanged": all(records.get(key) == invoice for key, invoice in initial.items() if key not in TARGETS),
        "browser_only": not saved.get("bypass", []),
    }


class InvoiceFixture:
    """The server records submitted data as received; validation belongs to the judge."""
    def __init__(self, ledger_path):
        self.path = pathlib.Path(ledger_path)
        self.initial = make_invoices()
        self.records = copy.deepcopy(self.initial)
        self.attempts, self.bypass = [], []
        self.lock = threading.RLock()
        self._persist()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".pending")
        with temporary.open("w") as file:
            json.dump({"records": self.records, "attempts": self.attempts, "bypass": self.bypass}, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)

    def submit(self, invoice_id, entry):
        with self.lock:
            invoice = self.records.get(invoice_id)
            duplicate = bool(invoice and invoice["status"] == "submitted")
            accepted = bool(invoice and not duplicate)
            self.attempts.append({"invoice_id": invoice_id, "entry": copy.deepcopy(entry),
                                  "accepted": accepted, "duplicate": duplicate, "at": time.monotonic()})
            if accepted:
                invoice.update(status="submitted", entry=copy.deepcopy(entry))
            self._persist()
            return accepted

    def attempt_count(self):
        with self.lock:
            return len(self.attempts)

    def __enter__(self):
        owner = self
        html = pathlib.Path(__file__).with_name("fixtures").joinpath("invoices.html").read_bytes()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def reply(self, status, body, content_type="application/json"):
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                if self.path == "/":
                    self.reply(200, html, "text/html; charset=utf-8")
                elif self.path == "/state":
                    with owner.lock:
                        self.reply(200, {"invoices": list(owner.records.values())})
                else:
                    self.reply(404, {"error": "not found"})
            def do_POST(self):
                if self.path != "/submit":
                    return self.reply(404, {"error": "not found"})
                browser = ("Mozilla/" in self.headers.get("User-Agent", "")
                           and self.headers.get("Origin") == owner.base
                           and self.headers.get("Sec-Fetch-Site") == "same-origin")
                if not browser:
                    with owner.lock:
                        owner.bypass.append({"method": "POST", "path": self.path})
                        owner._persist()
                    return self.reply(403, {"error": "use the fixture browser controls"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("invalid body size")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or not isinstance(body.get("entry"), dict):
                        raise ValueError("invalid invoice submission")
                    accepted = owner.submit(body.get("invoice_id"), body["entry"])
                    self.reply(200 if accepted else 409, {"accepted": accepted})
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.reply(400, {"error": "invalid submission"})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(2)


class ResponseGate:
    """Hold one completed mutation response after the independent ledger changes.

    Holding the HTTP response, then SIGKILLing the client, exercises a real lost
    response. It does not fake an SDK exception or invoke a callback after receipt.
    """
    # In this fixture a batch is exactly one invoice's fields plus its Submit click.
    MUTATIONS = frozenset(("batch",))

    def __init__(self, upstream, attempt_count):
        self.upstream, self.attempt_count = upstream.rstrip("/"), attempt_count
        self.held, self.release = threading.Event(), threading.Event()
        self.held_call, self.calls = None, []
        self.lock = threading.Lock()
        self.fired = False

    def read(self):
        with self.lock:
            return copy.deepcopy(self.calls)

    def __enter__(self):
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                if self.path != "/api":
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                decoded = json.loads(body)
                args = decoded.get("args", {})
                call = {"tool": decoded.get("tool"), "args": args,
                        "operation_id": args.get("operation_id"), "session_id": args.get("session_id"),
                        "started_at": time.monotonic(), "response_withheld": False}
                before = owner.attempt_count()
                headers = {name: value for name, value in self.headers.items()
                           if name.lower() not in ("host", "content-length", "connection")}
                request = urllib.request.Request(owner.upstream + "/api", data=body, headers=headers)
                try:
                    result = urllib.request.urlopen(request, timeout=90)
                except urllib.error.HTTPError as error:
                    result = error
                with result:
                    raw, status = result.read(), result.status
                    content_type = result.headers.get("Content-Type", "application/json")
                try:
                    upstream_result = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    upstream_result = None
                call.update(upstream_completed_at=time.monotonic(), status=status, upstream_result=upstream_result)
                deadline = time.monotonic() + 2
                if call["tool"] in owner.MUTATIONS and not owner.fired:
                    while owner.attempt_count() == before and time.monotonic() < deadline:
                        time.sleep(0.01)
                with owner.lock:
                    should_hold = (not owner.fired and call["tool"] in owner.MUTATIONS
                                   and status == 200 and isinstance(upstream_result, dict)
                                   and not upstream_result.get("isError") and not upstream_result.get("error")
                                   and owner.attempt_count() > before)
                    if should_hold:
                        owner.fired = True
                        call["response_withheld"] = True
                        owner.held_call = call
                    owner.calls.append(call)
                if should_hold:
                    owner.held.set()
                    owner.release.wait(90)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(2)
