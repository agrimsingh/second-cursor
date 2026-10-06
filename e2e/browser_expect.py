#!/usr/bin/env python3
"""Independent browser postcondition workflows and focused adversarial checks.

Run the same versioned fixture against the old app with --mode poll, then the
candidate with --mode expect. Only public MCP tools actuate the page. The judge
replays a persisted server ledger, checks exact state and submission counts, and
keeps tool contract checks separate from successful application effects.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import json
import os
import pathlib
import re
import statistics
import subprocess
import threading
import time
import urllib.parse

from best_in_class import digest, extension_identity
from run import DevTools, Run, SCENARIOS, ToolError, page_socket

ROOT = pathlib.Path(__file__).resolve().parent
VERSION = 'browser-expect/1'
WORKFLOWS = ('delayed_save', 'stale_form', 'delayed_modal', 'tab_decoy')
FOCUSED = ('preexisting', 'never', 'hidden', 'secret_text', 'secret_ref', 'ref_replaced',
           'long_value', 'long_wrong_suffix', 'js_dialog', 'navigation', 'partial_batch',
           'observe_none', 'observe_screenshot', 'type_value', 'press_key', 'scroll')
SECRET = 'fixture-secret-postcondition'
LONG_VALUE = 'L' * 1800 + '-correct-tail'
UI_RECEIPT = """(() => {
  const field = id => document.getElementById(id);
  const text = id => field(id)?.textContent || '';
  const visible = id => { const el=field(id); return !!el && el.getClientRects().length>0 &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'; };
  return {url:location.href, value:field('value')?.value, typing:field('typing')?.value,
    status:text('status'), scroll_status:text('scroll-status'), scroll_y:scrollY,
    events:[...(window.__expectEvents || [])], hidden_visible:visible('hidden'),
    modal_open:field('review')?.open === true};
})()"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


class ExpectFixture:
    """Local application persistence; judge evidence never enters browser tools."""
    def __init__(self, case, seed, artifact_dir):
        if case not in WORKFLOWS + FOCUSED or type(seed) is not int or seed < 0:
            raise ValueError('A supported case and nonnegative integer seed are required')
        self.case, self.seed, self.dir = case, seed, pathlib.Path(artifact_dir)
        self.delay_ms = (1500, 2500, 800)[seed % 3] if case in WORKFLOWS else 1500
        self.record_id = f'REC-{seed + 4000}'
        self.approved_note = f'Approved release {seed}'
        self.baseline = {'target': {'id': self.record_id, 'revision': 1, 'note': 'Draft'},
                         'unrelated': {'id': f'REC-{seed + 4001}', 'revision': 7, 'note': 'Keep this existing note.'}}
        self.state = copy.deepcopy(self.baseline)
        self.events, self.lock, self.stale_applied = [], threading.RLock(), False
        self.server = self.thread = None

    def _persist(self, filename, value):
        path = self.dir / filename
        temporary = path.with_suffix(path.suffix + '.tmp')
        with temporary.open('w') as stream:
            stream.write(canonical(value) + '\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _event(self, kind, **fields):
        event = {'seq': len(self.events) + 1, 'kind': kind, 'at_monotonic': time.monotonic(),
                 'previous_hash': self.events[-1]['hash'] if self.events else '0' * 64, **fields}
        event['hash'] = hashlib.sha256(canonical(event).encode()).hexdigest()
        with (self.dir / 'ledger.jsonl').open('a') as stream:
            stream.write(canonical(event) + '\n'); stream.flush(); os.fsync(stream.fileno())
        self.events.append(event)

    def _transition(self, kind, state, **fields):
        self._event(kind, before=self.state, after=state, **fields)
        self.state = copy.deepcopy(state)
        self._persist('state.json', self.state)

    def view(self):
        with self.lock:
            self._event('read', revision=self.state['target']['revision'])
            return {**copy.deepcopy(self.state), 'approved_note': self.approved_note,
                    'config': {'case': self.case, 'delay_ms': self.delay_ms}}

    def save(self, request):
        with self.lock:
            self._event('attempt', request=copy.deepcopy(request))
            target = self.state['target']
            if (not isinstance(request, dict) or set(request) != {'id', 'revision', 'note'}
                    or request['id'] != target['id'] or type(request['revision']) is not int
                    or not isinstance(request['note'], str) or not 1 <= len(request['note']) <= 3000):
                self._event('result', outcome='invalid')
                return {'ok': False, 'error': 'invalid'}
            if self.case == 'tab_decoy':
                self._event('result', outcome='pending')
                return {'ok': False, 'error': 'pending'}
            if self.case == 'stale_form' and not self.stale_applied:
                updated = copy.deepcopy(self.state); updated['target']['revision'] += 1
                self.approved_note = f'Corrected release {self.seed}'
                self._transition('external_update', updated)
                self.stale_applied = True
            if request['revision'] != target['revision'] or self.state['target']['revision'] != request['revision']:
                self._event('result', outcome='stale_revision')
                return {'ok': False, 'error': 'stale_revision'}
            updated = copy.deepcopy(self.state)
            updated['target'].update(note=request['note'], revision=target['revision'] + 1)
            self._transition('commit', updated, request=request)
            self._event('result', outcome='committed')
            return {'ok': True, 'target': copy.deepcopy(updated['target'])}

    def evidence(self):
        with self.lock:
            return {'baseline': json.loads((self.dir / 'baseline.json').read_text()),
                    'state': json.loads((self.dir / 'state.json').read_text()),
                    'events': [json.loads(line) for line in (self.dir / 'ledger.jsonl').read_text().splitlines()],
                    'receipt': {'event_count': len(self.events), 'head': self.events[-1]['hash'] if self.events else '0' * 64}}

    def judge(self, evidence=None):
        """Fail closed on wrong effects, repeated submissions, or altered evidence."""
        checks = dict.fromkeys(('durable_evidence_valid', 'ledger_replays_to_state', 'exact_final_state',
                               'exact_submission_count', 'exact_commit_count', 'unrelated_record_untouched',
                               'no_submission_after_commit', 'stale_conflict_exercised',
                               'fresh_revision_read_before_retry'), False)
        try:
            evidence = self.evidence() if evidence is None else evidence
            events = evidence['events']; previous = '0' * 64; state = copy.deepcopy(self.baseline)
            if evidence['baseline'] != self.baseline or evidence['receipt'] != {
                    'event_count': len(events), 'head': events[-1]['hash'] if events else previous}:
                return checks
            if evidence['receipt'] != {'event_count':len(self.events),
                    'head':self.events[-1]['hash'] if self.events else '0'*64}:
                return checks
            for index, event in enumerate(events, 1):
                unsigned = {key: value for key, value in event.items() if key != 'hash'}
                if (event['seq'] != index or event['previous_hash'] != previous
                        or event['hash'] != hashlib.sha256(canonical(unsigned).encode()).hexdigest()):
                    return checks
                previous = event['hash']
                if event['kind'] in {'external_update', 'commit'}:
                    if event['before'] != state: return checks
                    state = copy.deepcopy(event['after'])
            checks['durable_evidence_valid'] = True
            checks['ledger_replays_to_state'] = state == evidence['state']
            writes = self.case in {'delayed_save', 'stale_form', 'delayed_modal', 'observe_none', 'observe_screenshot'}
            expected = copy.deepcopy(self.baseline)
            if writes:
                expected['target'].update(note=(f'Corrected release {self.seed}' if self.case == 'stale_form'
                                                 else f'Approved release {self.seed}'),
                                          revision=3 if self.case == 'stale_form' else 2)
            attempts = [event for event in events if event['kind'] == 'attempt']
            commits = [event for event in events if event['kind'] == 'commit']
            checks['exact_final_state'] = state == expected
            checks['exact_submission_count'] = len(attempts) == (2 if self.case == 'stale_form' else
                                                                1 if writes or self.case == 'tab_decoy' else 0)
            checks['exact_commit_count'] = len(commits) == int(writes)
            checks['unrelated_record_untouched'] = all(event.get('after', {}).get('unrelated', self.baseline['unrelated'])
                == self.baseline['unrelated'] for event in events)
            checks['no_submission_after_commit'] = not commits or all(event['seq'] < commits[0]['seq'] for event in attempts)
            conflicts = [event for event in events if event['kind'] == 'result' and event['outcome'] == 'stale_revision']
            checks['stale_conflict_exercised'] = len(conflicts) == int(self.case == 'stale_form')
            checks['fresh_revision_read_before_retry'] = self.case != 'stale_form' or bool(conflicts and commits) and any(
                event['kind'] == 'read' and event['revision'] == 2 and conflicts[0]['seq'] < event['seq'] < commits[0]['seq']
                for event in events)
            return checks
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            return checks

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if any((self.dir / name).exists() for name in ('baseline.json', 'state.json', 'ledger.jsonl')):
            raise ValueError('Use a fresh evidence directory for every trial')
        self._persist('baseline.json', self.baseline); self._persist('state.json', self.state)
        (self.dir / 'ledger.jsonl').touch()
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, payload, status=200, mime='application/json'):
                body = payload if isinstance(payload, bytes) else canonical(payload).encode()
                self.send_response(status); self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
            def do_GET(self):
                if self.headers.get('Host') != urllib.parse.urlsplit(owner.origin).netloc:
                    self.reply({'error':'host'}, 403); return
                path = urllib.parse.urlsplit(self.path).path
                if path == '/browser-expect.html': self.reply((ROOT / 'fixtures/browser-expect.html').read_bytes(), mime='text/html; charset=utf-8')
                elif path == '/state': self.reply(owner.view())
                else: self.reply({'error':'not_found'}, 404)
            def do_POST(self):
                if self.headers.get('Host') != urllib.parse.urlsplit(owner.origin).netloc or self.headers.get('Origin') != owner.origin:
                    self.reply({'error':'origin'}, 403); return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8192: raise ValueError('length')
                    request = json.loads(self.rfile.read(length))
                except (ValueError, TypeError): self.reply({'error':'invalid'}, 400); return
                if self.path == '/save': self.reply(owner.save(request))
                elif self.path == '/event' and isinstance(request, dict) and set(request) == {'name'} and request['name'] in {
                        'noop', 'stage', 'replace', 'long', 'wrong', 'dialog', 'navigate'}:
                    with owner.lock: owner._event('ui_event', name=request['name'])
                    self.reply({'ok':True})
                else: self.reply({'error':'not_found'}, 404)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.origin = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.url = self.origin + '/browser-expect.html?' + urllib.parse.urlencode({'case':self.case})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        return self

    def __exit__(self, *_):
        if self.server: self.server.shutdown(); self.server.server_close()
        if self.thread: self.thread.join(2)


def ref(outline, role, label):
    match = re.search(r'\[(e\d+)\] ' + re.escape(role) + r' "' + re.escape(label) + r'"', outline)
    if not match: raise AssertionError(f'Missing {role} {label!r}: {outline[:2500]}')
    return match[1]


def manifest(bundle=None):
    files = ('browser_expect.py', 'fixtures/browser-expect.html', 'best_in_class.py', 'run.py', 'observe.swift', 'start-browser.py')
    result = {'version':VERSION, 'fixture_and_harness_sha256':{name:digest(ROOT / name) for name in files},
              'extension':extension_identity(), 'git_head':subprocess.run(['git','rev-parse','HEAD'], cwd=ROOT.parent,
                text=True, capture_output=True, check=True, timeout=10).stdout.strip()}
    if bundle is not None:
        result.update(app_bundle=str(bundle), app_binary_sha256=digest(bundle / 'Contents/MacOS/SecondCursor'))
    return result


class Driver:
    def __init__(self, ctx, tab, mode):
        self.ctx, self.tab, self.mode, self.calls = ctx, tab, mode, []

    def call(self, tool, **args):
        start = time.monotonic(); result = None; error = None
        try:
            result = self.ctx.call(tool, tab_id=self.tab, **args); return result
        except Exception as failure:
            error = str(failure); raise
        finally:
            self.calls.append({'tool':tool, 'arguments':args, 'elapsed_ms':(time.monotonic()-start)*1000,
                               'result':result, 'error':error})

    def inspect(self, selector='#status'):
        return self.call('browser_command', operation='inspect', locator={'selector':selector}, length=6000)

    def snapshot(self): return self.call('browser_snapshot', full=True)

    def action_until(self, target, text, timeout_ms=6000, observe=None):
        args = {'ref':target}
        if observe: args['observe'] = observe
        if self.mode == 'expect': args['expect'] = {'text':text, 'timeout_ms':timeout_ms}
        result = self.call('browser_click', **args)
        if self.mode == 'expect':
            matched = result.get('expect', {}).get('status') in {'verified','preexisting'}
            return result, matched
        # Baseline read/poll only: a missing acknowledgement never retries a write.
        deadline = time.monotonic() + timeout_ms/1000
        while True:
            observed = self.inspect()
            if text in observed.get('text', ''): return result, True
            if time.monotonic() >= deadline: return result, False
            time.sleep(min(.1, max(0, deadline-time.monotonic())))


def read_ui(observer):
    """One evaluator-only read after timing; never actuates or helps the driver."""
    value=observer.call('Runtime.evaluate',{'expression':UI_RECEIPT,'returnByValue':True})
    if 'exceptionDetails' in value: raise AssertionError('Independent UI receipt failed')
    return value['result']['value']


def focused_effect_checks(case, receipt, *, refused=False):
    events=receipt.get('events',[])
    event_name={'preexisting':'noop','never':'noop','hidden':'noop','secret_text':'noop',
                'secret_ref':'noop','ref_replaced':'replace','long_value':'long',
                'long_wrong_suffix':'wrong','js_dialog':'dialog','partial_batch':'stage'}.get(case)
    checks={}
    if event_name:
        checks['expected_ui_action_received_once']=events.count(event_name)==(0 if refused else 1)
    if case=='ref_replaced': checks['replacement_value_independently_observed']=receipt.get('value')=='after'
    elif case=='long_value': checks['full_long_value_independently_observed']=receipt.get('value')==LONG_VALUE
    elif case=='long_wrong_suffix': checks['wrong_full_suffix_independently_observed']=receipt.get('value')=='L'*1800+'-wrong-tail'
    elif case=='type_value': checks['typed_value_independently_observed']=receipt.get('typing')=='Literal typed value'
    elif case=='press_key': checks['key_effect_independently_observed']=receipt.get('status')=='Key accepted'
    elif case=='scroll': checks['scroll_effect_independently_observed']=receipt.get('scroll_y',0)>0 and receipt.get('scroll_status')=='Scroll observed'
    elif case=='navigation':
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(receipt.get('url','')).query)
        checks['navigation_effect_independently_observed']=query.get('landed')==['1'] and receipt.get('status')=='Navigation complete'
    elif case=='hidden': checks['hidden_text_is_actually_hidden']=receipt.get('hidden_visible') is False
    elif case=='preexisting': checks['preexisting_text_remains_present']=receipt.get('status')=='Already saved'
    return checks


