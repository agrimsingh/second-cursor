/** Optional host-independent loop. The adapter owns provider-specific message/image encoding.
 * Checkpoints are opt-in and contain task content. Execution and approval remain in Second Cursor.
 */
import { randomUUID } from 'node:crypto';
import { WorkflowCheckpointError } from './checkpoint.mjs';
export { fileCheckpointStore, WorkflowCheckpointError } from './checkpoint.mjs';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const untracked = new Set(['session', 'initialize', 'task', 'bind_target', 'operation_status', 'cancel_operation']);
function uncertain(result) {
  if (result?.outcome?.state === 'uncertain' || result?.outcome_known === false || result?.uncertain === true) return true;
  return [result?.results, result?.steps].some(steps => steps != null &&
    (!Array.isArray(steps) || steps.some(step => uncertain(step) || uncertain(step?.result))));
}
function invalid(message, code) { throw new WorkflowCheckpointError(message, code); }
function clone(value) {
  try { return JSON.parse(JSON.stringify(value)); }
  catch (cause) { throw new WorkflowCheckpointError('Workflow state must be JSON serializable', 'INVALID_CHECKPOINT', { cause }); }
}
function targetsOf(targets) {
  if (!Array.isArray(targets) || !targets.length || targets.length > 128) invalid('workflow.targets must identify at least one native window or browser tab');
  return targets.map(target => {
    if (object(target) && Number.isSafeInteger(target.windowId) && target.windowId > 0 && Object.keys(target).length === 1) return { windowId: target.windowId };
    if (object(target) && typeof target.browserId === 'string' && target.browserId && Number.isSafeInteger(target.tabId) && target.tabId > 0 && Object.keys(target).length === 2) return { browserId: target.browserId, tabId: target.tabId };
    invalid('Invalid workflow observation target');
  });
}
function parsedObject(value) {
  if (object(value)) return value;
  if (typeof value === 'string') {
    try { const parsed = JSON.parse(value); return object(parsed) ? parsed : {}; } catch { /* The runtime rejects malformed action arguments. */ }
  }
  return {};
}
function argumentTargets(args) {
  const targets = [];
  if (Number.isSafeInteger(args?.window_id) && args.window_id > 0) targets.push({ windowId: args.window_id });
  if (typeof args?.browser_id === 'string' && args.browser_id && Number.isSafeInteger(args.tab_id) && args.tab_id > 0)
    targets.push({ browserId: args.browser_id, tabId: args.tab_id });
  return targets;
}
function callTargets(name, args) {
  // Extended browser commands lift nested `arguments`, with outer fields winning.
  const outer = name === 'browser_command' ? { ...parsedObject(args.arguments), ...args } : args;
  const targets = argumentTargets(outer);
  if (name !== 'batch' && name !== 'act') return targets;
  // Tab batches pin every step to the top-level tab and browser; step IDs are ignored.
  if (outer.tab_id != null) return targets;
  let steps = outer.steps;
  if (typeof steps === 'string') { try { steps = JSON.parse(steps); } catch { steps = []; } }
  for (const step of Array.isArray(steps) ? steps : []) {
    if (!object(step)) continue;
    // Match native/tab batch parsing: nested `args` wins over flat fields.
    // A native step without its own window uses the top-level default window.
    const effective = { ...step, ...parsedObject(step.args) };
    targets.push(...argumentTargets({
      window_id: effective.window_id ?? outer.window_id,
    }));
  }
  return targets;
}
function runtimeTargets(snapshot) {
  if (snapshot.targets === undefined) return [];
  if (!Array.isArray(snapshot.targets)) invalid('Runtime task returned invalid target identities');
  return snapshot.targets.flatMap(target => {
    const normalized = argumentTargets(target);
    if (!normalized.length) invalid('Runtime task has a target without a concrete window or browser identity');
    return normalized;
  });
}
function mergeTargets(state, additions) {
  for (const target of additions)
    if (!state.targets.some(item => JSON.stringify(item) === JSON.stringify(target))) state.targets.push(target);
  targetsOf(state.targets);
}
function validCall(call) {
  return object(call) && typeof call.id === 'string' && call.id.length > 0 && typeof call.name === 'string' && call.name.length > 0 && (call.arguments === undefined || object(call.arguments));
}
function validateState(state, expected) {
  if (!object(state) || state.version !== 1 || !['active', 'completed'].includes(state.status) || typeof state.sessionId !== 'string' || !state.sessionId ||
      typeof state.taskId !== 'string' || !state.taskId || !Number.isSafeInteger(state.step) || state.step < 0 || !Number.isSafeInteger(state.completedCalls) || state.completedCalls < 0 ||
      !Array.isArray(state.history) || !Array.isArray(state.queue) || !state.queue.every(validCall) || !Array.isArray(state.targets) ||
      !state.history.every(item => object(item) && ['assistant', 'tool'].includes(item.role) && (item.role !== 'tool' || (typeof item.id === 'string' && typeof item.name === 'string'))) ||
      (state.pending !== null && (!object(state.pending) || !validCall(state.pending.call) || typeof state.pending.operationId !== 'string' || !state.pending.operationId)) ||
      (state.pending && (state.queue[0]?.id !== state.pending.call.id || JSON.stringify(state.queue[0]) !== JSON.stringify(state.pending.call))) ||
      (state.status === 'completed' && (state.pending || state.queue.length || !object(state.result)))) invalid('Checkpoint structure or version is invalid');
  targetsOf(state.targets);
  let unresolved = [];
  for (const item of state.history) {
    if (item.role === 'assistant') {
      if (unresolved.length || (item.text !== undefined && typeof item.text !== 'string') ||
          (item.calls !== undefined && (!Array.isArray(item.calls) || !item.calls.every(validCall) || new Set(item.calls.map(call => call.id)).size !== item.calls.length)))
        invalid('Checkpoint contains invalid assistant call history');
      unresolved = [...(item.calls ?? [])];
    } else {
      const call = unresolved.shift();
      if (!call || call.id !== item.id || call.name !== item.name) invalid('Checkpoint contains an unmatched tool result');
      if ((Object.hasOwn(item, 'result') === Object.hasOwn(item, 'error')) ||
          (Object.hasOwn(item, 'result') && !object(item.result)) || (Object.hasOwn(item, 'error') && typeof item.error !== 'string'))
        invalid('Checkpoint tool history requires a result object or an error string');
    }
  }
  if (JSON.stringify(unresolved) !== JSON.stringify(state.queue)) invalid('Checkpoint queue does not match unresolved call history');
  if (state.result?.text !== undefined && typeof state.result.text !== 'string') invalid('Checkpoint result is invalid');
  for (const key of ['id', 'task', 'endpoint', 'transport', 'initialTargets'])
    if (JSON.stringify(state[key]) !== JSON.stringify(expected[key])) invalid(`Checkpoint ${key} does not match this workflow`, 'CHECKPOINT_MISMATCH');
}
function endpointOf(client) {
  const url = new URL(client.url);
  if (url.username || url.password || url.search || url.hash) invalid('Workflow endpoint must not contain credentials or query parameters');
  return client.url;
}
function rejectCredential(value, token) {
  if (!token) return;
  if (typeof value === 'string' && value.includes(token)) invalid('Runtime bearer token must not appear in checkpoint content', 'CHECKPOINT_CREDENTIAL');
  if (Array.isArray(value)) value.forEach(item => rejectCredential(item, token));
  else if (object(value)) for (const [key, item] of Object.entries(value)) { rejectCredential(key, token); rejectCredential(item, token); }
}
async function definitions(client) {
  const response = await fetch(client.url + '/api', {
    method: 'POST', headers: { Authorization: `Bearer ${client.token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ tool: 'tools/list' }), signal: AbortSignal.timeout(client.timeoutMs ?? 65_000),
  });
  if (!response.ok) throw new Error('Could not load tool definitions');
  return response.json();
}

export async function runAgent({ adapter, client, task, maxSteps = 50, onApproval, onTurn, workflow }) {
  if (!adapter?.next) throw new Error('adapter.next is required');
  if (!Number.isSafeInteger(maxSteps) || maxSteps < 1) throw new Error('maxSteps must be a positive integer');
  if (!workflow) return transientAgent({ adapter, client, task, maxSteps, onApproval, onTurn });
  if (typeof task !== 'string' || !task || typeof workflow.id !== 'string' || !workflow.id || !workflow.store?.acquire)
    invalid('Durable workflows require a task, workflow.id, and checkpoint store');
  const initialTargets = targetsOf(workflow.targets);
  const expected = { id: workflow.id, task, endpoint: endpointOf(client), transport: client.transport ?? 'http', initialTargets };
  const lease = await workflow.store.acquire();
  try {
    let state = await lease.load();
    const resumed = state !== null;
    const persist = async () => { state.updatedAt = new Date().toISOString(); validateState(state, expected); rejectCredential(state, client.token); await lease.save(clone(state)); };
    const stopped = (reason, details = {}) => ({ stopped: reason, history: state.history, progress: state.progress, ...details });
    if (resumed) {
      validateState(state, expected);
      if (workflow.taskId && workflow.taskId !== state.taskId) invalid('Checkpoint task identity does not match', 'CHECKPOINT_MISMATCH');
      if (client.sessionId && client.sessionId !== state.sessionId) invalid('Use an unconnected client or the original checkpoint session', 'CHECKPOINT_MISMATCH');
      rejectCredential(state, client.token);
      if (state.status === 'completed') return { ...state.result, history: state.history, progress: state.progress };
      client.sessionId = state.sessionId;
    }
    await client.connect();
    if (resumed && client.sessionId !== state.sessionId) invalid('Runtime changed the checkpoint session', 'CHECKPOINT_MISMATCH');
    const readTask = async () => {
      const snapshot = (await client.call('task', { action: 'get', ...(state?.taskId ? { task_id: state.taskId } : workflow.taskId ? { task_id: workflow.taskId } : {}) })).task;
      if (!snapshot || typeof snapshot.id !== 'string' || !['queued', 'running', 'thinking', 'paused', 'needs_user', 'interrupted', 'completed', 'failed', 'cancelled'].includes(snapshot.status)) invalid('Runtime returned an invalid task lifecycle');
      if ((state?.taskId ?? workflow.taskId) && snapshot.id !== (state?.taskId ?? workflow.taskId)) invalid('Runtime task identity changed', 'CHECKPOINT_MISMATCH');
      const targets = runtimeTargets(snapshot);
      if (state) mergeTargets(state, targets);
      return snapshot;
    };
    const initialTask = await readTask();
    if (!state) {
      state = { version: 1, ...expected, sessionId: client.sessionId, taskId: initialTask.id, targets: clone(initialTargets), status: 'active', step: 0, completedCalls: 0, history: [], queue: [], pending: null, progress: null, createdAt: new Date().toISOString() };
      mergeTargets(state, runtimeTargets(initialTask));
      await persist();
    }
    const allowedTask = snapshot => ['queued', 'running', 'thinking'].includes(snapshot.status);
    if (!allowedTask(initialTask)) return stopped(`task_${initialTask.status}`);

    if (resumed) {
      if (state.pending) {
        const { call, operationId } = state.pending;
        if (untracked.has(call.name)) return stopped('recovery_required', { operationId, reason: 'untracked_control' });
        let recorded;
        try { recorded = await client.reconcile(operationId); }
        catch (error) { return stopped('recovery_required', { operationId, reason: 'journal_unavailable', error: error.message }); }
        if (!recorded || recorded.operation_id !== operationId || recorded.outcome_known !== true || recorded.result_expired === true ||
            !['completed', 'failed', 'cancelled'].includes(recorded.state) || uncertain(recorded) || uncertain(recorded.result) ||
            (recorded.state === 'completed' && !object(recorded.result)))
          return stopped('recovery_required', { operationId, reason: 'outcome_unresolved', operation: recorded });
        state.history.push({ role: 'tool', id: call.id, name: call.name, ...(recorded.state === 'completed' ? { result: recorded.result } : { error: recorded.error ?? `Operation ${recorded.state}` }), operation_id: operationId, recovered: true });
        state.completedCalls++; state.pending = null; state.queue.shift(); await persist();
      }
      // The lost operation may have registered its resolved target after the first task read.
      const recoveredTask = await readTask();
      if (!allowedTask(recoveredTask)) return stopped(`task_${recoveredTask.status}`);
      for (const call of state.queue) state.history.push({ role: 'tool', id: call.id, name: call.name, error: 'Call discarded after interruption; replan using fresh observations', discarded: true });
      state.queue = []; await persist();
      for (const target of state.targets) {
        const name = target.windowId ? 'get_window_state' : 'browser_snapshot';
        const args = target.windowId ? { window_id: target.windowId, structured: true, full: true } : { browser_id: target.browserId, tab_id: target.tabId, full: true };
        const observationCall = { id: `reobserve-${randomUUID()}`, name, arguments: args };
        state.history.push({ role: 'assistant', calls: [observationCall], recovered: true });
        state.queue = [observationCall]; await persist();
        let result;
        try { result = await client.call(name, args); }
        catch (error) { return stopped('recovery_required', { reason: 'observation_failed', error: error.message }); }
        if (uncertain(result) || (!target.windowId && result.freshness?.status !== 'current')) return stopped('recovery_required', { reason: 'observation_not_current' });
        state.history.push({ role: 'tool', id: observationCall.id, name, result, recovered: true });
        state.queue = []; await persist();
      }
    }
    const tools = await definitions(client);
    for (let step = 0; step < maxSteps; step++) {
      const lifecycle = await readTask();
      if (!allowedTask(lifecycle)) return stopped(`task_${lifecycle.status}`);
      const turn = clone(await adapter.next({ task, tools, history: clone(state.history), progress: clone(state.progress) }));
      if (!object(turn) || (turn.text !== undefined && typeof turn.text !== 'string') || (turn.calls !== undefined && (!Array.isArray(turn.calls) || !turn.calls.every(validCall)))) invalid('Adapter returned an invalid turn');
      if (turn.calls && new Set(turn.calls.map(call => call.id)).size !== turn.calls.length) invalid('Call IDs must be unique within a turn');
      await onTurn?.(clone(turn));
      state.history.push({ ...turn, role: 'assistant' });
      if (Object.hasOwn(turn, 'progress')) state.progress = turn.progress;
      state.queue = clone(turn.calls ?? []); state.step++; await persist();
      if (!state.queue.length) {
        state.status = 'completed'; state.result = { ...(turn.text === undefined ? {} : { text: turn.text }) }; await persist();
        return { ...state.result, history: state.history, progress: state.progress };
      }
      while (state.queue.length) {
        const call = state.queue[0];
        const currentTask = await readTask();
        if (!allowedTask(currentTask)) return stopped(`task_${currentTask.status}`);
        let denied;
        try { if (onApproval && !(await onApproval(clone(call)))) denied = 'Host denied this action'; }
        catch (error) { denied = `Host approval failed: ${error.message}`; }
        if (denied) {
          state.history.push({ role: 'tool', id: call.id, name: call.name, error: denied }); state.queue.shift(); await persist(); continue;
        }
        const approvedTask = await readTask();
        if (!allowedTask(approvedTask)) return stopped(`task_${approvedTask.status}`);
        const operationId = randomUUID();
        // Host-assigned identity overrides any model-supplied ID. Persist before crossing the network.
        state.pending = { call, operationId };
        const args = { ...(call.arguments ?? {}), operation_id: operationId };
        mergeTargets(state, callTargets(call.name, args));
        await persist();
        let result, failure;
        try { result = await client.call(call.name, args); }
        catch (error) {
          if (error.code === 'CALLBACK_ERROR' && object(error.result)) result = error.result;
          else if (error.outcomeUncertain !== false) return stopped('recovery_required', { operationId, reason: untracked.has(call.name) ? 'untracked_control' : 'dispatch_uncertain', error: error.message });
          else failure = error.message;
        }
        if (uncertain(result)) return stopped('recovery_required', { operationId, reason: 'outcome_uncertain', result });
        state.history.push({ role: 'tool', id: call.id, name: call.name, ...(failure ? { error: failure } : { result }), operation_id: operationId });
        state.completedCalls++; state.pending = null; state.queue.shift(); await persist();
      }
    }
    return stopped('step_limit');
  } finally { await lease.release(); }
}

async function transientAgent({ adapter, client, task, maxSteps, onApproval, onTurn }) {
  const tools = await definitions(client), history = [];
  for (let step = 0; step < maxSteps; step++) {
    const turn = await adapter.next({ task, tools, history });
    await onTurn?.(turn); history.push({ role: 'assistant', ...turn });
    if (!turn.calls?.length) return { text: turn.text, history };
    for (const call of turn.calls) {
      try {
        if (onApproval && !(await onApproval(call))) throw new Error('Host denied this action');
        const result = await client.call(call.name, call.arguments ?? {});
        history.push({ role: 'tool', id: call.id, name: call.name, result });
      } catch (error) { history.push({ role: 'tool', id: call.id, name: call.name, error: error.message, operation_id: error.operationId }); }
    }
  }
  return { stopped: 'step_limit', history };
}
