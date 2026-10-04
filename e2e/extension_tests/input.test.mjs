import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

// Exercise the actual input orchestration and its serialized read-only DOM programs. This deliberately models
// focus independently of DOM.focus's return value: accepting the command does not mean the target received it.
const source = (await readFile(new URL('../../extension/input.js', import.meta.url), 'utf8'))
  .replace(/^import .*;\n/gm, '').replace(/export /g, '');

function fixture() {
  const calls = [], nodes = new Map(), objects = new Map(), frames = new Map();
  let next = 0;
  const doc = { documentElement: { frameId: 'main' }, activeElement: null };
  doc.defaultView = { top: null, frameElement: null };
  doc.defaultView.top = doc.defaultView;
  const state = { doc, onFocus: null, afterInsert: null, failCheck: false };
  function element(tag = 'input', options = {}) {
    const root = options.root ?? options.parent?.getRootNode() ?? doc;
    const node = {
      id: ++next, localName: tag, type: 'text', isConnected: true, isContentEditable: false,
      parentElement: options.parent ?? null, ownerDocument: root.host?.ownerDocument ?? root,
      value: '', attributes: {}, ...options,
      getRootNode() { return root; },
      getAttribute(name) { return this.attributes[name] ?? null; },
      matches(selector) { return selector === ':disabled' && !!(this.disabled || this.fieldsetDisabled); },
      contains(other) { for (let n = other; n; n = n.parentElement) if (n === this) return true; return false; },
    };
    nodes.set(node.id, node);
    return node;
  }
  function focus(node) {
    let current = node;
    for (;;) {
      const root = current.getRootNode();
      root.activeElement = current;
      if (root.host) current = root.host;
      else if (root.frameOwner) current = root.frameOwner;
      else return;
    }
  }
  function documentFrame(frameOwner, { opaque = false, frameId = 'child' } = {}) {
    const child = { documentElement: { frameId }, activeElement: null, frameOwner };
    child.defaultView = { top: doc.defaultView, frameElement: opaque ? null : frameOwner };
    frameOwner.contentDocument = opaque ? null : child;
    return child;
  }
  function remote(value) {
    if (value == null) return { type: 'object', subtype: 'null', value: null };
    const objectId = `object-${objects.size}`;
    objects.set(objectId, value);
    return { objectId };
  }
  async function send(session, method, params = {}) {
    calls.push({ session, method, params });
    if (method === 'DOM.resolveNode') {
      if (!nodes.has(params.backendNodeId)) throw Error('No node with given id');
      return { object: remote(nodes.get(params.backendNodeId)) };
    }
    if (method === 'Runtime.evaluate') return { result: remote(vm.runInNewContext(params.expression, { document: doc })) };
    if (method === 'Runtime.callFunctionOn') {
      if (state.failCheck) return { exceptionDetails: { text: 'context unavailable' } };
      const program = vm.runInNewContext(`(${params.functionDeclaration})`);
      const value = program.apply(objects.get(params.objectId), (params.arguments ?? []).map(arg => arg.value));
      return { result: params.returnByValue ? { value } : remote(value) };
    }
    if (method === 'DOM.describeNode') {
      const node = nodes.get(params.backendNodeId) ?? objects.get(params.objectId);
      return { node: { localName: node.localName, frameId: node.frameId, shadowRoots: node.shadowRoots,
        attributes: Object.entries(node.attributes ?? {}).flat() } };
    }
    if (method === 'DOM.focus') { (state.onFocus ?? focus)(nodes.get(params.backendNodeId)); return {}; }
    if (method === 'Input.insertText') {
      let target = doc.activeElement;
      while (target?.shadowRoot?.activeElement || target?.contentDocument?.activeElement)
        target = target.shadowRoot?.activeElement ?? target.contentDocument.activeElement;
      if (target) target.value += params.text;
      state.afterInsert?.();
    }
    return {};
  }
  class BridgeError extends Error { constructor(code, message) { super(message); this.code = code; } }
  const context = vm.createContext({
    cdp: (_tab, method, params) => send('main', method, params),
    frameCDP: (_tab, frame, method, params) => send(frame, method, params),
    frameSession: (_tab, frame) => [...frames.values()].some(value => value.frameId === frame) ? 'session' : undefined,
    isolatedFrame: (_tab, ordinal) => frames.get(ordinal),
    pointTo() {}, show() {}, expectQuickReaction() {}, BridgeError,
    parseChord: () => ({ modifiers: [], key: { key: 'Enter', code: 'Enter', keyCode: 13, text: '\r' } }),
  });
  vm.runInContext(`${source}\nglobalThis.browserType = type;`, context);
  return { ...state, state, doc, element, focus, documentFrame, frames, calls,
    type: params => context.browserType(1, params),
    inputs: () => calls.filter(call => call.method.startsWith('Input.')) };
}