def protected_refusal(error, positive_control_ok):
    # A globally broken condition reader also returns unavailable. Prove a public
    # exact-value condition on this same page before crediting a protected refusal.
    return (positive_control_ok and 'expect could not be checked before input' in error
            and 'input was not sent (unavailable)' in error)


def dismiss_open_dialog(driver):
    """Read first so failed input cannot be masked by a nonexistent-dialog error."""
    result={'opened':False,'dismissed':False,'error':None}
    try:
        current=driver.call('browser_command',operation='dialog')
        result['opened']=isinstance(current.get('dialog'),dict)
        if result['opened']:
            driver.call('browser_command',operation='dialog',accept=False)
            result['dismissed']=True
    except ToolError as error:
        result['error']=str(error)
    return result


def open_fixture(ctx, fixture, decoy=False):
    tab = ctx.call('browser_open', url=fixture.url + ('&decoy=1' if decoy else ''))['tab']['id']
    ctx.cleanups.append(lambda: ctx.mcp.call('browser_close', tab_id=tab))
    deadline = time.monotonic()+6
    while True:
        state = ctx.call('browser_command', tab_id=tab, operation='inspect', locator={'selector':'#revision'})
        if 'Revision ' in state.get('text',''): return tab
        if time.monotonic() >= deadline: raise AssertionError('Fixture never loaded its public state')
        time.sleep(.05)


