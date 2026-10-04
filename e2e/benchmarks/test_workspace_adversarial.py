"""Independent falsification of persisted workspace judges; no model or GUI runs.

Golden edits are derived from the same public business documents a user reads,
not the fixture's private expected-state helper. Adversarial edits deliberately
tamper with evidence only inside a disposable test fixture.
"""
import copy
import hashlib
import pathlib
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks.workspace_fixtures import WORKSPACE_TASKS, WorkspaceFixture, canonical


@contextmanager
def fixture(task="workspace_reconcile", seed=1):
    with tempfile.TemporaryDirectory() as directory, WorkspaceFixture(task, seed, directory) as value:
        yield value


def cents(text):
    return int(Decimal(text) * 100)


def invoice_edit(server):
    records = server.read()["state"]["sections"]
    invoice_id = re.search(r"invoice (INV-\d+)", server.prompt)[1]
    row = next(item for item in records["invoices"] if item["id"] == invoice_id)
    order = next(item for item in records["orders"] if item["id"] == row["order_id"])
    latest = max((item for item in records["messages"] if item["record_id"] == order["id"]),
                 key=lambda item: item["sent_at"])
    accepted = int(re.search(r"confirmed (\d+) units", latest["body"])[1])
    credit = cents(re.search(r"credit of \$(\d+\.\d{2})", latest["body"])[1])
    memo = re.search(r"memo: ([\w-]+)", latest["body"])[1]
    policy = next(item["body"] for item in records["policy"] if item["id"] == "POL-INVOICE")
    threshold = cents(re.search(r"amounts of \$(\d+\.\d{2})", policy)[1])
    amount = accepted * order["unit_price_cents"] + min(row["shipping_cents"], order["delivery_cap_cents"]) - credit
    return {"action": "save_invoice", "id": invoice_id, "revision": row["revision"],
            "amount_cents": amount, "route": "finance_review" if amount >= threshold else "standard", "note": memo}


def handoff_edits(server):
    records = server.read()["state"]["sections"]
    policy = next(item["body"] for item in records["policy"] if item["id"] == "POL-HANDOFF")
    stable = next(item["version"] for item in records["releases"] if item["stage"] == "Stable")
    for ticket_id in re.findall(r"TKT-\d+", server.prompt):
        row = next(item for item in records["tickets"] if item["id"] == ticket_id)
        latest = max((item for item in records["messages"] if item["record_id"] == ticket_id),
                     key=lambda item: item["sent_at"])
        ready = "ready-for-verification" in latest["body"]
        owner = re.search(r"owner ([^.]+)\.", policy if ready else policy.split("For scheduled onboarding", 1)[1])[1]
        memo = re.search(r"memo: ([\w-]+)", latest["body"])[1]
        yield {"action": "update_ticket", "id": ticket_id, "revision": row["revision"],
               "status": "Ready for verification" if ready else "Scheduled", "owner": owner,
               "release": stable, "note": memo}


def complete(server, reread=True):
    if server.task == "workspace_handoff":
        for edit in list(handoff_edits(server)):
            assert server.action(edit)[0] == 200
    else:
        status, _ = server.action(invoice_edit(server))
        if server.task == "workspace_recovery":
            assert status == 409
            assert server.action(invoice_edit(server))[0] == 503
            if reread:
                server.view("invoices")
        else:
            assert status == 200


def rechain(evidence):
    previous = "0" * 64
    for seq, event in enumerate(evidence["events"], 1):
        event.update(seq=seq, previous_hash=previous)
        event.pop("hash", None)
        event["hash"] = hashlib.sha256(canonical(event).encode()).hexdigest()
        previous = event["hash"]
    evidence["receipt"] = {"event_count": len(evidence["events"]), "head": previous}


