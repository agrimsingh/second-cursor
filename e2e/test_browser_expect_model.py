"""No GUI/model work: negative controls for the scoped paired comparison."""
import copy
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.engines import EngineError, ScopeError
from browser_expect_model import (ALLOWED_TOOLS, EXPECT_FIELDS, EXPECT_TOOLS, BrowserExpectEngine,
                                  fingerprints, goal_prompt, initial_observation_text, metrics, schedule, trial, validate_expect)

URL = 'http://127.0.0.1:8123/fixture.html'


def raw(value):
    return {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value}


def catalog():
    result = []
    for name in sorted(ALLOWED_TOOLS):
        fields = {'tab_id': {'type': 'integer'}, 'session_id': {'type': 'string'},
                  'browser_id': {'type': 'string'}, 'observation_id': {'type': 'string'}}
        required = ['tab_id']
        if name in EXPECT_TOOLS:
            condition = {'text': {'type': 'string'}, 'ref': {'type': 'string'}, 'value': {'type': 'string'},
                         'gone': {'type': 'boolean'}, 'timeout_ms': {'type': 'integer', 'minimum': 0, 'maximum': 10000}}
            fields['expect'] = {'anyOf': [{'type': 'object', 'properties': condition, 'additionalProperties': False},
                                         {'type': 'string'}]}
        if name == 'browser_click': fields['ref'] = {'type': 'string'}
        if name == 'browser_type': fields.update({'ref': {'type': 'string'}, 'text': {'type': 'string'}})
        if name == 'browser_press_key': fields['keys'] = {'type': 'string'}
        if name == 'browser_scroll': fields['direction'] = {'type': 'string'}
        if name == 'browser_navigate': fields['url'] = {'type': 'string'}
        if name == 'browser_command':
            fields.update({'operation': {'type': 'string'}, 'arguments': {'type': 'object',
                'description': 'Rendered control arguments.', 'properties': {
                    'locator': {'type': 'object', 'properties': {'selector': {'type': 'string'},
                        'visible': {'type': 'boolean'}}}, 'keys': {'type': 'string'},
                    'value': {'type': 'string'}, 'expect': {'type': 'object'}, 'script': {'type': 'string'}}}})
            required.append('operation')
        result.append({'name': name, 'description': 'Public tool.', 'inputSchema': {
            'type': 'object', 'properties': fields, 'required': required}})
    return result


class FakeMCP:
    def __init__(self):
        self.calls, self.catalog = [], catalog()
        self.result = raw({'tab': {'id': 7, 'url': URL}})

    def rpc(self, method, params):
        if method == 'tools/list': return {'tools': copy.deepcopy(self.catalog)}
        self.calls.append(copy.deepcopy(params))
        return copy.deepcopy(self.result)


def engine(arm='expect'):
    client = FakeMCP()
    return BrowserExpectEngine(client, 'owned-task', 7, URL, arm).start(), client


