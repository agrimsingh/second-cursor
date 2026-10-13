import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const moduleSource = async name => (await readFile(new URL(`../../extension/${name}.js`, import.meta.url), 'utf8'))
  .replace(/^import[\s\S]*?;\n/gm, '').replace(/export /g, '');
const source = await moduleSource('expectations');
const background = (await moduleSource('background')).replace(/\nconnect\(\);\s*$/, '');
const snapshotSource = await moduleSource('snapshot');
const inputSource = await moduleSource('input');
class BridgeError extends Error { constructor(code, message) { super(message); this.code = code; } }

function fixture({ integrate = false } = {}) {
  let now = 1000, generation = 0, active = true, owner = 'session', dialog = null, serial = 0;
  const scheduled = [], reads = [], inputs = [], observations = [], sent = [];
  const state = { document: 'doc1', elements: [], sample: null, action: null, observation: null, observe: 'tree' };
  const advance = async ms => {
    const until = now + ms;
    while (scheduled.length && scheduled[0].at <= until) { const task = scheduled.shift(); now = task.at; await task.work(); }
    now = until;
  };
  const context = vm.createContext({
    Date: { now: () => now }, performance: { now: () => now }, crypto: { randomUUID: () => `token-${++serial}` },
    BridgeError, sleep: advance, setTimeout, clearTimeout,
    conditionSnapshot: async (tab, guard) => {
      reads.push(tab); await guard();
      if (state.sample) return state.sample(tab, guard);
      return { document: state.document, elements: structuredClone(state.elements) };
    },
    requireAgentTab: async (tab, session) => { if (tab !== 1 || !owner || (session && session !== owner)) throw Error('ownership'); return { id: tab }; },
    requireSessionInput: async () => { if (!active) throw Error('paused'); },
    sessionControlGeneration: async () => generation,
    openDialog: () => dialog,
    chrome: { tabs: { onRemoved: { addListener() {} }, get: async id => ({ id }) },
      runtime: { getManifest: () => ({ version: 'test' }), onStartup: { addListener() {} }, onInstalled: { addListener() {} } },
      alarms: { onAlarm: { addListener() {} }, get: async () => undefined, create() {} },
      windows: { WINDOW_ID_NONE: -1, onFocusChanged: { addListener() {} } },
      debugger: { onEvent: { addListener() {} } } },
    attach: async () => {}, show() {}, hideSession: async () => {}, setCaption() {},
    structuredSnapshot: async (_tab, guard) => { observations.push('tree'); if (state.observation) return state.observation(guard); return { outline: 'public result', elements: [] }; },
    browserRevision: async () => 'revision', sessionIsActive: async () => active,
    describe: tab => tab, settled: async () => { observations.push('settled'); return { state: 'quiet' }; },
    screenshot: async () => { observations.push('screenshot'); return { data: 'png' }; },
    timing: { timed: (_tab, _phase, work) => work(), add() {}, begin() {}, end() {} },
    noteAction() {}, noteInput: async () => {}, setTrackedQuietMs() {},
    withDialog: async (_tab, work) => { const value = await work(); return dialog ? { dialog_open: true, dialog } : value; },
    click: async () => { inputs.push('click'); await state.action?.(); },
    type: async () => { inputs.push('type'); await state.action?.(); },
    pressKey: async () => { inputs.push('key'); await state.action?.(); },
    scroll: async () => { inputs.push('scroll'); await state.action?.(); },
    settleAround: async (_tab, action) => action(),
    beginAgentInput: async () => ({}), endAgentInput() {}, takeReports: () => [],
    recordEvent() {}, extended: async () => ({}),
  });
  vm.runInContext(source + (integrate ? `\n${background}` : '') + `\nglobalThis.api = {
    parseExpectation, prepareExpectation, waitExpectation, prepareBatchExpectation, finishBatchExpectation,
    expectationOutcome, cancelExpectations${integrate ? ', handlers, dispatch' : ''} };`, context);
  const params = expect => ({ tabId: 1, session_id: 'session', expect });
  return { ...context.api, params, state, reads, inputs, observations, sent, advance, now: () => now,
    control({ paused = false, newOwner = owner } = {}) { generation++; active = !paused; owner = newOwner; },
    dialog(value) { dialog = value; },
    schedule(ms, work) { scheduled.push({ at: now + ms, work }); scheduled.sort((a, b) => a.at - b.at); },
    async dispatch(method, args) { await context.api.dispatch({ postMessage: result => sent.push(result) },
      { id: 1, method, params: { ...params(), ...args }, receivedAt: now, controlGeneration: generation }); return sent.at(-1); },
  };
}
const text = value => ({ ref: 'e1', name: value, value: '', secret: false });
const field = value => ({ ref: 'e2', name: 'Amount', value, secret: false });