class WorkspaceAdversarialTests(unittest.TestCase):
    def assert_passes(self, server):
        checks = server.judge()
        self.assertTrue(checks and all(checks.values()), checks)

    def assert_fails(self, server, evidence=None):
        checks = server.judge(evidence)
        self.assertFalse(all(checks.values()), checks)
        return checks

    def test_untouched_baselines_cannot_pass_any_workflow(self):
        for task in WORKSPACE_TASKS:
            with self.subTest(task=task), fixture(task) as server:
                self.assertFalse(self.assert_fails(server)["exact_final_state"])

    def test_public_documents_solve_distinct_seeded_cases(self):
        observed = set()
        for task in WORKSPACE_TASKS:
            for seed in (1, 8):
                with self.subTest(task=task, seed=seed), fixture(task, seed) as server:
                    observed.add(server.case_metadata()["case_id"])
                    complete(server)
                    self.assert_passes(server)
        self.assertEqual(len(observed), 6)

    def test_partial_handoff_and_wrong_release_do_not_pass(self):
        with fixture("workspace_handoff") as server:
            edits = list(handoff_edits(server))
            server.action(edits[0])
            self.assertFalse(self.assert_fails(server)["exact_committed_effects"])
            preview = next(item["version"] for item in server.read()["state"]["sections"]["releases"] if item["stage"] == "Preview")
            server.action({**edits[1], "release": preview})
            self.assertFalse(self.assert_fails(server)["exact_final_state"])

    def test_validation_retry_before_any_effect_is_allowed(self):
        with fixture() as server:
            edit = invoice_edit(server)
            self.assertEqual(server.action({**edit, "amount_cents": "not money"})[0], 400)
            self.assertEqual(server.action({**edit, "note": ""})[0], 400)
            self.assertEqual(server.action(edit)[0], 200)
            self.assert_passes(server)
            self.assertEqual(server.diagnostics()["validation_refusals"], 2)

    def test_rejected_write_to_unrelated_record_still_fails_preservation(self):
        with fixture() as server:
            edit = invoice_edit(server)
            wrong_id = next(item["id"] for item in server.read()["state"]["sections"]["invoices"] if item["id"] != edit["id"])
            self.assertEqual(server.action({**edit, "id": wrong_id, "note": ""})[0], 400)
            server.action(edit)
            checks = self.assert_fails(server)
            self.assertTrue(checks["exact_final_state"])
            self.assertFalse(checks["unrelated_records_untouched"])

    def test_correct_values_on_wrong_record_are_not_credit(self):
        with fixture() as server:
            edit = invoice_edit(server)
            wrong_id = next(item["id"] for item in server.read()["state"]["sections"]["invoices"] if item["id"] != edit["id"])
            self.assertEqual(server.action({**edit, "id": wrong_id})[0], 200)
            checks = self.assert_fails(server)
            self.assertFalse(checks["exact_committed_effects"])
            self.assertFalse(checks["unrelated_records_untouched"])

    def test_wrong_committed_amount_cannot_be_hidden_by_repair(self):
        with fixture() as server:
            edit = invoice_edit(server)
            server.action({**edit, "amount_cents": edit["amount_cents"] + 1})
            server.action({**edit, "revision": 2})
            checks = self.assert_fails(server)
            self.assertFalse(checks["exact_committed_effects"])
            self.assertFalse(checks["no_duplicate_submissions"])

    def test_replayed_save_after_success_is_rejected_by_judge_even_without_second_effect(self):
        with fixture() as server:
            edit = invoice_edit(server)
            server.action(edit)
            self.assertEqual(server.action(edit)[0], 409)
            checks = self.assert_fails(server)
            self.assertTrue(checks["exact_final_state"])
            self.assertFalse(checks["no_duplicate_submissions"])

    def test_recovery_requires_persisted_invoice_reread_after_lost_response(self):
        with fixture("workspace_recovery") as server:
            complete(server, reread=False)
            checks = self.assert_fails(server)
            self.assertTrue(checks["exact_final_state"])
            self.assertTrue(checks["recovery_conflict_exercised"])
            self.assertFalse(checks["recovery_persisted_state_rechecked"])
            server.view("messages")
            self.assertFalse(self.assert_fails(server)["recovery_persisted_state_rechecked"])
            server.view("invoices")
            self.assert_passes(server)

    def test_recovery_reusing_pre_update_answer_fails(self):
        with fixture("workspace_recovery") as server:
            stale = invoice_edit(server)
            self.assertEqual(server.action(stale)[0], 409)
            self.assertEqual(server.action({**stale, "revision": 2})[0], 503)
            server.view("invoices")
            checks = self.assert_fails(server)
            self.assertTrue(checks["recovery_conflict_exercised"])
            self.assertFalse(checks["exact_final_state"])

    def test_forged_and_reordered_ledger_cannot_override_server_receipt(self):
        with fixture("workspace_recovery") as server:
            complete(server)
            good = server.read()
            self.assert_passes(server)
            reorder = copy.deepcopy(good)
            reorder["events"][0], reorder["events"][1] = reorder["events"][1], reorder["events"][0]
            rechain(reorder)
            self.assertFalse(self.assert_fails(server, reorder)["durable_evidence_valid"])
            missing_update = copy.deepcopy(good)
            missing_update["events"] = [event for event in missing_update["events"] if event["kind"] != "system_revision"]
            rechain(missing_update)
            self.assertFalse(self.assert_fails(server, missing_update)["durable_evidence_valid"])

    def test_truncated_disk_tail_cannot_hide_post_success_replay(self):
        with fixture() as server:
            edit = invoice_edit(server)
            server.action(edit)
            good = server.read()
            server.action(edit)
            server._ledger.write_text("".join(canonical(event) + "\n" for event in good["events"]))
            server._write(server._paths["state"], good["state"])
            self.assertFalse(self.assert_fails(server)["durable_evidence_valid"])

    def test_conflicting_case_metadata_and_cross_seed_evidence_fail(self):
        with fixture(seed=1) as first, fixture(seed=2) as second:
            complete(first)
            good = first.read()
            conflicting = copy.deepcopy(good)
            conflicting["case_metadata"]["seed"] = 2
            self.assertFalse(self.assert_fails(first, conflicting)["durable_evidence_valid"])
            self.assertFalse(self.assert_fails(second, good)["durable_evidence_valid"])

    def test_overwriting_saved_state_without_commit_evidence_cannot_pass(self):
        with fixture() as server:
            complete(server)
            evidence = server.read()
            evidence["events"] = []
            evidence["receipt"] = {"event_count": 0, "head": "0" * 64}
            checks = self.assert_fails(server, evidence)
            self.assertFalse(checks["ledger_replays_to_saved_state"])
            self.assertFalse(checks["exact_committed_effects"])


if __name__ == "__main__":
    unittest.main()
