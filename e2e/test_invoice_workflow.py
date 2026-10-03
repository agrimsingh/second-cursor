"""Offline acceptance: no desktop apps, live SC runtime, or models are started."""
import copy
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from invoice_support import InvoiceFixture, ResponseGate, TARGETS, expected_entry, judge, make_invoices


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "ledger.json"
        self.fixture = InvoiceFixture(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def complete(self):
        for invoice_id in TARGETS:
            self.fixture.submit(invoice_id, expected_entry(self.fixture.initial[invoice_id]))
        return json.loads(self.path.read_text())

    def test_correct_totals_persist_and_unrelated_invoices_are_preserved(self):
        saved = self.complete()
        self.assertEqual(len(saved["records"]), 20)
        self.assertTrue(all(judge(self.fixture.initial, saved).values()))

    def test_tax_uses_half_up_integer_cents(self):
        invoice = {"lines": [{"quantity": 1, "unit_cents": 150}], "tax_percent": 9,
                   "account": "Software", "id": "INV-001"}
        self.assertEqual(expected_entry(invoice)["tax_cents"], 14)

    def test_each_wrong_amount_and_account_fails(self):
        for field in ("net_cents", "tax_cents", "gross_cents", "account"):
            saved = self.complete()
            saved["records"][TARGETS[0]]["entry"][field] = "wrong"
            self.assertFalse(judge(self.fixture.initial, saved)["exact_entries"], field)

    def test_duplicate_attempt_cannot_hide_behind_deduplication(self):
        saved = self.complete()
        self.fixture.submit(TARGETS[0], expected_entry(self.fixture.initial[TARGETS[0]]))
        duplicate = json.loads(self.path.read_text())
        self.assertEqual(duplicate["records"], saved["records"])
        self.assertFalse(judge(self.fixture.initial, duplicate)["exactly_one_attempt"])
        self.assertTrue(duplicate["attempts"][-1]["duplicate"])

    def test_changed_unrelated_invoice_fails(self):
        saved = self.complete()
        saved["records"]["INV-001"]["supplier"] = "changed"
        self.assertFalse(judge(self.fixture.initial, saved)["unrelated_unchanged"])

    def test_missing_target_extra_attempt_and_bypass_fail(self):
        saved = self.complete()
        del saved["records"][TARGETS[0]]["entry"]
        self.assertFalse(judge(self.fixture.initial, saved)["exact_entries"])
        saved = self.complete()
        saved["bypass"].append("POST /submit")
        self.assertFalse(judge(self.fixture.initial, saved)["browser_only"])

    def test_only_browser_origin_can_submit(self):
        with self.fixture:
            body = json.dumps({"invoice_id": TARGETS[0], "entry": expected_entry(self.fixture.initial[TARGETS[0]])}).encode()
            request = urllib.request.Request(self.fixture.base + "/submit", data=body,
                                             headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
            self.assertEqual(self.fixture.attempt_count(), 0)
            self.assertTrue(json.loads(self.path.read_text())["bypass"])


class ProxyTests(unittest.TestCase):
    def test_response_gate_holds_after_upstream_finishes(self):
        import http.server
        finished = threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                finished.set()
                body = b'{"completed":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=upstream.serve_forever, daemon=True)
        worker.start()
        received = []
        try:
            with ResponseGate(f"http://127.0.0.1:{upstream.server_port}", lambda: int(finished.is_set())) as proxy:
                def request():
                    body = json.dumps({"tool": "batch", "args": {"operation_id": "pending-1"}}).encode()
                    received.append(urllib.request.urlopen(urllib.request.Request(proxy.url + "/api", data=body), timeout=5).read())
                caller = threading.Thread(target=request)
                caller.start()
                self.assertTrue(proxy.held.wait(3))
                self.assertTrue(finished.is_set())
                self.assertEqual(received, [])
                self.assertEqual(proxy.held_call["operation_id"], "pending-1")
                proxy.release.set()
                caller.join(3)
                self.assertEqual(received, [b'{"completed":true}'])
                self.assertTrue(proxy.read()[0]["response_withheld"])
        finally:
            upstream.shutdown()
            upstream.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
