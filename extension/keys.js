import { BridgeError } from "./errors.js";

const MODIFIERS = {
  alt: { bit: 1, key: "Alt", code: "AltLeft", keyCode: 18 },
  ctrl: { bit: 2, key: "Control", code: "ControlLeft", keyCode: 17 },
  meta: { bit: 4, key: "Meta", code: "MetaLeft", keyCode: 91 },
  shift: { bit: 8, key: "Shift", code: "ShiftLeft", keyCode: 16 },
};
const MODIFIER_ALIASES = {
  alt: "alt", option: "alt", opt: "alt",
  ctrl: "ctrl", control: "ctrl",
  cmd: "meta", command: "meta", meta: "meta", super: "meta",
  shift: "shift",
};

const NAMED = {
  enter: ["Enter", "Enter", 13, "\r"],
  tab: ["Tab", "Tab", 9],
  escape: ["Escape", "Escape", 27],
  backspace: ["Backspace", "Backspace", 8],
  delete: ["Delete", "Delete", 46],
  space: [" ", "Space", 32, " "],
  arrowup: ["ArrowUp", "ArrowUp", 38],
  arrowdown: ["ArrowDown", "ArrowDown", 40],
  arrowleft: ["ArrowLeft", "ArrowLeft", 37],
  arrowright: ["ArrowRight", "ArrowRight", 39],
  home: ["Home", "Home", 36],
  end: ["End", "End", 35],
  pageup: ["PageUp", "PageUp", 33],
  pagedown: ["PageDown", "PageDown", 34],
  insert: ["Insert", "Insert", 45],
};
const NAMED_ALIASES = { return: "enter", esc: "escape", del: "delete", up: "arrowup", down: "arrowdown", left: "arrowleft", right: "arrowright" };
for (let i = 1; i <= 12; i++) NAMED[`f${i}`] = [`F${i}`, `F${i}`, 111 + i];

const PUNCTUATION = {
  "-": ["Minus", 189], "=": ["Equal", 187], "[": ["BracketLeft", 219], "]": ["BracketRight", 221],
  "\\": ["Backslash", 220], ";": ["Semicolon", 186], "'": ["Quote", 222], ",": ["Comma", 188],
  ".": ["Period", 190], "/": ["Slash", 191], "`": ["Backquote", 192],
};
const SHIFTED = {
  "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
  "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
};

// On macOS Chrome gets editing behaviour from AppKit key bindings, which synthetic CDP key events
// bypass, so the equivalent editing commands must ride along with the event.
const MAC_COMMANDS = {
  "Meta+KeyA": "selectAll", "Meta+KeyC": "copy", "Meta+KeyX": "cut", "Meta+KeyV": "paste",
  "Meta+KeyZ": "undo", "Shift+Meta+KeyZ": "redo",
  "Backspace": "deleteBackward", "Delete": "deleteForward",
  "Alt+Backspace": "deleteWordBackward", "Meta+Backspace": "deleteToBeginningOfLine",
  "Alt+ArrowLeft": "moveWordLeft", "Alt+ArrowRight": "moveWordRight",
  "Meta+ArrowLeft": "moveToLeftEndOfLine", "Meta+ArrowRight": "moveToRightEndOfLine",
  "Shift+Alt+ArrowLeft": "moveWordLeftAndModifySelection", "Shift+Alt+ArrowRight": "moveWordRightAndModifySelection",
  "Shift+Meta+ArrowLeft": "moveToLeftEndOfLineAndModifySelection", "Shift+Meta+ArrowRight": "moveToRightEndOfLineAndModifySelection",
};

function mainKey(token, modifiers) {
  const lower = token.toLowerCase();
  if(/^kp_[0-9]$/.test(lower)){const digit=lower.at(-1);return {key:digit,code:`Numpad${digit}`,keyCode:96+Number(digit),text:digit};}
  const named = NAMED[NAMED_ALIASES[lower] ?? lower];
  if (named) {
    const [key, code, keyCode, text] = named;
    return { key, code, keyCode, text };
  }
  if (token.length !== 1) throw new BridgeError("invalid_argument", `Unknown key "${token}"`);

  let char = token;
  if (SHIFTED[char]) {
    modifiers.add("shift");
    char = SHIFTED[char];
  } else if (/[A-Z]/.test(char) && !["meta", "ctrl", "alt"].some((name) => modifiers.has(name))) {
    // Shortcut labels such as Cmd+A name the key, not a shifted character. Literal A still types
    // uppercase, and an explicit Shift modifier continues to distinguish Cmd+Shift+Z from Cmd+Z.
    modifiers.add("shift");
  }
  const shifted = modifiers.has("shift");
  if (/[a-z]/i.test(char)) {
    const upper = char.toUpperCase();
    const key = shifted ? upper : upper.toLowerCase();
    return { key, code: `Key${upper}`, keyCode: upper.charCodeAt(0), text: key };
  }
  if (/[0-9]/.test(char)) {
    const key = shifted ? Object.keys(SHIFTED).find((k) => SHIFTED[k] === char) : char;
    return { key, code: `Digit${char}`, keyCode: char.charCodeAt(0), text: key };
  }
  if (PUNCTUATION[char]) {
    const [code, keyCode] = PUNCTUATION[char];
    const key = shifted ? Object.keys(SHIFTED).find((k) => SHIFTED[k] === char) : char;
    return { key, code, keyCode, text: key };
  }
  return { key: char, code: "", keyCode: 0, text: char };
}

/** Parses a `+`-separated chord such as "cmd+shift+t", "Enter" or "ctrl++". */
export function parseChord(chord) {
  if (typeof chord !== "string" || !chord.trim()) throw new BridgeError("invalid_argument", "keys is required");
  const trimmed = chord.trim();
  let tokens;
  if (trimmed === "+") tokens = ["+"];
  else if (trimmed.endsWith("++")) tokens = [...trimmed.slice(0, -2).split("+"), "+"];
  else tokens = trimmed.split("+");
  tokens = tokens.map((t) => t.trim()).filter(Boolean);

  const modifiers = new Set();
  const last = tokens.pop();
  for (const token of tokens) {
    const name = MODIFIER_ALIASES[token.toLowerCase()];
    if (!name) throw new BridgeError("invalid_argument", `Unknown modifier "${token}" in "${chord}"`);
    modifiers.add(name);
  }
  const lastModifier = MODIFIER_ALIASES[last.toLowerCase()];
  if (lastModifier) {
    modifiers.add(lastModifier);
    const { bit, ...key } = MODIFIERS[lastModifier];
    return { modifiers: [...modifiers].filter((m) => m !== lastModifier).map((m) => MODIFIERS[m]), key: { ...key, text: undefined } };
  }
  const key = mainKey(last, modifiers);
  const ordered = ["alt", "ctrl", "meta", "shift"].filter((m) => modifiers.has(m)).map((m) => MODIFIERS[m]);
  const combo = [
    modifiers.has("shift") && "Shift", modifiers.has("ctrl") && "Control",
    modifiers.has("alt") && "Alt", modifiers.has("meta") && "Meta", key.code,
  ].filter(Boolean).join("+");
  return { modifiers: ordered, key, command: MAC_COMMANDS[combo] };
}
