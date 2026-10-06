"""Negative controls for the real workspace's no-save regression judge."""
import copy
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from benchmarks.workspace_fixtures import WorkspaceFixture
from browser_expect_workspace import selection_matches,unchanged_checks


class WorkspaceUnchangedTests(unittest.TestCase):
    def fixture(self):
        directory=self.enterContext(tempfile.TemporaryDirectory())
        return self.enterContext(WorkspaceFixture('workspace_reconcile',401,directory))

    def checks(self,fixture,evidence=None,expected_review_id=None):
        return unchanged_checks(evidence or fixture.read(),fixture.baseline,
            {'event_count':len(fixture.events),'head':fixture.events[-1]['hash'] if fixture.events else '0'*64},expected_review_id)

    def test_record_reads_preserve_complete_persisted_state(self):
        fixture=self.fixture();fixture.view('invoices');fixture.view('invoices')
        self.assertTrue(all(self.checks(fixture).values()))

    def test_even_rejected_save_attempt_fails_no_write_contract(self):
        fixture=self.fixture();fixture.view('invoices');fixture.action({'action':'save_invoice'})
        checks=self.checks(fixture)
        self.assertTrue(checks['persisted_records_unchanged'])
        self.assertFalse(checks['only_public_record_reads_and_review']);self.assertFalse(checks['no_save_attempts_or_commits'])

    def test_persisted_change_cannot_pass_with_read_only_ledger(self):
        fixture=self.fixture();fixture.view('invoices');evidence=fixture.read()
        evidence['state']['sections']['invoices'][0]['note']='unrequested persisted edit'
        self.assertFalse(self.checks(fixture,evidence)['persisted_records_unchanged'])

    def test_old_receipt_cannot_hide_a_later_attempt(self):
        fixture=self.fixture();fixture.view('invoices');earlier=fixture.read()
        fixture.action({'action':'save_invoice'})
        self.assertFalse(self.checks(fixture,earlier)['current_complete_evidence'])

    def test_tampered_ledger_is_rejected(self):
        fixture=self.fixture();fixture.view('invoices');evidence=fixture.read()
        evidence['events'][0]['section']='policy'
        self.assertFalse(self.checks(fixture,evidence)['ledger_hashes_valid'])

    def test_selection_needs_both_literal_value_and_label(self):
        observed={'text':'finance_review','options':[{'value':'finance_review','label':'Finance review','selected':True},
            {'value':'hold','label':'Hold','selected':False}]}
        self.assertTrue(selection_matches(observed,'finance_review','Finance review'))
        self.assertFalse(selection_matches(observed,'Finance review','Finance review'))
        self.assertFalse(selection_matches(observed,'finance_review','Standard'))
        missing=copy.deepcopy(observed);missing['options']=[]
        self.assertFalse(selection_matches(missing,'finance_review','Finance review'))

    def test_exact_read_only_review_is_permitted_without_record_changes(self):
        fixture=self.fixture();fixture.view('invoices');invoice=fixture.baseline['sections']['invoices'][0]
        status,result=fixture.action({'action':'review_invoice','id':invoice['id'],'revision':invoice['revision']})
        self.assertEqual(status,200);self.assertTrue(result['ok'])
        self.assertTrue(all(self.checks(fixture,expected_review_id=invoice['id']).values()))

    def test_missing_or_duplicate_review_cannot_claim_modal_coverage(self):
        fixture=self.fixture();fixture.view('invoices');invoice=fixture.baseline['sections']['invoices'][0]
        self.assertFalse(self.checks(fixture,expected_review_id=invoice['id'])['expected_read_only_review'])
        request={'action':'review_invoice','id':invoice['id'],'revision':invoice['revision']}
        fixture.action(request);fixture.action(request)
        self.assertFalse(self.checks(fixture,expected_review_id=invoice['id'])['expected_read_only_review'])

    def test_reviewing_a_different_record_is_not_the_expected_review(self):
        fixture=self.fixture();fixture.view('invoices');first,second=fixture.baseline['sections']['invoices'][:2]
        fixture.action({'action':'review_invoice','id':second['id'],'revision':second['revision']})
        self.assertFalse(self.checks(fixture,expected_review_id=first['id'])['expected_read_only_review'])


if __name__=='__main__':unittest.main()
