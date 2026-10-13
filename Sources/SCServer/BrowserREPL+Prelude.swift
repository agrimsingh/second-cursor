extension BrowserREPL {
    /// Evaluated once per context, to a function of the host bridge that defines `page`, `context` and `console`
    /// and returns the runner. The bridge stays in its closure, never on the global object.
    static let prelude = #"""
(function (host) {
  "use strict";
  const global = globalThis;
  const { parse, stringify } = JSON;
  const now = Date.now;
  const { keys, freeze, getPrototypeOf, getOwnPropertyDescriptor } = Object;
  const isArray = Array.isArray;
  const INSPECT = Symbol("inspect");
  const FILTER = Symbol("filter");
  const CELL = "browser_repl";
  const GLOBAL = "__repl_global__";
  const MAX_TEXT = 40000;
  const ENDED = "The browser_repl call that started this has ended";
  const POLL = [20, 50, 100, 100, 250, 500];

  // Their callbacks would run outside any call.
  for (const name of ["WebAssembly", "FinalizationRegistry"]) try { delete global[name]; } catch {}

  // ---- Cells: top-level await, and declarations that outlive the call ----------------------------------------

  const PUNCTUATORS = [">>>=", "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=", "=>", "==", "!=",
    "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<", ">>"];
  const BEFORE_REGEX = new Set(["return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
    "do", "else", "await", "yield"]);
  const CONTINUE_BEFORE = new Set([".", "?.", ",", "(", "[", "?", ":", "=", "=>", "+", "-", "*", "/", "%", "**", "==",
    "===", "!=", "!==", "<", ">", "<=", ">=", "<<", ">>", ">>>", "&", "|", "^", "&&", "||", "??", "+=", "-=", "*=", "/=",
    "%=", "**=", "<<=", ">>=", ">>>=", "&=", "|=", "^=", "&&=", "||=", "??=", ")", "]", "}"]);
  const CONTINUE_BEFORE_WORDS = new Set(["in", "instanceof", "else", "catch", "finally"]);
  const CONTINUE_AFTER_WORDS = new Set(["new", "typeof", "void", "delete", "await", "yield", "in", "instanceof", "of",
    "else", "extends", "do", "const", "let", "var", "class", "function", "case"]);
  const CONTROL = new Set(["if", "for", "while", "with", "switch", "catch"]);
  const BLOCK_AFTER = new Set(["else", "try", "finally", "do"]);
  const STATEMENT_WORDS = new Set(["if", "for", "while", "do", "switch", "try", "throw", "return", "break", "continue",
    "const", "var", "function", "class", "import", "export", "debugger", "with"]);
  const isNewline = (c) => c === "\n" || c === "\r" || c === "\u2028" || c === "\u2029";
  const isSpace = (c) => c === " " || c === "\t" || c === "\v" || c === "\f" || (c > "\u007f" && /\s/.test(c));
  const isIdStart = (c) => /[#A-Za-z_$\\]/.test(c) || c > "\u007f";
  const isIdPart = (c) => /[\w$\\]/.test(c) || (c > "\u007f" && !/\s/.test(c));

  function tokenize(src) {
    const tokens = [], stack = [];
    let i = 0, newline = false;
    const push = (type, value, start) => {
      tokens.push({ type, value, start, end: i, newline, depth: stack.length });
      newline = false;
    };
    const template = (start) => {
      while (i < src.length) {
        const c = src[i];
        if (c === "\\") { i += 2; continue; }
        if (c === "`") { i++; return push("template", src.slice(start, i), start); }
        if (c === "$" && src[i + 1] === "{") {
          i += 2;
          push("template", src.slice(start, i), start);
          return stack.push("${");
        }
        i++;
      }
      throw new SyntaxError("Unterminated template literal");
    };
    const regexAllowed = () => {
      const last = tokens[tokens.length - 1];
      if (!last) return true;
      if (last.type === "name") return BEFORE_REGEX.has(last.value);
      if (last.type === "punct") return last.value !== ")" && last.value !== "]";
      return last.type === "template" && last.value.endsWith("${");
    };
    while (i < src.length) {
      const c = src[i], start = i;
      if (isNewline(c)) { newline = true; i++; continue; }
      if (isSpace(c)) { i++; continue; }
      if (c === "/" && src[i + 1] === "/") { while (i < src.length && !isNewline(src[i])) i++; continue; }
      if (c === "/" && src[i + 1] === "*") {
        const close = src.indexOf("*/", i + 2);
        if (close < 0) throw new SyntaxError("Unterminated comment");
        for (let k = i; k < close; k++) if (isNewline(src[k])) { newline = true; break; }
        i = close + 2;
        continue;
      }
      if (c === "'" || c === '"') {
        for (i++; i < src.length && src[i] !== c; i++) {
          if (src[i] === "\\") i++;
          else if (isNewline(src[i])) throw new SyntaxError("Unterminated string");
        }
        i++;
        push("string", src.slice(start, i), start);
        continue;
      }
      if (c === "`") { i++; template(start); continue; }
      if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(src[i + 1] ?? ""))) {
        for (i++; i < src.length && (/[\w.]/.test(src[i]) || (/[+-]/.test(src[i]) && /[eE]/.test(src[i - 1])
          && !/^0[xX]/.test(src.slice(start, i)))); i++);
        push("number", src.slice(start, i), start);
        continue;
      }
      if (isIdStart(c)) {
        for (i++; i < src.length && isIdPart(src[i]); i++);
        push("name", src.slice(start, i), start);
        continue;
      }
      if (c === "/" && regexAllowed()) {
        let inClass = false;
        for (i++; i < src.length; i++) {
          const d = src[i];
          if (d === "\\") { i++; continue; }
          if (isNewline(d)) throw new SyntaxError("Unterminated regular expression");
          if (d === "[") inClass = true;
          else if (d === "]") inClass = false;
          else if (d === "/" && !inClass) break;
        }
        for (i++; i < src.length && isIdPart(src[i]); i++);
        push("regex", src.slice(start, i), start);
        continue;
      }
      if (c === "(" || c === "[" || c === "{") { i++; push("punct", c, start); stack.push(c); continue; }
      if (c === ")" || c === "]" || c === "}") {
        const open = stack.pop();
        if (open === "${" && c === "}") { i++; template(start); continue; }
        if (open !== { ")": "(", "]": "[", "}": "{" }[c]) throw new SyntaxError(`Unbalanced ${c}`);
        i++;
        push("punct", c, start);
        continue;
      }
      const punct = PUNCTUATORS.find((p) => src.startsWith(p, i) && !(p === "?." && /[0-9]/.test(src[i + 2] ?? "")));
      i += punct ? punct.length : 1;
      push("punct", punct ?? c, start);
    }
    if (stack.length) throw new SyntaxError("Unbalanced brackets");
    return tokens;
  }

  const isPunct = (t, value) => t?.type === "punct" && t.value === value;
  function continuesAfter(t) {
    if (t.type === "name") return CONTINUE_AFTER_WORDS.has(t.value);
    if (t.type !== "punct") return false;
    if (t.value === ")") return !!t.control;
    return !["]", "}", "++", "--", ";"].includes(t.value);
  }
  function continuesBefore(t, statementWord, afterBlock) {
    if (t.type === "name") return CONTINUE_BEFORE_WORDS.has(t.value) || (t.value === "while" && statementWord === "do");
    return !afterBlock && (t.type === "template" || (t.type === "punct" && CONTINUE_BEFORE.has(t.value)));
  }

  /** Top-level statements as [first, end) token ranges, splitting where JavaScript would: at semicolons, after the
   * body of a function or class declaration, and at line breaks and closing braces that no token continues. */
  function statements(tokens) {
    const list = [];
    let begin = 0, prev = null, before = null, owner = null, ownerBefore = null, word = null, decl = null;
    let forced = false, block = false;
    for (let k = 0; k < tokens.length; k++) {
      const t = tokens[k];
      if (t.depth !== 0) continue;
      const afterBlock = isPunct(prev, "}") && prev.blockEnd;
      if (prev && (forced || (!continuesBefore(t, word, afterBlock)
        && ((t.newline && !continuesAfter(prev)) || isPunct(prev, "}"))))) {
        list.push([begin, k]);
        begin = k;
        prev = before = null;
      }
      if (!prev) {
        word = t.type === "name" ? t.value : null;
        const next = tokens[k + 1];
        decl = word === "class" ? "body" : word === "function" || (word === "async" && next?.value === "function")
          ? "params" : null;
        forced = false;
      }
      if (t.type === "punct") {
        if (t.value === "(") [owner, ownerBefore] = [prev, before];
        if (t.value === ")") {
          t.control = owner?.type === "name" && (CONTROL.has(owner.value)
            || (owner.value === "await" && ownerBefore?.value === "for")) && !(word === "do" && owner.value === "while");
          if (decl === "params") decl = "body";
        }
        // A statement's block (not an object literal or function expression) ends the statement at its brace.
        if (t.value === "{") block = !prev || !!prev.control || (prev.type === "name" && BLOCK_AFTER.has(prev.value));
        if (t.value === "}") t.blockEnd = block;
        if (t.value === "{" && decl === "body") decl = "inside";
        else if (t.value === "}" && decl === "inside") { decl = null; forced = true; t.bodyEnd = true; }
        if (t.value === ";") {
          list.push([begin, k + 1]);
          begin = k + 1;
          prev = before = null;
          continue;
        }
      }
      [before, prev] = [prev, t];
    }
    if (begin < tokens.length) list.push([begin, tokens.length]);
    return list;
  }

  /** The cell as an async function: top-level const/let/var become assignments to globals (sloppy-mode implicit
   * globals, so destructuring works too), classes are assigned the same way, function declarations are copied to
   * the global object, and the last expression statement is returned. Offsets keep every line where it was. */
  function transform(code, withReturn) {
    const tokens = tokenize(code);
    const edits = [], functions = [];
    let last = null;
    for (const [s, e] of statements(tokens)) {
      const first = tokens[s], second = tokens[s + 1];
      const end = isPunct(tokens[e - 1], ";") ? e - 1 : e;
      if (end === s) continue;
      last = { s, end, expression: false };
      if (first.type === "name" && (first.value === "const" || first.value === "var" || (first.value === "let"
        && (second?.type === "name" || isPunct(second, "[") || isPunct(second, "{"))))) {
        edits.push([first.start, first.end - first.start, "0,"]);
        let initialized = false, from = s + 1;
        for (let k = s + 1; k <= end; k++) {
          if (k === end || (tokens[k].depth === 0 && isPunct(tokens[k], ","))) {
            if (!initialized && k > from) edits.push([tokens[k - 1].end, 0, " = void 0"]);
            initialized = false;
            from = k + 1;
          } else if (tokens[k].depth === 0 && isPunct(tokens[k], "=")) initialized = true;
        }
      } else if (first.type === "name" && first.value === "class" && second?.type === "name") {
        edits.push([first.start, 0, `${second.value} = `]);
        const close = tokens.slice(s, end).findLast((t) => t.bodyEnd);
        if (close) edits.push([close.end, 0, ";"]);
      } else if (first.value === "function" || (first.value === "async" && second?.value === "function"
        && !second.newline)) {
        let name = tokens[first.value === "async" ? s + 2 : s + 1];
        if (isPunct(name, "*")) name = tokens[tokens.indexOf(name) + 1];
        if (name?.type === "name") functions.push(name.value);
      } else {
        last.expression = !(first.type === "name" && (STATEMENT_WORDS.has(first.value) || isPunct(second, ":")))
          && !isPunct(first, "{");
      }
    }
    if (withReturn && last?.expression) {
      edits.push([tokens[last.s].start, 0, "return ("]);
      edits.push([tokens[last.end - 1].end, 0, "\n)"]);
    }
    edits.sort((a, b) => a[0] - b[0]);
    let out = "", at = 0;
    for (const [offset, remove, insert] of edits) {
      out += code.slice(at, offset) + insert;
      at = offset + remove;
    }
    out += code.slice(at);
    // A statement first, so a "use strict" in the cell is not a directive and implicit globals keep working.
    const prefix = functions.map((name) => `${GLOBAL}.${name} = ${name};`).join(" ") || "void 0;";
    return `(async function (${GLOBAL}) { ${prefix} ${out}\n})`;
  }

  // The host compiles each candidate as a script named CELL, so errors carry the cell's own line numbers.
  function compile(code) {
    const sources = [];
    for (const withReturn of [true, false]) {
      try { sources.push(transform(code, withReturn)); } catch {}
    }
    sources.push(`(async function () { void 0; ${code}\n})`);
    let error;
    for (const [index, source] of sources.entries()) {
      const fn = host.compile(source);
      if (typeof fn === "function") return { fn, kept: index < sources.length - 1 };
      error = fn;
    }
    throw error;
  }

  // ---- Output ------------------------------------------------------------------------------------------------

  const quote = (s) => {
    const shown = s.length > 10000 ? s.slice(0, 10000) : s;
    const q = shown.includes("'") && !shown.includes('"') ? '"' : "'";
    const body = shown.replace(/[\\\n\r\t\b\f\v\0]/g, (c) => ({ "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t",
      "\b": "\\b", "\f": "\\f", "\v": "\\v", "\0": "\\0" })[c]).replaceAll(q, "\\" + q);
    return q + body + q + (s.length > shown.length ? `... ${s.length - shown.length} more characters` : "");
  };
  const constructorName = (value) => {
    try { return getPrototypeOf(value)?.constructor?.name || ""; } catch { return ""; }
  };

  function inspect(value, depth = 0, seen = []) {
    if (value === null) return "null";
    switch (typeof value) {
      case "undefined": return "undefined";
      case "string": return quote(value);
      case "number": return Object.is(value, -0) ? "-0" : String(value);
      case "bigint": return `${value}n`;
      case "boolean": return String(value);
      case "symbol": return value.toString();
      case "function": {
        const source = Function.prototype.toString.call(value);
        if (/^class\b/.test(source)) return `[class ${value.name || "(anonymous)"}]`;
        const kind = Object.prototype.toString.call(value).slice(8, -1);
        return `[${kind}${value.name ? `: ${value.name}` : " (anonymous)"}]`;
      }
    }
    try {
      if (typeof value[INSPECT] === "function") return value[INSPECT]();
      if (seen.includes(value)) return "[Circular]";
      if (value instanceof Error) return describe(value);
      if (value instanceof Date) return isNaN(value) ? "Invalid Date" : value.toISOString();
      if (value instanceof RegExp) return String(value);
      if (value instanceof Promise) return "Promise {}";
      if (value instanceof WeakMap || value instanceof WeakSet) return `${constructorName(value)} { <items unknown> }`;
      const name = constructorName(value);
      if (depth > 4) return isArray(value) ? "[Array]" : `[${name || "Object"}]`;
      const inner = [...seen, value];
      const items = [];
      let prefix = "", open = "{", close = "}";
      const more = (n, shown) => { if (n > shown) items.push(`... ${n - shown} more items`); };
      if (isArray(value) || (ArrayBuffer.isView(value) && !(value instanceof DataView))) {
        const n = value.length, shown = Math.min(n, 200);
        for (let k = 0; k < shown; k++) items.push(k in value ? inspect(value[k], depth + 1, inner) : "<empty>");
        more(n, shown);
        [open, close] = ["[", "]"];
        if (name !== "Array") prefix = `${name}(${n}) `;
      } else if (value instanceof Map || value instanceof Set) {
        let n = 0;
        for (const entry of value) {
          if (++n > 200) break;
          items.push(value instanceof Map ? `${inspect(entry[0], depth + 1, inner)} => ${inspect(entry[1], depth + 1, inner)}`
            : inspect(entry, depth + 1, inner));
        }
        more(value.size, Math.min(value.size, 200));
        prefix = `${name}(${value.size}) `;
      } else {
        for (const key of Reflect.ownKeys(value)) {
          const d = getOwnPropertyDescriptor(value, key);
          if (!d?.enumerable) continue;
          if (items.length >= 200) { items.push("..."); break; }
          const label = typeof key === "symbol" ? `[${key.toString()}]` : /^[A-Za-z_$][\w$]*$/.test(key) ? key : quote(key);
          items.push(`${label}: ${"value" in d ? inspect(d.value, depth + 1, inner) : d.get && d.set ? "[Getter/Setter]"
            : d.get ? "[Getter]" : "[Setter]"}`);
        }
        const proto = getPrototypeOf(value);
        prefix = proto === null ? "[Object: null prototype] " : proto === Object.prototype ? "" : `${name || "Object"} `;
      }
      if (!items.length) return `${prefix}${open}${close}`;
      const line = `${prefix}${open} ${items.join(", ")} ${close}`;
      if (line.length <= 72 && !line.includes("\n")) return line;
      const pad = "  ".repeat(depth + 1);
      let rows = items;
      // Like Node, many short items share rows instead of taking a line each.
      if (items.length > 6 && items.every((item) => item.length <= 16 && !item.includes("\n"))) {
        rows = [];
        for (const item of items) {
          if (rows.length && pad.length + rows[rows.length - 1].length + item.length + 2 <= 76)
            rows[rows.length - 1] += `, ${item}`;
          else rows.push(item);
        }
      }
      return `${prefix}${open}\n${rows.map((row) => pad + row).join(",\n")}\n${"  ".repeat(depth)}${close}`;
    } catch {
      return "[Uninspectable]";
    }
  }

  const cleanMessage = (message) => String(message).replace(/\n\s+at .*/g, "").replace(/^Error: /, "");
  function describe(error) {
    if (!(error instanceof Error) && typeof error?.message !== "string") return `Uncaught ${inspect(error)}`;
    const name = error.name || "Error";
    const message = cleanMessage(error.message);
    const text = message.startsWith(`${name}:`) ? message : `${name}: ${message}`;
    return error.sourceURL === CELL && error.line ? `${text} (line ${error.line})` : text;
  }

  const print = (text) => host.print(text.slice(0, MAX_TEXT), text.length);
  const capped = (text) => text.length > MAX_TEXT
    ? `${text.slice(0, MAX_TEXT)}\n… ${text.length - MAX_TEXT} more characters (output is capped at ${MAX_TEXT} characters)`
    : text;
  function format(args) {
    if (!args.length) return "";
    let [first, ...rest] = args;
    if (typeof first === "string" && rest.length && /%[sdifjoOc%]/.test(first)) {
      first = first.replace(/%([sdifjoOc%])/g, (match, f) => {
        if (f === "%") return "%";
        if (!rest.length) return match;
        const v = rest.shift();
        switch (f) {
          case "s": return typeof v === "string" ? v : inspect(v);
          case "d": return String(Number(v));
          case "i": return String(parseInt(v));
          case "f": return String(parseFloat(v));
          case "j": try { return stringify(v); } catch { return "[Circular]"; }
          case "c": return "";
          default: return inspect(v);
        }
      });
    }
    return [first, ...rest].map((v) => (typeof v === "string" ? v : inspect(v))).join(" ");
  }
  const consoleObject = {};
  for (const name of ["log", "info", "warn", "error", "debug", "trace", "dir", "table"])
    consoleObject[name] = (...args) => void print(format(args));
  global.console = freeze(consoleObject);

  // ---- The browser bridge ------------------------------------------------------------------------------------

  class TimeoutError extends Error {
    constructor(message) {
      super(message);
      this.name = "TimeoutError";
    }
  }
  const call = (method, params) => new Promise((resolve, reject) =>
    host.call(method, stringify(params), (json) => resolve(json ? parse(json) : {}),
      (message) => reject(new Error(cleanMessage(message)))));
  const sleep = (ms) => new Promise((resolve, reject) => host.sleep(ms, resolve, () => reject(new Error(ENDED))));
  const UNAVAILABLE = Symbol("unavailable");
  const notAvailable = (name, hint) => Object.assign(() => {
    throw new Error(`${name} is not available in browser_repl${hint ? `: ${hint}` : ""}`);
  }, { [UNAVAILABLE]: true });
  // Arguments past a method's parameters are refused, not ignored.
  const counted = (label, fn) => {
    if (fn[UNAVAILABLE]) return fn;
    const max = fn.length;
    return { [fn.name]: function (...args) {
      if (args.length > max)
        throw new TypeError(`${label} takes ${max === 0 ? "no arguments" : `at most ${max} argument${max === 1 ? "" : "s"}`}, not ${args.length}`);
      return fn.apply(this, args);
    } }[fn.name];
  };
  const countArguments = (object, prefix) => {
    for (const name of Object.getOwnPropertyNames(object)) {
      const d = getOwnPropertyDescriptor(object, name);
      if (name !== "constructor" && typeof d.value === "function") object[name] = counted(`${prefix}.${name}`, d.value);
    }
    return object;
  };
  const NO_SCRIPT = "there is no page JavaScript; operate the page through locators and read it with page.snapshot() "
    + "or locator.innerText()";
  const lit = (v) => v instanceof RegExp ? String(v) : `'${String(v).replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
  const pattern = (v, what) => {
    if (v instanceof RegExp) return { regex: { source: v.source, flags: v.flags.replace(/[gy]/g, "") } };
    if (typeof v === "string" || typeof v === "number") return String(v);
    throw new TypeError(`${what} takes a string or a RegExp`);
  };
  // The extension waits for a load to finish, which satisfies every waitUntil.
  const loadState = (state, what) => {
    if (state !== undefined && !["load", "domcontentloaded", "networkidle", "commit"].includes(state))
      throw new TypeError(`${what} is load, domcontentloaded, networkidle or commit`);
  };
  const text = (value, what) => {
    if (typeof value !== "string") throw new TypeError(`${what} must be a string, not ${inspect(value)}`);
    return value;
  };
  function allow(options, names, method) {
    if (options === undefined || options === null) return {};
    if (typeof options !== "object") throw new TypeError(`${method} takes an options object`);
    const extra = keys(options).filter((k) => !names.includes(k) && options[k] !== undefined);
    if (extra.length) throw new TypeError(`${method} does not support option ${extra.join(", ")} in browser_repl`);
    return options;
  }
  const described = (options) => {
    const parts = keys(options).filter((k) => options[k] !== undefined).map((k) => `${k}: ${
      typeof options[k] === "string" || options[k] instanceof RegExp ? lit(options[k]) : String(options[k])}`);
    return parts.length ? `, { ${parts.join(", ")} }` : "";
  };
  const MODIFIERS = { Alt: "alt", Control: "ctrl", Meta: "cmd", Shift: "shift", ControlOrMeta: "cmd" };
  const modifiers = (list) => list?.map((m) => {
    if (!MODIFIERS[m]) throw new TypeError(`Unknown modifier ${m}; use Alt, Control, Meta, Shift or ControlOrMeta`);
    return MODIFIERS[m];
  });
  function keyName(key) {
    const parts = String(key).split("+");
    const mapped = parts.map((part) => part === "ControlOrMeta" ? "Meta" : /^Key[A-Z]$/.test(part)
      ? part.slice(3).toLowerCase() : /^Digit\d$/.test(part) ? part.slice(5) : part);
    const chord = mapped.slice(0, -1).some((m) => /^(control|ctrl|meta|cmd|alt)$/i.test(m));
    if (chord && /^[A-Z]$/.test(mapped.at(-1))) mapped[mapped.length - 1] = mapped.at(-1).toLowerCase();
    return mapped.join("+");
  }

  function selectorSteps(selector) {
    if (typeof selector !== "string") throw new TypeError("locator() takes a selector string");
    return selector.split(/\s+>>\s+/).map((part) => {
      let m;
      if ((m = /^aria-ref=(\S+)$/.exec(part))) return { ref: m[1] };
      if ((m = /^css=([\s\S]*)$/.exec(part))) return { css: m[1] };
      if ((m = /^xpath=([\s\S]*)$/.exec(part))) return { xpath: m[1] };
      if (/^\.{0,2}\//.test(part)) return { xpath: part };
      if ((m = /^text=([\s\S]*)$/.exec(part))) {
        const quoted = /^(["'])([\s\S]*)\1$/.exec(m[1]);
        return quoted ? { text: quoted[2], exact: true } : { text: m[1] };
      }
      if ((m = /^data-testid=([\s\S]*)$/.exec(part))) return { attribute: "data-testid", value: m[1] };
      if (/^(internal:|role=|id=|nth=|visible=)/.test(part))
        throw new Error(`The ${part.split("=")[0]}= selector engine is not available in browser_repl; use getByRole, `
          + "getByText, filter() or nth()");
      return { css: part };
    });
  }
  const ROLE_OPTIONS = ["name", "exact", "checked", "disabled", "expanded", "includeHidden", "level", "pressed", "selected"];
  function roleStep(role, options) {
    allow(options, ROLE_OPTIONS, "getByRole");
    const o = options ?? {};
    const step = { role: String(role) };
    if (o.name !== undefined) step.name = pattern(o.name, "getByRole's name");
    if (o.exact) step.exact = true;
    for (const k of ["checked", "disabled", "expanded", "pressed", "selected"]) if (o[k] !== undefined) step[k] = !!o[k];
    if (o.level !== undefined) step.level = Number(o.level);
    if (o.includeHidden) step.include_hidden = true;
    return step;
  }
  const textStep = (field, value, options, method) => {
    const o = allow(options, ["exact"], method);
    const want = pattern(value, method);
    if (field === "text" || field === "label") return { [field]: want, exact: !!o.exact };
    return { attribute: field, value: want, exact: !!o.exact };
  };

  // Retries these until the timeout: the element may not be there, visible, enabled or unobstructed yet, or the page
  // may be between documents.
  const ACTIONABLE = /matched 0 elements|not visible|is disabled|not editable|is covered by|Execution context was destroyed|Cannot find context|context with specified id|Inspected target navigated/i;
  const TRANSIENT = /Execution context was destroyed|Cannot find context|context with specified id|Inspected target navigated/i;
  const ATTACHED = new RegExp(`matched 0 elements|${TRANSIENT.source}`, "i");
  const WAITING = /^waiting: /;
  const why = (message) => message.replace(/^waiting: /, "").replace(/Locator matched 0 elements.*/, "no element matches")
    .replace(/Element is /, "the element is ").replace(/Execution context was destroyed.*/, "the page was navigating")
    .replace(/, so the click was not sent.*/, "");

  const internals = new WeakMap();
  const images = new WeakMap();
  const pages = [];
  const byTab = new Map();
  // Set only by adopt, for the Page it constructs next; scripts cannot reach it, so no Page takes a tab id from them.
  let adopting = null;

  class Locator {
    #page; #chain; #desc;
    constructor(page, chain, desc) {
      this.#page = page;
      this.#chain = chain;
      this.#desc = desc;
    }
    [INSPECT]() { return this.#desc; }
    toString() { return this.#desc; }
    page() { return this.#page; }
    #then(steps, text) { return new Locator(this.#page, [...this.#chain, ...steps], this.#desc + text); }
    #relative(other, method) {
      if (!(other instanceof Locator)) throw new TypeError(`${method} takes a Locator`);
      return other.#chain;
    }
    locator(selector, options) {
      const next = selector instanceof Locator ? this.#then(this.#relative(selector), `.locator(${selector})`)
        : this.#then(selectorSteps(selector), `.locator(${lit(selector)})`);
      return options ? next[FILTER](options, "locator.locator") : next;
    }
    getByRole(role, options) { return this.#then([roleStep(role, options)], `.getByRole(${lit(role)}${described(options ?? {})})`); }
    getByText(text, options) { return this.#then([textStep("text", text, options, "getByText")], `.getByText(${lit(text)}${described(options ?? {})})`); }
    getByLabel(text, options) { return this.#then([textStep("label", text, options, "getByLabel")], `.getByLabel(${lit(text)}${described(options ?? {})})`); }
    getByPlaceholder(text, options) { return this.#then([textStep("placeholder", text, options, "getByPlaceholder")], `.getByPlaceholder(${lit(text)}${described(options ?? {})})`); }
    getByAltText(text, options) { return this.#then([textStep("alt", text, options, "getByAltText")], `.getByAltText(${lit(text)}${described(options ?? {})})`); }
    getByTitle(text, options) { return this.#then([textStep("title", text, options, "getByTitle")], `.getByTitle(${lit(text)}${described(options ?? {})})`); }
    getByTestId(id) { return this.#then([{ attribute: "data-testid", value: String(id) }], `.getByTestId(${lit(id)})`); }
    filter(options) { return this[FILTER](options, "filter"); }
    [FILTER](options, method) {
      const o = allow(options, ["hasText", "hasNotText", "has", "hasNot", "visible"], method);
      const steps = [];
      if (o.hasText !== undefined) steps.push({ has_text: pattern(o.hasText, "filter's hasText") });
      if (o.hasNotText !== undefined) steps.push({ has_not_text: pattern(o.hasNotText, "filter's hasNotText") });
      if (o.has !== undefined) steps.push({ has: this.#relative(o.has, "filter's has") });
      if (o.hasNot !== undefined) steps.push({ has_not: this.#relative(o.hasNot, "filter's hasNot") });
      if (o.visible !== undefined) steps.push({ visible: !!o.visible });
      return this.#then(steps, `.filter(${described(o).slice(2)})`);
    }
    nth(index) { return this.#then([{ nth: Number(index) }], `.nth(${index})`); }
    first() { return this.#then([{ nth: 0 }], ".first()"); }
    last() { return this.#then([{ nth: -1 }], ".last()"); }
    and(other) { return this.#then([{ and: this.#relative(other, "and") }], `.and(${other})`); }
    or(other) { return this.#then([{ or: this.#relative(other, "or") }], `.or(${other})`); }

    async #act(name, operation, args, options, retry = ACTIONABLE) {
      const page = internals.get(this.#page);
      const result = await page.retry(`locator.${name}`, this.#desc, options?.timeout, retry,
        () => page.command(operation, { locator: { chain: this.#chain }, ...args }));
      if (result?.outcome?.state === "uncertain")
        throw new Error(`locator.${name}: the requested ${result.outcome.verification} was not verified after the page handled the input; inspect the control before continuing`);
      return result;
    }
    #query(extra) {
      const page = internals.get(this.#page);
      return page.retry("locator.query", this.#desc, undefined, TRANSIENT,
        () => page.command("query", { locator: { chain: this.#chain }, ...extra }));
    }
    #strict(name, count) {
      return new Error(`locator.${name}: strict mode violation: ${this.#desc} resolved to ${count} elements`);
    }

    // A click or key press waits for a navigation it starts unless noWaitAfter; force skips the actionability checks.
    async click(options) {
      const o = allow(options, ["button", "clickCount", "modifiers", "timeout", "force", "noWaitAfter"], "click");
      await this.#act("click", "click", { button: o.button, count: o.clickCount, modifiers: modifiers(o.modifiers),
        force: o.force, wait_navigation: !o.noWaitAfter }, o);
    }
    async dblclick(options) {
      const o = allow(options, ["button", "modifiers", "timeout", "force", "noWaitAfter"], "dblclick");
      await this.#act("dblclick", "click", { button: o.button, count: 2, modifiers: modifiers(o.modifiers),
        force: o.force, wait_navigation: !o.noWaitAfter }, o);
    }
    async hover(options) {
      const o = allow(options, ["modifiers", "timeout", "force"], "hover");
      await this.#act("hover", "hover", { modifiers: modifiers(o.modifiers), force: o.force }, o);
    }
    async fill(value, options) {
      const o = allow(options, ["timeout", "force", "noWaitAfter"], "fill");
      await this.#act("fill", "fill", { text: text(value, "fill's value"), force: o.force }, o);
    }
    clear(options) { return this.fill("", options); }
    async press(key, options) {
      const o = allow(options, ["timeout", "noWaitAfter"], "press");
      await this.#act("press", "press_key", { keys: keyName(text(key, "press's key")), wait_navigation: !o.noWaitAfter }, o);
    }
    async pressSequentially(value, options) {
      const o = allow(options, ["timeout", "noWaitAfter"], "pressSequentially");
      await this.#act("pressSequentially", "type", { text: text(value, "pressSequentially's text") }, o);
    }
    type(value, options) { return this.pressSequentially(value, options); }
    async setChecked(checked, options) {
      const o = allow(options, ["timeout", "force", "noWaitAfter"], "setChecked");
      await this.#act(checked ? "check" : "uncheck", "check", { checked: !!checked, force: o.force }, o);
    }
    check(options) { return this.setChecked(true, options); }
    uncheck(options) { return this.setChecked(false, options); }
    async selectOption(values, options) {
      const o = allow(options, ["timeout", "force", "noWaitAfter"], "selectOption");
      const list = (isArray(values) ? values : [values]).map((v) => {
        if (typeof v === "string") return v;
        if (v && typeof v === "object" && keys(v).length === 1 && (v.value ?? v.label) !== undefined)
          return String(v.value ?? v.label);
        throw new TypeError("selectOption takes option values or labels, as strings or { value } / { label }");
      });
      const result = await this.#act("selectOption", "select_option", { value: list.length === 1 ? list[0] : list,
        force: o.force }, o);
      return [result.value];
    }
    async focus(options) { await this.#act("focus", "perform_action", { action: "focus" }, allow(options, ["timeout"], "focus")); }
    async scrollIntoViewIfNeeded(options) {
      await this.#act("scrollIntoViewIfNeeded", "perform_action", { action: "scroll_into_view" },
        allow(options, ["timeout"], "scrollIntoViewIfNeeded"));
    }
    #inspect(name, args, options) { return this.#act(name, "inspect", args, allow(options, ["timeout"], name), ATTACHED); }
    async textContent(options) { return (await this.#inspect("textContent", { length: 100000, text_kind: "content" }, options)).text; }
    async innerText(options) { return (await this.#inspect("innerText", { length: 100000, text_kind: "inner" }, options)).text; }
    async inputValue(options) { return (await this.#inspect("inputValue", { length: 100000 }, options)).text; }
    async getAttribute(name, options) { return (await this.#inspect("getAttribute", { length: 0 }, options)).attributes[name] ?? null; }
    async isEnabled(options) { return (await this.#inspect("isEnabled", { length: 0 }, options)).enabled; }
    async isDisabled(options) { return !(await this.#inspect("isDisabled", { length: 0 }, options)).enabled; }
    async isEditable(options) {
      const r = await this.#inspect("isEditable", { length: 0 }, options);
      return r.enabled && !("readonly" in r.attributes);
    }
    async isChecked(options) {
      const r = await this.#inspect("isChecked", { length: 0 }, options);
      if (typeof r.checked === "boolean") return r.checked;
      if (r.attributes["aria-checked"] === undefined) throw new Error("locator.isChecked: Not a checkbox or radio button");
      return r.attributes["aria-checked"] === "true";
    }
    async boundingBox(options) {
      const r = await this.#inspect("boundingBox", { length: 0 }, options);
      return r.visible ? r.bounds : null;
    }
    async count() { return (await this.#query()).count; }
    async isVisible() {
      const q = await this.#query();
      if (q.count > 1) throw this.#strict("isVisible", q.count);
      return q.count === 1 && q.visible;
    }
    async isHidden() {
      const q = await this.#query();
      if (q.count > 1) throw this.#strict("isHidden", q.count);
      return !(q.count === 1 && q.visible);
    }
    async all() { return Array.from({ length: await this.count() }, (_, i) => this.nth(i)); }
    async allInnerTexts() { return (await this.#query({ texts: "inner" })).texts; }
    async allTextContents() { return (await this.#query({ texts: "content" })).texts; }
    async waitFor(options) {
      const o = allow(options, ["state", "timeout"], "waitFor");
      const state = o.state ?? "visible";
      if (!["attached", "detached", "visible", "hidden"].includes(state))
        throw new TypeError("waitFor's state is attached, detached, visible or hidden");
      const page = internals.get(this.#page);
      await page.retry("locator.waitFor", `${this.#desc} to be ${state}`, o.timeout, WAITING, async () => {
        const q = await page.retry("locator.waitFor", this.#desc, undefined, TRANSIENT,
          () => page.command("query", { locator: { chain: this.#chain } }));
        if (q.count > 1 && (state === "attached" || state === "visible")) throw this.#strict("waitFor", q.count);
        const reached = { attached: q.count === 1, detached: q.count === 0, visible: q.count === 1 && q.visible,
          hidden: q.count === 0 || (q.count === 1 && !q.visible) }[state];
        if (!reached) throw new Error(`waiting: ${q.count ? (q.visible ? "the element is visible" : "the element is not visible")
          : "no element matches"}`);
      });
    }
  }
  const NO_ELEMENT_SCRIPT = "there is no page JavaScript; use the locator's own methods";
  for (const name of ["evaluate", "evaluateAll", "evaluateHandle", "elementHandle", "elementHandles", "dispatchEvent"])
    Locator.prototype[name] = notAvailable(`locator.${name}`, NO_ELEMENT_SCRIPT);
  for (const [name, hint] of [["dragTo", "use browser_command drag for a pointer drag"], ["tap", "use click"],
    ["setInputFiles", "file uploads need the user's approval"], ["blur", ""], ["highlight", ""], ["screenshot", "use page.screenshot()"],
    ["innerHTML", "read text with innerText() or the page with page.snapshot()"], ["outerHTML", "read text with innerText() or the page with page.snapshot()"]])
    Locator.prototype[name] = notAvailable(`locator.${name}`, hint);

  class Screenshot {
    constructor(image, width, height) {
      images.set(this, image);
      this.width = width;
      this.height = height;
    }
    [INSPECT]() { return `<PNG screenshot ${this.width}x${this.height}>`; }
  }

  class Dialog {
    #page; #info; #answer;
    constructor(page, info, answer) {
      this.#page = page;
      this.#info = info;
      this.#answer = answer;
    }
    [INSPECT]() { return `Dialog ${this.type()} ${lit(this.message())}`; }
    type() { return this.#info.type; }
    message() { return this.#info.message ?? ""; }
    defaultValue() { return this.#info.defaultPrompt ?? ""; }
    page() { return this.#page; }
    accept(promptText) { return this.#answer(true, promptText); }
    dismiss() { return this.#answer(false); }
  }

  const READS = new Set(["tab.snapshot", "tabs.list", "query", "inspect"]);
  class Page {
    #tab = null;
    #tabId = null;
    #url = "about:blank";
    #closed = false;
    #timeout = 5000;
    #navigationTimeout = 30000;
    #listeners = new Map();
    constructor() {
      pages.push(this);
      if (adopting) {
        [this.#tabId, this.#url] = [adopting.id, adopting.url];
        this.#tab = Promise.resolve(adopting.id);
        byTab.set(adopting.id, this);
        adopting = null;
      }
      const self = this;
      const ready = (url = "about:blank") => {
        if (this.#closed) return Promise.reject(new Error("Target page has been closed"));
        this.#tab ??= call("tabs.open", { url }).then((r) => {
          this.#url = r.tab?.url || url;
          this.#tabId = r.tab.id;
          byTab.set(r.tab.id, this);
          return r.tab.id;
        }, (error) => {
          this.#tab = null;
          throw error;
        });
        return this.#tab;
      };
      const timeout = (ms) => {
        const wanted = ms === undefined ? this.#timeout : Number(ms) || Infinity;
        return Math.max(0, Math.min(wanted, host.remaining() - 250));
      };
      const dialog = async (tabId, info) => {
        let answered = false, done;
        const handled = new Promise((resolve) => (done = resolve));
        const answer = async (accept, text) => {
          if (answered) throw new Error("Cannot accept dialog which is already handled!");
          answered = true;
          await call("extended", { tabId, operation: "dialog", accept, ...(text === undefined ? {} : { prompt_text: String(text) }) });
          done();
        };
        const listeners = [...(this.#listeners.get("dialog") ?? [])];
        if (!listeners.length) {
          await answer(false);
          return print(`[system] Dismissed a ${info.type} dialog saying ${lit(info.message ?? "")}; to answer it, `
            + "register page.on('dialog', d => d.accept()) before the action that opens it.");
        }
        const d = new Dialog(this, info, answer);
        for (const listener of listeners) {
          try { listener(d); } catch (error) { print(`[system] A dialog handler threw ${describe(error)}`); }
        }
        const limit = timeout();
        await Promise.race([handled, sleep(limit)]);
        if (!answered) {
          await answer(false);
          print(`[system] Dismissed a ${info.type} dialog that no handler answered within ${limit} ms.`);
        }
      };
      const request = async (method, params, again = true) => {
        if (this.#closed) throw new Error("Target page has been closed");
        let result;
        try {
          result = await call(method, params);
        } catch (error) {
          if (/no longer exists|is not a Second Cursor tab/.test(error.message)) {
            this.#closed = true;
            throw new Error("Target page has been closed");
          }
          throw error;
        }
        if (typeof result?.url === "string" && result.url) this.#url = result.url;
        // Popups, downloads and closed tabs the extension reported since its last reply; dialogs are answered below.
        if (isArray(result?.events))
          for (const line of result.events) if (!/^\[system\] dialog (opened|closed) /.test(line)) print(String(line));
        if (!result?.dialog_open) return result;
        await dialog(params.tabId, result.dialog);
        return again && READS.has(params.operation ?? method) ? request(method, params, false) : { ok: true };
      };
      internals.set(this, {
        ready,
        timeout,
        request,
        command: async (operation, args) => request("extended", { tabId: await ready(), operation, report_url: true, ...args }),
        async retry(name, desc, ms, retryable, attempt) {
          const limit = timeout(ms);
          const until = now() + limit;
          for (let n = 0; ; n++) {
            try {
              return await attempt();
            } catch (error) {
              const message = error.message;
              if (/^(locator|page)\.\w+: /.test(message) || error instanceof TimeoutError) throw error;
              if (/^strict mode violation/.test(message))
                throw new Error(`${name}: ${message.replace("strict mode violation:", `strict mode violation: ${desc}`)}`);
              if (!retryable.test(message)) throw new Error(`${name}: ${message}`);
              if (now() >= until)
                throw new TimeoutError(`${name}: Timeout ${limit}ms exceeded waiting for ${desc} (${why(message)})`);
            }
            await sleep(Math.min(POLL[Math.min(n, POLL.length - 1)], Math.max(0, until - now())));
          }
        },
      });
      this.keyboard = freeze(countArguments({
        press: async (key, options) => {
          const o = allow(options, ["noWaitAfter"], "keyboard.press");
          await self.#command("press_key", { keys: keyName(text(key, "keyboard.press's key")), wait_navigation: !o.noWaitAfter });
        },
        type: async (value) => void (await self.#command("type", { text: text(value, "keyboard.type's text") })),
        insertText: async (value) => void (await self.#command("type", { text: text(value, "keyboard.insertText's text") })),
        down: notAvailable("keyboard.down", "press a chord such as 'Shift+ArrowDown' with keyboard.press"),
        up: notAvailable("keyboard.up", "press a chord such as 'Shift+ArrowDown' with keyboard.press"),
      }, "keyboard"));
      this.mouse = freeze(countArguments({
        click: async (x, y, options) => {
          const o = allow(options, ["button", "clickCount", "noWaitAfter"], "mouse.click");
          await self.#command("click", { x: Number(x), y: Number(y), button: o.button, count: o.clickCount,
            wait_navigation: !o.noWaitAfter });
        },
        dblclick: async (x, y, options) => {
          const o = allow(options, ["button", "noWaitAfter"], "mouse.dblclick");
          await self.#command("click", { x: Number(x), y: Number(y), button: o.button, count: 2,
            wait_navigation: !o.noWaitAfter });
        },
        wheel: async (dx, dy) => {
          const vertical = Math.abs(dy) >= Math.abs(dx);
          const distance = vertical ? dy : dx;
          await self.#command("scroll", { direction: vertical ? (dy < 0 ? "up" : "down") : (dx < 0 ? "left" : "right"),
            amount: Math.max(1, Math.round(Math.abs(distance) / 100)) });
        },
        move: async (x, y, options) => {
          allow(options, [], "mouse.move");
          await self.#command("hover", { x: Number(x), y: Number(y) });
        },
        down: notAvailable("mouse.down", ""),
        up: notAvailable("mouse.up", ""),
      }, "mouse"));
    }
    [INSPECT]() { return `Page ${this.#url}${this.#tabId === null ? "" : ` (tab ${this.#tabId})`}${this.#closed ? " (closed)" : ""}`; }
    #command(operation, args) { return internals.get(this).command(operation, args); }
    #locator(steps, desc) { return new Locator(this, steps, desc); }
    context() { return context; }
    url() { return this.#url; }
    isClosed() { return this.#closed; }
    setDefaultTimeout(ms) { this.#timeout = Number(ms) || Infinity; }
    setDefaultNavigationTimeout(ms) { this.#navigationTimeout = Number(ms) || Infinity; }
    async bringToFront() {}
    async goto(url, options) {
      const o = allow(options, ["timeout", "waitUntil"], "goto");
      if (typeof url !== "string") throw new TypeError("page.goto takes a URL string");
      loadState(o.waitUntil, "goto's waitUntil");
      const page = internals.get(this);
      const limit = page.timeout(o.timeout ?? this.#navigationTimeout);
      const navigate = async () => {
        if (!this.#tab) return void (await page.ready(url));
        const r = await page.request("tab.navigate", { tabId: await this.#tab, url });
        this.#url = r.tab?.url || url;
      };
      await Promise.race([navigate(), sleep(limit).then(() => {
        throw new TimeoutError(`page.goto: Timeout ${limit}ms exceeded navigating to ${lit(url)}`);
      })]);
      return null;
    }
    async title() {
      const page = internals.get(this);
      const tabId = await page.ready();
      const tab = (await page.request("tabs.list", {})).tabs?.find((t) => t.id === tabId);
      if (tab) this.#url = tab.url;
      return tab?.title ?? "";
    }
    async snapshot() {
      const page = internals.get(this);
      const r = await page.request("tab.snapshot", { tabId: await page.ready(), full: true });
      if (r.tab?.url) this.#url = r.tab.url;
      return r.outline;
    }
    locator(selector, options) {
      const l = this.#locator(selectorSteps(selector), `locator(${lit(selector)})`);
      return options ? l[FILTER](options, "page.locator") : l;
    }
    getByRole(role, options) { return this.#locator([roleStep(role, options)], `getByRole(${lit(role)}${described(options ?? {})})`); }
    getByText(text, options) { return this.#locator([textStep("text", text, options, "getByText")], `getByText(${lit(text)}${described(options ?? {})})`); }
    getByLabel(text, options) { return this.#locator([textStep("label", text, options, "getByLabel")], `getByLabel(${lit(text)}${described(options ?? {})})`); }
    getByPlaceholder(text, options) { return this.#locator([textStep("placeholder", text, options, "getByPlaceholder")], `getByPlaceholder(${lit(text)}${described(options ?? {})})`); }
    getByAltText(text, options) { return this.#locator([textStep("alt", text, options, "getByAltText")], `getByAltText(${lit(text)}${described(options ?? {})})`); }
    getByTitle(text, options) { return this.#locator([textStep("title", text, options, "getByTitle")], `getByTitle(${lit(text)}${described(options ?? {})})`); }
    getByTestId(id) { return this.#locator([{ attribute: "data-testid", value: String(id) }], `getByTestId(${lit(id)})`); }
    async waitForTimeout(ms) { await sleep(Number(ms) || 0); }
    async waitForLoadState(state, options) {
      loadState(state, "waitForLoadState's state");
      const page = internals.get(this);
      const limit = page.timeout(allow(options, ["timeout"], "waitForLoadState").timeout ?? this.#navigationTimeout);
      try {
        const r = await page.command("wait", { event: "navigation", timeout_ms: limit });
        if (r.url) this.#url = r.url;
      } catch (error) {
        if (/deadline expired/.test(error.message)) throw new TimeoutError(`page.waitForLoadState: Timeout ${limit}ms exceeded`);
        throw error;
      }
    }
    async waitForURL(url, options) {
      const o = allow(options, ["timeout", "waitUntil"], "waitForURL");
      loadState(o.waitUntil, "waitForURL's waitUntil");
      let test;
      if (url instanceof RegExp) test = (u) => url.test(u);
      else if (typeof url === "function") test = (u) => !!url(u);
      else if (/[*?]/.test(String(url))) {
        const glob = new RegExp(`^${String(url).replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*/g, "\u0000")
          .replace(/\*/g, "[^/]*").replace(/\u0000/g, ".*").replace(/\?/g, ".")}$`);
        test = (u) => glob.test(u);
      } else test = (u) => u === String(url);
      const page = internals.get(this);
      await page.retry("page.waitForURL", `the URL to match ${lit(url)}`, o.timeout ?? this.#navigationTimeout, WAITING, async () => {
        const tabId = await page.ready();
        const tab = (await page.request("tabs.list", {})).tabs?.find((t) => t.id === tabId);
        if (tab) this.#url = tab.url;
        if (!test(this.#url)) throw new Error(`waiting: the URL is ${this.#url}`);
      });
    }
    async waitForSelector(selector, options) {
      const o = allow(options, ["state", "timeout"], "waitForSelector");
      const l = this.locator(selector);
      await l.waitFor(o);
      return o.state === "hidden" || o.state === "detached" ? null : l;
    }
    async reload() { await this.#command("reload"); return null; }
    async goBack() { await this.#command("back"); return null; }
    async goForward() { await this.#command("forward"); return null; }
    async screenshot(options) {
      const o = allow(options, ["fullPage", "path", "type"], "screenshot");
      if (o.type !== undefined && o.type !== "png") throw new TypeError("page.screenshot takes only type: 'png'");
      if (o.path) throw new Error("page.screenshot: browser_repl has no filesystem; return the screenshot as the cell's value to see it");
      const page = internals.get(this);
      const tabId = await page.ready();
      const r = o.fullPage ? await page.request("extended", { tabId, operation: "screenshot", full_page: true })
        : await page.request("tab.screenshot", { tabId });
      return new Screenshot(r.image, r.width, r.height);
    }
    async close() {
      if (this.#tab && !this.#closed) await call("tabs.close", { tabId: await this.#tab }).catch(() => {});
      this.#closed = true;
    }
    on(event, listener) {
      if (event !== "dialog") throw new Error(`page.on('${event}') is not available in browser_repl; only 'dialog' events are reported`);
      if (typeof listener !== "function") throw new TypeError("page.on takes a listener function");
      this.#listeners.set(event, [...(this.#listeners.get(event) ?? []), listener]);
      return this;
    }
    addListener(event, listener) { return this.on(event, listener); }
    once(event, listener) {
      const wrapper = (value) => {
        this.off(event, wrapper);
        return listener(value);
      };
      return this.on(event, wrapper);
    }
    off(event, listener) {
      this.#listeners.set(event, (this.#listeners.get(event) ?? []).filter((l) => l !== listener));
      return this;
    }
    removeListener(event, listener) { return this.off(event, listener); }
  }
  for (const name of ["evaluate", "evaluateHandle", "$eval", "$$eval", "addScriptTag", "addStyleTag", "addInitScript",
    "exposeFunction", "exposeBinding", "route", "content", "setContent"])
    Page.prototype[name] = notAvailable(`page.${name}`, NO_SCRIPT);
  for (const name of ["$", "$$"]) Page.prototype[name] = notAvailable(`page.${name}`, "use page.locator(selector)");
  for (const name of ["waitForEvent", "waitForRequest", "waitForResponse", "frameLocator", "pdf"])
    Page.prototype[name] = notAvailable(`page.${name}`, "");

  class BrowserContext {
    [INSPECT]() { return `BrowserContext (${this.pages().length} pages)`; }
    async newPage() {
      const page = new Page();
      await internals.get(page).ready();
      return page;
    }
    pages() { return pages.filter((page) => !page.isClosed()); }
  }
  for (const [cls, prefix] of [[Locator, "locator"], [Page, "page"], [BrowserContext, "context"], [Dialog, "dialog"]])
    countArguments(cls.prototype, prefix);
  const context = new BrowserContext();
  global.context = context;
  global.page = new Page();
  const adopt = (id, url) => {
    const known = byTab.get(id);
    if (known && !known.isClosed()) return known;
    adopting = { id, url };
    return new Page();
  };

  // One hop later: unhandled rejections are reported once the microtask queue drains, after the cell settles.
  const finish = (ok, text, image = -1) => host.sleep(0, () => host.done(ok, capped(text), image), () => {});
  return freeze({
    run(code, tabId, url) {
      let compiled, promise;
      try {
        if (tabId !== undefined) global.page = adopt(tabId, url);
        compiled = compile(String(code));
        if (!compiled.kept) print("[system] This cell ran without keeping its declarations for later calls.");
        promise = compiled.fn.call(global, global);
      } catch (error) {
        return finish(false, describe(error));
      }
      promise.then((value) => {
        if (value instanceof Screenshot) return finish(true, inspect(value), images.get(value) ?? -1);
        finish(true, value === undefined ? "" : typeof value === "string" && value ? value : inspect(value));
      }, (error) => finish(false, describe(error)));
    },
    unhandledRejection(promise, reason) {
      print(`[system] Unhandled promise rejection: ${describe(reason)}`);
    },
  });
})
"""#
}