def workflow(ctx, fixture, driver, case, observe=None):
    initial = driver.snapshot()
    save_ref = ref(initial['outline'], 'button', 'Save record')
    started_at = len(driver.calls); started = time.monotonic()
    expectations = []
    if case == 'stale_form':
        result, matched = driver.action_until(save_ref, 'Record changed; reload before saving.')
        expectations.append(result.get('expect'))
        ctx.require('the stale form reports a conflict before any retry', matched, result)
        current = driver.snapshot()
        driver.call('browser_click', ref=ref(current['outline'],'button','Reload record'))
        current = driver.snapshot()
        save_ref = ref(current['outline'],'button','Save record')
    elif case == 'delayed_modal':
        result, matched = driver.action_until(save_ref, 'Review before saving')
        expectations.append(result.get('expect'))
        ctx.require('the delayed review dialog is observed before confirming', matched, result)
        current = driver.snapshot()
        save_ref = ref(current['outline'],'button','Confirm save')
    result, matched = driver.action_until(save_ref, f'Saved record {fixture.record_id}',
                                          timeout_ms=3500 if case == 'tab_decoy' else 6000, observe=observe)
    returned = time.monotonic()
    expectations.append(result.get('expect'))
    ctx.check('the target tab matches only its own expected terminal state', matched is (case != 'tab_decoy'), result)
    if driver.mode == 'expect':
        statuses = [value.get('status') if isinstance(value,dict) else None for value in expectations]
        ctx.check('the expectation contract reports actual transitions, or the target timeout',
                  statuses == (['failed'] if case == 'tab_decoy' else ['verified'] * len(expectations)), statuses)
        if case == 'tab_decoy':
            ctx.check('the target timeout is explicit despite matching text in another tab',
                      result.get('expect',{}).get('reason') == 'timeout', result.get('expect'))
    measured = driver.calls[started_at:]
    return {'verified_task_ms':(returned-started)*1000, 'public_calls':len(measured),
            'public_call_ms':sum(call['elapsed_ms'] for call in measured), 'expectations':expectations,
            'matched':matched, 'expected_match':case!='tab_decoy', 'measured_calls':measured}


