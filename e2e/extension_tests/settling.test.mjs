import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

// Run the production CDP event handler and settling loop together. A virtual clock gives deterministic four-
// second requests and ten-second timeouts without changing either algorithm or making the suite take minutes.
const source = (await Promise.all(['cdp.js', 'tabs.js'].map(name =>
  readFile(new URL(`../../extension/${name}`, import.meta.url), 'utf8'))))
  .map(text => text.replace(/^import .*;\n/gm, '').replace(/export /g, '')).join('\n');

function fixture() {
  let now = 100000;
  const listeners = new Map(), scheduled = [], calls = [], timings = [];
  const state = { status: 'complete', mutatedAt: null, command: null, failProbe: false };
  const event = name => ({
    addListener(listener) { const values = listeners.get(name) ?? []; values.push(listener); listeners.set(name, values); },
    removeListener(listener) { listeners.set(name, (listeners.get(name) ?? []).filter(value => value !== listener)); },
  });
  const emit = (name, ...args) => { for (const listener of listeners.get(name) ?? []) listener(...args); };
  const advance = async ms => {
    const until = now + ms;
    while (scheduled.length && scheduled[0].at <= until) {
      const next = scheduled.shift();
      now = next.at;
      await next.work();
    }
    now = until;
  };
  class BridgeError extends Error { constructor(code, message) { super(message); this.code = code; } }
  const context = vm.createContext({
    Date: { now: () => now }, BridgeError, sleep: advance,
    addTiming: (...args) => timings.push(args), timed: (_tab, _phase, work) => work(),
    removeCursor() {}, show() {},
    chrome: {
      debugger: {
        onEvent: event('event'), onDetach: event('detach'), attach: async () => {},
        sendCommand: async (target, method, params) => {
          calls.push({ target, method, params });
          const custom = await state.command?.(target, method, params);
          if (custom !== undefined) return custom;
          if (method === 'Page.getFrameTree') return { frameTree: { frame: { id: 'root', loaderId: 'loader' } } };
          if (method === 'Page.createIsolatedWorld') return { executionContextId: 1 };
          if (method === 'Runtime.evaluate') {
            if (state.failProbe) throw Error('context unavailable');
            return { result: { value: { ready: 'complete', mutated: state.mutatedAt !== null,
              quiet: now - (state.mutatedAt ?? 0) } } };
          }
          return {};
        },
      },
      tabs: { onRemoved: event('removed'), onActivated: event('activated'), onUpdated: event('updated'),
        query: async () => [], get: async id => ({ id, status: state.status }) },
      windows: { onRemoved: event('window-removed') },
      alarms: { onAlarm: event('alarm') },
      webNavigation: { onCreatedNavigationTarget: event('created-target') },
      storage: { session: { get: async () => ({ ownership: { 1: { session: 'test', created: true } }, agentTabs: [1] }), set: async () => {} } },
    },
  });
  vm.runInContext(`${source}\nglobalThis.api = { ensureActionNetwork, actionNetworkState, noteInput, noteAction, settled, expectQuickReaction,
    records: tabId => [...requestTracking.get(tabId).requests.values()] };`, context);
  const send = (method, params = {}, { tabId = 1, sessionId } = {}) => emit('event', { tabId, sessionId }, method, params);
  return {
    api: context.api, state, calls, timings, send, advance, now: () => now,
    schedule(delay, work) { scheduled.push({ at: now + delay, work }); scheduled.sort((a, b) => a.at - b.at); },
    async action() { await context.api.noteInput(1); context.api.noteAction(1, now); return now; },
    request(id, { age = 0, type = 'Fetch', frameId = 'root', loaderId = 'loader', sessionId, tabId = 1, redirect = false, httpMethod = 'GET' } = {}) {
      send('Network.requestWillBeSent', { requestId: id, type, frameId, loaderId, wallTime: (now - age) / 1000,
        request: { method: httpMethod },
        ...(redirect ? { redirectResponse: {} } : {}) }, { tabId, sessionId });
    },
    response(id, response = {}, source = {}, hasExtraInfo = false) {
      send('Network.responseReceived', { requestId: id, response: { status: 200,
        headers: { 'Content-Length': '11' }, ...response }, hasExtraInfo }, source);
    },
    data(id, length, source = {}) {
      send('Network.dataReceived', { requestId: id, dataLength: length, encodedDataLength: length }, source);
    },
    finish(id, { failed = false, sessionId, tabId = 1, mutate = false } = {}) {
      send(failed ? 'Network.loadingFailed' : 'Network.loadingFinished', { requestId: id }, { tabId, sessionId });
      if (mutate) state.mutatedAt = now;
    },
    isolated(frame, sessionId, tabId = 1) { send('Target.attachedToTarget', { targetInfo: { targetId: frame }, sessionId }, { tabId }); },
    detach: tabId => emit('detach', { tabId }),
  };
}

