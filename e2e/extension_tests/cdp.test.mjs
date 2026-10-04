import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = (await readFile(new URL('../../extension/cdp.js', import.meta.url), 'utf8'))
  .replace(/^import .*;\n/gm, '').replace(/export /g, '');

function fixture() {
  const listeners = {}, calls = [];
  const state = { inputGuard: null, command: null };
  const event = name => ({ addListener(listener) { listeners[name] = listener; } });
  class BridgeError extends Error { constructor(code, message) { super(message); this.code = code; } }
  const context = vm.createContext({
    BridgeError,
    requireAgentInput: async () => { await state.inputGuard?.(); },
    requireAgentTab: async () => {},
    chrome: {
      debugger: {
        onEvent: event('event'), onDetach: event('detach'), attach: async () => {},
        sendCommand: async (target, method, params) => {
          calls.push({ target, method, params });
          return await state.command?.(target, method, params) ?? {};
        },
      },
      tabs: { onRemoved: event('removed') },
    },
  });
  vm.runInContext(`${source}\nglobalThis.api = { attach, frameCDP, frameSession };`, context);
  return {
    api: context.api, state, calls,
    isolated(tab, frame, session) {
      listeners.event({ tabId: tab }, 'Target.attachedToTarget', { targetInfo: { targetId: frame }, sessionId: session });
    },
    lost(tab, session) { listeners.event({ tabId: tab }, 'Target.detachedFromTarget', { sessionId: session }); },
    detach: tab => listeners.detach({ tabId: tab }),
    remove: tab => listeners.removed(tab),
    commands: method => calls.filter(call => call.method === method),
  };
}

test('root and same-process frames retain their intentional tab-session routing', async () => {
  const f = fixture();
  await f.api.frameCDP(1, 'main', 'Page.createIsolatedWorld', { frameId: 'main' });
  await f.api.frameCDP(1, 'local-child', 'Runtime.evaluate', { contextId: 7, expression: 'document.title' });
  assert.equal(f.commands('Page.createIsolatedWorld')[0].target.sessionId, undefined);
  assert.equal(f.commands('Runtime.evaluate')[0].target.sessionId, undefined);
});

test('an isolated frame uses its session and cannot fall back after the session disappears', async () => {
  const f = fixture();
  f.isolated(1, 'child', 'session-a');
  await f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 8 });
  assert.equal(f.commands('DOM.focus')[0].target.sessionId, 'session-a');
  f.lost(1, 'session-a');
  await assert.rejects(f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 8 }), /frame changed or detached/);
  assert.equal(f.commands('DOM.focus').length, 1);
});

for (const replacement of [false, true]) test(`a ${replacement ? 'replaced' : 'detached'} session during ownership checks prevents dispatch`, async () => {
  const f = fixture();
  await f.api.attach(1);
  f.isolated(1, 'child', 'session-a');
  f.state.inputGuard = () => {
    f.lost(1, 'session-a');
    if (replacement) f.isolated(1, 'child', 'session-b');
  };
  await assert.rejects(f.api.frameCDP(1, 'child', 'Input.insertText', { text: 'must not reach main' }), /frame changed or detached/);
  assert.equal(f.commands('Input.insertText').length, 0);
});

test('a pinned ref cannot follow a replacement renderer between commands', async () => {
  const f = fixture();
  f.isolated(1, 'child', 'session-a');
  await f.api.frameCDP(1, 'child', 'DOM.resolveNode', { backendNodeId: 8 }, { expectedSession: 'session-a' });
  f.isolated(1, 'child', 'session-b');
  await assert.rejects(f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 8 }, { expectedSession: 'session-a' }), /frame changed or detached/);
  assert.equal(f.commands('DOM.focus').length, 0);
  await f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 9 }, { expectedSession: 'session-b' });
  assert.equal(f.commands('DOM.focus')[0].target.sessionId, 'session-b');
});

test('whole-tab detach clears live sessions but preserves stale-ref refusal and other tabs', async () => {
  const f = fixture();
  f.isolated(1, 'child', 'a');
  f.isolated(1, 'nested', 'b');
  f.isolated(2, 'child', 'c');
  f.detach(1);
  assert.equal(f.api.frameSession(1, 'child'), undefined);
  assert.equal(f.api.frameSession(1, 'nested'), undefined);
  assert.equal(f.api.frameSession(2, 'child'), 'c');
  await assert.rejects(f.api.frameCDP(1, 'child', 'DOM.resolveNode', { backendNodeId: 8 }), /frame changed or detached/);
  assert.equal(f.commands('DOM.resolveNode').length, 0);
});

test('an explicit fresh context can prove a frame migrated back to the main renderer, but cannot revive pinned refs', async () => {
  const f = fixture();
  f.isolated(1, 'child', 'session-a');
  f.lost(1, 'session-a');
  f.state.command = async (_target, method) => method === 'Page.createIsolatedWorld' ? { executionContextId: 42 } : {};
  const context = await f.api.frameCDP(1, 'child', 'Page.createIsolatedWorld', { frameId: 'child', worldName: 'fresh' });
  assert.equal(context.executionContextId, 42);
  assert.equal(f.commands('Page.createIsolatedWorld')[0].target.sessionId, undefined);
  await f.api.frameCDP(1, 'child', 'Runtime.evaluate', { contextId: 42, expression: 'document.title' });
  assert.equal(f.commands('Runtime.evaluate')[0].target.sessionId, undefined);
  await assert.rejects(f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 8 }, { expectedSession: 'session-a' }), /frame changed or detached/);
  assert.equal(f.commands('DOM.focus').length, 0);
});

test('failed or mismatched frame-context proof cannot authorize a root fallback', async () => {
  for (const failure of ['invalid-context', 'frame-detached', 'wrong-frame']) {
    const f = fixture();
    f.isolated(1, 'child', 'session-a');
    f.lost(1, 'session-a');
    f.state.command = async (_target, method) => {
      if (method !== 'Page.createIsolatedWorld') return {};
      if (failure === 'frame-detached') throw Error('No frame with given id found');
      return {};
    };
    await assert.rejects(f.api.frameCDP(1, 'child', 'Page.createIsolatedWorld', {
      frameId: failure === 'wrong-frame' ? 'main' : 'child',
    }));
    await assert.rejects(f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 8 }), /frame changed or detached/);
    assert.equal(f.commands('DOM.focus').length, 0);
    if (failure === 'wrong-frame') assert.equal(f.commands('Page.createIsolatedWorld').length, 0);
  }
});

test('session detach events are scoped to their tab and tab closure clears its live sessions', () => {
  const f = fixture();
  f.isolated(1, 'child', 'same-id');
  f.isolated(2, 'child', 'same-id');
  f.lost(1, 'same-id');
  assert.equal(f.api.frameSession(2, 'child'), 'same-id');
  f.remove(2);
  assert.equal(f.api.frameSession(2, 'child'), undefined);
});

test('a CDP session error is returned without retrying the command against the root', async () => {
  const f = fixture();
  f.isolated(1, 'child', 'session-a');
  f.state.command = async (_target, method) => {
    if (method === 'DOM.focus') throw Error('{"code":-32001,"message":"Session with given id not found"}');
  };
  await assert.rejects(f.api.frameCDP(1, 'child', 'DOM.focus', { backendNodeId: 8 }), /Session with given id not found/);
  assert.equal(f.commands('DOM.focus').length, 1);
  assert.equal(f.commands('DOM.focus')[0].target.sessionId, 'session-a');
});
