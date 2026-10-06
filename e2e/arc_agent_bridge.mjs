/** Real SDK adapter for the Arc-inspired form experiment. JSON lines are host IPC,
 * never provider credentials. The Python host owns the isolated model and judge. */
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';

const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
const { SecondCursor } = await import(pathToFileURL(`${config.sdk}/index.mjs`));
const { runAgent } = await import(pathToFileURL(`${config.sdk}/agent.mjs`));
const runtime = JSON.parse(await readFile(`${process.env.SC_HOME}/config.json`, 'utf8'));
const client = new SecondCursor({ url: `http://127.0.0.1:${runtime.port}`, token: runtime.token,
  sessionId: config.session_id, transport: 'http' });
const lines = createInterface({ input: process.stdin });
const iterator = lines[Symbol.asyncIterator]();
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const receive = async () => {
  const item = await iterator.next();
  if (item.done) throw new Error('Model bridge input closed');
  return JSON.parse(item.value);
};
const target = { browser_id: config.browser_id, tab_id: config.tab_id };
const buttons = new Set(['Review invoice', 'Save invoice', 'Back to editing', 'Reload invoice']);
const fields = { 'Approved amount ($)': ['fill', 'amount'], Decision: ['select_option', 'route'], 'Review note': ['fill', 'note'] };
let observed, observationId, calls = 0;
const realCall = client.call.bind(client);
client.call = async (name, args = {}, options) => {
  const started = performance.now();
  let result, error;
  try { result = await realCall(name, args, options); return result; }
  catch (failure) { error = { message: failure.message, code: failure.code, outcomeUncertain: failure.outcomeUncertain }; throw failure; }
  finally { emit({ type: 'sdk_call', name, arguments: args, result, error,
    elapsed_ms: performance.now() - started, number: ++calls }); }
};
async function observe() {
  const text = await client.call('browser_command', { ...target, operation: 'inspect', locator: { selector: '#workspace' }, length: 30000 });
  const dom = await client.call('browser_command', { ...target, operation: 'dom_snapshot' });
  if (dom.url !== config.url) throw new Error('Browser left the permitted fixture URL');
  const snapshot = await client.call('browser_snapshot', { ...target, full: true });
  if (!snapshot.snapshot_id || snapshot.freshness?.status !== 'current') throw new Error('A fresh browser snapshot is required');
  observationId = snapshot.snapshot_id;
  const elements = (dom.elements ?? []).filter(item => item.visible && item.enabled);
  const modal = elements.some(item => item.role === 'dialog');
  observed = { text: text.text, controls: elements.filter(item => item.role === 'button' || fields[item.name])
    .filter(item => modal ? ['Save invoice', 'Back to editing'].includes(item.name) : item.role === 'button' ? buttons.has(item.name) : true)
    .map(({ role, name, value, actions, options }) => ({ role, name, value, actions, options })) };
  return observed;
}
function rawCall(packet) {
  if (packet.operation === 'observe') return { name: 'browser_command', arguments: { ...target, operation: 'inspect', locator: { selector: '#workspace' }, length: 30000 } };
  const matches = observed.controls.filter(item => item.name === packet.name);
  if (matches.length !== 1) throw new Error('Choose exactly one currently observed control');
  const control = matches[0];
  if (packet.operation === 'click' && control.role === 'button' && buttons.has(control.name))
    return { name: 'browser_command', arguments: { ...target, observation_id: observationId, operation: 'click', locator: { role: 'button', name: packet.name, exact: true } } };
  if (fields[control.name]?.[0] !== packet.operation || typeof packet.value !== 'string') throw new Error('Unsupported operation for current control');
  return { name: 'browser_command', arguments: { ...target, observation_id: observationId, operation: packet.operation,
    locator: { label: packet.name, exact: true }, [packet.operation === 'fill' ? 'text' : 'value']: packet.value } };
}
async function decisionSpace() {
  const observation = await observe(), choices = [];
  for (const [index, control] of observation.controls.entries()) {
    if (control.role === 'button') choices.push({ id: `control-${index}`, label: `Click ${control.name}`, kind: 'act',
      call: rawCall({ operation: 'click', name: control.name }) });
    else if (fields[control.name]) {
      const [operation, key] = fields[control.name];
      const call = rawCall({ operation, name: control.name, value: config.inputs[key] });
      delete call.arguments[operation === 'fill' ? 'text' : 'value'];
      choices.push({ id: `control-${index}`, label: `Set ${control.name} to supplied ${key}`, kind: 'act', call,
        inputBindings: { [operation === 'fill' ? 'text' : 'value']: key } });
    }
  }
  choices.push({ id: 'observe', label: 'Read the current workspace', kind: 'observe', call: rawCall({ operation: 'observe' }) });
  return { key: createHash('sha256').update(JSON.stringify(observation)).digest('hex'), observation, choices };
}
const adapter = { async next(input) {
  const observation = config.mode === 'candidate' ? input.decisionSpace.observation : await observe();
  const latest = [...input.history].reverse().find(item => item.role === 'tool');
  emit({ type: 'ready', observation, previous: latest,
    choices: input.decisionSpace?.choices?.map(({ id, label, kind }) => ({ id, label, kind })) });
  const packet = await receive();
  if (packet.finish === true) return { text: 'Form completed; inspect independent persisted evidence.' };
  if (config.mode === 'candidate') return { choice: packet.choice_id };
  return { calls: [{ id: randomUUID(), ...rawCall(packet) }] };
} };
try {
  const result = await runAgent({ client, adapter, task: config.task, maxSteps: 25,
    ...(config.mode === 'candidate' ? {
      contract: { id: 'arc-invoice-form-v1', inputs: config.inputs,
        criteria: ['The assigned invoice is reconciled with the supplied values, persisted exactly once, and unrelated records remain unchanged.'],
        maxActions: 20, maxNoProgress: 3, maxRepeatedFailures: 3 },
      getDecisionSpace: decisionSpace,
      verifyCompletion: async ({ reason }) => {
        emit({ type: 'verify', reason });
        const reply = await receive();
        if (reply.type !== 'verification') throw new Error('Expected host verification reply');
        return { complete: reply.complete === true, evidence: { independent_ledger_checks_passed: reply.complete === true } };
      },
    } : {}),
    onApproval: call => call.name === 'browser_command' && call.arguments?.browser_id === target.browser_id && call.arguments?.tab_id === target.tab_id,
  });
  emit({ type: 'done', result, calls });
} catch (error) {
  emit({ type: 'fatal', error: { name: error.name, message: error.message, code: error.code } });
  process.exitCode = 1;
} finally { lines.close(); }