test('a four-second action fetch waits through completion and paint while an old long poll is ignored', async () => {
  const f = fixture();
  await f.api.ensureActionNetwork(1);
  f.request('old', { age: 30000 });
  const at = await f.action();
  f.request('new');
  f.schedule(4000, () => f.finish('new', { mutate: true }));
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(result.network, 'observed');
  assert.equal(result.pending_requests, 0);
  assert.equal(f.now() - at, 4300);
  assert.equal(f.calls.filter(call => call.method === 'Network.enable').length, 1);
  assert.equal(f.timings.find(([, phase]) => phase === 'settled_requests')[2], 4000);
});

test('a pre-existing long poll including a redirect never extends a new action deadline', async () => {
  const f = fixture();
  await f.api.ensureActionNetwork(1);
  f.request('poll', { age: 100 });
  const at = await f.action();
  f.schedule(100, () => f.request('poll', { redirect: true }));
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(result.pending_requests, 0);
  assert.equal(f.now() - at, 1200);
});

test('delayed request start inside the reaction grace extends readiness beyond its spinner mutation', async () => {
  const f = fixture();
  const at = await f.action();
  f.schedule(800, () => { f.request('delayed'); f.state.mutatedAt = f.now(); });
  f.schedule(4800, () => f.finish('delayed', { mutate: true }));
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(f.now() - at, 5100);
});

test('a request racing the DOM probe is rechecked before returning quiet', async () => {
  const f = fixture();
  const at = await f.action();
  await f.advance(1500);
  let injected = false;
  f.state.command = (_target, method) => {
    if (method === 'Runtime.evaluate' && !injected) {
      injected = true;
      f.request('race');
      f.schedule(4000, () => f.finish('race', { mutate: true }));
    }
  };
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(f.now() - at, 5800);
});

test('a never-completing action request returns an explicit bounded timeout and never retries input', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('hang');
  const result = await f.api.settled(1);
  assert.equal(result.state, 'timed_out');
  assert.match(result.reason, /Observe again before retrying/);
  assert.equal(result.pending_requests, 1);
  assert.equal(f.now() - at, 10000);
  assert.equal(f.calls.some(call => call.method.startsWith('Input.')), false);
});

test('failed requests finish readiness without being represented as successful mutations', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('fail');
  f.schedule(4000, () => f.finish('fail', { failed: true }));
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(result.failed_requests, 1);
  assert.equal(result.verified, undefined);
  assert.equal(f.now() - at, 4300);
});

test('quick input hints cannot bypass an observed pending request', async () => {
  const f = fixture();
  const at = await f.action();
  f.api.expectQuickReaction(1, 50);
  f.api.noteAction(1, at);
  f.request('unexpected-fetch');
  f.schedule(4000, () => f.finish('unexpected-fetch'));
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 4300);
});

test('isolated frames are enabled before input and request IDs are scoped by session', async () => {
  const f = fixture();
  f.isolated('child', 'child-session');
  const at = await f.action();
  assert.ok(f.calls.some(call => call.method === 'Network.enable' && call.target.sessionId === 'child-session'));
  f.request('same');
  f.request('same', { sessionId: 'child-session', frameId: 'child' });
  f.finish('same');
  f.schedule(4000, () => f.finish('same', { sessionId: 'child-session', mutate: true }));
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 4300);
});