test('a dropdown ref refuses before focus or input and preserves the previously focused field', async () => {
  const f = fixture(), name = f.element(), dropdown = f.element('select');
  f.focus(name);
  await assert.rejects(f.type({ ref: `e${dropdown.id}`, text: 'Priority' }), /dropdown.*select_option/);
  assert.equal(name.value, '');
  assert.equal(f.doc.activeElement, name);
  assert.deepEqual(f.inputs(), []);
  assert(!f.calls.some(call => call.method === 'DOM.focus'));
});

for (const [name, tag, options] of [
  ['readonly', 'input', { readOnly: true }], ['disabled', 'textarea', { disabled: true }],
  ['disabled fieldset', 'input', { fieldsetDisabled: true }], ['checkbox', 'input', { type: 'checkbox' }],
  ['file chooser', 'input', { type: 'file' }], ['button combobox', 'button', { attributes: { role: 'combobox' } }],
  ['ARIA readonly', 'div', { isContentEditable: true, attributes: { 'aria-readonly': 'true' } }],
]) test(`${name} is rejected without input dispatch`, async () => {
  const f = fixture(), target = f.element(tag, options);
  await assert.rejects(f.type({ ref: `e${target.id}`, text: 'unsafe' }), /Typing was not sent/);
  assert.deepEqual(f.inputs(), []);
});

test('inert and ARIA-disabled ancestors prevent typing', async () => {
  for (const options of [{ inert: true }, { attributes: { 'aria-disabled': 'true' } }]) {
    const f = fixture(), parent = f.element('section', options), target = f.element('input', { parent });
    await assert.rejects(f.type({ ref: `e${target.id}`, text: 'unsafe' }), /disabled or inert/);
    assert.deepEqual(f.inputs(), []);
  }
});

for (const [name, tag, options] of [
  ['editable combobox', 'input', { attributes: { role: 'combobox' } }],
  ['textarea', 'textarea', {}], ['contenteditable', 'div', { isContentEditable: true }],
]) test(`${name} receives literal text through trusted CDP input`, async () => {
  const f = fixture(), target = f.element(tag, options), text = '"; window.bad = true; //\n$literal';
  await f.type({ ref: `e${target.id}`, text });
  assert.equal(target.value, text);
  assert.equal(f.inputs()[0].method, 'Input.insertText');
  assert(!f.calls.some(call => call.params.expression?.includes(text) || call.params.functionDeclaration?.includes(text)));
});

test('focus redirects and target removal after DOM.focus refuse input', async () => {
  for (const remove of [false, true]) {
    const f = fixture(), target = f.element(), other = f.element();
    f.state.onFocus = node => { f.focus(remove ? node : other); if (remove) node.isConnected = false; };
    await assert.rejects(f.type({ ref: `e${target.id}`, text: 'unsafe' }), /Typing was not sent/);
    assert.deepEqual(f.inputs(), []);
  }
});

test('contenteditable focus redirected to a nested editor cannot inherit ownership from the requested host', async () => {
  for (const mode of ['separate-editor', 'readonly-editor', 'shadow-editor']) {
    const f = fixture(), target = f.element('div', { isContentEditable: true });
    const island = f.element('div', { parent: target, isContentEditable: false });
    const redirected = f.element('div', { parent: island, isContentEditable: true,
      attributes: mode === 'readonly-editor' ? { 'aria-readonly': 'true' } : {} });
    f.state.onFocus = () => {
      if (mode === 'shadow-editor') {
        target.shadowRoot = { host: target, activeElement: redirected };
        f.focus(target);
      } else f.focus(redirected);
    };
    await assert.rejects(f.type({ ref: `e${target.id}`, text: 'unsafe' }), /focus moved/);
    assert.deepEqual(f.inputs(), []);
  }
});

test('ref typing follows a closed shadow root and rejects focus outside its host', async () => {
  for (const redirect of [false, true]) {
    const f = fixture(), host = f.element('custom-field'), other = f.element();
    const root = { host, activeElement: null }, target = f.element('input', { root });
    if (redirect) f.state.onFocus = node => { f.focus(node); f.doc.activeElement = other; };
    const operation = f.type({ ref: `e${target.id}`, text: 'text' });
    if (redirect) { await assert.rejects(operation, /shadow root/); assert.deepEqual(f.inputs(), []); }
    else { await operation; assert.equal(f.inputs()[0].method, 'Input.insertText'); }
  }
});