for (const invalid of [[], 'Saved', {}, { text: '' }, { ref: 'e2' }, { value: '' }, { ref: 'bad', value: '' },
  { text: 1 }, { text: 'Saved', gone: 'false' }, { text: 'Saved', timeout_ms: -1 },
  { text: 'Saved', timeout_ms: 10001 }, { text: 'Saved', timeout_ms: 1.5 }, { text: 'Saved', typo: true }]) {
  test(`invalid expectation is rejected before dispatch: ${JSON.stringify(invalid)}`, async () => {
    const f = fixture({ integrate: true });
    await assert.rejects(() => f.handlers['tab.click']({ ...f.params(invalid), ref: 'e1', observe: 'none' }), /expect/);
    assert.equal(f.inputs.length, 0);
  });
}

for (const delay of [0, 300, 800, 1500, 4000]) {
  test(`waits for a ${delay} ms delayed effect without repeating the mutation`, async () => {
    const f = fixture({ integrate: true });
    f.state.action = () => f.schedule(delay, () => { f.state.elements = [text('Saved')]; });
    const reply = await f.handlers['tab.click']({ ...f.params({ text: 'Saved', timeout_ms: 5000 }), ref: 'e1', observe: 'tree' });
    assert.equal(reply.expect.status, 'verified');
    assert.equal(reply.outcome.verified, true);
    assert.equal(f.inputs.length, 1);
    assert.deepEqual(f.observations, ['tree'], 'explicit postconditions do not add a second generic settling wait');
    assert(reply.expect.waited_ms >= delay);
  });
}

for (const observe of ['none', 'tree', 'screenshot', 'both']) {
  test(`expect is preserved for observe=${observe}`, async () => {
    const f = fixture({ integrate: true });
    f.state.action = () => { f.state.elements = [text('Saved')]; };
    const reply = await f.handlers['tab.click']({ ...f.params({ text: 'Saved' }), ref: 'e1', observe });
    assert.equal(reply.expect.status, 'verified');
    assert.equal(!!reply.observation, ['tree', 'both'].includes(observe));
    assert.equal(!!reply.screenshot, ['screenshot', 'both'].includes(observe));
  });
}

for (const method of ['tab.click', 'tab.type', 'tab.pressKey', 'tab.scroll']) {
  test(`${method} checks its explicit postcondition`, async () => {
    const f = fixture({ integrate: true });
    f.state.action = () => { f.state.elements = [text('Saved')]; };
    const reply = await f.dispatch(method, { expect: { text: 'Saved' }, observe: 'none' });
    assert.equal(reply.result.expect.status, 'verified'); assert.equal(f.inputs.length, 1);
  });
}

test('preexisting confirmation never claims the action caused success', async () => {
  const f = fixture({ integrate: true }); f.state.elements = [text('Saved')];
  const reply = await f.handlers['tab.click']({ ...f.params({ text: 'Saved' }), observe: 'none' });
  assert.equal(reply.expect.status, 'preexisting'); assert.equal(reply.outcome.verified, false);
  assert.equal(reply.outcome.state, 'dispatched');
});

test('timeout is result data with one mutation and no retry', async () => {
  const f = fixture({ integrate: true });
  const reply = await f.dispatch('tab.click', { expect: { text: 'Saved', timeout_ms: 100 }, observe: 'none' });
  assert.equal(reply.result.expect.reason, 'timeout'); assert.equal(reply.result.outcome.state, 'uncertain');
  assert.equal(f.inputs.length, 1); assert.equal(reply.error, undefined);
});

test('gone waits for the combined condition to stop holding', async () => {
  const f = fixture(); f.state.elements = [text('Saving'), field('100')];
  const prepared = await f.prepareExpectation(f.params({ text: 'Saving', ref: 'e2', value: '100', gone: true }));
  f.state.elements = [text('Saved'), field('100')];
  assert.equal((await f.waitExpectation(prepared)).status, 'verified');
});