test('a newly attached nested frame participates in settling and detachment drops only its requests', async () => {
  const f = fixture();
  const at = await f.action();
  f.isolated('child', 'new-child');
  f.request('child', { sessionId: 'new-child', frameId: 'child' });
  f.request('root');
  f.send('Target.detachedFromTarget', { sessionId: 'new-child' });
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  f.schedule(4000, () => f.finish('root'));
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 4300);
});

test('navigation clears abandoned document requests but preserves the new document request', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('old-fetch', { loaderId: 'old' });
  f.request('document', { type: 'Document', loaderId: 'new' });
  f.send('Page.frameNavigated', { frame: { id: 'root', loaderId: 'new' } });
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  f.finish('document');
  assert.equal(f.api.actionNetworkState(1, at).pending, 0);
});

test('whole-tab detach drops tracking while keeping other tabs isolated, and the next action re-enables it', async () => {
  const f = fixture();
  await f.action();
  await f.api.ensureActionNetwork(2);
  f.request('one');
  f.request('two', { tabId: 2 });
  f.detach(1);
  assert.equal(f.api.actionNetworkState(1, f.now()).available, false);
  assert.equal(f.api.actionNetworkState(2, f.now()).pending, 1);
  await f.action();
  assert.equal(f.api.actionNetworkState(1, f.now()).pending, 0);
  assert.equal(f.calls.filter(call => call.target.tabId === 1 && call.method === 'Network.enable').length, 2);
});

test('images and WebSockets do not hold Document/XHR/Fetch readiness', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('image', { type: 'Image' });
  f.request('socket', { type: 'WebSocket' });
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 1200);
});

test('request overflow degrades network coverage instead of claiming a complete observation', async () => {
  const f = fixture();
  const at = await f.action();
  for (let index = 0; index < 2050; index++) f.request(`flood-${index}`);
  assert.equal(f.api.actionNetworkState(1, at).available, false);
  assert.equal(f.api.actionNetworkState(1, at).pending, 2048);
  // The two evicted requests have not completed. Finishing every retained request cannot prove quietness.
  for (let index = 2; index < 2050; index++) f.finish(`flood-${index}`);
  const result = await f.api.settled(1);
  assert.equal(result.state, 'unavailable');
  assert.match(result.reason, /bounded capacity/);
});

test('network enable failure does not replay input or claim observed network coverage', async () => {
  const f = fixture();
  f.state.command = (_target, method) => { if (method === 'Network.enable') throw Error('Network unavailable'); };
  await f.action();
  const result = await f.api.settled(1);
  assert.equal(result.network, 'unavailable');
  assert.equal(f.calls.filter(call => call.method === 'Network.enable').length, 1);
  f.state.command = null;
  await f.action();
  assert.equal(f.api.actionNetworkState(1, f.now()).available, true);
});

test('unreadable DOM reports unavailable rather than quiet', async () => {
  const f = fixture();
  await f.action();
  f.state.failProbe = true;
  assert.equal((await f.api.settled(1)).state, 'unavailable');
});

test('an old request event delivered after dispatch uses its browser start time, not delivery time', async () => {
  const f = fixture();
  const at = await f.action();
  f.schedule(100, () => f.request('delayed-delivery', { age: 500 }));
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 1200);
});

test('a dispatch that throws after starting work still leaves the next observation waiting for that work', async () => {
  const f = fixture();
  // Deliberately omit the normal post-action noteAction call, as background.js does after a dispatch error.
  await f.api.noteInput(1);
  const at = f.now();
  f.request('started-before-error');
  f.schedule(4000, () => f.finish('started-before-error', { mutate: true }));
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 4300);
});

