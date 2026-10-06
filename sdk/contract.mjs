// Optional host-owned decision constraints. This module never invokes a model or retries an action.
import { randomUUID, createHash } from 'node:crypto';
import { WorkflowCheckpointError } from './checkpoint.mjs';
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const copy = value => JSON.parse(JSON.stringify(value));
const fail = message => { throw new WorkflowCheckpointError(message, 'INVALID_CONTRACT'); };
const positive = value => Number.isSafeInteger(value) && value > 0;
export function normalizeContract(contract, getDecisionSpace, verifyCompletion) {
  if (contract === undefined) {
    if (getDecisionSpace !== undefined) fail('getDecisionSpace requires a contract');
    return undefined;
  }
  if (!object(contract) || typeof contract.id !== 'string' || !contract.id ||
      !object(contract.inputs) || !Object.values(contract.inputs).every(value => typeof value === 'string') ||
      !Array.isArray(contract.criteria) || !contract.criteria.length || !contract.criteria.every(value => typeof value === 'string' && value.trim()) ||
      !positive(contract.maxActions) || !positive(contract.maxNoProgress ?? 3) || !positive(contract.maxRepeatedFailures ?? 3) ||
      typeof getDecisionSpace !== 'function' || typeof verifyCompletion !== 'function')
    fail('A contract requires id, literal string inputs, criteria, positive budgets, getDecisionSpace, and verifyCompletion');
  return copy({ id: contract.id, inputs: contract.inputs, criteria: contract.criteria, maxActions: contract.maxActions,
    maxNoProgress: contract.maxNoProgress ?? 3, maxRepeatedFailures: contract.maxRepeatedFailures ?? 3 });
}
export function initialControl() {
  return { actions: 0, noProgress: 0, repeatedFailures: 0, lastFailure: null, lastObservationKey: null, awaitingObservation: false };
}
export function validateControl(control) {
  if (!object(control) || !['actions', 'noProgress', 'repeatedFailures'].every(key => Number.isSafeInteger(control[key]) && control[key] >= 0) ||
      !['lastFailure', 'lastObservationKey'].every(key => control[key] === null || typeof control[key] === 'string') || typeof control.awaitingObservation !== 'boolean')
    fail('Invalid persisted contract budget');
}
export function prepareSpace(raw, contract, control) {
  if (!object(raw) || typeof raw.key !== 'string' || !raw.key || !Array.isArray(raw.choices)) fail('Decision space requires a semantic key and choices');
  const space = copy(raw);
  const ids = new Set();
  for (const choice of space.choices) {
    if (!object(choice) || typeof choice.id !== 'string' || !choice.id || ids.has(choice.id) || typeof choice.label !== 'string' || !choice.label ||
        !['act', 'observe'].includes(choice.kind) || !object(choice.call) || typeof choice.call.name !== 'string' || !choice.call.name ||
        (choice.call.arguments !== undefined && !object(choice.call.arguments)) || (choice.inputBindings !== undefined && !object(choice.inputBindings)))
      fail('Choices require unique IDs, labels, kinds, and concrete calls');
    ids.add(choice.id);
    for (const [argument, input] of Object.entries(choice.inputBindings ?? {})) {
      if (['__proto__', 'prototype', 'constructor', 'operation_id'].includes(argument) || typeof input !== 'string' || !Object.hasOwn(contract.inputs, input))
        fail('Choice input bindings must reference supplied literals');
    }
    if (Object.hasOwn(choice.call.arguments ?? {}, 'operation_id')) fail('Operation IDs belong to the host loop');
  }
  if (control.lastObservationKey !== space.key) control.noProgress = 0;
  else if (control.awaitingObservation) control.noProgress++;
  control.awaitingObservation = false;
  control.lastObservationKey = space.key;
  return space;
}
export function contractLimit(contract, control) {
  if (control.repeatedFailures >= contract.maxRepeatedFailures) return 'repeated_failure';
  if (control.noProgress >= contract.maxNoProgress) return 'no_progress';
  if (control.actions >= contract.maxActions) return 'action_limit';
  return null;
}
export function resolveChoice(turn, space, contract) {
  if (!object(turn) || (turn.calls !== undefined && (!Array.isArray(turn.calls) || turn.calls.length))) return { reason: 'Free-form calls are disabled by this contract' };
  if (turn.handoff !== undefined && (!['needs_input', 'needs_agent'].includes(turn.handoff) || turn.choice !== undefined)) return { reason: 'Invalid handoff' };
  if (turn.choice === undefined) return { turn: { ...turn, calls: [] } };
  const choice = space.choices.find(item => item.id === turn.choice);
  if (!choice) return { reason: 'The choice is not present in the current decision space' };
  const args = copy(choice.call.arguments ?? {});
  for (const [argument, key] of Object.entries(choice.inputBindings ?? {})) args[argument] = contract.inputs[key];
  return { turn: { ...turn, calls: [{ id: `choice-${randomUUID()}`, name: choice.call.name, arguments: args }] }, kind: choice.kind };
}
function semanticArguments(value) {
  if (Array.isArray(value)) return value.map(semanticArguments);
  if (!object(value)) return value;
  return Object.fromEntries(Object.keys(value).sort().filter(key => !['operation_id', 'observation_id', 'snapshot_id'].includes(key)).map(key => [key, semanticArguments(value[key])]));
}
export function recordOutcome(control, call, kind, failure) {
  const signature = createHash('sha256').update(JSON.stringify({ name: call.name, arguments: semanticArguments(call.arguments ?? {}) })).digest('hex');
  if (failure) {
    control.repeatedFailures = control.lastFailure === signature ? control.repeatedFailures + 1 : 1;
    control.lastFailure = signature;
  } else { control.repeatedFailures = 0; control.lastFailure = null; }
  if (kind === 'act') control.awaitingObservation = true;
}
export function failedResult(result) {
  return result?.ok === false || result?.isError === true || result?.success === false || result?.outcome?.state === 'failed' ||
    ['results', 'steps'].some(key => Array.isArray(result?.[key]) && result[key].some(step => step?.error || failedResult(step) || failedResult(step?.result)));
}
export async function verify(verifyCompletion, context) {
  try {
    const result = await verifyCompletion(copy(context));
    if (!object(result) || typeof result.complete !== 'boolean') return { complete: false, reason: 'Completion verifier returned an invalid result' };
    return copy(result);
  } catch (error) { return { complete: false, reason: `Completion verification failed: ${error?.message ?? String(error)}` }; }
}