test('untruncated ref values require an exact suffix match', async () => {
  const f = fixture(); const prefix = 'v'.repeat(1800);
  f.state.elements = [field(prefix + 'old')];
  const prepared = await f.prepareExpectation(f.params({ ref: 'e2', value: prefix + 'new', timeout_ms: 0 }));
  assert.equal((await f.waitExpectation(prepared)).reason, 'timeout');
  f.state.elements = [field(prefix + 'old')];
  const next = await f.prepareExpectation(f.params({ ref: 'e2', value: prefix + 'new', timeout_ms: 0 }));
  f.state.elements = [field(prefix + 'new')];
  assert.equal((await f.waitExpectation(next)).status, 'verified');
});

for (const change of ['replacement', 'navigation']) {
  test(`a ref never retargets after ${change}`, async () => {
    const f = fixture(); f.state.elements = [field('old')];
    const prepared = await f.prepareExpectation(f.params({ ref: 'e2', value: 'new' }));
    f.state.elements = [{ ...field('new'), ref: change === 'replacement' ? 'e3' : 'e2' }];
    if (change === 'navigation') f.state.document = 'doc2';
    assert.equal((await f.waitExpectation(prepared)).reason, 'target_changed');
  });
}

test('text can follow same-tab navigation but never reads another tab', async () => {
  const f = fixture(); const prepared = await f.prepareExpectation(f.params({ text: 'Saved' }));
  f.state.document = 'doc2'; f.state.elements = [text('Saved')];
  assert.equal((await f.waitExpectation(prepared)).status, 'verified');
  assert(f.reads.every(tab => tab === 1));
});

test('secret values cannot be verified or echoed, including the redaction marker', async () => {
  for (const value of ['SYNTHETIC_SECRET_VALUE', '[redacted]']) {
    const f = fixture(); f.state.elements = [{ ...field(value), secret: true }];
    await assert.rejects(() => f.prepareExpectation(f.params({ ref: 'e2', value })), error => {
      assert(!error.message.includes(value)); return true;
    });
    const prepared = await f.prepareExpectation(f.params({ text: value, timeout_ms: 0 }));
    const result = await f.waitExpectation(prepared);
    assert.equal(result.reason, 'timeout'); assert(!JSON.stringify(result).includes(value));
  }
});

for (const condition of [{ text: 'Saved' }, { text: 'Saving', gone: true }]) {
  test(`incomplete page coverage cannot verify ${JSON.stringify(condition)}`, async () => {
    const f = fixture(); const prepared = await f.prepareExpectation(f.params(condition));
    f.state.sample = () => { throw Error('missing frame SYNTHETIC_SECRET_VALUE'); };
    const result = await f.waitExpectation(prepared);
    assert.equal(result.reason, 'unavailable'); assert(!JSON.stringify(result).includes('SYNTHETIC_SECRET_VALUE'));
  });
}

for (const change of ['pause', 'resume', 'owner', 'dialog', 'deadline']) {
  test(`${change} stops a hung post-action read and prevents later reads`, async () => {
    const f = fixture(); const prepared = await f.prepareExpectation({ ...f.params({ text: 'Saved' }), deadline: f.now() + 400 });
    let release, continued = false;
    f.state.sample = async (_tab, guard) => { await new Promise(resolve => { release = resolve; }); await guard(); continued = true; return { document: 'doc1', elements: [text('Saved')] }; };
    if (change !== 'deadline') f.schedule(100, () => {
      if (change === 'dialog') f.dialog({ type: 'confirm' });
      else f.control({ paused: change === 'pause', newOwner: change === 'owner' ? 'other' : 'session' });
    });
    const result = await f.waitExpectation(prepared);
    assert.equal(result.reason, change === 'dialog' ? 'dialog_open' : change === 'deadline' ? 'deadline' : 'control_changed');
    release(); await Promise.resolve(); await Promise.resolve(); assert.equal(continued, false);
  });
}

test('dispatch preserves dialog interruption metadata and one input', async () => {
  const f = fixture({ integrate: true }); f.state.action = () => f.dialog({ type: 'confirm', message: 'Continue?' });
  const reply = await f.dispatch('tab.click', { expect: { text: 'Saved' }, observe: 'tree' });
  assert.equal(reply.result.expect.reason, 'dialog_open'); assert.equal(reply.result.dialog_open, true);
  assert.equal(f.inputs.length, 1); assert.equal(f.reads.length, 1); assert.equal(f.observations.length, 0);
});

