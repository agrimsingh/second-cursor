"""Business outcome and HTTP-boundary tests; no browser, app or model is started."""
import contextlib
import copy
import http.client
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks.workspace_fixtures import WorkspaceFixture, WORKSPACE_TASKS, MAX_BODY, OWNERS


class WorkspaceTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, task="workspace_reconcile", seed=7):
        with tempfile.TemporaryDirectory() as folder:
            with WorkspaceFixture(task, seed, folder) as fixture:
                yield fixture

    def request_for(self, fixture, record_id=None, revision=1):
        expected = fixture._expected_state()
        if fixture.task == "workspace_handoff":
            record_id = record_id or fixture._private["ticket_ids"][0]
            row = next(row for row in expected["sections"]["tickets"] if row["id"] == record_id)
            return {"action": "update_ticket", "id": record_id, "revision": revision,
                    **{key: row[key] for key in ("status", "owner", "release", "note")}}
        record_id = record_id or fixture._private["invoice_id"]
        row = next(row for row in expected["sections"]["invoices"] if row["id"] == record_id)
        return {"action": "save_invoice", "id": record_id, "revision": revision,
                "amount_cents": row["approved_amount_cents"], "route": row["route"], "note": row["note"]}

    def complete(self, fixture):
        if fixture.task == "workspace_handoff":
            for rid in fixture._private["ticket_ids"]:
                self.assertEqual(fixture.action(self.request_for(fixture, rid))[0], 200)
        elif fixture.task == "workspace_recovery":
            request = self.request_for(fixture)
            status, payload = fixture.action(request)
            self.assertEqual((status, payload["error"]), (409, "stale_revision"))
            fixture.view("messages")
            fixture.view("invoices")
            status, payload = fixture.action({**request, "revision": 2})
            self.assertEqual((status, payload["error"]), (503, "response_lost"))
            self.assertTrue(payload["uncertain"])
            fixture.view("invoices")
        else:
            self.assertEqual(fixture.action(self.request_for(fixture))[0], 200)

    def http(self, fixture, method, path, value=None, headers=None, raw=None):
        parsed = urllib.parse.urlsplit(fixture.url)
        client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        data = json.dumps(value).encode() if value is not None else raw
        request_headers = {"Origin": fixture.origin, "Content-Type": "application/json", **(headers or {})}
        client.request(method, path, data, request_headers)
        response = client.getresponse()
        status, body = response.status, response.read()
        content_type = response.getheader("Content-Type", "").split(";", 1)[0]
        client.close()
        return status, (json.loads(body) if content_type == "application/json" else body) if body else None

    def test_correct_workflows_pass_across_seeds(self):
        for task in WORKSPACE_TASKS:
            for seed in (1, 37):
                with self.subTest(task=task, seed=seed), self.fixture(task, seed) as fixture:
                    self.complete(fixture)
                    self.assertTrue(all(fixture.judge().values()), fixture.judge())
                    self.assertEqual(fixture.diagnostics()["commits"], 2 if task == "workspace_handoff" else 1)

    def test_unchanged_baseline_never_passes(self):
        for task in WORKSPACE_TASKS:
            with self.subTest(task=task), self.fixture(task) as fixture:
                fixture.view("home")
                checks = fixture.judge()
                self.assertTrue(checks["durable_evidence_valid"])
                self.assertFalse(checks["exact_final_state"])
                self.assertFalse(checks["exact_committed_effects"])

    def test_seed_changes_actual_data_and_order_but_is_repeatable(self):
        cases = [WorkspaceFixture("workspace_reconcile", seed, "/unused") for seed in (1, 2, 1)]
        self.assertEqual(cases[0].baseline, cases[2].baseline)
        self.assertEqual(cases[0].prompt, cases[2].prompt)
        self.assertNotEqual(cases[0].baseline, cases[1].baseline)
        self.assertNotEqual(cases[0]._private["amount"], cases[1]._private["amount"])
        self.assertNotEqual(cases[0]._private["invoice_id"], cases[1]._private["invoice_id"])
        self.assertNotEqual(cases[0].case_metadata()["case_id"], cases[1].case_metadata()["case_id"])
        self.assertGreater(len(cases[0].baseline["sections"]["invoices"]), 1)

    def test_options_expose_full_roster_not_target_owner_order(self):
        with self.fixture("workspace_handoff") as fixture:
            owners = fixture.view("tickets")["options"]["owners"]
            self.assertEqual(owners, ["Unassigned", *sorted(OWNERS)])
            self.assertGreater(len(owners), len(fixture._private["owners"]) + 1)

    def test_wrong_amount_and_wrong_record_fail(self):
        with self.fixture() as fixture:
            request = self.request_for(fixture)
            fixture.action({**request, "amount_cents": request["amount_cents"] + 100})
            self.assertFalse(fixture.judge()["exact_final_state"])
        with self.fixture() as fixture:
            request = self.request_for(fixture)
            wrong = next(row["id"] for row in fixture.baseline["sections"]["invoices"] if row["id"] != request["id"])
            fixture.action({**request, "id": wrong})
            fixture.action(request)
            self.assertFalse(fixture.judge()["unrelated_records_untouched"])
            self.assertFalse(fixture.judge()["exact_committed_effects"])

    def test_safe_validation_retry_does_not_fail_success(self):
        with self.fixture() as fixture:
            request = self.request_for(fixture)
            self.assertEqual(fixture.action({**request, "note": ""})[0], 400)
            self.assertEqual(fixture.action(request)[0], 200)
            self.assertTrue(all(fixture.judge().values()))
            self.assertEqual(fixture.diagnostics(), {"write_attempts": 2, "commits": 1, "validation_refusals": 1, "stale_refusals": 0})

    def test_duplicate_postcommit_attempt_fails_even_when_stale_refused(self):
        with self.fixture() as fixture:
            request = self.request_for(fixture)
            fixture.action(request)
            self.assertEqual(fixture.action(request)[0], 409)
            checks = fixture.judge()
            self.assertTrue(checks["exact_final_state"])
            self.assertTrue(checks["exact_committed_effects"])
            self.assertFalse(checks["no_duplicate_submissions"])

    def test_handoff_requires_both_correct_records(self):
        with self.fixture("workspace_handoff") as fixture:
            fixture.action(self.request_for(fixture))
            self.assertFalse(fixture.judge()["exact_committed_effects"])
            self.assertFalse(fixture.judge()["exact_final_state"])

    def test_recovery_requires_conflict_then_current_state_reread(self):
        with self.fixture("workspace_recovery") as fixture:
            request = self.request_for(fixture)
            fixture.action(request)
            fixture.action({**request, "revision": 2})
            checks = fixture.judge()
            self.assertTrue(checks["exact_final_state"])
            self.assertTrue(checks["recovery_conflict_exercised"])
            self.assertFalse(checks["recovery_persisted_state_rechecked"])
            fixture.view("invoices")
            self.assertTrue(all(fixture.judge().values()))
            self.assertEqual(fixture.diagnostics()["stale_refusals"], 1)

    def test_guessed_future_revision_cannot_skip_conflict(self):
        with self.fixture("workspace_recovery") as fixture:
            status, payload = fixture.action(self.request_for(fixture, revision=2))
            self.assertEqual((status, payload["error"]), (409, "stale_revision"))
            self.assertEqual(fixture.diagnostics()["commits"], 0)

    def test_review_is_read_only_and_checks_current_revision(self):
        with self.fixture() as fixture:
            row = self.request_for(fixture)
            status, payload = fixture.action({key: row[key] for key in ("id", "revision")} | {"action": "review_invoice"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["review_revision"], 1)
            self.assertEqual(fixture.state, fixture.baseline)
            self.assertEqual(fixture.diagnostics()["write_attempts"], 0)

    def test_persisted_evidence_survives_server_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = WorkspaceFixture("workspace_reconcile", 7, folder)
            with fixture:
                self.complete(fixture)
                before = fixture.read()
            self.assertEqual(before, fixture.read())
            self.assertTrue(all(fixture.judge(before).values()))
            with self.assertRaises(ValueError):
                WorkspaceFixture("workspace_reconcile", 7, folder).__enter__()

    def test_state_and_ledger_corruption_fail_closed(self):
        for kind in ("state", "baseline", "ledger", "tail"):
            with self.subTest(kind=kind), self.fixture() as fixture:
                self.complete(fixture)
                if kind == "tail":
                    lines = fixture._ledger.read_text().splitlines()
                    fixture._ledger.write_text("\n".join(lines[:-1]) + "\n")
                elif kind == "ledger":
                    rows = fixture.read()["events"]
                    rows[0]["action"] = "forged"
                    fixture._ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                else:
                    fixture._paths[kind].write_text('{"corrupted":true}')
                self.assertFalse(all(fixture.judge().values()))

    def test_snapshot_judge_is_not_affected_by_later_reads(self):
        with self.fixture() as fixture:
            self.complete(fixture)
            snapshot = fixture.read()
            fixture.view("messages")
            self.assertTrue(all(fixture.judge(snapshot).values()))
            self.assertTrue(all(fixture.judge().values()))

    def test_http_api_exposes_business_views_but_no_evaluator(self):
        with self.fixture() as fixture:
            for section in ("home", "invoices", "orders", "messages", "policy", "tickets", "releases", "activity"):
                status, payload = self.http(fixture, "GET", "/api/view?section=" + section)
                self.assertEqual(status, 200)
                self.assertEqual(payload["section"], section)
                self.assertNotIn("expected", payload)
                self.assertNotIn("events", payload)
            for path in ("/api/judge", "/api/state", "/workspace-state.json", "/events", "/api/view?section=private", "/api/view?section=home&section=invoices"):
                self.assertEqual(self.http(fixture, "GET", path)[0], 404)

    def test_http_mutation_requires_same_origin_and_bounded_json(self):
        with self.fixture() as fixture:
            request = self.request_for(fixture)
            for headers in ({"Origin": "https://elsewhere.invalid"}, {"Host": "attacker.invalid"},
                            {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
                self.assertEqual(self.http(fixture, "POST", "/api/action", request, headers)[0], 403)
            self.assertEqual(self.http(fixture, "POST", "/api/action", request, {"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(self.http(fixture, "POST", "/api/action", raw=b"x" * (MAX_BODY + 1))[0], 413)
            self.assertEqual(self.http(fixture, "POST", "/api/action", raw=b'{"amount_cents":NaN}')[0], 400)
            self.assertEqual(fixture.state, fixture.baseline)
            self.assertEqual(self.http(fixture, "POST", "/api/action", request)[0], 200)
            self.assertTrue(all(fixture.judge().values()))

    def test_public_document_allows_cross_site_browser_navigation_only(self):
        with self.fixture() as fixture:
            navigation = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
            for site in ("cross-site", "same-site", "none", "same-origin"):
                with self.subTest(site=site):
                    status, body = self.http(fixture, "GET", "/fixture.html", headers={
                        **navigation, "Sec-Fetch-Site": site, "Origin": "chrome-extension://fixture-driver"})
                    self.assertEqual(status, 200)
                    self.assertIn(b"<!doctype html>", body.lower())
            # Loading the shell reads no business state and performs no action.
            self.assertEqual(fixture.read()["events"], [])
            self.assertEqual(fixture.state, fixture.baseline)

    def test_document_navigation_exception_cannot_bypass_api_or_host_checks(self):
        with self.fixture() as fixture:
            navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                          "Sec-Fetch-Dest": "document"}
            for headers in ({**navigation, "Host": "attacker.invalid"},
                            {**navigation, "Sec-Fetch-Mode": "cors"},
                            {**navigation, "Sec-Fetch-Dest": "iframe"},
                            {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(self.http(fixture, "GET", "/fixture.html", headers=headers)[0], 403)
            for path in ("/api/view?section=invoices", "/fixture.html?section=invoices",
                         "/api/action", "/workspace-state.json"):
                self.assertEqual(self.http(fixture, "GET", path, headers=navigation)[0], 403)
            request = self.request_for(fixture)
            self.assertEqual(self.http(fixture, "POST", "/api/action", request, navigation)[0], 403)
            self.assertEqual(self.http(fixture, "POST", "/api/action", request, {
                **navigation, "Sec-Fetch-Site": "same-origin", "Origin": "null"})[0], 403)
            self.assertEqual(fixture.read()["events"], [])
            self.assertEqual(fixture.state, fixture.baseline)

    def test_bounded_field_validation_prevents_untyped_values(self):
        changes = ({"amount_cents": True}, {"amount_cents": -1}, {"amount_cents": 10 ** 10},
                   {"revision": True}, {"note": "x" * 501}, {"extra": "not allowed"}, {"route": "anything"},
                   {"action": []}, {"action": {}}, {"route": []}, {"route": {}}, {"note": []})
        with self.fixture() as fixture:
            request = self.request_for(fixture)
            for change in changes:
                self.assertEqual(fixture.action({**request, **change})[0], 400)
            self.assertEqual(fixture.state, fixture.baseline)
            fixture.action(request)
            self.assertTrue(all(fixture.judge().values()))


if __name__ == "__main__":
    unittest.main()
