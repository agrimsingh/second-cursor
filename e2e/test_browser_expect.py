"""Prove the independent judge rejects wrong and duplicated effects without GUI."""
import copy
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import browser_expect
from browser_expect import ExpectFixture, LONG_VALUE, ToolError, canonical, dismiss_open_dialog, focused_effect_checks, parser, plan, protected_refusal, summarize


class FixtureTests(unittest.TestCase):
    def fixture(self,case='delayed_save',seed=401):
        directory=self.enterContext(tempfile.TemporaryDirectory())
        return self.enterContext(ExpectFixture(case,seed,directory))

    def request(self,fixture,**changes):
        return {'id':fixture.record_id,'revision':fixture.state['target']['revision'],
                'note':fixture.approved_note,**changes}

    def test_exact_commit_passes_and_rereads_persisted_state(self):
        fixture=self.fixture()
        self.assertTrue(fixture.save(self.request(fixture))['ok'])
        self.assertTrue(all(fixture.judge().values()))
        state_path=fixture.dir/'state.json'; state=json.loads(state_path.read_text())
        state['target']['note']='tampered'; state_path.write_text(canonical(state))
        self.assertFalse(fixture.judge()['ledger_replays_to_state'])

    def test_correct_success_text_cannot_rescue_wrong_saved_note(self):
        fixture=self.fixture()
        self.assertTrue(fixture.save(self.request(fixture,note='Wrong approved note'))['ok'])
        checks=fixture.judge()
        self.assertTrue(checks['durable_evidence_valid'])
        self.assertTrue(checks['exact_commit_count'])
        self.assertFalse(checks['exact_final_state'])

    def test_second_submission_fails_even_when_server_refuses_stale_revision(self):
        fixture=self.fixture(); request=self.request(fixture)
        fixture.save(request); fixture.save(request)
        checks=fixture.judge()
        self.assertTrue(checks['exact_final_state'])
        self.assertTrue(checks['exact_commit_count'])
        self.assertFalse(checks['exact_submission_count'])
        self.assertFalse(checks['no_submission_after_commit'])

    def test_old_successful_snapshot_cannot_hide_a_later_duplicate(self):
        fixture=self.fixture(); request=self.request(fixture)
        fixture.save(request); earlier=fixture.evidence()
        self.assertTrue(all(fixture.judge(earlier).values()))
        fixture.save(request)
        self.assertFalse(fixture.judge(earlier)['durable_evidence_valid'])

    def test_stale_conflict_requires_new_revision_and_corrected_note(self):
        fixture=self.fixture('stale_form')
        first=fixture.save(self.request(fixture))
        self.assertEqual(first['error'],'stale_revision')
        self.assertEqual(fixture.view()['target']['revision'],2)
        fixture.save(self.request(fixture))
        self.assertTrue(all(fixture.judge().values()))

    def test_stale_correct_revision_with_old_note_is_still_wrong(self):
        fixture=self.fixture('stale_form'); old=fixture.approved_note
        fixture.save(self.request(fixture)); fixture.view()
        fixture.save(self.request(fixture,note=old))
        checks=fixture.judge()
        self.assertTrue(checks['stale_conflict_exercised'])
        self.assertFalse(checks['exact_final_state'])

    def test_stale_retry_without_fresh_public_read_fails(self):
        fixture=self.fixture('stale_form')
        fixture.save(self.request(fixture)); fixture.save(self.request(fixture))
        self.assertTrue(fixture.judge()['exact_final_state'])
        self.assertFalse(fixture.judge()['fresh_revision_read_before_retry'])

    def test_pending_decoy_never_commits(self):
        fixture=self.fixture('tab_decoy')
        self.assertEqual(fixture.save(self.request(fixture))['error'],'pending')
        self.assertTrue(all(fixture.judge().values()))

    def test_mutated_or_recomputed_ledger_rejected(self):
        fixture=self.fixture(); fixture.save(self.request(fixture))
        evidence=fixture.evidence(); evidence['events'][0]['request']['note']='forged'
        self.assertFalse(fixture.judge(evidence)['durable_evidence_valid'])
        evidence=fixture.evidence(); evidence['events'].pop(); evidence['receipt']['event_count']-=1
        # A shortened claim may be an older valid snapshot, but its state cannot
        # falsely match after removing the commit itself.
        evidence['events']=evidence['events'][:1]
        evidence['receipt']={'event_count':1,'head':evidence['events'][0]['hash']}
        self.assertFalse(fixture.judge(evidence)['ledger_replays_to_state'])

    def test_unrelated_mutation_is_not_hidden_by_correct_target(self):
        fixture=self.fixture(); fixture.save(self.request(fixture))
        changed=copy.deepcopy(fixture.state); changed['unrelated']['note']='collateral change'
        with fixture.lock: fixture._transition('commit',changed,request=self.request(fixture))
        self.assertFalse(fixture.judge()['unrelated_record_untouched'])

    def test_http_serves_fixture_and_refuses_cross_origin_write(self):
        fixture=self.fixture()
        with urllib.request.urlopen(fixture.url) as response:
            html=response.read().decode()
        self.assertIn('Review before saving',html)
        self.assertIn('await delay(config.delay_ms)',html)
        self.assertNotIn('judge(',html)
        request=urllib.request.Request(fixture.origin+'/save',data=canonical(self.request(fixture)).encode(),
                                       headers={'Content-Type':'application/json','Origin':'https://unrelated.example'})
        with self.assertRaises(urllib.error.HTTPError) as raised: urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code,403)
        raised.exception.close()
        self.assertEqual(fixture.evidence()['events'],[])

    def test_http_save_records_one_durable_attempt_and_commit(self):
        fixture=self.fixture()
        request=urllib.request.Request(fixture.origin+'/save',data=canonical(self.request(fixture)).encode(),
                                       headers={'Content-Type':'application/json','Origin':fixture.origin})
        with urllib.request.urlopen(request) as response: self.assertTrue(json.load(response)['ok'])
        self.assertTrue(all(fixture.judge().values()))


