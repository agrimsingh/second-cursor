import { cdp, frameCDP } from "./cdp.js";
import { concealDuring, dragTo, pointTo, setCaption, show } from "./cursor.js";
import { exportContent } from "./exports.js";
import { BridgeError, sleep } from "./errors.js";
import { click, pressKey, scroll, type } from "./input.js";
import {
  requireAgentTab,
  userTabs,
  claimTab,
  releaseTab,
  retainTab,
  settleAround,
} from "./tabs.js";

const events = new Map();
chrome.tabs.onRemoved.addListener((id) => events.delete(id));
function state(id) {
  if (!events.has(id))
    events.set(id, {
      logs: [],
      downloads: [],
      assets: new Map(),
      dialog: null,
      chooser: null,
      dialogWaiters: new Set(),
      naturalSize: null,
    });
  return events.get(id);
}
export function recordEvent(source, method, p) {
  const s = state(source.tabId);
  if (method === "Runtime.consoleAPICalled") {
    s.logs.push({
      level: p.type,
      message: p.args.map((a) => a.value ?? a.description ?? "").join(" "),
      timestamp: p.timestamp,
    });
    if (s.logs.length > 500) s.logs.shift();
  }
  if (method === "Runtime.exceptionThrown")
    s.logs.push({
      level: "error",
      message: p.exceptionDetails.text,
      timestamp: p.timestamp,
    });
  if (method === "Page.javascriptDialogOpening") {
    s.dialog = p;
    for (const wake of s.dialogWaiters) wake(p);
  }
  if (method === "Page.javascriptDialogClosed") s.dialog = null;
  if (method === "Page.fileChooserOpened") s.chooser = p;
  if (method === "Page.downloadWillBegin") {
    s.downloads.push(p);
    if (s.downloads.length > 100) s.downloads.shift();
  }
  if (method === "Network.responseReceived") {
    s.assets.set(p.requestId, {
      id: p.requestId,
      url: p.response.url,
      mime_type: p.response.mimeType,
      status: p.response.status,
    });
    if (s.assets.size > 500) s.assets.delete(s.assets.keys().next().value);
  }
}