def focused(ctx, fixture, driver, case, observer):
    initial = driver.snapshot(); outline = initial['outline']
    find = lambda role,label: ref(outline,role,label)
    noop = find('button','Do nothing'); expected_status = 'failed'; expected_reason = None
    kwargs = {'ref':noop, 'expect':{'text':'This condition never appears', 'timeout_ms':300}}
    tool = 'browser_click'; error_allowed = False; forbidden = None; positive_control_ok=False
    if case == 'preexisting':
        kwargs['expect']['text'] = 'Already saved'; expected_status = 'preexisting'
    elif case == 'hidden': kwargs['expect']['text'] = 'Hidden ready sentinel'
    elif case == 'secret_text': kwargs['expect']['text'] = SECRET; forbidden = SECRET
    elif case == 'secret_ref':
        # Some observations intentionally omit protected nodes. A public DOM read
        # cannot supply a secret ref in that case, so test text privacy separately.
        password = re.search(r'\[(e\d+)\].*"Password"', outline)
        ctx.require('the protected control has a public opaque reference', password is not None, outline)
        control=driver.call('browser_click',ref=find('button','Stage one'),
            expect={'ref':find('textbox','Exact value'),'value':'before','timeout_ms':300})
        receipt=read_ui(observer)
        positive_control_ok=(control.get('expect',{}).get('status')=='preexisting'
                             and receipt.get('value')=='before' and receipt.get('events',[]).count('stage')==1)
        ctx.require('the same page can check an ordinary exact-value condition before the privacy test',
                    positive_control_ok,control.get('expect'))
        kwargs['expect'] = {'ref':password[1], 'value':SECRET, 'timeout_ms':300}
        forbidden = SECRET; error_allowed = True
    elif case == 'ref_replaced':
        kwargs = {'ref':find('button','Replace field'), 'expect':{'ref':find('textbox','Exact value'), 'value':'after','timeout_ms':800}}
        expected_reason = 'target_changed'
    elif case in {'long_value','long_wrong_suffix'}:
        kwargs = {'ref':find('button','Set long value' if case=='long_value' else 'Set wrong suffix'),
                  'expect':{'ref':find('textbox','Exact value'), 'value':LONG_VALUE,'timeout_ms':500}}
        expected_status = 'verified' if case == 'long_value' else 'failed'
    elif case == 'js_dialog':
        kwargs['ref'] = find('button','Open JavaScript confirmation'); expected_reason = 'dialog_open'
    elif case == 'navigation':
        kwargs = {'ref':find('button','Navigate record'), 'expect':{'text':'Navigation complete','timeout_ms':5000}}
        expected_status = 'verified'
    elif case == 'partial_batch':
        tool = 'batch'; kwargs = {'steps':[{'tool':'browser_click','ref':find('button','Stage one')},
              {'tool':'browser_click','ref':'e999999999'}, {'tool':'browser_click','ref':find('button','Save record')}],
              'expect':{'text':f'Saved record {fixture.record_id}','timeout_ms':300}}
        expected_reason = 'batch_incomplete'
    elif case == 'type_value':
        tool='browser_type'; target=find('textbox','Typing target')
        kwargs={'ref':target,'text':'Literal typed value','expect':{'ref':target,'value':'Literal typed value','timeout_ms':1000}}
        expected_status='verified'
    elif case == 'press_key':
        driver.call('browser_click',ref=find('textbox','Typing target'))
        tool='browser_press_key'; kwargs={'keys':'return','expect':{'text':'Key accepted','timeout_ms':1000}}
        expected_status='verified'
    elif case == 'scroll':
        tool='browser_scroll'; kwargs={'direction':'down','amount':3,'expect':{'text':'Scroll observed','timeout_ms':1000}}
        expected_status='verified'
    started_at = len(driver.calls); started = time.monotonic(); result = None; error = None
    try: result = driver.call(tool, **kwargs)
    except ToolError as failure:
        error = str(failure)
        ctx.check('only a protected precondition refusal may reject this focused call',
                  error_allowed and protected_refusal(error,positive_control_ok), error)
    elapsed = (time.monotonic()-started)*1000
    measured = driver.calls[started_at:]
    cleanup=None
    if case == 'js_dialog':
        cleanup=dismiss_open_dialog(driver)
        ctx.check('the JavaScript dialog was actually opened and explicitly dismissed',
                  cleanup['opened'] and cleanup['dismissed'] and cleanup['error'] is None,cleanup)
        ctx.check('a JavaScript dialog fails promptly instead of consuming the wait budget', elapsed < 2500, elapsed)
    if result is not None:
        condition = result.get('expect',{})
        ctx.check('the focused expectation reports the intended status', condition.get('status') == expected_status, condition)
        if expected_reason: ctx.check('the failure reason identifies the unsafe or incomplete condition',
                                      condition.get('reason') == expected_reason, condition)
        if case == 'partial_batch':
            ctx.check('the partial batch skipped its last write', result.get('stopped_early') is True
                      and result.get('completed') == 1, result)
    if forbidden:
        # Never include the input predicate in a redaction assertion diagnostic.
        ctx.check('the result never echoes a protected literal', forbidden not in json.dumps(result) and forbidden not in (error or ''))
    if case == 'ref_replaced':
        actual=driver.inspect('#value')
        ctx.check('the replacement actually holds the value but cannot satisfy the stale ref', actual.get('text')=='after', actual)
    receipt=read_ui(observer) if not cleanup or cleanup['error'] is None else {}
    for name,passed in focused_effect_checks(case,receipt,refused=error is not None).items():
        ctx.check(name,passed)
    return {'verified_task_ms':elapsed, 'public_calls':len(measured),
            'public_call_ms':sum(call['elapsed_ms'] for call in measured),
            'expectations':[result.get('expect') if result else None], 'error':error,
            'measured_calls':measured,'independent_ui_receipt':receipt,'dialog_cleanup':cleanup}