class PlanTests(unittest.TestCase):
    def test_both_paths_have_identical_workflow_seed_schedule(self):
        poll=plan(parser().parse_args(['--mode','poll']))
        expect=plan(parser().parse_args(['--mode','expect']))
        self.assertEqual(poll,expect); self.assertEqual(len(poll),12)

    def test_baseline_cannot_silently_drop_expect_contract_cases(self):
        with self.assertRaises(ValueError): plan(parser().parse_args(['--mode','poll','--focused']))
        with self.assertRaises(ValueError): plan(parser().parse_args(['--mode','poll','--cases','never']))

    def test_focused_cases_run_once_instead_of_inflating_cohort(self):
        entries=plan(parser().parse_args(['--cases','preexisting','never','--repetitions','3']))
        self.assertEqual(len(entries),2)

    def test_summary_retains_failed_effects_and_missing_measurements(self):
        rows=[{'case':'delayed_save','application_effects_passed':True,'scenario_checks_passed':True,
               'verified_task_ms':1500,'public_calls':1},
              {'case':'delayed_save','application_effects_passed':False,'scenario_checks_passed':False}]
        result=summarize(rows)['delayed_save']
        self.assertEqual(result['trials'],2); self.assertEqual(result['application_effects_passed'],1)
        self.assertEqual(result['public_calls'],[1])

    def test_final_manifest_uses_bundle_saved_before_run_cleans_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact=pathlib.Path(directory)
            bundle=artifact/'Owned.app'
            class FakeRun:
                def __init__(self,**kwargs):
                    self.dir=artifact; self.instance=types.SimpleNamespace(app=bundle)
                    self.notes=[]; self.results=[]; self.browser_runtime={'test':True}
                def run(self,_):
                    self.instance=None
                    self.results=[{'status':'PASS'}]
                    (self.dir/'browser-expect.json').write_text('{}')
                    return True
            with mock.patch.object(browser_expect,'Run',FakeRun), mock.patch.object(browser_expect,'manifest',return_value={'frozen':True}) as identity:
                self.assertEqual(browser_expect.main(['--takeover','--cases','delayed_save','--repetitions','1']),0)
                self.assertEqual(identity.call_args_list,[mock.call(bundle),mock.call(bundle)])
            self.assertFalse(json.loads((artifact/'browser-expect.json').read_text())['source_changed'])


class IndependentUIReceiptTests(unittest.TestCase):
    def test_fake_verified_outcome_cannot_prove_typed_or_keyboard_effect(self):
        self.assertFalse(all(focused_effect_checks('type_value',{'typing':'','events':[]}).values()))
        self.assertFalse(all(focused_effect_checks('press_key',{'status':'Ready for review.','events':[]}).values()))

    def test_long_values_require_full_value_and_one_observed_action(self):
        self.assertTrue(all(focused_effect_checks('long_value',{'value':LONG_VALUE,'events':['long']}).values()))
        self.assertFalse(all(focused_effect_checks('long_value',{'value':LONG_VALUE[:1000],'events':['long']}).values()))
        self.assertFalse(all(focused_effect_checks('long_value',{'value':LONG_VALUE,'events':[]}).values()))

    def test_secret_ref_refusal_cannot_have_dispatched_the_action(self):
        self.assertTrue(all(focused_effect_checks('secret_ref',{'events':[]},refused=True).values()))
        self.assertFalse(all(focused_effect_checks('secret_ref',{'events':['noop']},refused=True).values()))

    def test_navigation_needs_destination_and_rendered_status(self):
        self.assertTrue(all(focused_effect_checks('navigation',{'url':'http://127.0.0.1/browser-expect.html?landed=1','status':'Navigation complete'}).values()))
        self.assertFalse(all(focused_effect_checks('navigation',{'url':'http://127.0.0.1/browser-expect.html','status':'Navigation complete'}).values()))

    def test_globally_unavailable_condition_reader_does_not_pass_privacy(self):
        error='expect could not be checked before input; input was not sent (unavailable)'
        self.assertFalse(protected_refusal(error,False))
        self.assertTrue(protected_refusal(error,True))
        self.assertFalse(protected_refusal('Invalid schema',True))

    def test_missing_dialog_does_not_attempt_dismissal(self):
        driver=mock.Mock(); driver.call.return_value={'dialog':None}
        self.assertEqual(dismiss_open_dialog(driver),{'opened':False,'dismissed':False,'error':None})
        driver.call.assert_called_once_with('browser_command',operation='dialog')

    def test_open_dialog_is_explicitly_dismissed(self):
        driver=mock.Mock(); driver.call.side_effect=[{'dialog':{'type':'confirm'}},{'ok':True}]
        self.assertEqual(dismiss_open_dialog(driver),{'opened':True,'dismissed':True,'error':None})
        self.assertEqual(driver.call.call_args_list,[mock.call('browser_command',operation='dialog'),
                                                     mock.call('browser_command',operation='dialog',accept=False)])

    def test_dialog_cleanup_failure_is_data_and_does_not_replace_primary_failure(self):
        driver=mock.Mock(); driver.call.side_effect=ToolError('transport interrupted')
        result=dismiss_open_dialog(driver)
        self.assertEqual(result,{'opened':False,'dismissed':False,'error':'transport interrupted'})


if __name__=='__main__': unittest.main()