test('batch tokens are single-use and bound to the original session and tab', async () => {
  const f = fixture(); const token = await f.prepareBatchExpectation(f.params({ text: 'Saved' }));
  f.state.elements = [text('Saved')];
  for (const wrong of [{ tabId: 2 }, { session_id: 'other' }])
    assert.equal((await f.finishBatchExpectation({ ...f.params(), ...token, complete: true, ...wrong })).reason, 'token_unavailable');
  assert.equal((await f.finishBatchExpectation({ ...f.params(), ...token, complete: true })).status, 'verified');
  assert.equal((await f.finishBatchExpectation({ ...f.params(), ...token, complete: true })).reason, 'token_unavailable');
});

test('failed batches cannot verify even when their text happens to be present', async () => {
  const f = fixture(); const token = await f.prepareBatchExpectation(f.params({ text: 'Saved' }));
  f.state.elements = [text('Saved')]; const before = f.reads.length;
  assert.equal((await f.finishBatchExpectation({ ...f.params(), ...token, complete: false })).reason, 'batch_incomplete');
  assert.equal(f.reads.length, before);
});

test('expired tokens, session invalidation and pending capacity are bounded', async () => {
  const f = fixture(); const token = await f.prepareBatchExpectation({ ...f.params({ text: 'Saved' }), deadline: f.now() + 100 });
  await f.advance(101);
  assert.equal((await f.finishBatchExpectation({ ...f.params(), ...token, complete: true })).reason, 'token_unavailable');
  for (let i = 0; i < 128; i++) await f.prepareBatchExpectation(f.params({ text: 'Saved' }));
  await assert.rejects(() => f.prepareBatchExpectation(f.params({ text: 'Saved' })), /Too many/);
  f.cancelExpectations('session');
  assert((await f.prepareBatchExpectation(f.params({ text: 'Saved' }))).expect_token);
});

test('default actions still use the existing generic settling path', async () => {
  const f = fixture({ integrate: true });
  const result = await f.handlers['tab.click']({ ...f.params(), observe: 'tree' });
  assert.equal(result.expect, undefined); assert.deepEqual(f.observations, ['settled', 'tree']);
});