// This function executes in an isolated page world and only operates on DOM elements.
function domOperation(p) {
  let doc = document;
  for (const selector of Array.isArray(p.locator?.frame)
    ? p.locator.frame
    : p.locator?.frame
      ? [p.locator.frame]
      : []) {
    const frames = [...doc.querySelectorAll(selector)];
    if (frames.length !== 1)
      throw Error("Frame selector must match exactly one frame");
    doc = frames[0].contentDocument;
    if (!doc)
      throw Error("Frame is cross-origin; resolve it through a frame context");
  }
  const root = doc;
  const role = (e) =>
    e.getAttribute("role") ||
    {
      BUTTON: "button",
      A: "link",
      TEXTAREA: "textbox",
      SELECT: "combobox",
      IMG: "img",
      H1: "heading",
      H2: "heading",
      IFRAME: "iframe",
    }[e.tagName] ||
    (e.tagName === "INPUT"
      ? {
          checkbox: "checkbox",
          radio: "radio",
          range: "slider",
          number: "spinbutton",
          search: "searchbox",
          button: "button",
          submit: "button",
        }[e.type] || "textbox"
      : "");
  const labelText = (l) => {
    const clone = l.cloneNode(true);
    clone
      .querySelectorAll("input,select,textarea,button")
      .forEach((e) => e.remove());
    return clone.textContent.trim();
  };
  const name = (e) =>
    e.getAttribute("aria-label") ||
    (e.getAttribute("aria-labelledby") || "")
      .split(/\s+/)
      .map((id) => doc.getElementById(id)?.textContent || "")
      .join(" ")
      .trim() ||
    [...(e.labels ?? [])].map(labelText).join(" ").trim() ||
    e.getAttribute("alt") ||
    e.getAttribute("title") ||
    (e.matches("input[type=button],input[type=submit]") ? e.value : "") ||
    e.innerText ||
    "";
  const visible = (e) =>
    !!e.getClientRects().length && getComputedStyle(e).visibility !== "hidden";
  const matches = (a, b) =>
    p.locator?.exact === false ? a.includes(b) : a.trim() === b;
  function locate() {
    const l = p.locator || {};
    // The agent's own cursor (cursor.js) is not part of the page.
    let found = [...root.querySelectorAll(l.selector || "*")].filter(
      (e) => e.id !== "second-cursor-overlay",
    );
    if (l.role) found = found.filter((e) => role(e) === l.role);
    if (l.name !== undefined)
      found = found.filter((e) => matches(name(e), l.name));
    if (l.label !== undefined)
      found = found.filter((e) =>
        matches(
          e.getAttribute("aria-label") ||
            [...(e.labels ?? [])].map(labelText).join(" ").trim(),
          l.label,
        ),
      );
    if (l.placeholder !== undefined)
      found = found.filter((e) =>
        matches(e.getAttribute("placeholder") || "", l.placeholder),
      );
    if (l.test_id !== undefined)
      found = found.filter((e) => e.getAttribute("data-testid") === l.test_id);
    if (l.text !== undefined)
      found = found.filter(
        (e) =>
          matches(e.innerText || "", l.text) &&
          ![...e.children].some((c) => matches(c.innerText || "", l.text)),
      );
    if (l.visible !== undefined)
      found = found.filter((e) => visible(e) === l.visible);
    if (Number.isInteger(l.nth)) found = found.slice(l.nth, l.nth + 1);
    return found;
  }
  if (p.operation === "dom_snapshot") {
    const walk = (d, depth = 0) =>
      [...d.querySelectorAll("body *")].flatMap((e) => {
        if (["SCRIPT", "STYLE", "NOSCRIPT"].includes(e.tagName)) return [];
        if (e.tagName === "IFRAME")
          return [
            {
              role: "iframe",
              name: name(e),
              src: e.src,
              children:
                e.contentDocument && depth < 8
                  ? walk(e.contentDocument, depth + 1)
                  : [],
              cross_origin: !e.contentDocument,
            },
          ];
        const r = role(e),
          n = name(e);
        if (
          !r &&
          !e.matches(
            "p,li,td,th,[contenteditable],[draggable=true],[tabindex],[onclick]",
          )
        )
          return [];
        const b = e.getBoundingClientRect();
        return [
          {
            role: r || e.tagName.toLowerCase(),
            name: n.slice(0, 300),
            value: "value" in e ? String(e.value).slice(0, 300) : null,
            value_length: "value" in e ? String(e.value).length : null,
            enabled: !e.disabled,
            focused: d.activeElement === e,
            checked: "checked" in e ? e.checked : null,
            selected: e.getAttribute("aria-selected"),
            expanded: e.getAttribute("aria-expanded"),
            editable: e.isContentEditable || e.matches("input,textarea"),
            visible: visible(e),
            bounds: { x: b.x, y: b.y, width: b.width, height: b.height },
            actions: e.disabled
              ? []
              : [
                  "click",
                  "focus",
                  "scroll_into_view",
                  ...(e.hasAttribute("aria-expanded")
                    ? [
                        e.getAttribute("aria-expanded") === "true"
                          ? "collapse"
                          : "expand",
                      ]
                    : []),
                  ...(e.matches("input[type=number],input[type=range]")
                    ? ["increment", "decrement"]
                    : []),
                  ...(e.matches("input,textarea,[contenteditable]")
                    ? ["fill", "type", "select_text"]
                    : []),
                ],
          },
        ];
      });
    return {
      url: location.href,
      title: doc.title,
      elements: walk(doc).slice(0, 3000),
      truncated: doc.querySelectorAll("body *").length > 3000,
    };
  }
  const found = locate();
  if (p.operation === "query")
    return {
      count: found.length,
      visible: found.length === 1 && visible(found[0]),
    };
  if (found.length !== 1)
    throw Error(
      `Locator matched ${found.length} elements; refine it to exactly one`,
    );
  const e = found[0];
  if (p.operation === "node") return e;
  if (p.operation === "inspect") {
    const text =
        "value" in e ? String(e.value) : e.innerText || e.textContent || "",
      offset = p.offset ?? 0,
      length = p.length ?? 4000;
    if (offset < 0 || length < 0 || length > 100000)
      throw Error("Invalid text range");
    const b = e.getBoundingClientRect();
    return {
      bounds: { x: b.x, y: b.y, width: b.width, height: b.height },
      text: text.slice(offset, offset + length),
      total_length: text.length,
      has_more: offset + length < text.length,
      role: role(e),
      name: name(e),
      enabled: !e.disabled,
      visible: visible(e),
      checked: "checked" in e ? e.checked : null,
      attributes: Object.fromEntries(
        [...e.attributes].map((x) => [x.name, x.value]),
      ),
    };
  }
  if (e.disabled) throw Error("Element is disabled");
  if (p.operation === "focus") {
    e.focus();
    return { focused: doc.activeElement === e };
  }
  if (p.operation === "perform_action") {
    const actions = [
      "focus",
      "scroll_into_view",
      ...(e.hasAttribute("aria-expanded")
        ? [e.getAttribute("aria-expanded") === "true" ? "collapse" : "expand"]
        : []),
      ...(e.matches("input[type=number],input[type=range]")
        ? ["increment", "decrement"]
        : []),
    ];
    if (!actions.includes(p.action))
      throw Error(
        "Action is not exposed by this element: " + actions.join(", "),
      );
    if (p.action === "focus") e.focus();
    else if (p.action === "scroll_into_view")
      e.scrollIntoView({ block: "nearest" });
    else if (p.action === "expand" || p.action === "collapse") e.click();
    else {
      p.action === "increment" ? e.stepUp() : e.stepDown();
      e.dispatchEvent(new Event("input", { bubbles: true }));
      e.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return { ok: true };
  }
  if (p.operation === "fill") {
    e.focus();
    if (e.isContentEditable) e.textContent = p.text;
    else {
      const proto =
        e.tagName === "TEXTAREA"
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(e, p.text);
    }
    e.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: p.text,
      }),
    );
    e.dispatchEvent(new Event("change", { bubbles: true }));
    return { value: "value" in e ? e.value : e.textContent };
  }
  if (p.operation === "select_option") {
    const values = Array.isArray(p.value) ? p.value : [p.value];
    const opts = [...e.options];
    if (!values.every((v) => opts.some((o) => o.value === v)))
      throw Error("Option not found");
    for (const o of opts) o.selected = values.includes(o.value);
    e.dispatchEvent(new Event("input", { bubbles: true }));
    e.dispatchEvent(new Event("change", { bubbles: true }));
    return { value: e.value };
  }
  if (p.operation === "check") {
    if (!["checkbox", "radio"].includes(e.type))
      throw Error("Element is not checkbox/radio");
    if (e.checked !== (p.checked !== false)) e.click();
    return { checked: e.checked };
  }
  if (p.operation === "select_text") {
    const text = "value" in e ? e.value : e.textContent,
      needle = p.text;
    if (!needle) throw Error("Nonempty text required");
    const indexes = [];
    for (
      let i = text.indexOf(needle);
      i >= 0;
      i = text.indexOf(needle, i + needle.length)
    )
      if (
        (p.prefix === undefined || text.slice(0, i).endsWith(p.prefix)) &&
        (p.suffix === undefined ||
          text.slice(i + needle.length).startsWith(p.suffix))
      )
        indexes.push(i);
    if (indexes.length !== 1)
      throw Error(`Text matched ${indexes.length} times; use prefix/suffix`);
    let start = indexes[0],
      end = start + needle.length;
    if (p.selection_type === "cursor_before") end = start;
    else if (p.selection_type === "cursor_after") start = end;
    e.focus();
    if (e.setSelectionRange) e.setSelectionRange(start, end);
    else {
      const walker = doc.createTreeWalker(e, NodeFilter.SHOW_TEXT),
        nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      const point = (offset) => {
        for (const n of nodes) {
          if (offset <= n.length) return [n, offset];
          offset -= n.length;
        }
        return [e, e.childNodes.length];
      };
      const range = doc.createRange();
      range.setStart(...point(start));
      range.setEnd(...point(end));
      const selection = doc.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    return { start, end };
  }
  if (p.operation === "paste") {
    e.focus();
    const transfer = new DataTransfer();
    transfer.setData("text/plain", p.text);
    if (p.format === "html") transfer.setData("text/html", p.text);
    const event = new ClipboardEvent("paste", {
      clipboardData: transfer,
      bubbles: true,
      cancelable: true,
    });
    if (e.dispatchEvent(event))
      doc.execCommand(
        p.format === "html" ? "insertHTML" : "insertText",
        false,
        p.text,
      );
    return { ok: true };
  }
  if (p.operation === "scroll") {
    const n = (p.amount ?? 3) * 100;
    const [x, y] =
      { up: [0, -n], down: [0, n], left: [-n, 0], right: [n, 0] }[
        p.direction
      ] || [];
    if (x === undefined) throw Error("Invalid direction");
    e.scrollBy(x, y);
    return { scroll_top: e.scrollTop, scroll_left: e.scrollLeft };
  }
  if (p.operation === "center") {
    if (p.scroll === "if_needed") {
      // Only as far as focusing the element would: the cursor must not move the page more than the action does.
      const r = e.getBoundingClientRect(),
        view = doc.defaultView;
      if (
        r.bottom < 0 ||
        r.right < 0 ||
        r.top > view.innerHeight ||
        r.left > view.innerWidth
      )
        e.scrollIntoView({ block: "nearest", inline: "nearest" });
    } else e.scrollIntoView({ block: "center", inline: "center" });
    const b = e.getBoundingClientRect();
    let x = b.x + b.width / 2,
      y = b.y + b.height / 2;
    let view = doc.defaultView;
    while (view !== window && view.frameElement) {
      const r = view.frameElement.getBoundingClientRect();
      x += r.x;
      y += r.y;
      view = view.parent;
    }
    return { x, y };
  }
  throw Error("Unsupported DOM operation");
}

