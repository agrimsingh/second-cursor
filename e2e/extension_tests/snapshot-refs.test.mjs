import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const readModule = async name => (await readFile(new URL(`../../extension/${name}.js`, import.meta.url), 'utf8'))
  .replace(/^import .*;\n/gm, '').replace(/export /g, '');
const snapshotSource = await readModule('snapshot'), inputSource = await readModule('input');

function fixture() {
  const sessions = new Map([['child', 'session-a']]), calls = [];
  const state = { replaceDuringFacts: false };
  class BridgeError extends Error { constructor(code, message) { super(message); this.code = code; } }
  const frameSession = (_tab, id) => sessions.get(id);
  const frameCDP = async (_tab, frame, method, params, options) => {
    calls.push({ frame, method, params, expectedSession: options?.expectedSession });
    if (options?.expectedSession !== sessions.get(frame)) throw Error('renderer changed');
    if (method === 'DOMSnapshot.captureSnapshot') {
      if (state.replaceDuringFacts) sessions.set(frame, 'session-b');
      return { documents: [], strings: [] };
    }
    if (method === 'Accessibility.getFullAXTree') return { nodes: [
      { nodeId: 'root', role: { value: 'RootWebArea' }, childIds: ['field'] },
      { nodeId: 'field', parentId: 'root', backendDOMNodeId: 8, role: { value: 'textbox' }, name: { value: 'Field' } },
    ] };
    return {};
  };
  const cdp = async (_tab, method) => {
    if (method === 'DOMSnapshot.captureSnapshot') return { documents: [], strings: [] };
    if (method === 'DOM.describeNode') return { node: { frameId: 'child' } };
    if (method === 'Accessibility.getFullAXTree') return { nodes: [
      { nodeId: 'root', role: { value: 'RootWebArea' }, childIds: ['frame'] },
      { nodeId: 'frame', parentId: 'root', backendDOMNodeId: 10, role: { value: 'Iframe' } },
    ] };
    return {};
  };
  const snapshotContext = vm.createContext({ cdp, frameCDP, frameSession, chrome: { tabs: { onRemoved: { addListener() {} } } } });
  vm.runInContext(`${snapshotSource}\nglobalThis.api = { collect, isolatedFrame, frameOrdinal };`, snapshotContext);
  const inputContext = vm.createContext({ cdp, frameCDP, frameSession, BridgeError, isolatedFrame: snapshotContext.api.isolatedFrame });
  vm.runInContext(`${inputSource}\nglobalThis.resolveRef = resolve;`, inputContext);
  return { ...snapshotContext.api, resolve: ref => inputContext.resolveRef(1, ref), sessions, calls, state };
}

test('backend ID reuse after renderer replacement rejects the old snapshot ref and accepts only the new ordinal', async () => {
  const f = fixture();
  const before = (await f.collect(1)).find(node => node.role?.value === 'textbox').ref;
  assert.equal(before, 'f1e8');
  f.sessions.set('child', 'session-b');
  const commands = f.calls.length;
  assert.throws(() => f.resolve(before), /no longer on the page/);
  assert.equal(f.calls.length, commands, 'a stale ref must be rejected before resolving a potentially colliding backend ID');
  const after = (await f.collect(1)).find(node => node.role?.value === 'textbox').ref;
  assert.equal(after, 'f2e8');
  assert.equal(f.isolatedFrame(1, 1).session, 'session-a');
  assert.equal(f.isolatedFrame(1, 2).session, 'session-b');
  assert.throws(() => f.resolve(before), /no longer on the page/);
  await f.resolve(after).send('DOM.resolveNode', { backendNodeId: 8 });
  assert.equal(f.calls.at(-1).expectedSession, 'session-b');
});

test('replacement during snapshot collection cannot label the new renderer with an old-session ordinal', async () => {
  const f = fixture();
  f.state.replaceDuringFacts = true;
  const nodes = await f.collect(1);
  assert(!nodes.some(node => node.ref === 'f1e8'));
  assert(f.calls.every(call => call.expectedSession === 'session-a'));
  assert.throws(() => f.resolve('f1e8'), /no longer on the page/);
});

test('every ancestor session is checked and fresh ancestry never retargets an old ordinal', () => {
  const f = fixture();
  f.sessions.set('parent', 'parent-a');
  const parent = f.frameOrdinal(1, 'parent', 'parent-a', 0, 10);
  const child = f.frameOrdinal(1, 'child', 'session-a', parent, 20);
  f.sessions.set('parent', 'parent-b');
  assert.throws(() => f.resolve(`f${child}e8`), /no longer on the page/);
  const newParent = f.frameOrdinal(1, 'parent', 'parent-b', 0, 11);
  const newChild = f.frameOrdinal(1, 'child', 'session-a', newParent, 21);
  assert.notEqual(newChild, child);
  assert.equal(f.isolatedFrame(1, child).parent, parent);
  assert.equal(f.isolatedFrame(1, child).owner, 20);
  assert.throws(() => f.resolve(`f${child}e8`), /no longer on the page/);
  assert.equal(f.resolve(`f${newChild}e8`).id, 8);
});
