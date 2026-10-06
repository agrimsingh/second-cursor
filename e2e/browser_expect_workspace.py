#!/usr/bin/env python3
"""Exercise browser expectations on the real messy-workspace native form shape.

This is a focused integration regression, not a model or latency benchmark.
Only public MCP tools actuate the fixture. Independent rendered-control reads
check drafts; the persisted ledger permits reads and one read-only review, with
no save attempts or changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re

from best_in_class import digest, extension_identity
from benchmarks.workspace_fixtures import WorkspaceFixture
from browser_expect import Driver, ref, write_json
from run import Run, SCENARIOS

ROOT=pathlib.Path(__file__).resolve().parent
VERSION='browser-expect-workspace/2'
AMOUNT='42.50'
NOTE='Unsaved native form coverage check.'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def fingerprints(bundle=None):
    names=('browser_expect_workspace.py','browser_expect.py','best_in_class.py','run.py','observe.swift',
           'start-browser.py','benchmarks/workspace_fixtures.py','benchmarks/workspace.html')
    value={'version':VERSION,'source_sha256':{name:digest(ROOT/name) for name in names},
           'extension':extension_identity()}
    if bundle is not None:
        value.update(app_bundle=str(bundle),app_binary_sha256=digest(bundle/'Contents/MacOS/SecondCursor'))
    return value


def unchanged_checks(evidence,baseline,current_receipt,expected_review_id=None):
    """Independently validate the persisted read-only ledger, not task completion."""
    checks=dict.fromkeys(('current_complete_evidence','ledger_hashes_valid','persisted_records_unchanged',
                         'only_public_record_reads_and_review','expected_read_only_review',
                         'no_save_attempts_or_commits'),False)
    try:
        events=evidence['events']; previous='0'*64
        checks['current_complete_evidence']=(evidence['baseline']==baseline and evidence['receipt']==current_receipt
            and current_receipt=={'event_count':len(events),'head':events[-1]['hash'] if events else previous})
        if not checks['current_complete_evidence']: return checks
        for sequence,event in enumerate(events,1):
            unsigned={key:value for key,value in event.items() if key!='hash'}
            if (event['seq']!=sequence or event['previous_hash']!=previous
                    or event['hash']!=hashlib.sha256(canonical(unsigned).encode()).hexdigest()): return checks
            previous=event['hash']
        checks['ledger_hashes_valid']=True
        checks['persisted_records_unchanged']=evidence['state']==baseline
        reviews=[event for event in events if event['kind']=='review']
        results=[event for event in events if event['kind']=='result']
        review_ok=not reviews and not results if expected_review_id is None else False
        if expected_review_id is not None:
            record=next(row for row in baseline['sections']['invoices'] if row['id']==expected_review_id)
            review_ok=(len(reviews)==len(results)==1 and reviews[0].get('action')=='review_invoice'
                and reviews[0].get('record_id')==expected_review_id and reviews[0].get('request')=={
                    'action':'review_invoice','id':expected_review_id,'revision':record['revision']}
                and results[0].get('action')=='review_invoice' and results[0].get('record_id')==expected_review_id
                and results[0].get('outcome')=='reviewed' and reviews[0]['seq']<results[0]['seq'])
        checks['expected_read_only_review']=review_ok
        checks['only_public_record_reads_and_review']=bool(events) and review_ok and all(
            event['kind'] in {'view','review','result'} for event in events)
        checks['no_save_attempts_or_commits']=not any(event['kind'] in {'attempt','commit','system_revision'} for event in events)
        return checks
    except (KeyError,TypeError,ValueError,AttributeError,StopIteration): return checks


def selection_matches(readback,value,label):
    """Check both HTML option value and label through the public inspect read."""
    selected=[option for option in readback.get('options',[]) if option.get('selected') is True]
    return (readback.get('text')==value and len(selected)==1
            and selected[0].get('value')==value and selected[0].get('label')==label)


def require_status(ctx,result,status,label,reason=None):
    actual=result.get('expect',{})
    ctx.require(label,actual.get('status')==status and (reason is None or actual.get('reason')==reason),actual)


def exercise(ctx,fixture,driver):
    invoice=re.search(r'invoice (INV-\d+)',fixture.prompt).group(1)
    driver.call('browser_command',operation='wait',locator={'text':'Loading workspace…','exact':True},state='hidden',timeout_ms=5000)
    initial=driver.snapshot()
    driver.call('browser_click',ref=ref(initial['outline'],'button','Invoices'))
    driver.call('browser_command',operation='wait',locator={'role':'button','name':'Open invoice '+invoice[4:],'exact':True},
                state='visible',timeout_ms=5000)
    listing=driver.snapshot()
    opened=driver.call('browser_click',ref=ref(listing['outline'],'button','Open invoice '+invoice[4:]),
                       expect={'text':invoice,'timeout_ms':2000})
    require_status(ctx,opened,'verified','opening the observed invoice verifies text with native SELECT and empty fields present')
    detail=driver.inspect('#record-detail')
    ctx.require('the separate public detail read identifies the assigned invoice',
                re.search(r'Invoice ID:\s*'+re.escape(invoice)+r'\b',detail.get('text','')) is not None,detail)
    before={key:driver.inspect(selector) for key,selector in
            (('amount','#approved-amount'),('note','#review-note'),('decision','#invoice-decision'))}
    ctx.require('the real amount and multiline note both start empty',before['amount'].get('text')=='' and before['note'].get('text')=='',before)
    ctx.require('the real native decision SELECT exposes its options',
                before['decision'].get('role')=='combobox' and len(before['decision'].get('options',[]))==4,before['decision'])
    form=driver.snapshot(); outline=form['outline']
    amount_ref=ref(outline,'textbox','Approved amount ($)'); note_ref=ref(outline,'textbox','Review note')
    typed=driver.call('browser_type',ref=amount_ref,text=AMOUNT,
        expect={'text':invoice,'ref':amount_ref,'value':AMOUNT,'timeout_ms':1500})
    require_status(ctx,typed,'verified','typing verifies the empty amount while SELECT and empty TEXTAREA remain present')
    ctx.require('the public amount read independently sees the exact draft',driver.inspect('#approved-amount').get('text')==AMOUNT)
    typed_note=driver.call('browser_type',ref=note_ref,text=NOTE,
        expect={'ref':note_ref,'value':NOTE,'timeout_ms':1500})
    require_status(ctx,typed_note,'verified','typing verifies the initially empty native TEXTAREA')
    ctx.require('the public note read independently sees the exact draft',driver.inspect('#review-note').get('text')==NOTE)
    driver.call('browser_command',operation='select_option',locator={'label':'Decision','exact':True},value='finance_review')
    literal=driver.inspect('#invoice-decision')
    ctx.require('selecting by literal option value preserves its visible label',selection_matches(literal,'finance_review','Finance review'),literal)
    selected=driver.call('browser_click',ref=amount_ref,expect={'text':'Finance review','timeout_ms':0})
    require_status(ctx,selected,'preexisting','the selected native option label remains readable to conditions')
    unselected=driver.call('browser_click',ref=amount_ref,expect={'text':'Hold','timeout_ms':0})
    require_status(ctx,unselected,'preexisting','the unselected Hold option remains readable beyond the combobox value')
    not_gone=driver.call('browser_click',ref=amount_ref,expect={'text':'Hold','gone':True,'timeout_ms':150})
    require_status(ctx,not_gone,'failed','a present unselected option cannot satisfy a gone condition','timeout')
    driver.call('browser_command',operation='select_option',locator={'label':'Decision','exact':True},value='Standard')
    labelled=driver.inspect('#invoice-decision')
    ctx.require('selecting by visible label resolves its exact literal value',selection_matches(labelled,'standard','Standard'),labelled)
    current=driver.snapshot()
    reviewed=driver.call('browser_click',ref=ref(current['outline'],'button','Review invoice'),
        expect={'text':'Review invoice update','timeout_ms':2500})
    modal=driver.inspect('#review-dialog')
    ctx.require('the separate public read sees the actual review modal with the exact draft',
                modal.get('visible') is True and all(text in modal.get('text','') for text in
                    ('Review invoice update',invoice,'$'+AMOUNT,'Standard',NOTE)),modal)
    require_status(ctx,reviewed,'verified','opening the actual workspace review modal verifies its heading')
    current=driver.snapshot()
    dismissed=driver.call('browser_click',ref=ref(current['outline'],'button','Back to editing'),
        expect={'text':'Review invoice update','gone':True,'timeout_ms':1500})
    hidden_modal=driver.inspect('#review-dialog')
    ctx.require('the independent dialog read confirms dismissal',hidden_modal.get('visible') is False,hidden_modal)
    require_status(ctx,dismissed,'verified','dismissing the review modal verifies the heading is gone')
    drafts={key:driver.inspect(selector) for key,selector in
            (('amount','#approved-amount'),('note','#review-note'),('decision','#invoice-decision'))}
    ctx.require('dismissing review preserves every unsaved draft field',drafts['amount'].get('text')==AMOUNT
                and drafts['note'].get('text')==NOTE and selection_matches(drafts['decision'],'standard','Standard'),drafts)
    current=driver.snapshot()
    reloaded=driver.call('browser_click',ref=ref(current['outline'],'button','Reload invoice'),
        expect={'text':'Invoice reloaded from saved records. Unsaved edits were replaced.','timeout_ms':2500})
    require_status(ctx,reloaded,'verified','reloading verifies current text after replacing SELECT and empty form nodes')
    after={key:driver.inspect(selector) for key,selector in
           (('amount','#approved-amount'),('note','#review-note'),('decision','#invoice-decision'))}
    ctx.require('reloading restores both empty fields and unselected decision',
                all(after[key].get('text')=='' for key in after),after)
    return {'invoice':invoice,'before':before,'literal_selection':literal,'label_selection':labelled,
            'review_modal':modal,'dismissed_modal':hidden_modal,'drafts_after_dismissal':drafts,'after_reload':after}


def scenario(ctx,args):
    artifact=ctx.run.dir/ctx.name; artifact.mkdir()
    row={'version':VERSION,'seed':args.seed,'manifest_before':args.frozen}
    calls=[]
    with WorkspaceFixture('workspace_reconcile',args.seed,artifact) as fixture:
        try:
            ctx.require('the isolated browser is connected',ctx.call('browser_status').get('connected'))
            tab=ctx.call('browser_open',url=fixture.url)['tab']['id']
            ctx.cleanups.append(lambda:ctx.mcp.call('browser_close',tab_id=tab))
            driver=Driver(ctx,tab,'expect'); calls=driver.calls
            row['public_readbacks']=exercise(ctx,fixture,driver)
        except Exception as error:
            row['error']={'type':type(error).__name__,'message':str(error)}
            ctx.check('the public workspace form regression completed',False,str(error))
            raise
        finally:
            evidence=fixture.read()
            receipt={'event_count':len(fixture.events),'head':fixture.events[-1]['hash'] if fixture.events else '0'*64}
            invoice=re.search(r'invoice (INV-\d+)',fixture.prompt).group(1)
            checks=unchanged_checks(evidence,fixture.baseline,receipt,expected_review_id=invoice)
            for name,passed in checks.items():ctx.check(name,passed)
            row.update(calls=calls,persisted_evidence=evidence,independent_checks=checks,
                       checks=ctx.checks,passed=all(check['ok'] for check in ctx.checks))
            write_json(artifact/'result.json',row)
            write_json(ctx.run.dir/'browser-expect-workspace.json',row)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',type=int,default=401)
    parser.add_argument('--takeover',action='store_true')
    parser.add_argument('--list',action='store_true')
    args=parser.parse_args(argv)
    if args.seed<0:raise SystemExit('Use a nonnegative seed')
    if args.list:
        print(json.dumps({'version':VERSION,'seed':args.seed,'scenario':'native SELECT, empty INPUT/TEXTAREA, and review-modal open/dismiss in the real workspace',
                         'manifest':fingerprints()},indent=2));return 0
    if not args.takeover:raise SystemExit('Pass --takeover during an authorized quiet desktop window')
    SCENARIOS.clear()
    def run_scenario(ctx):scenario(ctx,args)
    run_scenario.__name__='browser_expect_workspace_native_form';SCENARIOS.append(run_scenario)
    run=Run(browser=True,installed=False,headed_browser=True);bundle=run.instance.app;args.frozen=fingerprints(bundle)
    run.notes+=['Actual messy-workspace form regression; public MCP actions and independent rendered-control reads.',
                'Only unsaved drafts are changed; the server permits reads and one read-only review, with no save attempts or persisted changes.']
    ok=run.run([]);path=run.dir/'browser-expect-workspace.json'
    if path.exists():
        row=json.loads(path.read_text());after=fingerprints(bundle)
        row.update(manifest_after=after,source_changed=after!=args.frozen,results=run.results,browser_runtime=run.browser_runtime)
        write_json(path,row);ok=ok and after==args.frozen and row.get('passed') is True
    else:ok=False
    return 0 if ok and len(run.results)==1 and run.results[0]['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