// Exercise the production AX collector separately; matcher tests above use already-filtered public samples.
function snapshotFixture({ factsFail = false, childFails = false, emptyChild = false, emptyFacts = false, missingFacts = false, nativeInternals = false, nativeLostValue = false, ownedPublic = false, unknownUnderOwned = false, unknownNative = null, nativeFallback = false, inactiveModal = false, nativeSelect = false, navigating = false } = {}) {
  let framesRead = 0;
  const value = 'x'.repeat(1800) + 'tail';
  const nodes = [
    { nodeId: 'root', role: { value: 'RootWebArea' }, name: { value: 'Saved title' }, childIds: ['hidden', 'private', 'public', 'overlay', ...(childFails || emptyChild ? ['frame'] : [])] },
    { nodeId: 'hidden', parentId: 'root', backendDOMNodeId: 1, ignored: true, role: { value: 'StaticText' }, name: { value: 'Hidden Saved' } },
    { nodeId: 'private', parentId: 'root', backendDOMNodeId: 2, role: { value: 'textbox' }, name: { value: 'Password' }, value: { value: 'SYNTHETIC_SECRET_VALUE' } },
    { nodeId: 'public', parentId: 'root', backendDOMNodeId: 3, role: { value: 'textbox' }, name: { value: 'Notes' }, value: { value } },
    { nodeId: 'overlay', parentId: 'root', backendDOMNodeId: 4, role: { value: 'StaticText' }, name: { value: 'Overlay Saved' } },
    ...(childFails || emptyChild ? [{ nodeId: 'frame', parentId: 'root', backendDOMNodeId: 5, role: { value: 'Iframe' } }] : []),
  ];
  if (nativeInternals || nativeLostValue) {
    // Actual Chrome 152 protocol shape: DOM INPUT -> AX generic editable renderer -> AX StaticText;
    // renderer/text backend IDs are omitted from DOMSnapshot's nodes.backendNodeId array.
    for (const id of ['private', 'public']) {
      const host = nodes.find(node => node.nodeId === id);
      host.childIds = [id + '-renderer'];
      nodes.push({ nodeId: id + '-renderer', parentId: id, backendDOMNodeId: 100 + host.backendDOMNodeId,
        role: { value: 'generic' }, properties: [{ name: 'editable', value: { value: 'plaintext' } }], childIds: [id + '-text'] },
        { nodeId: id + '-text', parentId: id + '-renderer', backendDOMNodeId: 200 + host.backendDOMNodeId,
          role: { value: 'StaticText' }, name: { value: host.value.value }, properties: [{ name: 'editable', value: { value: 'plaintext' } }] });
      if (nativeLostValue && id === 'public') delete host.value;
    }
  }
  if (ownedPublic || unknownUnderOwned) {
    const host = nodes.find(node => node.nodeId === 'public');
    host.childIds = [...(host.childIds ?? []), 'owned-list'];
    nodes.push({ nodeId: 'owned-list', parentId: 'public', backendDOMNodeId: 6,
      role: { value: 'listbox' }, name: { value: 'Suggestions' }, childIds: ['owned-option'] },
      { nodeId: 'owned-option', parentId: 'owned-list', backendDOMNodeId: unknownUnderOwned ? 999 : 7,
        role: { value: 'option' }, name: { value: 'Saving approval' } });
  }
  if (unknownNative) {
    const host = nodes.find(node => node.nodeId === 'public');
    host.childIds = [...(host.childIds ?? []), 'new-owned'];
    nodes.push({ nodeId: 'new-owned', parentId: 'public', backendDOMNodeId: 9999,
      role: { value: unknownNative === 'interactive' ? 'option' : 'generic' },
      name: { value: unknownNative === 'value' ? '' : 'NEW_SUGGESTION_CREATED_AFTER_DOM_FACTS' },
      ...(unknownNative === 'value' ? { value: { value: 'NEW_DISTINCT_VALUE' } } : {}) });
  }
  if (nativeFallback !== false) {
    delete nodes.find(node => node.nodeId === 'public').value;
    delete nodes.find(node => node.nodeId === 'private').value;
  }
  if (inactiveModal) {
    const withOptions = String(inactiveModal).startsWith('covered-');
    nodes[0].childIds.push('modal', 'inactive-renderer');
    nodes.push({ nodeId: 'modal', parentId: 'root', backendDOMNodeId: 8,
      role: { value: 'dialog' }, name: { value: 'Review before saving' }, childIds: [] },
      { nodeId: 'inactive-renderer', parentId: 'root', backendDOMNodeId: 999,
        ignored: inactiveModal !== 'visible', role: { value: 'none' },
        childIds: withOptions ? ['inactive-option'] : inactiveModal === 'child' ? ['unknown-descendant'] : [],
        ...(inactiveModal === 'named' ? { name: { value: 'Unreadable content' } } : {}),
        ignoredReasons: [{ name: 'activeModalDialog', value: { type: 'idref', relatedNodes: [
          { backendDOMNodeId: inactiveModal === 'unbound' ? 99999 : 8 } ] } }] });
    if (withOptions) {
      nodes.push({ nodeId: 'inactive-option', parentId: inactiveModal === 'covered-wrong-parent' ? 'root' : 'inactive-renderer',
        backendDOMNodeId: inactiveModal === 'covered-missing-facts' ? 777 : 6,
        ignored: inactiveModal !== 'covered-visible', role: { value: inactiveModal === 'covered-role' ? 'option' : 'none' },
        childIds: inactiveModal === 'covered-descendant' ? ['unread-descendant'] : [],
        ...(inactiveModal === 'covered-name' ? { name: { value: 'Unreadable option' } } : {}),
        ...(inactiveModal === 'covered-value' ? { value: { value: 'Unreadable value' } } : {}),
        ignoredReasons: [{ name: 'activeModalDialog', value: { relatedNodes: [
          { backendDOMNodeId: inactiveModal === 'covered-wrong-modal' ? 99999 : 8 } ] } }] });
      if (inactiveModal === 'covered-missing-child') nodes.pop();
    }
  }
  if (nativeSelect) {
    const host = nodes.find(node => node.nodeId === 'public');
    host.role = { value: 'combobox' }; host.name = { value: nativeSelect === 'secret' ? 'Password' : 'Decision' };
    host.value = { value: 'Choose a decision' }; host.childIds = ['select-popup'];
    nodes.push({ nodeId: 'select-popup', parentId: nativeSelect === 'unknown-parent' ? 'root' : 'public', backendDOMNodeId: 999,
      ignored: false, role: { type: nativeSelect === 'wrong-role' ? 'role' : 'internalRole', value: 'MenuListPopup' },
      name: { value: nativeSelect === 'named' ? 'Uncovered text' : '' },
      ...(nativeSelect === 'value' ? { value: { value: 'Uncovered value' } } : {}),
      childIds: ['select-standard', 'select-finance'] },
      { nodeId: 'select-standard', parentId: 'select-popup', backendDOMNodeId: 6, role: { value: 'option' }, name: { value: 'Standard' } },
      { nodeId: 'select-finance', parentId: 'select-popup', backendDOMNodeId: nativeSelect === 'unknown-child' ? 99999 : 7,
        role: { value: 'option' }, name: { value: nativeSelect === 'secret' ? 'SYNTHETIC_SECRET_OPTION' : 'Finance review' } });
  }
  const strings = ['INPUT', 'DIV', 'id', 'second-cursor-overlay', 'SYNTHETIC_SECRET_VALUE', ...(typeof nativeFallback === 'string' ? [nativeFallback] : []), 'SELECT'];
  const cdp = async (_tab, method, params) => {
    if (method === 'Page.getFrameTree') return { frameTree: { frame: { id: 'main', loaderId: navigating && framesRead++ ? 'other' : 'doc' } } };
    if (method === 'Accessibility.getFullAXTree') { if (params?.frameId) { if (emptyChild) return { nodes: [] }; throw Error('frame unavailable'); } return { nodes: structuredClone(nodes) }; }
    if (method === 'DOM.describeNode') return { node: { frameId: 'child' } };
    if (method === 'DOMSnapshot.captureSnapshot') {
      if (factsFail) throw Error('privacy facts unavailable');
      if (emptyFacts) return { documents: [], strings: [] };
      return { strings, documents: [{ nodes: { nodeType: [1, 1, 1, 1, 1, 1, 1, 1], nodeName: [1, 0, nativeSelect && nativeSelect !== 'non-select-parent' ? strings.indexOf('SELECT') : 0, 1, 1, 1, 1, 1],
        backendNodeId: [1, missingFacts ? 20 : 2, 3, 4, 5, 6, 7, 8], parentIndex: [-1, -1, -1, -1, -1, -1, 5, -1], attributes: [[], [], [], [2, 3], [], [], [], []],
        ...(nativeFallback !== false ? { inputValue: { index: [1, 2], value: [4, nativeFallback === true ? -1 : 5] } } : {}) },
        layout: { nodeIndex: [], styles: [], bounds: [] } }] };
    }
    throw Error('unexpected command ' + method);
  };
  const context = vm.createContext({ cdp, frameSession: () => null,
    chrome: { tabs: { onRemoved: { addListener() {} } } } });
  vm.runInContext(snapshotSource + '\nglobalThis.condition = conditionSnapshot;', context);
  return { check: () => context.condition(1), value };
}