async function contextFor(tabId, p) {
  const frames = Array.isArray(p.locator?.frame)
    ? p.locator.frame
    : p.locator?.frame
      ? [p.locator.frame]
      : [];
  if (!frames.length)
    return {
      send: (method, params) => cdp(tabId, method, params),
      offset: { x: 0, y: 0 },
    };
  let frameId = (await cdp(tabId, "Page.getFrameTree")).frameTree.frame.id,
    offset = { x: 0, y: 0 },
    contextId;
  for (const selector of frames) {
    contextId = (
      await frameCDP(tabId, frameId, "Page.createIsolatedWorld", {
        frameId,
        worldName: "SecondCursor",
      })
    ).executionContextId;
    const expression = `(()=>{const es=[...document.querySelectorAll(${JSON.stringify(selector)})];if(es.length!==1)throw Error('Frame selector must match exactly one frame');return es[0];})()`;
    const node = await frameCDP(tabId, frameId, "Runtime.evaluate", {
      expression,
      contextId,
    });
    if (node.exceptionDetails || !node.result.objectId)
      throw new BridgeError("failed", "Frame selector did not resolve");
    const rect = await frameCDP(tabId, frameId, "Runtime.callFunctionOn", {
      objectId: node.result.objectId,
      functionDeclaration:
        'function(){this.scrollIntoView({block:"nearest"});const b=this.getBoundingClientRect();return {x:b.x+this.clientLeft,y:b.y+this.clientTop}}',
      returnByValue: true,
    });
    offset.x += rect.result.value.x;
    offset.y += rect.result.value.y;
    const description = await frameCDP(tabId, frameId, "DOM.describeNode", {
      objectId: node.result.objectId,
    });
    await frameCDP(tabId, frameId, "Runtime.releaseObject", {
      objectId: node.result.objectId,
    });
    const child =
      description.node.contentDocument?.frameId ?? description.node.frameId;
    if (!child || child === frameId)
      throw new BridgeError("failed", "Frame has not loaded");
    frameId = child;
  }
  contextId = (
    await frameCDP(tabId, frameId, "Page.createIsolatedWorld", {
      frameId,
      worldName: "SecondCursor",
    })
  ).executionContextId;
  return {
    send: (method, params) => frameCDP(tabId, frameId, method, params),
    contextId,
    offset,
  };
}
async function dom(tabId, p, object = false) {
  const context = await contextFor(tabId, p);
  const arg = { ...p, locator: { ...p.locator, frame: undefined } };
  const result = await context.send("Runtime.evaluate", {
    expression: `(${domOperation.toString()})(${JSON.stringify(arg)})`,
    returnByValue: !object,
    awaitPromise: true,
    ...(context.contextId ? { contextId: context.contextId } : {}),
  });
  if (result.exceptionDetails)
    throw new BridgeError(
      "failed",
      result.exceptionDetails.exception?.description ??
        result.exceptionDetails.text,
    );
  if (object) return { ...result.result, send: context.send };
  const value = result.result.value;
  if (p.operation === "center") {
    value.x += context.offset.x;
    value.y += context.offset.y;
  }
  return value;
}
const ok = () => ({ ok: true });
// Operations that act on the page; each sets the cursor's caption, or clears it when it has none.
const CURSOR_OPERATIONS = new Set([
  "click",
  "drag",
  "scroll",
  "fill",
  "type",
  "paste",
  "select_text",
  "check",
  "select_option",
  "perform_action",
  "press_key",
  "back",
  "forward",
  "reload",
]);
// Moves the cursor to the element before the action and resolves when it arrives. A locator that matches nothing
// is left for the action itself to report.
const pointAtLocator = async (tabId, p) => {
  const point = await dom(tabId, {
    ...p,
    operation: "center",
    scroll: "if_needed",
  }).catch(() => null);
  if (point) await pointTo(tabId, point);
};
const PRIVATE_OPERATIONS = new Set([
  "user_tabs",
  "claim",
  "history",
  "clipboard",
  "cdp",
  "upload",
]);
const CDP_ALLOWLIST = new Set([
  "Page.getLayoutMetrics",
  "Page.getNavigationHistory",
  "DOM.getDocument",
  "Accessibility.getFullAXTree",
]);
const OPERATION_ARGUMENTS = {
  user_tabs: "No arguments. Local approval required.",
  claim:
    "tab_id. Local approval required. Control lasts until release, session close, turn end (default session), or 5 idle minutes. browser_close on a claimed tab only releases it.",
  release: "tab_id. Release ownership without closing the tab.",
  retain: "mode: ephemeral | deliverable | handoff",
  dom_snapshot: "tab_id",
  inspect: "locator; optional offset, length in UTF-16 units",
  click:
    "locator or x,y; optional button (left/right/middle), count (1..3), modifiers array",
  fill: "locator, text",
  type: "text; optional locator",
  paste:
    "text; optional locator, format (text/md/html). HTML requires locator.",
  select_text:
    "locator, text; optional prefix, suffix, selection_type (text/cursor_before/cursor_after)",
  perform_action: "locator, action from the observed actions",
  check: "locator, checked boolean",
  select_option: "locator, value",
  press_key: "keys; optional locator",
  scroll: "direction, amount; optional locator",
  drag: "from_x, from_y, to_x, to_y in viewport CSS pixels",
  back: "tab_id",
  forward: "tab_id",
  reload: "tab_id",
  wait: "url substring, or locator and state (visible/hidden/attached/detached), or event (download/filechooser/navigation); timeout_ms",
  dialog:
    "omit accept to inspect; accept boolean and optional prompt_text to answer",
  upload:
    "files array of absolute paths; optional locator, otherwise intercepted chooser. Local approval required.",
  downloads: "tab_id",
  logs: "tab_id",
  history:
    "optional query, from/to (Unix milliseconds), limit. Local approval required.",
  export: "format (text/html/md/pdf/xlsx/csv/docx/pptx/youtube_transcript)",
  assets: "omit action to list; action:bundle and asset_ids array for content",
  viewport: "width, height or reset:true",
  screenshot: "optional full_page boolean or clip:{x,y,width,height}",
  cdp: "method from cdp_allowlist; optional params object. Local approval required.",
  batch:
    "steps array of {operation,...arguments}. One tab, 1..50 steps, no nested batches. Executed by the native runtime.",
  clipboard: "action read/write; text for write. Local approval required.",
};