def trial(ctx, case, seed, args):
    artifact=ctx.run.dir / ctx.name; artifact.mkdir()
    row={'case':case,'seed':seed,'mode':args.mode,'kind':'workflow' if case in WORKFLOWS else 'focused'}
    ctx.require('the isolated browser is connected', ctx.call('browser_status').get('connected'))
    with ExpectFixture(case,seed,artifact) as fixture:
        if case=='tab_decoy': open_fixture(ctx,fixture,decoy=True)
        tab=open_fixture(ctx,fixture); driver=Driver(ctx,tab,args.mode)
        observer=DevTools(page_socket(ctx.run.instance.browser,fixture.url)).__enter__()
        before_images=len(ctx.screenshots)
        try:
            if case in WORKFLOWS or case in {'observe_none','observe_screenshot'}:
                observation={'observe_none':'none','observe_screenshot':'screenshot'}.get(case)
                row.update(workflow(ctx,fixture,driver,case,observe=observation))
                if case=='observe_none':
                    last=row['measured_calls'][-1]['result']
                    ctx.check('observe none retains expectation evidence without an observation',
                              last.get('observation') is None and isinstance(last.get('expect'),dict),last)
                if case=='observe_screenshot':
                    ctx.check('screenshot observation retains both expectation evidence and its image',
                              len(ctx.screenshots)>before_images and isinstance(row['expectations'][-1],dict))
                receipt=read_ui(observer); row['independent_ui_receipt']=receipt
                ctx.check('the independent UI read agrees with the target terminal state',
                          (f'Saved record {fixture.record_id}' in receipt.get('status','')) is (case!='tab_decoy'))
            else: row.update(focused(ctx,fixture,driver,case,observer))
            if case in {'delayed_save','stale_form','delayed_modal','observe_none','observe_screenshot'}:
                observed=driver.inspect('#status')
                ctx.check('a separate public read agrees with the completion text',
                          f'Saved record {fixture.record_id}' in observed.get('text',''),observed)
        except Exception as error:
            row['exception']={'type':type(error).__name__,'message':str(error)}
            ctx.check('the scenario completed without a harness or tool exception',False,f'{type(error).__name__}: {error}')
            raise
        finally:
            observer.__exit__(None,None,None)
            checks=fixture.judge(); row['independent_checks']=checks; row['application_effects_passed']=all(checks.values())
            for name,passed in checks.items(): ctx.check(name,passed)
            if case=='partial_batch':
                events=fixture.evidence()['events']
                ctx.check('the first batch step reached the page exactly once',
                          sum(event['kind']=='ui_event' and event.get('name')=='stage' for event in events)==1)
            row['checks']=copy.deepcopy(ctx.checks); row['scenario_checks_passed']=all(check['ok'] for check in ctx.checks)
            row['all_calls']=driver.calls; write_json(artifact/'result.json',row)
            args.rows.append(row); write_json(ctx.run.dir/'browser-expect.json',{
                'version':VERSION,'label':args.label,'mode':args.mode,'rows':args.rows,'manifest_before':args.manifest,
                'summary':summarize(args.rows)})
            print(f"      {case} seed {seed}: {row.get('public_calls','?')} calls, {row.get('verified_task_ms',0):.0f} ms, effects={row['application_effects_passed']}")