test('detaching a frame while its Network enable is pending cannot resurrect its tracking state', async () => {
  const f = fixture();
  const at = await f.action();
  let enabled;
  f.state.command = (target, method) => target.sessionId === 'child' && method === 'Network.enable'
    ? new Promise(resolve => { enabled = resolve; }) : undefined;
  f.isolated('child-frame', 'child');
  assert.equal(f.api.actionNetworkState(1, at).enabling, true);
  f.send('Target.detachedFromTarget', { sessionId: 'child' });
  enabled({});
  await Promise.resolve();
  await Promise.resolve();
  f.request('late-old-frame', { sessionId: 'child', frameId: 'child-frame' });
  assert.equal(f.api.actionNetworkState(1, at).enabling, false);
  assert.equal(f.api.actionNetworkState(1, at).pending, 0);
});

test('same-process frame navigation and removal clear only the replaced frame requests', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('root');
  f.request('old-child', { frameId: 'child', loaderId: 'old-child' });
  f.request('new-child', { frameId: 'child', loaderId: 'new-child', type: 'Document' });
  f.send('Page.frameNavigated', { frame: { id: 'child', parentId: 'root', loaderId: 'new-child' } });
  assert.equal(f.api.actionNetworkState(1, at).pending, 2);
  f.send('Page.frameDetached', { frameId: 'child', reason: 'remove' });
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
});

test('settling Network events do not retain asset URLs until the caller requests assets', async () => {
  const extended = await readFile(new URL('../../extension/extended.js', import.meta.url), 'utf8');
  const program = extended.slice(extended.indexOf('const events ='), extended.indexOf('// This function executes'))
    .replace(/export /g, '');
  const context = vm.createContext({ chrome: { tabs: { onRemoved: { addListener() {} } } }, report() {} });
  vm.runInContext(`${program}\nglobalThis.api = { recordEvent, state };`, context);
  const receive = (id, sessionId) => context.api.recordEvent({ tabId: 1, sessionId }, 'Network.responseReceived', {
    requestId: id, response: { url: 'https://private.invalid/resource', mimeType: 'text/plain', status: 200 },
  });
  receive('unrequested');
  assert.equal(context.api.state(1).assets.size, 0);
  context.api.state(1).collectAssets = true;
  receive('requested');
  receive('isolated', 'child-session');
  assert.equal(context.api.state(1).assets.size, 1);
  assert.equal(context.api.state(1).assets.has('requested'), true);
});

test('a request starting during the final page-load idle query cannot return a stale quiet result', async () => {
  const f = fixture();
  const at = await f.action();
  f.api.noteAction(1, at, true);
  f.send('Page.lifecycleEvent', { frameId: 'root', loaderId: 'loader', name: 'networkIdle' });
  let reads = 0;
  f.state.command = (_target, method) => {
    // The first query creates the DOM probe world; the second checks the current loader's idle signal.
    if (method === 'Page.getFrameTree' && ++reads === 2) {
      f.request('final-await-race');
      f.schedule(4000, () => f.finish('final-await-race', { mutate: true }));
    }
  };
  assert.equal((await f.api.settled(1)).state, 'quiet');
  assert.equal(f.now() - at, 4300);
});

test('unconsumed Fetch body can finish readiness from observed full declared bytes without loadingFinished', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('unconsumed');
  f.schedule(4000, () => {
    f.response('unconsumed');
    f.data('unconsumed', 11);
    f.state.mutatedAt = f.now();
  });
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(result.pending_requests, 0);
  assert.equal(result.body_completion_inferred, 1);
  assert.equal(f.now() - at, 4300);
});

test('response headers and a partial streaming body never finish readiness before the final bytes', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('stream');
  f.response('stream');
  f.data('stream', 5);
  f.state.mutatedAt = f.now();
  f.schedule(4000, () => { f.data('stream', 6); f.state.mutatedAt = f.now(); });
  const result = await f.api.settled(1);
  assert.equal(result.state, 'quiet');
  assert.equal(result.body_completion_inferred, 1);
  assert.equal(f.now() - at, 4300);
});

test('headers alone for a positive-length body remain pending until the bounded deadline', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('headers-only');
  f.response('headers-only');
  const result = await f.api.settled(1);
  assert.equal(result.state, 'timed_out');
  assert.equal(result.pending_requests, 1);
  assert.equal(result.body_completion_inferred, undefined);
  assert.equal(f.now() - at, 10000);
});

