import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

// Run the production parser without a browser or input dispatch.
const source = (await readFile(new URL('../../extension/keys.js', import.meta.url), 'utf8'))
  .replace(/^import .*;\n/gm, '').replace(/export /g, '');
class BridgeError extends Error { constructor(code, message) { super(message); this.code = code; } }
const context = vm.createContext({ BridgeError });
vm.runInContext(`${source}\nglobalThis.parse = parseChord;`, context);
const parse = chord => JSON.parse(JSON.stringify(context.parse(chord)));
const bits = result => result.modifiers.map(modifier => modifier.bit);

test('Cmd+A and its case-insensitive aliases preserve the selectAll editing command', () => {
  for (const chord of ['Cmd+A', 'cmd+a', 'Command+A', 'Meta+A', 'Super+A']) {
    const result = parse(chord);
    assert.deepEqual(result, parse('cmd+a'));
    assert.deepEqual(bits(result), [4]);
    assert.equal(result.command, 'selectAll');
    assert.deepEqual(result.key, { key: 'a', code: 'KeyA', keyCode: 65, text: 'a' });
  }
});

test('Control and Alt shortcut letters are case-insensitive without implicit Shift', () => {
  for (const [upper, lower, modifiers] of [
    ['Ctrl+A', 'ctrl+a', [2]], ['Control+Z', 'ctrl+z', [2]],
    ['Alt+E', 'alt+e', [1]], ['Option+K', 'alt+k', [1]],
    ['Ctrl+Alt+Q', 'ctrl+alt+q', [1, 2]],
  ]) {
    assert.deepEqual(parse(upper), parse(lower));
    assert.deepEqual(bits(parse(upper)), modifiers);
  }
});

test('explicit Shift preserves shortcut semantics, including redo versus undo', () => {
  for (const chord of ['Cmd+Shift+Z', 'cmd+shift+z', 'Shift+Command+Z']) {
    const result = parse(chord);
    assert.deepEqual(bits(result), [4, 8]);
    assert.equal(result.key.key, 'Z');
    assert.equal(result.command, 'redo');
  }
  assert.equal(parse('Cmd+Z').command, 'undo');
  assert.equal(parse('cmd+z').command, 'undo');
  assert.deepEqual(parse('Ctrl+Shift+A'), parse('ctrl+shift+a'));
  assert.deepEqual(parse('Alt+Shift+E'), parse('alt+shift+e'));
});

test('literal uppercase and lowercase retain different text and Shift behavior', () => {
  const upper = parse('A'), lower = parse('a');
  assert.deepEqual(bits(upper), [8]);
  assert.deepEqual(bits(lower), []);
  assert.equal(upper.key.text, 'A');
  assert.equal(lower.key.text, 'a');
  assert.deepEqual(parse('Shift+a'), upper);
  assert.deepEqual(parse('Shift+A'), upper);
});

test('punctuation and plus still imply the physical Shift modifier', () => {
  for (const [chord, key, code, modifiers] of [
    ['?', '?', 'Slash', [8]], ['Cmd+?', '?', 'Slash', [4, 8]],
    ['/', '/', 'Slash', []], ['Cmd+/', '/', 'Slash', [4]],
    ['+', '+', 'Equal', [8]], ['Cmd++', '+', 'Equal', [4, 8]],
    ['Ctrl++', '+', 'Equal', [2, 8]], ['Alt+!', '!', 'Digit1', [1, 8]],
  ]) {
    const result = parse(chord);
    assert.equal(result.key.key, key);
    assert.equal(result.key.code, code);
    assert.deepEqual(bits(result), modifiers);
  }
});

test('named arrows remain case-insensitive and retain editing commands', () => {
  for (const direction of ['Up', 'Down', 'Left', 'Right']) {
    assert.deepEqual(parse(`Arrow${direction}`), parse(direction.toLowerCase()));
    assert.deepEqual(parse(`Cmd+Arrow${direction}`), parse(`cmd+${direction.toLowerCase()}`));
    assert.deepEqual(bits(parse(`Arrow${direction}`)), []);
  }
  assert.equal(parse('Cmd+ArrowLeft').command, 'moveToLeftEndOfLine');
  assert.equal(parse('Cmd+Shift+ArrowRight').command, 'moveToRightEndOfLineAndModifySelection');
});