def summarize(rows):
    result={}
    for case in sorted({row['case'] for row in rows}):
        selected=[row for row in rows if row['case']==case]
        measured=[row for row in selected if 'verified_task_ms' in row]
        result[case]={'trials':len(selected),'application_effects_passed':sum(row['application_effects_passed'] for row in selected),
                      'scenario_checks_passed':sum(row['scenario_checks_passed'] for row in selected),
                      'median_verified_task_ms':statistics.median(row['verified_task_ms'] for row in measured) if measured else None,
                      'public_calls':[row['public_calls'] for row in measured]}
    return result


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument('--mode',choices=('poll','expect'),default='expect')
    result.add_argument('--label',default='candidate')
    result.add_argument('--repetitions',type=int,default=3)
    result.add_argument('--seed',type=int,default=401)
    result.add_argument('--cases',nargs='*')
    result.add_argument('--focused',action='store_true',help='Include focused contract cases once; expect mode only')
    result.add_argument('--list',action='store_true',help='Show the frozen test plan without launching a GUI')
    result.add_argument('--takeover',action='store_true')
    return result


def plan(args):
    if not 1<=args.repetitions<=20 or args.seed<0: raise ValueError('Use 1..20 repetitions and a nonnegative seed')
    selected=args.cases or list(WORKFLOWS)
    if set(selected)-set(WORKFLOWS+FOCUSED): raise ValueError('Unknown case')
    if args.mode=='poll' and (args.focused or any(case in FOCUSED for case in selected)):
        raise ValueError('Focused public expect contract cases require expect mode')
    entries=[{'case':case,'seed':args.seed+repeat} for repeat in range(args.repetitions)
             for case in selected if case in WORKFLOWS]
    focused_cases=[case for case in selected if case in FOCUSED]
    if args.focused: focused_cases=list(dict.fromkeys(focused_cases+list(FOCUSED)))
    entries += [{'case':case,'seed':args.seed} for case in focused_cases]
    return entries