test('condition reads exclude hidden/title/overlay content and never export secret values', async () => {
  const f = snapshotFixture(); const result = await f.check();
  assert.equal(result.elements.find(row => row.ref === 'e3').value, f.value);
  assert(!JSON.stringify(result).includes('SYNTHETIC_SECRET_VALUE'));
  assert(!JSON.stringify(result).includes('Hidden Saved'));
  assert(!JSON.stringify(result).includes('Overlay Saved'));
  assert(!JSON.stringify(result).includes('Saved title'));
});
for (const mode of ['factsFail', 'childFails', 'emptyChild', 'emptyFacts', 'missingFacts', 'navigating']) {
  test(`complete condition reads reject ${mode}`, async () => {
    await assert.rejects(snapshotFixture({ [mode]: true }).check);
  });
}


test('an existing ref losing its AX value cannot verify the empty string', async () => {
  const f = fixture(); f.state.elements = [field('old')];
  const prepared = await f.prepareExpectation(f.params({ ref: 'e2', value: '' }));
  f.state.elements = [{ ...field(null) }];
  assert.equal((await f.waitExpectation(prepared)).reason, 'unavailable');
});

for (const interruption of ['dialog', 'deadline', 'control']) {
  test(`postcondition outcome survives ${interruption} during final observation`, async () => {
    const f = fixture({ integrate: true });
    f.state.action = () => { f.state.elements = [text('Saved')]; };
    let release, continued = false;
    f.state.observation = async guard => {
      if (interruption !== 'deadline') f.schedule(100, () => {
        if (interruption === 'dialog') f.dialog({ type: 'confirm' }); else f.control({ paused: true });
      });
      await new Promise(resolve => { release = resolve; });
      await guard(); continued = true; return { outline: 'late result' };
    };
    const reply = await f.dispatch('tab.click', { expect: { text: 'Saved' }, observe: 'tree', deadline: f.now() + 500 });
    assert.equal(reply.result.expect.status, 'verified');
    assert.equal(reply.result.observation, undefined); assert(reply.result.observation_error);
    release(); await Promise.resolve(); await Promise.resolve(); assert.equal(continued, false);
  });
}

