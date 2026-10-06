#!/usr/bin/env python3
"""Paired real-model recovery workflow: explicit polling vs browser postconditions.

Both arms use one frozen candidate app/extension and fresh Sol 6.1 high host
conversations. Only the four simple browser input tools gain `expect` in the
candidate arm. The model derives the invoice values from visible messy records;
the independent durable ledger judges stale conflict/lost response recovery.
--list is read-only. GUI/model trials require --takeover and run serially.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import pathlib
import sys
import time

from benchmarks.cross_app_engine import _schema_value
from benchmarks.engines import EngineError, ScopeError, ScopedEngine, _ok, payloads, records
from benchmarks.model_host import CodexModelHost, HostConfig, host_version
from benchmarks.workspace_fixtures import WorkspaceFixture
from cross_app_model_support import host_evidence_checks
from run import APP_BUNDLE, Run, SCENARIOS

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
VERSION = 'browser-expect-model/1'
EXPECT_TOOLS = {'browser_click', 'browser_type', 'browser_press_key', 'browser_scroll'}
ALLOWED_TOOLS = EXPECT_TOOLS | {'browser_snapshot', 'browser_screenshot', 'browser_command', 'browser_navigate'}
EXPECT_FIELDS = {'text', 'ref', 'value', 'gone', 'timeout_ms'}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def schedule(seed=401, rounds=2, arm='both'):
    if (type(seed) is not int or type(rounds) is not int or not 1 <= rounds <= 20
            or not 0 <= seed <= 2 ** 63 - rounds):
        raise ValueError('Require a nonnegative seed and 1..20 rounds within the fixture seed range')
    if arm not in {'both', 'poll', 'expect'}:
        raise ValueError('Unknown comparison arm')
    return [{'seed': seed + index, 'arm': candidate} for index in range(rounds)
            for candidate in ((['poll', 'expect'] if index % 2 == 0 else ['expect', 'poll'])
                              if arm == 'both' else [arm])]


def fingerprints():
    files = [ROOT / name for name in ('browser_expect_model.py', 'benchmarks/model_host.py',
        'benchmarks/engines.py', 'benchmarks/cross_app_engine.py', 'benchmarks/workspace_fixtures.py',
        'benchmarks/workspace.html', 'cross_app_model_support.py', 'run.py', 'start-browser.py')]
    # The disposable browser loads the explicit extension override when present,
    # not necessarily the repository or the copy embedded inside the app bundle.
    extension = pathlib.Path(os.environ.get('SC_E2E_EXTENSION', REPO / 'extension')).resolve()
    if not (extension / 'manifest.json').is_file():
        raise FileNotFoundError('Effective browser extension manifest is unavailable')
    files.extend(path for path in extension.rglob('*') if path.is_file())
    files.extend([APP_BUNDLE / 'Contents/MacOS/SecondCursor', APP_BUNDLE / 'Contents/Info.plist'])
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(files))}


def validate_expect(value):
    if not isinstance(value, dict) or set(value) - EXPECT_FIELDS:
        raise ScopeError('expect accepts only text, ref, value, gone and timeout_ms')
    for key in ('text', 'ref', 'value'):
        if key in value and (not isinstance(value[key], str) or (key != 'value' and not value[key])):
            raise ScopeError('expect text/ref must be nonempty strings; value must be a string')
    if not any(key in value for key in ('text', 'ref', 'value')):
        raise ScopeError('expect requires text or ref with value')
    if ('ref' in value) != ('value' in value):
        raise ScopeError('expect ref and value must be supplied together')
    if 'gone' in value and type(value['gone']) is not bool:
        raise ScopeError('expect gone must be boolean')
    if 'timeout_ms' in value and (type(value['timeout_ms']) is not int or not 0 <= value['timeout_ms'] <= 10000):
        raise ScopeError('expect timeout_ms must be an integer between 0 and 10000')
    return copy.deepcopy(value)


class BrowserExpectEngine(ScopedEngine):
    """Narrow extension of the existing public MCP fixture validator.

    The harness creates the task and tab. This wrapper preserves existing key,
    locator, operation and exact-target checks, adds full schema type checking,
    and injects only the caller-owned task session. It never solves the workflow,
    interprets business records, retries inputs, or exposes evaluator endpoints.
    """
    def __init__(self, client, session_id, tab_id, url, arm, *, emit=None, clock_start=None):
        if arm not in {'poll', 'expect'}:
            raise ScopeError('Unknown comparison arm')
        if not isinstance(session_id, str) or not session_id or session_id == 'legacy':
            raise ScopeError('A named fixture task session is required')
        if type(tab_id) is not int or tab_id <= 0:
            raise ScopeError('A positive fixture-owned tab ID is required')
        super().__init__('second-cursor', {'task_id': 'workspace_recovery', 'targets': {'url': url}},
                         client, emit=emit, clock_start=clock_start)
        self.session, self.arm, self.tab_id = session_id, arm, tab_id
        self.owned_tabs = {tab_id}

    def _schema(self, source):
        tool = super()._schema(source)
        name, schema = tool['name'], tool['inputSchema']
        props = schema['properties']
        props['tab_id'] = {'type': 'integer', 'enum': [self.tab_id]}
        schema['required'] = list(dict.fromkeys([*schema.get('required', []), 'tab_id']))
        if self.arm == 'expect' and name in EXPECT_TOOLS:
            declared = props.get('expect')
            if not isinstance(declared, dict):
                raise EngineError('Candidate runtime does not advertise browser expect')
            choices = declared.get('anyOf', [declared])
            condition = next((copy.deepcopy(row) for row in choices if row.get('type') == 'object'), None)
            if condition is None or set(condition.get('properties', {})) != EXPECT_FIELDS:
                raise EngineError('Candidate runtime browser expect schema differs from the pinned contract')
            condition['description'] = declared.get('description', '') + ' This fixture accepts an object, not a JSON string.'
            condition['additionalProperties'] = False
            props['expect'] = condition
        else:
            props.pop('expect', None)
        schema['required'] = [key for key in schema['required'] if key in props]
        tool['description'] = tool.get('description', '') + (
            ' Fixture scope: only the supplied disposable tab. No scripts, CDP, files, clipboard, '
            'browser chrome, other tabs or evaluator access. Batch tools are unavailable.')
        return tool

    def start(self):
        if self.closed:
            raise EngineError('Fixture engine is closed')
        if self.started:
            return self
        cursor, seen = None, set()
        while True:
            page = self.client.rpc('tools/list', {'cursor': cursor} if cursor else {})
            if not isinstance(page.get('tools'), list):
                raise EngineError('Runtime tools/list did not return a catalog')
            for tool in page['tools']:
                if (not isinstance(tool, dict) or not isinstance(tool.get('name'), str)
                        or not isinstance(tool.get('inputSchema'), dict) or tool['name'] in self.catalog):
                    raise EngineError('Runtime returned an invalid or duplicate tool')
                self.catalog[tool['name']] = copy.deepcopy(tool)
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise EngineError('Runtime repeated its catalog cursor')
            seen.add(cursor)
        if ALLOWED_TOOLS - self.catalog.keys():
            raise EngineError('Runtime lacks required browser tools')
        self.allowed = {name: self._schema(self.catalog[name]) for name in sorted(ALLOWED_TOOLS)}
        observation = self._invoke('browser_snapshot', {'tab_id': self.tab_id, 'full': True}, 'setup')
        if not _ok(observation):
            raise EngineError('Runtime refused fixture browser observation')
        self._attest(observation, required=True)
        self.initial_observations = [{'tool': 'browser_snapshot', 'result': observation}]
        self.started = True
        return self

    def _attest(self, result, required=False):
        targets = [row['tab'] for row in records(result) if isinstance(row.get('tab'), dict)]
        if required and not targets:
            raise EngineError('Runtime did not attest the fixture tab')
        if any(type(tab.get('id')) is not int or tab['id'] != self.tab_id or tab.get('url') != self.url for tab in targets):
            self.closed = True
            raise EngineError('Runtime target changed outside the permitted fixture tab')

    def _validate(self, name, args, nested=False):
        if name not in self.allowed:
            raise ScopeError('Tool is outside the browser comparison allowlist')
        _schema_value(args, self.allowed[name]['inputSchema'])
        if isinstance(args, dict) and 'expect' in args:
            if self.arm != 'expect' or name not in EXPECT_TOOLS:
                raise ScopeError('Browser expect is unavailable for this tool or comparison arm')
            validate_expect(args['expect'])
        # The base validator continues to reject nested browser_command expect,
        # hidden locators, unsafe keys and all targets outside owned_tabs.
        return super()._validate(name, args, nested=nested)

    def _invoke(self, name, args, phase='model'):
        dispatched = {**copy.deepcopy(args), 'session_id': self.session}
        begin = (time.monotonic() - self.clock_start) * 1000
        result, status = None, 'error'
        try:
            result = self.client.rpc('tools/call', {'name': name, 'arguments': dispatched})
            if not isinstance(result, dict) or not isinstance(result.get('content'), list):
                raise EngineError('Runtime returned malformed MCP content')
            status = 'completed'
            self._attest(result)
            return result
        finally:
            self.emit({'type': 'tool', 'name': name, 'phase': phase, 'arguments': copy.deepcopy(args),
                'start_ms': begin, 'end_ms': (time.monotonic() - self.clock_start) * 1000,
                'ok': _ok(result) if result is not None else False, 'transport_status': status,
                'retry': False, 'result': result})


def goal_prompt(task_prompt, tab_id, arm):
    common = (task_prompt + f'\n\nThe disposable browser fixture is tab_id={tab_id}. '
        'Use the public browser tools to read the visible supporting records, derive the amount, routing decision '
        'and exact memo yourself, then update only the assigned invoice. Read back its persisted values before '
        'claiming completion. A tool acknowledgement or a visible confirmation alone does not prove the final '
        'business result. Preserve every unrelated record; never repeat an uncertain save before reconciling it. '
        'Only the supplied tab is available; do not access scripts, source files or evaluator state.')
    capability = ('\n\nThis arm exposes optional expect on browser_click, browser_type, browser_press_key and '
        'browser_scroll. Use it when you can name the intended visible postcondition from observed UI context. '
        'It accepts text or ref with exact value, optional gone and timeout_ms (0..10000). A status of verified '
        'means the condition holds after input; preexisting means it already held before input. Failed does not '
        'mean input did not happen, so reconcile before retrying. Choose the expected condition yourself.'
        if arm == 'expect' else
        '\n\nThis arm does not expose expect. Use the returned observations and explicit browser reads or waits '
        'to check the visible result of an action. Choose what to check from the observed UI context yourself.')
    return common + capability


def initial_observation_text(result):
    # Setup creates the first observation revision. Give its genuine text to the
    # solver so its next diff is meaningful; do not silently consume that state.
    text = [block['text'] for block in result.get('content', [])
            if block.get('type') == 'text' and isinstance(block.get('text'), str)]
    if not text:
        raise EngineError('Initial public browser snapshot has no textual observation')
    return '\n\nInitial public browser_snapshot text:\n' + '\n'.join(text)


def metrics(host, host_events, dispatches, fixture, evidence, verified_ms):
    model = [row for row in dispatches if row.get('phase') == 'model']
    expectations = []
    for row in model:
        if 'expect' not in row.get('arguments', {}):
            continue
        outcome = next((data.get('expect') for data in payloads(row.get('result') or {})
                        if isinstance(data, dict) and isinstance(data.get('expect'), dict)), {})
        expectations.append({'tool': row['name'], 'status': outcome.get('status', 'missing'),
                             'waited_ms': outcome.get('waited_ms')})
    return {'model_host_wall_ms': host.get('wall_ms'), 'verified_task_wall_ms': verified_ms,
        'model_tool_calls': host.get('tool_calls'), 'public_dispatch_calls': len(model),
        'public_dispatch_ms': sum(row['end_ms'] - row['start_ms'] for row in model),
        'public_read_calls': sum(row['name'] in {'browser_snapshot', 'browser_screenshot'} or
            row['name'] == 'browser_command' and row.get('arguments', {}).get('operation') in
            {'capabilities', 'dom_snapshot', 'inspect', 'wait', 'screenshot'} for row in model),
        'model_errors': sum(row.get('type') == 'tool' and row.get('status') == 'error' for row in host_events),
        'public_outcome_failures': sum(not row['ok'] for row in model),
        'expect_calls': len(expectations), 'expect_status_counts': {status: sum(row['status'] == status for row in expectations)
            for status in ('verified', 'preexisting', 'failed', 'missing')}, 'expectations': expectations,
        'diagnostics': fixture.diagnostics(evidence),
        'note': 'Verified task time includes isolated host startup, model reasoning, public dispatches and final durable judging. Separate fresh conversations prevent a model-only speed claim.'}


def trial(ctx, entry, config, profile):
    artifact = ctx.run.dir / ctx.name
    artifact.mkdir()
    before = fingerprints()
    ctx.require('frozen app extension and harness before trial', before == profile['sources'])
    write_json(artifact / 'source-before.json', before)
    write_json(artifact / 'profile.json', profile)
    write_json(artifact / 'browser-runtime.json', ctx.run.browser_runtime)
    fixture = WorkspaceFixture('workspace_recovery', entry['seed'], artifact).__enter__()
    ctx.cleanups.append(lambda: fixture.__exit__(None, None, None))
    sid = ctx.call('session', action='create')['session_id']
    ctx.cleanups.append(lambda: ctx.mcp.call('session', action='close', session_id=sid))
    ctx.call('task', action='create', session_id=sid, title=f"Browser expect {entry['arm']} seed {entry['seed']}")
    instances = ctx.call('browser_instances')['browsers']
    ctx.require('exactly one isolated fixture browser', len(instances) == 1)
    browser_id = instances[0]['id']
    tab = ctx.call('browser_open', session_id=sid, browser_id=browser_id, url=fixture.url)['tab']['id']
    ctx.cleanups.append(lambda: ctx.mcp.call('browser_close', session_id=sid, browser_id=browser_id, tab_id=tab))
    host, host_events, dispatches, failure = {}, [], [], None
    started = None
    trace = (artifact / 'model-trace.jsonl').open('w')
    def record(source, event):
        trace.write(json.dumps({'source': source, **event}, ensure_ascii=False, allow_nan=False) + '\n')
        trace.flush()
    def public_event(event):
        dispatches.append(event)
        record('scoped_mcp', event)
    def host_event(event):
        host_events.append(event)
        record('model_host', event)
        if event.get('type') == 'tool':
            print(f"  {entry['arm']} seed {entry['seed']}: {event.get('name')} {event.get('status')}", flush=True)
    try:
        engine = BrowserExpectEngine(ctx.mcp, sid, tab, fixture.url, entry['arm'], emit=public_event).start()
        tools = engine.tools()
        prompt = goal_prompt(fixture.prompt, tab, entry['arm']) + initial_observation_text(engine.initial_observations[0]['result'])
        write_json(artifact / 'model-tool-catalog.json', tools)
        (artifact / 'prompt.txt').write_text(prompt)
        write_json(artifact / 'trial-profile.json', {'entry': entry, 'profile_sha256': digest(profile),
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'catalog_sha256': digest(tools),
            'catalog_characters': len(json.dumps(tools)), 'fixture': fixture.case_metadata(),
            'model_host': dataclasses.asdict(config)})
        started = time.monotonic()
        host = CodexModelHost(config).run(prompt, tools, engine.call, host_event).as_dict()
    except Exception as error:
        failure = f'{type(error).__name__}: {error}'
        record('harness', {'type': 'error', 'error': failure})
    finally:
        write_json(artifact / 'host-result.json', host)
        evidence = fixture.read()
        checks = {'harness_completed_without_exception': failure is None}
        checks.update({'workspace_' + key: value for key, value in fixture.judge(evidence).items()})
        host_checks = host_evidence_checks(host, host_events, model=config.model,
            reasoning_effort=config.reasoning_effort, host_version=config.model_host_version)
        # Workspace HTTP stale/lost replies are visible app state and need not be
        # MCP errors. The independent ledger proves both disruptions occurred.
        host_checks.pop('host_fault_errors_present_in_trace', None)
        checks.update(host_checks)
        after = fingerprints()
        checks['source_and_runtime_unchanged_during_trial'] = after == profile['sources']
        passed = bool(checks) and all(checks.values())
        verified_ms = (time.monotonic() - started) * 1000 if passed and started is not None else None
        result = {'entry': entry, 'profile_sha256': digest(profile), 'harness_error': failure,
            'checks': checks, 'passed': passed, 'workspace': evidence,
            'metrics': metrics(host, host_events, dispatches, fixture, evidence, verified_ms)}
        write_json(artifact / 'independent-result.json', result)
        write_json(artifact / 'source-after.json', after)
        trace.close()
        for label, okay in checks.items():
            ctx.check(label, okay)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--arm', choices=('both', 'poll', 'expect'), default='both')
    result.add_argument('--seed', type=int, default=401)
    result.add_argument('--rounds', type=int, default=2)
    result.add_argument('--model', default='gpt-6.1-sol')
    result.add_argument('--reasoning-effort', default='high', choices=('low', 'medium', 'high', 'xhigh'))
    result.add_argument('--timeout', type=float, default=420)
    result.add_argument('--max-steps', type=int, default=100)
    result.add_argument('--codex-binary', default='codex')
    result.add_argument('--list', action='store_true')
    result.add_argument('--takeover', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    plan = schedule(args.seed, args.rounds, args.arm)
    config = HostConfig(model=args.model, reasoning_effort=args.reasoning_effort, max_steps=args.max_steps,
        timeout_seconds=args.timeout, codex_binary=args.codex_binary, model_host_version=host_version(args.codex_binary))
    profile = {'version': VERSION, 'schedule': plan, 'sources': fingerprints(), 'model_host': dataclasses.asdict(config),
        'browser_visibility': 'headed', 'fixture': 'workspace_recovery',
        'scope': 'Same frozen app/extension and goal; optional simple browser expect vs explicit observations. Fresh conversation per trial.',
        'limits': 'Two seeded pairs are integration evidence for host use of a capability, not a general model or driver ranking. No native, arbitrary-site or physical-user coexistence claim.'}
    if args.list:
        print(json.dumps(profile, indent=2))
        return 0
    if not args.takeover:
        raise SystemExit('GUI/model runs require --takeover while the user is away')
    SCENARIOS.clear()
    for entry in plan:
        def scenario(ctx, entry=entry):
            trial(ctx, entry, config, profile)
        scenario.__name__ = f"browser_expect_model_{entry['arm']}_{entry['seed']}"
        SCENARIOS.append(scenario)
    run = Run(browser=True, installed=False, headed_browser=True)
    write_json(run.dir / 'browser-expect-model-profile.json', profile)
    run.notes.append('Model-directed public MCP browser recovery, same runtime in both arms; final outcome uses the durable ledger, not model claims or expect status.')
    okay = run.run([])
    return 0 if okay and len(run.results) == len(plan) and all(row['status'] == 'PASS' for row in run.results) else 1


if __name__ == '__main__':
    sys.exit(main())