for (const headers of [
  {}, { 'Content-Length': '0' }, { 'Content-Length': '-11' }, { 'Content-Length': '+11' },
  { 'Content-Length': '11.0' }, { 'Content-Length': '1.1e1' }, { 'Content-Length': '011' },
  { 'Content-Length': '11, 11' }, { 'Content-Length': '11\n11' }, { 'Content-Length': 11 },
  { 'Content-Length': '9007199254740992' }, { 'Content-Length': '11', 'content-length': '11' },
  { 'Content-Length': '11', 'Content-Encoding': 'gzip' },
  { 'Content-Length': '11', 'Content-Encoding': 'br' },
  { 'Content-Length': '11', 'Content-Encoding': '' },
  { 'Content-Length': '11', 'Content-Encoding': 'identity', 'content-encoding': 'identity' },
  { 'Content-Length': '11', 'Transfer-Encoding': 'chunked' },
  { 'Content-Length': '11', 'Transfer-Encoding': '' },
]) test(`body inference rejects ambiguous or encoded framing ${JSON.stringify(headers)}`, async () => {
  const f = fixture();
  const at = await f.action();
  f.request('body'); f.response('body', { headers }); f.data('body', 11);
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  assert.equal(f.api.actionNetworkState(1, at).inferred, 0);
  f.finish('body');
  assert.equal(f.api.actionNetworkState(1, at).pending, 0);
});

for (const extra of [{ fromDiskCache: true }, { fromPrefetchCache: true }, { fromServiceWorker: true },
  { status: 204 }, { status: 205 }, { status: 304 }, { status: 500 }])
  test(`body inference refuses cached, bodyless, or unsuccessful response ${JSON.stringify(extra)}`, async () => {
    const f = fixture();
    const at = await f.action();
    f.request('body'); f.response('body', extra); f.data('body', 11);
    assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  });

test('HEAD requests and cache events cannot establish full-body inference', async () => {
  for (const kind of ['HEAD', 'cache-before-response', 'cache-after-body']) {
    const f = fixture();
    const at = await f.action();
    f.request('body', { httpMethod: kind === 'HEAD' ? 'HEAD' : 'GET' });
    if (kind === 'cache-before-response') f.send('Network.requestServedFromCache', { requestId: 'body' });
    f.response('body'); f.data('body', 11);
    if (kind === 'cache-after-body') f.send('Network.requestServedFromCache', { requestId: 'body' });
    assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  }
});

test('identity encoding and case-insensitive framing names allow exact byte completion', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('identity');
  f.response('identity', { headers: { 'cOnTeNt-LeNgTh': ' 11 ', 'CONTENT-ENCODING': ' identity ' } });
  f.data('identity', 4); f.data('identity', 7);
  assert.equal(f.api.actionNetworkState(1, at).inferred, 1);
  assert.equal(f.api.actionNetworkState(1, at).pending, 0);
});

test('duplicate response events, extra bytes, and invalid byte counts revoke inferred readiness', async () => {
  for (const invalid of ['duplicate-response', 'extra-byte', 'negative', 'fractional', 'unsafe']) {
    const f = fixture();
    const at = await f.action();
    f.request('body'); f.response('body'); f.data('body', 11);
    assert.equal(f.api.actionNetworkState(1, at).inferred, 1);
    if (invalid === 'duplicate-response') f.response('body');
    else f.data('body', { 'extra-byte': 1, negative: -1, fractional: 0.5, unsafe: Number.MAX_SAFE_INTEGER }[invalid]);
    assert.equal(f.api.actionNetworkState(1, at).inferred, 0);
    assert.equal(f.api.actionNetworkState(1, at).pending, 1);
    f.finish('body');
    assert.equal(f.api.actionNetworkState(1, at).pending, 0);
  }
});