test('a closed author shadow host refuses while native user-agent shadow input remains editable', async () => {
  for (const closed of [true, false]) {
    const f = fixture(), target = f.element(closed ? 'div' : 'input', {
      isContentEditable: closed, shadowRoots: [{ shadowRootType: closed ? 'closed' : 'user-agent' }],
    });
    const operation = f.type({ ref: `e${target.id}`, text: 'text' });
    if (closed) { await assert.rejects(operation, /closed shadow host/); assert.deepEqual(f.inputs(), []); }
    else { await operation; assert.equal(target.value, 'text'); }
  }
});

test('same-origin frame focus must also belong to its iframe in the parent document', async () => {
  const f = fixture(), frame = f.element('iframe'), other = f.element();
  const root = f.documentFrame(frame), target = f.element('input', { root });
  f.state.onFocus = node => { f.focus(node); f.doc.activeElement = other; };
  await assert.rejects(f.type({ ref: `e${target.id}`, text: 'unsafe' }), /different element/);
  assert.deepEqual(f.inputs(), []);
});

test('disabled or inert frame owners block typing in both same-origin and isolated frames', async () => {
  for (const isolated of [false, true]) for (const options of [{ attributes: { 'aria-disabled': 'true' } }, { inert: true }]) {
    const f = fixture(), frame = f.element('iframe', options);
    const root = f.documentFrame(frame, { opaque: isolated }), target = f.element('input', { root });
    if (isolated) f.frames.set(1, { frameId: 'child', session: 'session', parent: 0, owner: frame.id });
    await assert.rejects(f.type({ ref: `${isolated ? 'f1' : ''}e${target.id}`, text: 'unsafe' }), /disabled or inert/);
    assert.deepEqual(f.inputs(), []);
  }
});

test('isolated-frame refs require both the verified iframe owner and the exact opaque boundary', async () => {
  for (const mode of ['valid', 'wrong-owner', 'unknown-boundary']) {
    const f = fixture(), frame = f.element('iframe'), other = f.element();
    const root = f.documentFrame(frame, { opaque: true, frameId: mode === 'unknown-boundary' ? 'unknown' : 'child' });
    const target = f.element('input', { root });
    f.frames.set(1, { frameId: 'child', session: 'session', parent: 0, owner: frame.id });
    if (mode === 'wrong-owner') f.state.onFocus = node => { f.focus(node); f.doc.activeElement = other; };
    const operation = f.type({ ref: `f1e${target.id}`, text: 'text' });
    if (mode === 'valid') { await operation; assert.equal(f.inputs().length, 1); }
    else { await assert.rejects(operation, /Typing was not sent/); assert.deepEqual(f.inputs(), []); }
  }
});

test('no-ref typing requires current editable focus, including open shadow and same-origin frames', async () => {
  for (const kind of ['plain', 'shadow', 'frame']) {
    const f = fixture();
    let root = f.doc;
    if (kind === 'shadow') { const host = f.element('custom-field'); root = { host, activeElement: null }; host.shadowRoot = root; }
    if (kind === 'frame') root = f.documentFrame(f.element('iframe'));
    const target = f.element('input', { root });
    f.focus(target);
    await f.type({ text: 'safe' });
    assert.equal(target.value, 'safe');
    assert(!f.calls.some(call => call.method === 'DOM.focus'));
  }
  for (const kind of ['none', 'dropdown', 'opaque-frame', 'closed-shadow']) {
    const f = fixture();
    if (kind !== 'none') f.focus(f.element(kind === 'dropdown' ? 'select' : kind === 'opaque-frame' ? 'iframe' : 'custom-field'));
    await assert.rejects(f.type({ text: 'unsafe' }), /Typing was not sent/);
    assert.deepEqual(f.inputs(), []);
  }
});

test('submit is withheld if an input handler moves focus after insertion', async () => {
  const f = fixture(), target = f.element(), other = f.element();
  f.state.afterInsert = () => f.focus(other);
  await assert.rejects(f.type({ ref: `e${target.id}`, text: 'inserted', submit: true }), /Text was inserted, but Enter was not sent: focus moved/);
  assert.equal(target.value, 'inserted');
  assert.deepEqual(f.inputs().map(call => call.method), ['Input.insertText']);
});

test('failed state evaluation fails closed and always releases its remote object group', async () => {
  const f = fixture(), target = f.element();
  f.state.failCheck = true;
  await assert.rejects(f.type({ ref: `e${target.id}`, text: 'unsafe' }), /could not be verified/);
  assert.deepEqual(f.inputs(), []);
  assert.equal(f.calls.at(-1).method, 'Runtime.releaseObjectGroup');
});