export async function extended(p) {
  const { tabId, operation: op } = p,
    session = p.session_id ?? "legacy";
  if (
    ["fill", "type", "paste", "select_text"].includes(op) &&
    typeof p.text !== "string"
  )
    throw new BridgeError("invalid_argument", "text must be a string");
  if (
    p.selection_type &&
    !["text", "cursor_before", "cursor_after"].includes(p.selection_type)
  )
    throw new BridgeError("invalid_argument", "Invalid selection_type");
  if (p.button && !["left", "right", "middle"].includes(p.button))
    throw new BridgeError("invalid_argument", "Invalid mouse button");
  if (
    (p.modifiers ?? []).some(
      (m) => !["alt", "ctrl", "cmd", "shift"].includes(m),
    )
  )
    throw new BridgeError("invalid_argument", "Unsupported modifier");
  if (
    p.count !== undefined &&
    (!Number.isInteger(p.count) || p.count < 1 || p.count > 3)
  )
    throw new BridgeError("invalid_argument", "count must be 1, 2 or 3");
  if (op === "capabilities")
    return {
      operations: Object.keys(OPERATION_ARGUMENTS),
      arguments: OPERATION_ARGUMENTS,
      cdp_allowlist: [...CDP_ALLOWLIST],
      local_approval_required: [...PRIVATE_OPERATIONS],
      locator_fields: [
        "selector",
        "role",
        "name",
        "label",
        "text",
        "placeholder",
        "test_id",
        "frame",
        "nth",
        "visible",
        "exact",
      ],
    };
  if (op === "cdp" && !CDP_ALLOWLIST.has(p.method)) {
    throw new BridgeError(
      "not_allowed",
      "CDP method is outside the tab-scoped allowlist",
    );
  }
  if (PRIVATE_OPERATIONS.has(op) && !p.local_approvals?.includes(op)) {
    throw new BridgeError("not_allowed", "Local approval required");
  }
  if (op === "user_tabs") return userTabs();
  if (op === "history")
    return {
      history: await chrome.history.search({
        text: p.query ?? "",
        startTime: p.from ?? Date.now() - 86400000,
        endTime: p.to ?? Date.now(),
        maxResults: Math.min(p.limit ?? 100, 1000),
      }),
    };
  if (op === "claim") return claimTab(tabId, session);
  await requireAgentTab(tabId, session);
  if (op === "release") return releaseTab(tabId, session);
  if (op === "retain") return retainTab(tabId, session, p.mode);
  if (CURSOR_OPERATIONS.has(op)) setCaption(tabId, p.caption ?? "", session);
  if (op === "dom_snapshot" || op === "inspect") return dom(tabId, p);
  if (
    ["fill", "check", "select_option", "select_text", "perform_action"].includes(
      op,
    )
  ) {
    await pointAtLocator(tabId, p);
    return dom(tabId, p);
  }
  if (op === "click") {
    const point = p.locator
      ? await dom(tabId, { ...p, operation: "center" })
      : { x: p.x, y: p.y };
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y))
      throw new BridgeError(
        "invalid_argument",
        "click needs locator or coordinates",
      );
    const modifiers = (p.modifiers ?? []).reduce(
        (n, m) => n | ({ alt: 1, ctrl: 2, cmd: 4, shift: 8 }[m] ?? 0),
        0,
      ),
      button = p.button ?? "left";
    const send = (type) =>
      cdp(tabId, "Input.dispatchMouseEvent", {
        type,
        ...point,
        button,
        clickCount: p.count ?? 1,
        modifiers,
      });
    await pointTo(tabId, point, p.count ?? 1);
    // Do not wait for navigation while a JS dialog is open.
    await send("mousePressed");
    await send("mouseReleased");
    return ok();
  }
  if (op === "type" || op === "press_key") {
    if (p.locator) {
      if (op === "type") await pointAtLocator(tabId, p);
      await dom(tabId, { ...p, operation: "focus" });
    }
    if (op === "type") await type(tabId, { text: p.text });
    else await pressKey(tabId, p.keys);
    return ok();
  }
  if (op === "paste") {
    if (!["text", "md", "html"].includes(p.format ?? "text"))
      throw new BridgeError("invalid_argument", "Unsupported paste format");
    if (p.locator) {
      await pointAtLocator(tabId, p);
      return dom(tabId, p);
    }
    if (p.format === "html")
      throw new BridgeError(
        "invalid_argument",
        "HTML paste requires a locator",
      );
    show(tabId);
    await cdp(tabId, "Input.insertText", { text: p.text });
    return ok();
  }
  if (op === "scroll") {
    if (p.locator) {
      await pointAtLocator(tabId, p);
      return dom(tabId, p);
    }
    await scroll(tabId, p.direction, p.amount);
    return ok();
  }
  if (op === "drag") {
    const values = [p.from_x, p.from_y, p.to_x, p.to_y];
    if (!values.every(Number.isFinite))
      throw new BridgeError(
        "invalid_argument",
        "Four drag coordinates required",
      );
    await dragTo(tabId, { x: p.from_x, y: p.from_y }, { x: p.to_x, y: p.to_y });
    await cdp(tabId, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: p.from_x,
      y: p.from_y,
      button: "left",
      buttons: 1,
      clickCount: 1,
    });
    try {
      for (let i = 1; i <= 20; i++)
        await cdp(tabId, "Input.dispatchMouseEvent", {
          type: "mouseMoved",
          x: p.from_x + ((p.to_x - p.from_x) * i) / 20,
          y: p.from_y + ((p.to_y - p.from_y) * i) / 20,
          button: "left",
          buttons: 1,
        });
    } finally {
      await cdp(tabId, "Input.dispatchMouseEvent", {
        type: "mouseReleased",
        x: p.to_x,
        y: p.to_y,
        button: "left",
        buttons: 0,
        clickCount: 1,
      });
    }
    return ok();
  }
  if (op === "back" || op === "forward") {
    const history = await cdp(tabId, "Page.getNavigationHistory");
    const entry =
      history.entries[history.currentIndex + (op === "back" ? -1 : 1)];
    if (!entry) throw new BridgeError("failed", "No history entry");
    await settleAround(tabId, () =>
      cdp(tabId, "Page.navigateToHistoryEntry", { entryId: entry.id }),
    );
    show(tabId);
    return ok();
  }
  if (op === "reload") {
    await settleAround(tabId, () => chrome.tabs.reload(tabId));
    show(tabId);
    return ok();
  }
  if (op === "dialog") {
    const dialog = state(tabId).dialog;
    if (p.accept === undefined) return { dialog };
    if (!dialog) throw new BridgeError("failed", "No JavaScript dialog");
    await cdp(tabId, "Page.handleJavaScriptDialog", {
      accept: p.accept,
      promptText: p.prompt_text ?? "",
    });
    return ok();
  }
  if (op === "upload") {
    if (
      !Array.isArray(p.files) ||
      !p.files.length ||
      p.files.some((x) => !x.startsWith("/"))
    )
      throw new BridgeError("invalid_argument", "Absolute file paths required");
    if (!p.locator) {
      const chooser = state(tabId).chooser;
      if (!chooser) throw new BridgeError("failed", "No active file chooser");
      await cdp(tabId, "DOM.setFileInputFiles", {
        backendNodeId: chooser.backendNodeId,
        files: p.files,
      });
      state(tabId).chooser = null;
      return ok();
    }
    const node = await dom(tabId, { ...p, operation: "node" }, true);
    try {
      await node.send("DOM.setFileInputFiles", {
        objectId: node.objectId,
        files: p.files,
      });
    } finally {
      await node.send("Runtime.releaseObject", { objectId: node.objectId });
    }
    return ok();
  }
  if (op === "wait") {
    const until = Date.now() + Math.min(p.timeout_ms ?? 10000, 60000);

    while (Date.now() < until) {
      if (p.event === "download" && state(tabId).downloads.length) {
        const event = state(tabId).downloads.shift();
        const matches = await chrome.downloads.search({
          url: event.url,
          orderBy: ["-startTime"],
          limit: 1,
        });
        return { download: { ...event, ...matches[0] } };
      }
      if (p.event === "filechooser" && state(tabId).chooser)
        return { filechooser: state(tabId).chooser };
      const t = await chrome.tabs.get(tabId);
      if (p.url && t.url?.includes(p.url) && t.status === "complete")
        return { url: t.url };
      if (p.event === "navigation" && t.status === "complete")
        return { url: t.url };
      if (p.locator) {
        const q = await dom(tabId, { ...p, operation: "query" });
        if (
          {
            attached: q.count > 0,
            detached: q.count === 0,
            visible: q.visible,
            hidden: !q.visible,
          }[p.state ?? "visible"]
        )
          return q;
      }
      await sleep(50);
    }
    throw new BridgeError("failed", "Wait deadline expired");
  }
  if (op === "downloads") return { downloads: state(tabId).downloads };
  if (op === "logs")
    return { logs: state(tabId).logs.slice(-(p.limit ?? 100)) };
  if (op === "viewport") {
    const s = state(tabId);
    const size = async () =>
      (
        await cdp(tabId, "Runtime.evaluate", {
          expression: "[innerWidth, innerHeight]",
          returnByValue: true,
        })
      ).result.value;
    let want;
    if (p.reset) {
      await cdp(tabId, "Emulation.clearDeviceMetricsOverride");
      [want, s.naturalSize] = [s.naturalSize, null];
    } else {
      if (
        !Number.isInteger(p.width) ||
        !Number.isInteger(p.height) ||
        p.width < 1 ||
        p.height < 1
      )
        throw new BridgeError(
          "invalid_argument",
          "Positive width and height required",
        );
      s.naturalSize ??= await size();
      await cdp(tabId, "Emulation.setDeviceMetricsOverride", {
        width: p.width,
        height: p.height,
        deviceScaleFactor: 1,
        mobile: false,
      });
      want = [p.width, p.height];
    }
    // The page resizes some time after the call returns; an action before then aims at the old layout.
    const until = Date.now() + 1000;
    while (want && Date.now() < until) {
      const [width, height] = await size();
      if (width === want[0] && height === want[1]) break;
      await sleep(20);
    }
    return ok();
  }
  if (op === "screenshot") {
    const metrics = await cdp(tabId, "Page.getLayoutMetrics"),
      v = metrics.cssVisualViewport;
    const clip =
      p.clip ??
      (p.full_page
        ? {
            x: 0,
            y: 0,
            width: metrics.cssContentSize.width,
            height: metrics.cssContentSize.height,
          }
        : {
            x: v.pageX,
            y: v.pageY,
            width: v.clientWidth,
            height: v.clientHeight,
          });
    if (
      ![clip.x, clip.y, clip.width, clip.height].every(Number.isFinite) ||
      clip.width <= 0 ||
      clip.height <= 0 ||
      clip.width * clip.height > 40e6
    )
      throw new BridgeError(
        "invalid_argument",
        "Invalid or excessively large screenshot",
      );
    const { result } = await cdp(tabId, "Runtime.evaluate", {
      expression: "devicePixelRatio",
      returnByValue: true,
    });
    const { data } = await concealDuring(tabId, () =>
      cdp(tabId, "Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: true,
        clip: { ...clip, scale: 1 / (result.value || 1) },
      }),
    );
    const bytes = atob(data.slice(0, 32)),
      u32 = (o) =>
        ((bytes.charCodeAt(o) << 24) |
          (bytes.charCodeAt(o + 1) << 16) |
          (bytes.charCodeAt(o + 2) << 8) |
          bytes.charCodeAt(o + 3)) >>>
        0;
    return {
      data,
      width: u32(16),
      height: u32(20),
      scale: 1,
      origin: { x: clip.x, y: clip.y },
      coordinate_space: "document CSS pixels",
    };
  }
  if (op === "export") return exportContent(tabId, p);
  if (op === "assets") {
    const { result } = await cdp(tabId, "Runtime.evaluate", {
      expression:
        '[location.href,...performance.getEntriesByType("resource").map(x=>x.name),...[...document.querySelectorAll("img[src],video[src],audio[src],source[src],link[href]")].map(x=>x.src||x.href)]',
      returnByValue: true,
    });
    for (const url of result.value ?? [])
      if (
        /^https?:/.test(url) &&
        ![...state(tabId).assets.values()].some((a) => a.url === url)
      )
        state(tabId).assets.set("url:" + url, {
          id: "url:" + url,
          url,
          mime_type: null,
          source: "page_observation",
        });
    const assets = [...state(tabId).assets.values()];
    if (p.action !== "bundle") return { assets };
    const selected = p.asset_ids ?? [];
    if (selected.length > 30)
      throw new BridgeError("invalid_argument", "Maximum 30 assets per bundle");
    return {
      assets: await Promise.all(
        selected.map(async (id) => {
          const item = state(tabId).assets.get(id);
          if (!item)
            throw new BridgeError("invalid_argument", "Unknown observed asset");
          if (!id.startsWith("url:"))
            return {
              ...item,
              ...(await cdp(tabId, "Network.getResponseBody", {
                requestId: id,
              })),
            };
          const expression = `(async()=>{const r=await fetch(${JSON.stringify(item.url)});if(!r.ok)throw Error('Asset fetch failed '+r.status);const bytes=new Uint8Array(await r.arrayBuffer());if(bytes.length>8388608)throw Error('Asset exceeds 8MB');let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));return {body:btoa(binary),base64Encoded:true,mime_type:r.headers.get('content-type')}})()`;
          const response = await cdp(tabId, "Runtime.evaluate", {
            expression,
            awaitPromise: true,
            returnByValue: true,
          });
          if (response.exceptionDetails)
            throw new BridgeError(
              "failed",
              "Could not read observed asset: " + item.url,
            );
          return { ...item, ...response.result.value };
        }),
      ),
    };
  }
  if (op === "cdp") {
    return { result: await cdp(tabId, p.method, p.params ?? {}) };
  }
  if (op === "clipboard") {
    const expression =
      p.action === "write"
        ? `navigator.clipboard.writeText(${JSON.stringify(p.text)}).then(()=>({ok:true}))`
        : "navigator.clipboard.readText().then(text=>({text}))";
    const result = await cdp(tabId, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails)
      throw new BridgeError("failed", "Clipboard unavailable on this origin");
    return result.result.value;
  }

  throw new BridgeError("invalid_argument", `Unknown operation ${op}`);
}

// A click that opens a modal dialog may leave its CDP command pending until the dialog
// is answered. Return the observed dialog so the client can answer it without deadlock.
export async function withDialog(tabId, work) {
  const s = state(tabId);
  let wake;
  const dialog = new Promise((resolve) => {
    wake = (p) => resolve({ dialog_open: true, dialog: p });
    s.dialogWaiters.add(wake);
  });
  try {
    return await Promise.race([work(), dialog]);
  } finally {
    s.dialogWaiters.delete(wake);
  }
}