for (const action of ['pressKey', 'type']) {
  test(`interrupted ${action} cannot send later chars or submit, but releases held keys`, async () => {
    let cancelled = false, release, reached;
    const blocked = new Promise(resolve => { reached = resolve; });
    const calls = [];
    const cdp = async (_tab, method, params) => {
      calls.push({ method, ...params });
      if ((action === 'pressKey' && params.type === 'rawKeyDown') || method === 'Input.insertText') {
        reached(); await new Promise(resolve => { release = resolve; }); return {};
      }
      if (method === 'Runtime.evaluate') return { result: { objectId: 'editable' } };
      if (method === 'Runtime.callFunctionOn') return { result: { value: null } };
      if (method === 'DOM.describeNode') return { node: { localName: 'input', attributes: [] } };
      return {};
    };
    const context = vm.createContext({ cdp, show() {}, expectQuickReaction() {}, BridgeError,
      parseChord: () => ({ modifiers: [], key: { key: 'Enter', code: 'Enter', keyCode: 13, text: '\r' } }) });
    vm.runInContext(inputSource + '\nglobalThis.api = { type, pressKey };', context);
    const guard = async () => { if (cancelled) throw Error('interrupted'); };
    const pending = action === 'pressKey' ? context.api.pressKey(1, 'Enter', guard)
      : context.api.type(1, { text: 'draft', submit: true }, guard);
    await blocked; cancelled = true; release(); await assert.rejects(pending, /interrupted/);
    const keyTypes = calls.filter(call => call.method === 'Input.dispatchKeyEvent').map(call => call.type);
    assert.deepEqual(keyTypes, action === 'pressKey' ? ['rawKeyDown', 'keyUp'] : []);
  });
}

test('same-tab navigation can recover from transient missing AX coverage within the wait budget', async () => {
  const f = fixture(); const prepared = await f.prepareExpectation(f.params({ text: 'Saved', timeout_ms: 500 }));
  f.state.sample = () => { throw Error('navigation replaced the world'); };
  f.schedule(100, () => { f.state.sample = null; f.state.document = 'doc2'; f.state.elements = [text('Saved')]; });
  assert.equal((await f.waitExpectation(prepared)).status, 'verified');
});


test('Chrome native input shadow AX nodes use the complete host value without exposing or requiring missing DOM facts', async () => {
  const f = snapshotFixture({ nativeInternals: true }); const result = await f.check();
  assert.equal(result.elements.find(row => row.ref === 'e3').value, f.value);
  assert(!result.elements.some(row => ['e102', 'e103', 'e202', 'e203'].includes(row.ref)));
  assert(!JSON.stringify(result).includes('SYNTHETIC_SECRET_VALUE'));
});

test('unreadable native input host with remaining internal text cannot establish absence', async () => {
  await assert.rejects(snapshotFixture({ nativeLostValue: true }).check, /Native field value coverage/);
});


test('DOM-backed aria-owned popup descendants remain public and cannot falsely satisfy gone', async () => {
  const publicSample = await snapshotFixture({ nativeInternals: true, ownedPublic: true }).check();
  assert(publicSample.elements.some(node => node.ref === 'e6' && node.name === 'Suggestions'));
  assert(publicSample.elements.some(node => node.ref === 'e7' && node.name === 'Saving approval'));
  const f = fixture(); f.state.elements = publicSample.elements;
  const prepared = await f.prepareExpectation(f.params({ text: 'Saving approval', gone: true, timeout_ms: 0 }));
  assert.equal(prepared.before, false);
  assert.equal((await f.waitExpectation(prepared)).reason, 'timeout');
});

