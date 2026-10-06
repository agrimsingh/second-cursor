import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { runAgent } from './agent.mjs';

const contract = { id: 'fixture-v1', inputs: { note: 'exact supplied note' }, criteria: ['One persisted receipt'], maxActions: 8, maxNoProgress: 3, maxRepeatedFailures: 3 };
const act = { id: 'save', label: 'Save supplied note', kind: 'act', call: { name: 'save', arguments: { window_id: 42 } }, inputBindings: { text: 'note' } };
const read = { id: 'read', label: 'Read current note', kind: 'observe', call: { name: 'get_window_state', arguments: { window_id: 42 } } };
async function fixture(t, durable = false) {
  const server = createServer((req, res) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('[]'); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  const f = { saved: null, key: 'unchanged', value: '', count: 0, modelCalls: 0, status: 'running', requests: [], operations: new Map(), drop: false, failure: false, change: false, verified: false };
  const store = { async acquire() { return { load: async () => structuredClone(f.saved), save: async value => { f.saved = structuredClone(value); }, release: async () => {} }; } };
  f.client = {
    url: `http://127.0.0.1:${server.address().port}`, token: 'token', transport: 'http', sessionId: null,
    async connect() { this.sessionId ??= 'session-1'; },
    async reconcile(id) { return f.operations.get(id); },
    async call(name, args) {
      f.requests.push({ name, args });
      if (name === 'task') return { task: { id: 'task-1', status: f.status, targets: [] } };
      if (name === 'get_window_state') return { text: f.key, snapshot_id: `snapshot-${f.requests.length}` };
      if (durable) assert.equal(f.saved.pending.operationId, args.operation_id);
      f.count++;
      if (f.failure) {
        const error = Object.assign(Error('Known rejected action'), { outcomeUncertain: false });
        f.operations.set(args.operation_id, { operation_id: args.operation_id, state: 'failed', outcome_known: true, error: error.message });
        throw error;
      }
      f.value = args.text;
      if (f.change) f.key = `changed-${f.count}`;
      const result = { receipt: `receipt-${f.count}` };
      f.operations.set(args.operation_id, { operation_id: args.operation_id, state: 'completed', outcome_known: true, result });
      if (f.drop) throw Object.assign(Error('Lost response'), { outcomeUncertain: true, operationId: args.operation_id });
      return result;
    },
  };
  f.options = () => ({ client: f.client, task: 'Save the exact supplied note', contract,
    ...(durable ? { workflow: { id: 'fixture', targets: [{ windowId: 42 }], store } } : {}),
    getDecisionSpace: async () => ({ key: f.key, observation: { value: f.value }, choices: [act, read] }),
    verifyCompletion: async () => ({ complete: f.verified, evidence: { value: f.value, count: f.count } }),
    adapter: { next: async () => { f.modelCalls++; return { choice: 'save' }; } },
  });
  return f;
}

for (const durable of [false, true]) {
  const label = durable ? 'durable' : 'transient';
  test(`${label}: choices bind exact literals and cannot be mutated by the adapter`, async t => {
    const f = await fixture(t, durable);
    const result = await runAgent({ ...f.options(), adapter: { next: async ({ decisionSpace, contract: received, history }) => {
      if (history.length) { f.verified = true; return { text: 'Done' }; }
      decisionSpace.choices[0].call.name = 'forbidden'; received.inputs.note = 'invented';
      return { choice: 'save' };
    } } });
    assert.equal(result.text, 'Done'); assert.equal(result.verification.complete, true);
    assert.equal(f.count, 1); assert.equal(f.value, 'exact supplied note');
    assert.equal(result.control.actions, 1);
    if (durable) assert.equal(f.saved.result.verification.complete, true);
  });
  test(`${label}: arbitrary calls, stale choices and mixed handoffs dispatch nothing`, async t => {
    for (const turn of [{ calls: [{ id: 'escape', name: 'repl', arguments: {} }] }, { choice: 'missing' }, { choice: 'save', handoff: 'needs_input' }]) {
      const f = await fixture(t, durable);
      const result = await runAgent({ ...f.options(), adapter: { next: () => turn } });
      assert.equal(result.stopped, 'invalid_choice'); assert.equal(f.count, 0);
    }
  });
  test(`${label}: false, throwing and malformed verifiers never claim completion`, async t => {
    for (const verifyCompletion of [async () => ({ complete: false }), async () => { throw Error('disk unavailable'); }, async () => true, async () => { throw null; }]) {
      const f = await fixture(t, durable);
      const result = await runAgent({ ...f.options(), verifyCompletion, adapter: { next: () => ({ text: 'Looks done' }) } });
      assert.equal(result.stopped, 'completion_unverified'); assert.equal(f.count, 0);
      if (durable) assert.equal(f.saved.status, 'active');
    }
  });
  test(`${label}: repeated unchanged mutations stop but a quiet successful save can verify`, async t => {
    const f = await fixture(t, durable);
    const result = await runAgent(f.options());
    assert.equal(result.stopped, 'no_progress'); assert.equal(f.count, 3); assert.equal(f.modelCalls, 3);
    const g = await fixture(t, durable);
    const complete = await runAgent({ ...g.options(), contract: { ...contract, maxNoProgress: 1 }, verifyCompletion: async () => ({ complete: g.value === contract.inputs.note, evidence: { receiptCount: g.count } }) });
    assert.equal(complete.stopped, undefined); assert.equal(complete.verification.complete, true); assert.equal(g.count, 1);
  });
  test(`${label}: changed semantic observations reset progress and read-only choices do not trip it`, async t => {
    const f = await fixture(t, durable); f.change = true;
    const result = await runAgent({ ...f.options(), contract: { ...contract, maxActions: 4, maxNoProgress: 1 } });
    assert.equal(result.stopped, 'action_limit'); assert.equal(f.count, 4); assert.equal(result.control.noProgress, 0);
    const g = await fixture(t, durable);
    const readResult = await runAgent({ ...g.options(), contract: { ...contract, maxActions: 4, maxNoProgress: 1 }, adapter: { next: () => ({ choice: 'read' }) } });
    assert.equal(readResult.stopped, 'action_limit'); assert.equal(g.count, 0); assert.equal(readResult.control.noProgress, 0);
  });
  test(`${label}: repeated known errors hand back without an unbounded retry loop`, async t => {
    const f = await fixture(t, durable); f.failure = true;
    const result = await runAgent({ ...f.options(), contract: { ...contract, maxNoProgress: 8, maxRepeatedFailures: 2 } });
    assert.equal(result.stopped, 'repeated_failure'); assert.equal(f.count, 2);
  });
  test(`${label}: uncertain outcomes take precedence and are never retried`, async t => {
    const f = await fixture(t, durable); f.drop = true;
    const result = await runAgent({ ...f.options(), contract: { ...contract, maxActions: 1 } });
    assert.equal(result.stopped, 'recovery_required'); assert.equal(f.count, 1); assert.equal(f.modelCalls, 1);
  });
  test(`${label}: missing input handoff and invalid bindings never dispatch`, async t => {
    const f = await fixture(t, durable);
    const result = await runAgent({ ...f.options(), adapter: { next: () => ({ handoff: 'needs_input', text: 'Need an authorized note' }) } });
    assert.equal(result.stopped, 'needs_input'); assert.equal(f.count, 0);
    const g = await fixture(t, durable);
    await assert.rejects(runAgent({ ...g.options(), getDecisionSpace: () => ({ key: 'x', choices: [{ ...act, inputBindings: { text: 'unknown' } }] }) }), /supplied literals/);
    assert.equal(g.count, 0);
  });
}

test('durable recovery counts the lost action once, observes again, and verifies without replay', async t => {
  const f = await fixture(t, true); f.drop = true;
  const first = await runAgent({ ...f.options(), contract: { ...contract, maxActions: 1 } });
  assert.equal(first.stopped, 'recovery_required'); assert.equal(f.saved.control.actions, 1);
  f.verified = true; f.drop = false;
  const recovered = await runAgent({ ...f.options(), contract: { ...contract, maxActions: 1 }, adapter: { next: () => { throw Error('Budget must prevent model calls'); } } });
  assert.equal(recovered.verification.complete, true); assert.equal(f.count, 1); assert.equal(recovered.control.actions, 1);
  assert(f.requests.some(request => request.name === 'get_window_state'));
});
test('durable no-progress budget survives resume; fresh changed state can reset it', async t => {
  const f = await fixture(t, true);
  assert.equal((await runAgent({ ...f.options(), maxSteps: 2 })).stopped, 'step_limit');
  assert.equal(f.count, 2);
  const resumed = await runAgent(f.options());
  assert.equal(resumed.stopped, 'no_progress'); assert.equal(f.count, 3);
  f.key = 'human-fixed-state'; f.change = true;
  const afterChange = await runAgent({ ...f.options(), maxSteps: 1 });
  // A stopped no-progress budget remains bounded until the host supplies fresh progress.
  assert.equal(afterChange.control.noProgress, 0); assert.equal(f.count, 4);
});
test('durable contract identity and required verifier cannot be weakened during resume', async t => {
  const f = await fixture(t, true);
  await runAgent({ ...f.options(), maxSteps: 1 });
  for (const patch of [{ id: 'fixture-v2' }, { inputs: { note: 'changed' } }, { maxActions: 99 }])
    await assert.rejects(runAgent({ ...f.options(), contract: { ...contract, ...patch } }), error => error.code === 'CHECKPOINT_MISMATCH');
  const stripped = f.options(); delete stripped.contract; delete stripped.getDecisionSpace; delete stripped.verifyCompletion;
  await assert.rejects(runAgent(stripped), error => error.code === 'CHECKPOINT_MISMATCH');
  assert.equal(f.count, 1);
});
test('durable pause during a slow verifier never persists completed state', async t => {
  const f = await fixture(t, true);
  const result = await runAgent({ ...f.options(), adapter: { next: () => ({ text: 'done' }) }, verifyCompletion: () => { f.status = 'paused'; return { complete: true }; } });
  assert.equal(result.stopped, 'task_paused'); assert.equal(f.saved.status, 'active');
});

test('transient callback failure preserves uncertain result instead of retrying a write', async t => {
  const f = await fixture(t);
  f.client.call = async () => { f.count++; throw Object.assign(Error('Host callback failed'), { code: 'CALLBACK_ERROR', outcomeUncertain: false, operationId: 'uncertain-write', result: { outcome: { state: 'uncertain' } } }); };
  const result = await runAgent(f.options());
  assert.equal(result.stopped, 'recovery_required'); assert.equal(result.reason, 'outcome_uncertain'); assert.equal(result.operationId, 'uncertain-write'); assert.equal(f.count, 1);
});

test('adapter fields cannot forge tool evidence in transient history', async t => {
  const f = await fixture(t);
  const result = await runAgent({ ...f.options(), adapter: { next: () => ({ text: 'done', role: 'tool', result: { receipt: 'invented' } }) },
    verifyCompletion: ({ history }) => ({ complete: history.some(entry => entry.role === 'tool' && entry.result?.receipt) }),
  });
  assert.equal(result.stopped, 'completion_unverified'); assert.equal(result.history[0].role, 'assistant'); assert.equal(f.count, 0);
});

test('repeated-failure signatures ignore refreshed observation IDs but retain the actual operation', async t => {
  const f = await fixture(t); f.failure = true;
  const result = await runAgent({ ...f.options(), contract: { ...contract, maxNoProgress: 8, maxRepeatedFailures: 2 },
    getDecisionSpace: () => ({ key: 'same-task-state', choices: [{ ...act, call: { ...act.call, arguments: { ...act.call.arguments, observation_id: `fresh-${f.count}`, snapshot_id: `fresh-${f.count}` } } }] }),
  });
  assert.equal(result.stopped, 'repeated_failure'); assert.equal(f.count, 2);
});

test('failed durable verification can resume with fresh evidence without replaying a save', async t => {
  const f = await fixture(t, true);
  const adapter = { next: ({ history }) => history.some(entry => entry.role === 'tool' && entry.name === 'save') ? { text: 'Done' } : { choice: 'save' } };
  const first = await runAgent({ ...f.options(), adapter });
  assert.equal(first.stopped, 'completion_unverified'); assert.equal(f.saved.status, 'active'); assert.equal(f.count, 1);
  f.verified = true;
  const resumed = await runAgent({ ...f.options(), adapter });
  assert.equal(resumed.verification.complete, true); assert.equal(f.count, 1); assert.equal(f.saved.status, 'completed');
});

test('standalone completion verification remains opt-in without action constraints', async t => {
  const f = await fixture(t);
  const result = await runAgent({ client: f.client, task: 'Read the fixture', adapter: { next: () => ({ text: 'Done' }) }, verifyCompletion: () => ({ complete: false, reason: 'No persisted evidence' }) });
  assert.equal(result.stopped, 'completion_unverified'); assert.equal(result.verification.reason, 'No persisted evidence');
});