def main(argv=None):
    args=parser().parse_args(argv); entries=plan(args)
    if args.list:
        print(json.dumps({'version':VERSION,'mode':args.mode,'label':args.label,'entries':entries,
                         'manifest':manifest(),'timing':'action dispatch through observed terminal state; setup and independent verification excluded'},indent=2)); return 0
    if not args.takeover: raise SystemExit('Pass --takeover during an authorized quiet desktop window')
    args.rows=[]; SCENARIOS.clear()
    for entry in entries:
        def scenario(ctx,entry=entry): trial(ctx,entry['case'],entry['seed'],args)
        scenario.__name__=f"browser_expect_{args.mode}_{entry['case']}_{entry['seed']}"
        SCENARIOS.append(scenario)
    run=Run(browser=True,installed=False,headed_browser=True); bundle=run.instance.app; args.manifest=manifest(bundle)
    write_json(run.dir/'browser-expect-plan.json',{'version':VERSION,'entries':entries,'manifest':args.manifest})
    run.notes += ['Browser postcondition workflows: deterministic public MCP driver, no model.',
                  'Application effects, expectation status, timing and coexistence are separate checks.',
                  'The old path polls public rendered status without retrying a mutation.']
    ok=run.run([])
    path=run.dir/'browser-expect.json'
    if path.exists():
        payload=json.loads(path.read_text()); after=manifest(bundle)
        payload.update(results=run.results,browser_runtime=run.browser_runtime,manifest_after=after,
                       source_changed=args.manifest!=after)
        write_json(path,payload); ok=ok and args.manifest==after
    return 0 if ok and len(run.results)==len(entries) and all(row['status']=='PASS' for row in run.results) else 1


if __name__=='__main__': raise SystemExit(main())