test('redirect resets body counters, requires real finish, and keeps old chains excluded from newer actions', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('body'); f.response('body'); f.data('body', 5);
  f.request('body', { redirect: true }); f.response('body'); f.data('body', 6);
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  f.data('body', 5);
  assert.equal(f.api.records(1)[0].receivedBytes, 11);
  assert.equal(f.api.actionNetworkState(1, at).inferred, 0);
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  f.finish('body');
  assert.equal(f.api.actionNetworkState(1, at).pending, 0);
  await f.advance(10);
  const next = await f.action();
  f.request('body', { redirect: true }); f.response('body');
  assert.equal(f.api.actionNetworkState(1, next).pending, 0);
});

test('real loadingFinished or loadingFailed supersedes the inferred byte hint', async () => {
  for (const failed of [false, true]) {
    const f = fixture();
    const at = await f.action();
    f.request('body'); f.response('body'); f.data('body', 11);
    f.finish('body', { failed });
    const result = f.api.actionNetworkState(1, at);
    assert.equal(result.inferred, 0);
    assert.equal(result.failed, failed ? 1 : 0);
    assert.equal(result.pending, 0);
  }
});

test('body inference retains only numeric and boolean response metadata', async () => {
  const f = fixture();
  await f.action();
  f.request('body');
  f.response('body', { headers: { 'Content-Length': '11', 'Set-Cookie': 'private-cookie', Authorization: 'private-secret' },
    url: 'https://private.invalid/token', mimeType: 'private/type' });
  f.data('body', 11);
  const record = f.api.records(1)[0];
  const serialized = JSON.stringify(record);
  assert.equal(/private|Content-Length|Set-Cookie|Authorization|https/.test(serialized), false);
  assert.equal(record.expectedBytes, 11);
  assert.equal(record.receivedBytes, 11);
});

for (const order of ['before-response', 'after-bytes']) {
  for (const status of [200, 304]) test(`ExtraInfo ${status} ${order} confirms or rejects body inference`, async () => {
    const f = fixture();
    const at = await f.action();
    f.request('body');
    const extra = () => f.send('Network.responseReceivedExtraInfo', { requestId: 'body', statusCode: status });
    if (order === 'before-response') extra();
    f.response('body', {}, {}, true); f.data('body', 11);
    if (order === 'after-bytes') {
      assert.equal(f.api.actionNetworkState(1, at).pending, 1);
      extra();
    }
    assert.equal(f.api.actionNetworkState(1, at).pending, status === 200 ? 0 : 1);
    assert.equal(f.api.actionNetworkState(1, at).inferred, status === 200 ? 1 : 0);
  });
}

test('promised but missing ExtraInfo cannot infer completion from a synthetic cached body', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('body'); f.response('body', {}, {}, true); f.data('body', 11);
  const result = await f.api.settled(1);
  assert.equal(result.state, 'timed_out');
  assert.equal(result.pending_requests, 1);
  assert.equal(f.now() - at, 10000);
});

test('an unexpected 304 ExtraInfo revokes a byte hint even if the response did not promise ExtraInfo', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('body'); f.response('body'); f.data('body', 11);
  f.send('Network.responseReceivedExtraInfo', { requestId: 'body', statusCode: 304 });
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
});

test('duplicate ExtraInfo cannot hide conflicting cached status', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('body'); f.response('body', {}, {}, true); f.data('body', 11);
  f.send('Network.responseReceivedExtraInfo', { requestId: 'body', statusCode: 304 });
  f.send('Network.responseReceivedExtraInfo', { requestId: 'body', statusCode: 200 });
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
});

test('late ExtraInfo from a redirect cannot authorize the next response body', async () => {
  const f = fixture();
  const at = await f.action();
  f.request('body');
  f.request('body', { redirect: true });
  f.send('Network.responseReceivedExtraInfo', { requestId: 'body', statusCode: 200 });
  f.response('body', {}, {}, true); f.data('body', 11);
  assert.equal(f.api.actionNetworkState(1, at).pending, 1);
  f.finish('body');
  assert.equal(f.api.actionNetworkState(1, at).pending, 0);
});