class BrowserExpectModelTests(unittest.TestCase):
    def test_schedule_is_seed_matched_abba(self):
        self.assertEqual(schedule(), [{'seed': 401, 'arm': 'poll'}, {'seed': 401, 'arm': 'expect'},
                                     {'seed': 402, 'arm': 'expect'}, {'seed': 402, 'arm': 'poll'}])
        for seed, rounds in [(-1, 2), (401, 0), (401, 21), (True, 2), (401, True)]:
            with self.assertRaises(ValueError): schedule(seed, rounds)

    def test_public_capability_is_only_candidate_simple_actions(self):
        candidate, _ = engine()
        baseline, _ = engine('poll')
        for name in ALLOWED_TOOLS:
            candidate_props = candidate.allowed[name]['inputSchema']['properties']
            baseline_props = baseline.allowed[name]['inputSchema']['properties']
            self.assertEqual('expect' in candidate_props, name in EXPECT_TOOLS)
            self.assertNotIn('expect', baseline_props)
            self.assertFalse(candidate.allowed[name]['inputSchema']['additionalProperties'])
            self.assertEqual(candidate_props['tab_id']['enum'], [7])
            self.assertFalse({'session_id', 'browser_id', 'observation_id'} & candidate_props.keys())
            if name in EXPECT_TOOLS:
                self.assertEqual(set(candidate_props['expect']['properties']), EXPECT_FIELDS)
        self.assertFalse({'act', 'batch', 'browser_repl', 'browser_open', 'browser_close'} & candidate.allowed.keys())

    def test_conditions_dispatch_unchanged_and_only_in_owned_session(self):
        scoped, client = engine()
        value = {'text': 'Saved', 'ref': 'e8', 'value': '', 'gone': False, 'timeout_ms': 10000}
        arguments = {'tab_id': 7, 'ref': 'e2', 'expect': value}
        frozen = copy.deepcopy(arguments)
        scoped.call('browser_click', arguments)
        self.assertEqual(arguments, frozen)
        self.assertEqual(client.calls[-1], {'name': 'browser_click', 'arguments': {**arguments, 'session_id': 'owned-task'}})

    def test_all_scope_refusals_dispatch_nothing(self):
        scoped, client = engine()
        bad = [('batch', {'tab_id': 7, 'steps': []}),
               ('browser_click', {'tab_id': 8, 'ref': 'e2', 'expect': {'text': 'Saved'}}),
               ('browser_click', {'tab_id': True, 'ref': 'e2'}),
               ('browser_click', {'tab_id': 7, 'session_id': 'other', 'ref': 'e2'}),
               ('browser_press_key', {'tab_id': 7, 'keys': 'cmd+l'}),
               ('browser_navigate', {'tab_id': 7, 'url': 'https://example.com'}),
               ('browser_command', {'tab_id': 7, 'operation': 'evaluate', 'arguments': {}}),
               ('browser_command', {'tab_id': 7, 'operation': 'inspect', 'arguments': {'locator': {'selector': '#secret', 'visible': False}}}),
               ('browser_command', {'tab_id': 7, 'operation': 'click', 'arguments': {'expect': {'text': 'Saved'}}}),
               ('browser_command', {'tab_id': 7, 'operation': 'click', 'arguments': {'script': 'run()'}}),
               ('browser_command', {'tab_id': 7, 'operation': 'press_key', 'arguments': {'keys': 'cmd+v'}})]
        before = len(client.calls)
        for name, arguments in bad:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ScopeError):
                scoped.call(name, arguments)
        self.assertEqual(len(client.calls), before)

    def test_poll_arm_rejects_even_valid_expect(self):
        scoped, client = engine('poll')
        before = len(client.calls)
        with self.assertRaises(ScopeError):
            scoped.call('browser_click', {'tab_id': 7, 'ref': 'e2', 'expect': {'text': 'Saved'}})
        self.assertEqual(len(client.calls), before)

    def test_invalid_conditions_cannot_dispatch(self):
        scoped, client = engine()
        bad = [{}, {'gone': True}, {'ref': 'e1'}, {'value': 'yes'}, {'text': ''}, {'text': None},
               {'text': 'x', 'gone': 1}, {'text': 'x', 'timeout_ms': True}, {'text': 'x', 'timeout_ms': -1},
               {'text': 'x', 'timeout_ms': 10001}, {'text': 'x', 'timeout_ms': 2.5},
               {'text': 'x', 'script': 'run()'}, {'ref': '', 'value': 'x'}, '{"text":"Saved"}']
        before = len(client.calls)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ScopeError):
                scoped.call('browser_click', {'tab_id': 7, 'ref': 'e2', 'expect': value})
        self.assertEqual(len(client.calls), before)
        self.assertEqual(validate_expect({'text': 'Saved', 'timeout_ms': 0}), {'text': 'Saved', 'timeout_ms': 0})

    def test_runtime_target_change_closes_without_retry(self):
        scoped, client = engine()
        before = len(client.calls)
        client.result = raw({'tab': {'id': 7, 'url': 'https://example.com'}})
        with self.assertRaises(EngineError): scoped.call('browser_snapshot', {'tab_id': 7})
        with self.assertRaises(EngineError): scoped.call('browser_snapshot', {'tab_id': 7})
        self.assertEqual(len(client.calls), before + 1)

    def test_missing_runtime_capability_is_a_failure(self):
        client = FakeMCP()
        for tool in client.catalog:
            tool['inputSchema']['properties'].pop('expect', None)
        with self.assertRaises(EngineError): BrowserExpectEngine(client, 'owned', 7, URL, 'expect').start()
        self.assertEqual(client.calls, [])

    def test_prompt_task_and_instructions_match_before_capability_clause(self):
        poll = goal_prompt('TASK WITH NO HIDDEN ANSWER', 7, 'poll')
        candidate = goal_prompt('TASK WITH NO HIDDEN ANSWER', 7, 'expect')
        self.assertEqual(poll.split('\n\nThis arm')[0], candidate.split('\n\nThis arm')[0])
        self.assertNotIn('RCPT-', poll + candidate)

    def test_model_receives_the_authentic_initial_text_before_later_diffs(self):
        observation = raw({'tab': {'id': 7, 'url': URL}, 'tree': 'Observed page text'})
        observation['content'].append({'type': 'image', 'data': 'not a prompt string'})
        text = initial_observation_text(observation)
        self.assertIn(observation['content'][0]['text'], text)
        self.assertNotIn('not a prompt string', text)
        with self.assertRaises(EngineError): initial_observation_text({'content': []})

    def test_metrics_do_not_duplicate_structured_and_text_expect_or_invent_verification(self):
        class Fixture:
            def diagnostics(self, evidence): return {'commits': 1}
        dispatch = {'phase': 'model', 'name': 'browser_click', 'arguments': {'expect': {'text': 'Saved'}},
                    'start_ms': 10, 'end_ms': 35, 'ok': False, 'result': raw({'expect': {'status': 'failed', 'waited_ms': 25}})}
        actual = metrics({}, [], [dispatch], Fixture(), {}, None)
        self.assertEqual(actual['expect_calls'], 1)
        self.assertEqual(actual['expect_status_counts']['failed'], 1)
        self.assertEqual(actual['public_outcome_failures'], 1)
        self.assertIsNone(actual['verified_task_wall_ms'])

    def test_failed_host_trials_retain_evidence_and_never_report_verified_time(self):
        class Fixture:
            prompt, url = 'A visible fixture task', URL
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def case_metadata(self): return {'seed': 401}
            def read(self): return {'events': [], 'state': 'unchanged'}
            def judge(self, evidence): return {'exact_final_state': False}
            def diagnostics(self, evidence): return {'commits': 0}
        class Context:
            name = 'failed_trial'
            def __init__(self, root):
                self.run = SimpleNamespace(dir=root, browser_runtime={'isolated': True})
                self.cleanups, self.checks, self.mcp = [], {}, object()
            def require(self, label, okay):
                if not okay: raise AssertionError(label)
            def check(self, label, okay): self.checks[label] = okay
            def call(self, name, **arguments):
                if name == 'session': return {'session_id': 'owned-task'}
                if name == 'task': return {}
                if name == 'browser_instances': return {'browsers': [{'id': 'owned-browser'}]}
                if name == 'browser_open': return {'tab': {'id': 7}}
                raise AssertionError('Unexpected setup call')
        fake_engine = SimpleNamespace(initial_observations=[{'result': raw({'tree': 'Visible fixture'})}],
                                      tools=lambda: catalog(), call=lambda *args: None)
        fake_engine.start = lambda: fake_engine
        from benchmarks.model_host import HostConfig
        config = HostConfig(model='gpt-6.1-sol', reasoning_effort='high', max_steps=100,
                            timeout_seconds=420, model_host_version='test-host')
        failure = {'status': 'failed', 'error': 'model deadline', 'tool_calls': 0}
        for thrown in (False, True):
            with self.subTest(exception=thrown), tempfile.TemporaryDirectory() as root:
                ctx = Context(pathlib.Path(root))
                with patch('browser_expect_model.fingerprints', return_value={'stable': 'hash'}), \
                     patch('browser_expect_model.WorkspaceFixture', return_value=Fixture()), \
                     patch('browser_expect_model.BrowserExpectEngine', return_value=fake_engine), \
                     patch('browser_expect_model.CodexModelHost') as host:
                    if thrown: host.return_value.run.side_effect = RuntimeError('failed before result')
                    else: host.return_value.run.return_value.as_dict.return_value = failure
                    trial(ctx, {'seed': 401, 'arm': 'poll'}, config, {'sources': {'stable': 'hash'}})
                artifact = pathlib.Path(root) / ctx.name
                result = json.loads((artifact / 'independent-result.json').read_text())
                retained = json.loads((artifact / 'host-result.json').read_text())
                self.assertFalse(result['passed'])
                self.assertIsNone(result['metrics']['verified_task_wall_ms'])
                self.assertEqual(retained, {} if thrown else failure)
                self.assertEqual(bool(result['harness_error']), thrown)
                self.assertFalse(ctx.checks['workspace_exact_final_state'])

    def test_fingerprints_use_effective_extension_and_detect_runtime_changes(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            extension, app = root / 'override', root / 'Candidate.app'
            extension.mkdir()
            (extension / 'manifest.json').write_text('{}')
            (extension / 'worker.js').write_text('first')
            (app / 'Contents/MacOS').mkdir(parents=True)
            (app / 'Contents/MacOS/SecondCursor').write_bytes(b'one')
            (app / 'Contents/Info.plist').write_text('plist')
            with patch.dict('os.environ', {'SC_E2E_EXTENSION': str(extension)}), patch('browser_expect_model.APP_BUNDLE', app):
                first = fingerprints()
                self.assertIn(str((extension / 'worker.js').resolve()), first)
                (extension / 'worker.js').write_text('second')
                self.assertNotEqual(first, fingerprints())
                (extension / 'worker.js').write_text('first')
                (app / 'Contents/MacOS/SecondCursor').write_bytes(b'two')
                self.assertNotEqual(first, fingerprints())


if __name__ == '__main__': unittest.main()