test('unknown descendants below a DOM-backed popup cannot inherit native-input coverage', async () => {
  await assert.rejects(snapshotFixture({ unknownUnderOwned: true }).check, /Privacy fact coverage/);
});


for (const kind of ['interactive', 'text', 'value']) {
  test(`new uncovered native-host descendant with distinct ${kind} cannot silently disappear`, async () => {
    await assert.rejects(snapshotFixture({ unknownNative: kind }).check, /Native field descendant coverage/);
  });
}

for (const nativeFallback of [true, 'Full explicit DOM value ' + 'v'.repeat(1800)]) {
  test(`explicit native sparse value facts safely fill missing AX value (${nativeFallback === true ? 'empty' : 'long'})`, async () => {
    const sample = await snapshotFixture({ nativeFallback }).check();
    assert.equal(sample.elements.find(node => node.ref === 'e3').value, nativeFallback === true ? '' : nativeFallback);
    assert.equal(sample.elements.find(node => node.ref === 'e2').value, null);
    assert(!JSON.stringify(sample).includes('SYNTHETIC_SECRET_VALUE'));
  });
}


test('Chrome empty ignored native renderer leaves tied to a covered active modal do not block its public text', async () => {
  const sample = await snapshotFixture({ inactiveModal: true }).check();
  assert(sample.elements.some(node => node.name === 'Review before saving'));
  assert(!sample.elements.some(node => node.ref === 'e999'));
});
for (const inactiveModal of ['named', 'child', 'unbound', 'visible']) {
  test(`modal coverage exception rejects an uncovered ${inactiveModal} node`, async () => {
    await assert.rejects(snapshotFixture({ inactiveModal }).check, /Privacy fact coverage/);
  });
}

test('an inactive native SELECT wrapper with covered empty option leaves permits its actual modal text', async () => {
  const sample = await snapshotFixture({ inactiveModal: 'covered-options' }).check();
  assert(sample.elements.some(node => node.name === 'Review before saving'));
  assert(!sample.elements.some(node => ['e999', 'e6'].includes(node.ref)));
  const f = fixture(); f.state.elements = sample.elements;
  const prepared = await f.prepareExpectation(f.params({ text: 'Review before saving', gone: true, timeout_ms: 0 }));
  assert.equal(prepared.before, false); assert.equal((await f.waitExpectation(prepared)).reason, 'timeout');
});
for (const kind of ['wrong-parent', 'missing-facts', 'missing-child', 'visible', 'role', 'descendant', 'name', 'value', 'wrong-modal']) {
  test(`inactive modal container rejects ${kind} option coverage`, async () => {
    await assert.rejects(snapshotFixture({ inactiveModal: 'covered-' + kind }).check, /Privacy fact coverage/);
  });
}


test('native SELECT menu wrapper preserves its complete public option contents', async () => {
  const sample = await snapshotFixture({ nativeSelect: true }).check();
  assert(!sample.elements.some(node => node.ref === 'e999'));
  assert(sample.elements.some(node => node.ref === 'e7' && node.name === 'Finance review'));
  const f = fixture(); f.state.elements = sample.elements;
  const prepared = await f.prepareExpectation(f.params({ text: 'Finance review', gone: true, timeout_ms: 0 }));
  assert.equal(prepared.before, false); assert.equal((await f.waitExpectation(prepared)).reason, 'timeout');
});
for (const nativeSelect of ['named', 'value', 'unknown-child', 'unknown-parent', 'non-select-parent', 'wrong-role']) {
  test(`native SELECT wrapper coverage rejects ${nativeSelect}`, async () => {
    await assert.rejects(snapshotFixture({ nativeSelect }).check);
  });
}

test('native SELECT wrapper keeps sensitive host privacy on its option descendants', async () => {
  const sample = await snapshotFixture({ nativeSelect: 'secret' }).check();
  assert(!JSON.stringify(sample).includes('SYNTHETIC_SECRET_OPTION'));
  assert.equal(sample.elements.find(node => node.ref === 'e3').value, null);
});
