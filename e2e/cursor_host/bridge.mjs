/** Private JSONL transport for the pinned Cursor SDK. No credentials enter IPC. */
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFile, readdir, mkdir, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const require = createRequire(import.meta.url);
const AUDITED_SDK_HASH = '8c60f53f6ab4772c779437fd732d41a3b7dcf3d9ac0a26ba11e84de119e3bdc0';
const fail = message => { throw new Error(message); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function params(selection) {
  if (!object(selection) || typeof selection.id !== 'string' || !selection.id || !Array.isArray(selection.params))
    fail('Explicit model and parameters required');
  const result = new Map();
  for (const item of selection.params) {
    if (!object(item) || typeof item.id !== 'string' || !item.id || typeof item.value !== 'string' || !item.value || result.has(item.id))
      fail('Malformed or duplicate model parameter');
    result.set(item.id, item.value);
  }
  return result;
}

export function sameSelection(a, b) {
  try {
    const ap = params(a), bp = params(b);
    return a.id === b.id && ap.size === bp.size && [...ap].every(([key, value]) => bp.get(key) === value);
  } catch { return false; }
}

export function validateSelection(selection, catalog) {
  const actual = params(selection), model = catalog.find(m => m.id === selection.id);
  if (!model || !Array.isArray(model.parameters) || actual.size !== model.parameters.length)
    fail('Model unavailable or parameters incomplete');
  for (const definition of model.parameters) {
    if (!definition.values.some(v => v.value === actual.get(definition.id))) fail('Unsupported model parameter');
  }
  return selection;
}

export function validateTools(tools) {
  if (!Array.isArray(tools) || !tools.length) fail('Fixture tools required');
  const names = new Set();
  for (const tool of tools) {
    if (!object(tool) || !/^[A-Za-z0-9_-]{1,64}$/.test(tool.name) || names.has(tool.name)
        || typeof tool.description !== 'string' || !object(tool.inputSchema) || tool.inputSchema.type !== 'object')
      fail('Malformed fixture tool');
    names.add(tool.name);
  }
  return tools;
}

export function restrictedOptions(model, cwd, customTools, store) {
  return { model, tools: ['mcp_tool_call', 'get_mcp_tools_tool_call'], disallowedTools: ['shell', 'task'], mcpServers: {},
    local: { cwd, settingSources: [], enableAgentRetries: false, customTools, store } };
}

export function validateCallback(context, agentId) {
  if (!context || context.sessionId !== agentId) fail('Unexpected callback session');
}

export function validateStreamCall(event, names) {
  if (event.name !== 'mcp' && !names.includes(event.name)) fail('Unexpected SDK tool call');
  if (event.name === 'mcp' && object(event.args)) {
    if (event.args.providerIdentifier !== undefined && event.args.providerIdentifier !== 'custom-user-tools')
      fail('Unexpected MCP provider');
    if (event.args.toolName !== undefined && !names.includes(event.args.toolName)) fail('Unexpected MCP tool');
  }
}

async function identity() {
  const root = resolve(dirname(require.resolve('@cursor/sdk')), '../..');
  const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  const files = ['package.json'];
  async function walk(dir) {
    for (const entry of await readdir(join(root, dir), { withFileTypes: true })) {
      const relative = `${dir}/${entry.name}`;
      if (entry.isDirectory()) await walk(relative);
      else if (entry.isFile() && entry.name.endsWith('.js')) files.push(relative);
    }
  }
  await walk('dist');
  const digest = createHash('sha256');
  for (const file of files.sort()) digest.update(file).update('\0').update(await readFile(join(root, file))).update('\0');
  const hash = digest.digest('hex');
  return { version: `cursor-sdk ${manifest.version} node ${process.version} sha256:${hash}`,
    audited: manifest.version === '1.0.35' && hash === AUDITED_SDK_HASH, hash };
}

const send = message => process.stdout.write(JSON.stringify(message) + '\n');

export async function runBridge(testDependencies = {}) {
  const sdkIdentity = testDependencies.identity ?? await identity();
  if (!sdkIdentity.audited) fail('Cursor SDK differs from the audited runtime');
  const { Agent, Cursor, JsonlLocalAgentStore } = testDependencies.sdk ?? await import('@cursor/sdk');
  const send = message => (testDependencies.output ?? process.stdout).write(JSON.stringify(message) + '\n');
  const reader = createInterface({ input: testDependencies.input ?? process.stdin, crlfDelay: Infinity });
  let initResolve, initReject, initialized = false, fatal, agent, run, directory, timer, selection;
  const initial = new Promise((res, rej) => { initResolve = res; initReject = rej; });
  const pending = new Map(), seen = new Set();
  let sequence = Promise.resolve(), callCount = 0;
  const stop = reason => {
    fatal ??= reason;
    for (const entry of pending.values()) entry.reject(new Error(reason));
    pending.clear();
    void run?.cancel().catch(() => {});
  };
  const checkCancelled = () => { if (fatal) fail(fatal); };
  reader.on('line', line => {
    try {
      const message = JSON.parse(line);
      if (!initialized) {
        if (message.type !== 'init') fail('Expected initialization');
        initialized = true; initResolve(message); return;
      }
      if (message.type === 'cancel') { stop('Parent cancelled'); return; }
      if (message.type !== 'tool_result' || !pending.has(message.call_id)) fail('Unexpected tool response');
      pending.get(message.call_id).resolve(message.result); pending.delete(message.call_id);
    } catch { initReject(new Error('Invalid parent protocol')); stop('Invalid parent protocol'); }
  });
  reader.on('close', () => { initReject(new Error('Parent disconnected')); stop('Parent disconnected'); });
  let result;
  try {
    const input = await initial;
    const tools = validateTools(input.tools);
    if (typeof input.prompt !== 'string' || !input.prompt || typeof input.cwd !== 'string'
        || typeof input.store_dir !== 'string' || resolve(input.store_dir) === resolve(input.cwd)
        || dirname(resolve(input.store_dir)) !== dirname(resolve(input.cwd))
        || !Number.isInteger(input.max_steps) || input.max_steps < 1
        || !Number.isFinite(input.timeout_seconds) || input.timeout_seconds <= 0) fail('Invalid trial request');
    if ((await readdir(input.cwd)).length !== 0) fail('Workspace must be empty');
    timer = setTimeout(() => stop('Trial deadline exceeded'), input.timeout_seconds * 1000);
    selection = validateSelection(input.model, await Cursor.models.list());
    checkCancelled();
    directory = input.store_dir;
    await mkdir(directory, { mode: 0o700 });
    checkCancelled();
    const customTools = Object.fromEntries(tools.map(tool => [tool.name, {
      description: tool.description, inputSchema: tool.inputSchema,
      execute: (arguments_, context) => {
        const task = sequence.then(async () => {
          if (fatal) fail(fatal);
          try { validateCallback(context, agent.agentId); }
          catch { stop('Unexpected callback session'); fail(fatal); }
          const call_id = context.toolCallId;
          if (typeof call_id !== 'string' || !call_id || seen.has(call_id)) { stop('Invalid tool call identity'); fail(fatal); }
          seen.add(call_id);
          // The parent counts and rejects the first over-budget attempt before
          // executing it. A second one here would be a protocol failure.
          if (++callCount > input.max_steps + 1) { stop('Tool budget exceeded'); fail(fatal); }
          const promise = new Promise((resolve, reject) => pending.set(call_id, { resolve, reject }));
          send({ type: 'call', call_id, name: tool.name, arguments: arguments_ });
          return promise;
        });
        sequence = task.catch(() => {});
        return task;
      },
    }]));
    agent = await Agent.create(restrictedOptions(selection, input.cwd, customTools, new JsonlLocalAgentStore(directory)));
    checkCancelled();
    if (!sameSelection(agent.model, selection)) fail('Created agent model differs from request');
    send({ type: 'ready', model: agent.model, version: sdkIdentity.version, isolation: {
      verified: true, model_host: 'cursor-sdk', model_params: Object.fromEntries(params(selection)),
      fixture_tools: tools.map(tool => tool.name), non_fixture_tools: [], inherited_mcp_tool_count: 0,
      workspace_roots: [], environment_access: false, setting_sources: [],
      tools: ['mcp_tool_call', 'get_mcp_tools_tool_call'], disallowed_tools: ['shell', 'task'], automatic_retries: false,
      verification_basis: 'documented-sdk-allowlist-and-pinned-source-audit',
      actual_tool_inventory_observed: false, sdk_source_sha256: sdkIdentity.hash,
      limitation: 'SDK restrictions filter backend tools; this is not an OS sandbox or an observed full tool inventory.',
    } });
    checkCancelled();
    run = await agent.send(input.prompt);
    checkCancelled();
    for await (const event of run.stream()) {
      if (fatal) break;
      if (event.type === 'system') {
        if (event.model && !sameSelection(event.model, selection)) { stop('Stream model changed'); break; }
        if (event.tools && event.tools.some(name => name !== 'mcp' && !Object.hasOwn(customTools, name))) {
          stop('Unexpected SDK tool inventory'); break;
        }
      } else if (event.type === 'tool_call') {
        try { validateStreamCall(event, Object.keys(customTools)); }
        catch { stop('Unexpected SDK tool call'); break; }
      } else if (event.type === 'task') {
        stop('Unexpected SDK subagent event'); break;
      } else if (event.type === 'thinking') send({ type: 'event', event: { type: 'activity' } });
      else if (event.type === 'status') send({ type: 'event', event: { type: 'status', status: event.status } });
      else if (event.type === 'usage') send({ type: 'event', event: { type: 'usage', usage: event.usage } });
      else if (event.type === 'assistant') {
        const text = event.message.content.filter(c => c.type === 'text').map(c => c.text).join('');
        if (text) send({ type: 'event', event: { type: 'assistant', text } });
      }
    }
    if (fatal) fail(fatal);
    const completed = await run.wait();
    if (!sameSelection(completed.model, selection)) fail('Resolved model differs from pinned selection');
    if (completed.status === 'finished') send({ type: 'event', event: { type: 'final_response', text: completed.result ?? '' } });
    result = { type: 'result', status: completed.status, model: completed.model,
      usage: completed.usage, result: completed.result ?? '',
      ...(completed.error ? { error: 'Cursor SDK run failed' } : {}) };
  } catch (error) {
    // Provider errors may contain credentials or private URLs. Do not serialize them.
    result = { type: 'result', status: 'error', model: agent?.model, result: '',
      error: fatal ?? `Cursor bridge failed (${error.name})` };
  } finally {
    clearTimeout(timer);
    try {
      if (run && run.status === 'running') await run.cancel();
      if (agent) await agent[Symbol.asyncDispose]();
    } catch { result = { type: 'result', status: 'error', model: agent?.model, result: '', error: 'SDK cleanup failed' }; }
    if (directory) await rm(directory, { recursive: true, force: true });
    reader.removeAllListeners('close'); reader.close();
  }
  send(result);
}

async function main() {
  const mode = process.argv[2];
  if (mode === '--version') { console.log((await identity()).version); return; }
  if (mode === '--run') { await runBridge(); return; }
  const { Cursor } = await import('@cursor/sdk');
  if (mode === '--status') { send({ status: (await Cursor.auth.status()).status }); return; }
  if (mode === '--catalog') {
    const models = await Cursor.models.list();
    send(process.argv[3] ? models.filter(m => m.id === process.argv[3]) : models); return;
  }
  if (mode === '--login') {
    await Cursor.auth.login({ apiKeyName: 'Second Cursor benchmarks', signal: AbortSignal.timeout(600000) });
    send({ status: 'logged-in' }); return;
  }
  fail('Expected --version, --status, --catalog [model], --login, or --run');
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { send({ type: 'result', status: 'error', error: `Cursor bridge failed (${error.name})` }); process.exitCode = 1; });
}
