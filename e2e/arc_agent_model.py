#!/usr/bin/env python3
"""Matched Sol 6.1 high experiment: raw SDK form calls vs bounded SDK choices.

Both arms first discover the same seeded messy workspace through public browser
tools, then the model supplies its own literal form inputs. The execution phase
uses the actual runAgent API; the new optional contract is candidate-only. This
isolates a narrow form-execution policy, not a replacement for cross-app recovery.
--list performs no GUI or model work. Real runs require --takeover.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from benchmarks.model_host import CodexModelHost, HostConfig, host_version
from benchmarks.workspace_fixtures import WorkspaceFixture
from cross_app_model_support import host_evidence_checks
from run import APP_BUNDLE, Run, SCENARIOS

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
VERSION = 'arc-sdk-form/1'
NAVIGATION = {'Overview', 'Inbox', 'Orders', 'Policy', 'Invoices', 'Support', 'Release notes', 'Activity'}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def schedule(seed, rounds, mode='both'):
    if type(seed) is not int or seed < 0 or type(rounds) is not int or not 1 <= rounds <= 20:
        raise ValueError('Require a nonnegative seed and 1..20 rounds')
    return [{'seed': seed + index, 'mode': arm} for index in range(rounds)
            for arm in ((['baseline', 'candidate'] if index % 2 == 0 else ['candidate', 'baseline'])
                        if mode == 'both' else [mode])]


def fingerprints(baseline_sdk):
    paths = [ROOT / name for name in ('arc_agent_model.py', 'arc_agent_bridge.mjs',
        'benchmarks/model_host.py', 'benchmarks/workspace_fixtures.py', 'benchmarks/workspace.html',
        'cross_app_model_support.py', 'run.py', 'start-browser.py')]
    paths += list((REPO / 'sdk').glob('*.mjs')) + list(pathlib.Path(baseline_sdk).glob('*.mjs'))
    extension = pathlib.Path(os.environ.get('SC_E2E_EXTENSION', REPO / 'extension')).resolve()
    paths += list(extension.glob('*.js')) + [extension / 'manifest.json', APP_BUNDLE / 'Contents/MacOS/SecondCursor']
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))}


def navigation_allowed(name):
    return isinstance(name, str) and (name in NAVIGATION or name.startswith(
        ('Open message ', 'Open order ', 'Open policy ', 'Open invoice ', 'Open release ', 'Open ticket ')))


def validate_inputs(value):
    if not isinstance(value, dict) or set(value) != {'amount', 'route', 'note'}:
        raise ValueError('Supply exactly amount, route and note')
    if not isinstance(value['amount'], str) or not re.fullmatch(r'\d{1,7}\.\d{2}', value['amount']):
        raise ValueError('amount must be a decimal string with two places')
    if value['route'] not in {'Standard', 'Finance review', 'Hold'}:
        raise ValueError('route must be an available decision label')
    if not isinstance(value['note'], str) or not 1 <= len(value['note']) <= 500:
        raise ValueError('note must be 1..500 characters')
    return dict(value)


def tool_specs(mode):
    def spec(name, description, properties, required):
        return {'name': name, 'description': description, 'inputSchema': {
            'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
    tools = [spec('read_context', 'Before form execution, inspect the workspace or click an observed navigation/record button. No edits are permitted here.',
        {'operation': {'type': 'string', 'enum': ['observe', 'click']}, 'name': {'type': 'string'}}, ['operation']),
        spec('begin_execution', 'After gathering all supporting records and opening the assigned invoice, submit the exact values YOU derived. Values are frozen for the form execution phase; this does not save anything.',
        {'amount': {'type': 'string', 'description': 'Dollar amount with two decimal places'},
         'route': {'type': 'string', 'enum': ['Standard', 'Finance review', 'Hold']},
         'note': {'type': 'string', 'description': 'Exact reconciliation memo from visible records'}}, ['amount', 'route', 'note'])]
    if mode == 'candidate':
        tools.append(spec('execute_form', 'Choose one currently offered SDK choice by its exact id. Set finish=true only after saving and reloading the assigned invoice to verify persisted values.',
            {'choice_id': {'type': 'string'}, 'finish': {'type': 'boolean'}}, []))
    else:
        tools.append(spec('execute_form', 'Issue one raw SDK browser operation on an observed form control. Click uses a button name; fill/select_option use a field label and literal value. Set finish=true only after saving and reloading the assigned invoice to verify persisted values.',
            {'operation': {'type': 'string', 'enum': ['observe', 'click', 'fill', 'select_option']},
             'name': {'type': 'string'}, 'value': {'type': 'string'}, 'finish': {'type': 'boolean'}}, []))
    return tools


class SDKBridge:
    def __init__(self, config, artifact, judge, record):
        self.judge, self.record, self.result = judge, record, None
        self.events = queue.Queue()
        self.path = artifact / 'bridge-config.json'
        write_json(self.path, config)
        self.stderr = (artifact / 'bridge-stderr.log').open('w')
        self.process = subprocess.Popen([shutil.which('node') or 'node', str(ROOT / 'arc_agent_bridge.mjs'), str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, bufsize=1, start_new_session=True)
        def read():
            try:
                for line in self.process.stdout:
                    try: self.events.put(json.loads(line))
                    except ValueError: self.events.put({'type': 'fatal', 'error': {'message': 'Invalid SDK bridge JSON'}})
            finally: self.events.put({'type': 'eof'})
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, value):
        self.process.stdin.write(json.dumps(value, allow_nan=False) + '\n')
        self.process.stdin.flush()

    def next(self):
        while True:
            event = self.events.get(timeout=90)
            self.record('sdk_bridge', event)
            if event['type'] == 'verify':
                checks = self.judge()
                self.send({'type': 'verification', 'complete': bool(checks) and all(checks.values())})
            elif event['type'] == 'ready':
                return event
            elif event['type'] == 'done':
                self.result = event['result']
                return event
            elif event['type'] in {'fatal', 'eof'}:
                raise RuntimeError('SDK bridge stopped: ' + json.dumps(event.get('error', event)))

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()


def trial(ctx, entry, config, profile, baseline_sdk):
    artifact = ctx.run.dir / ctx.name
    artifact.mkdir()
    frozen = profile['sources']
    ctx.require('source profile unchanged before trial', fingerprints(baseline_sdk) == frozen)
    fixture = WorkspaceFixture('workspace_reconcile', entry['seed'], artifact).__enter__()
    ctx.cleanups.append(lambda: fixture.__exit__(None, None, None))
    session = ctx.call('session', action='create')['session_id']
    ctx.cleanups.append(lambda: ctx.mcp.call('session', action='close', session_id=session))
    ctx.call('task', action='create', session_id=session, title=f"Arc SDK {entry['mode']} seed {entry['seed']}")
    instances = ctx.call('browser_instances')['browsers']
    ctx.require('only isolated browser available', len(instances) == 1)
    browser = instances[0]['id']
    target = {'session_id': session, 'browser_id': browser}
    tab = ctx.call('browser_open', **target, url=fixture.url)['tab']['id']
    target['tab_id'] = tab
    ctx.cleanups.append(lambda: ctx.mcp.call('browser_close', **target))
    events, host_events, bridge, phase_started = [], [], None, None
    started = time.monotonic()
    current = None
    host = {}
    def record(source, event):
        row = {'source': source, 'elapsed_ms': (time.monotonic() - started) * 1000, **event}
        events.append(row)
        with (artifact / 'trace.jsonl').open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    def observe():
        nonlocal current
        text = ctx.call('browser_command', **target, operation='inspect', locator={'selector': '#workspace'}, length=30000)
        dom = ctx.call('browser_command', **target, operation='dom_snapshot')
        if dom.get('url') != fixture.url: raise ValueError('Fixture browser URL changed')
        current = {'text': text['text'], 'navigation': [row['name'] for row in dom.get('elements', [])
            if row.get('role') == 'button' and row.get('visible') and row.get('enabled') and navigation_allowed(row.get('name'))]}
        record('context', {'type': 'observation', 'observation': current})
        return current
    def execute(name, arguments):
        nonlocal bridge, phase_started
        if name == 'read_context':
            if bridge is not None: raise ValueError('Context phase is closed; use execute_form')
            if arguments.get('operation') == 'click':
                label = arguments.get('name')
                if not current or current['navigation'].count(label) != 1 or not navigation_allowed(label):
                    raise ValueError('Choose an observed navigation or record button')
                ctx.call('browser_command', **target, operation='click', locator={'role': 'button', 'name': label, 'exact': True})
            elif arguments.get('operation') != 'observe': raise ValueError('Unknown context operation')
            return observe()
        if name == 'begin_execution':
            if bridge is not None: raise ValueError('Literal inputs are already frozen')
            inputs = validate_inputs(arguments)
            invoice = re.search(r'invoice (INV-\d+)', fixture.prompt).group(1)
            observation = observe()
            if not re.search(r'Invoice ID:\s*' + re.escape(invoice) + r'\b', observation['text']):
                raise ValueError('Open the assigned invoice before beginning form execution')
            write_json(artifact / 'model-literals.json', inputs)
            phase_started = (time.monotonic() - started) * 1000
            bridge = SDKBridge({'mode': entry['mode'], 'sdk': str(baseline_sdk if entry['mode'] == 'baseline' else REPO / 'sdk'),
                'session_id': session, 'browser_id': browser, 'tab_id': tab, 'url': fixture.url,
                'inputs': inputs, 'task': fixture.prompt}, artifact, fixture.judge, record)
            ctx.cleanups.append(bridge.close)
            return bridge.next()
        if name == 'execute_form':
            if bridge is None: raise ValueError('First read the workspace and call begin_execution')
            if bridge.result is not None: raise ValueError('SDK form run already ended')
            bridge.send(arguments)
            return bridge.next()
        raise ValueError('Unknown fixture tool')
    def host_event(event):
        host_events.append(event)
        record('model_host', event)
        if event.get('type') == 'tool':
            print(f"  {entry['mode']} seed {entry['seed']}: {event.get('name')} {event.get('status')}", flush=True)
    initial = observe()
    prompt = (fixture.prompt + '\n\nRead the visible workspace to derive the correct amount, decision and exact memo. '
        'Use read_context to navigate records; after opening the assigned invoice, call begin_execution with your derived literal values. '
        'Then use execute_form to fill, review, save exactly once, reload the invoice and confirm its saved values. '
        'Signal finish=true through execute_form before your final answer. Preserve every unrelated record. '
        'Only the disposable fixture is accessible. No hidden answer or recovery recipe is supplied.\n\nInitial observation:\n' + json.dumps(initial))
    (artifact / 'prompt.txt').write_text(prompt)
    write_json(artifact / 'tools.json', tool_specs(entry['mode']))
    write_json(artifact / 'profile.json', profile)
    try:
        host = CodexModelHost(config).run(prompt, tool_specs(entry['mode']), execute, host_event).as_dict()
    finally:
        write_json(artifact / 'host-result.json', host)
        checks = fixture.judge()
        host_checks = host_evidence_checks(host, host_events, model=config.model,
            reasoning_effort=config.reasoning_effort, host_version=config.model_host_version)
        host_checks.pop('host_fault_errors_present_in_trace', None)
        checks.update(host_checks)
        checks['sdk_completed'] = bridge is not None and bridge.result is not None and not bridge.result.get('stopped')
        checks['source_profile_unchanged'] = fingerprints(baseline_sdk) == frozen
        sdk_events = [event for event in events if event['source'] == 'sdk_bridge' and event.get('type') == 'sdk_call']
        done = next((event for event in events if event['source'] == 'sdk_bridge' and event.get('type') == 'done'), None)
        metrics = {'model_host_wall_ms': host.get('wall_ms'), 'model_tool_calls': host.get('tool_calls'),
            'execution_phase_wall_ms': done['elapsed_ms'] - phase_started if done and phase_started is not None else None,
            'sdk_dispatch_calls': len(sdk_events), 'sdk_dispatch_ms': sum(event['elapsed_ms'] for event in sdk_events),
            'sdk_errors': sum(bool(event.get('error')) for event in sdk_events),
            'model_errors': sum(event.get('type') == 'tool' and event.get('status') == 'error' for event in host_events),
            'diagnostics': fixture.diagnostics(), 'note': 'Execution phase includes SDK startup, model, host and tool time; it is not model-only inference latency.'}
        write_json(artifact / 'independent-result.json', {'entry': entry, 'checks': checks, 'passed': all(checks.values()), 'metrics': metrics})
        write_json(artifact / 'sdk-result.json', bridge.result if bridge else None)
        for name, passed in checks.items(): ctx.check(name, passed)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--mode', choices=('both', 'baseline', 'candidate'), default='both')
    result.add_argument('--seed', type=int, default=201)
    result.add_argument('--rounds', type=int, default=2)
    result.add_argument('--model', default='gpt-6.1-sol')
    result.add_argument('--reasoning-effort', default='high', choices=('low', 'medium', 'high', 'xhigh'))
    result.add_argument('--timeout', type=float, default=420)
    result.add_argument('--max-steps', type=int, default=80)
    result.add_argument('--baseline-sdk', type=pathlib.Path, default=REPO / 'build/arc-eval/baseline/sdk')
    result.add_argument('--codex-binary', default='codex')
    result.add_argument('--list', action='store_true')
    result.add_argument('--takeover', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    args.baseline_sdk = args.baseline_sdk.resolve()
    if not (args.baseline_sdk / 'agent.mjs').is_file(): raise SystemExit('Baseline SDK snapshot is missing')
    config = HostConfig(model=args.model, reasoning_effort=args.reasoning_effort, max_steps=args.max_steps,
        timeout_seconds=args.timeout, codex_binary=args.codex_binary, model_host_version=host_version(args.codex_binary))
    plan = schedule(args.seed, args.rounds, args.mode)
    profile = {'version': VERSION, 'schedule': plan, 'sources': fingerprints(args.baseline_sdk),
        'model_host': dataclasses.asdict(config), 'browser_visibility': 'headed',
        'scope': 'Model discovers messy workspace; raw calls vs constrained choices for one invoice form. Same app and extension in both arms.',
        'limits': 'Separate fresh conversations vary model reasoning; two seeds are paired integration evidence, not a speed/reliability ranking. No native coexistence or recovery faults in this track.'}
    if args.list:
        print(json.dumps(profile, indent=2))
        return 0
    if not args.takeover: raise SystemExit('Real runs require --takeover while the user is away')
    SCENARIOS.clear()
    for entry in plan:
        def scenario(ctx, entry=entry): trial(ctx, entry, config, profile, args.baseline_sdk)
        scenario.__name__ = f"arc_agent_{entry['mode']}_{entry['seed']}"
        SCENARIOS.append(scenario)
    run = Run(browser=True, installed=False, headed_browser=True)
    write_json(run.dir / 'arc-agent-profile.json', profile)
    ok = run.run([])
    return 0 if ok and len(run.results) == len(plan) and all(row['status'] == 'PASS' for row in run.results) else 1


if __name__ == '__main__':
    sys.exit(main())
