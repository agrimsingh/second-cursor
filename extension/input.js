import { cdp, frameCDP, frameSession } from "./cdp.js";
import { concealDuring, pointTo, show } from "./cursor.js";
import { BridgeError, sleep, withTimeout } from "./errors.js";
import { parseChord } from "./keys.js";
import { isolatedFrame } from "./snapshot.js";
import { expectQuickReaction } from "./tabs.js";
import { timed } from "./timing.js";

// Long enough for a field that checks what was typed (or was left, on Tab) after a short debounce.
const QUICK_REACT_MS = 300;
// Moving focus or dismissing something starts no request worth waiting for. Arrow keys can switch a tab whose panel
// loads, and page keys can scroll in more results, so they keep the full wait.
const FOCUS_KEYS = new Set(["Tab", "Escape"]);

const gone = (ref) => new BridgeError("invalid_argument", `Ref ${ref} is no longer on the page; take a new browser_snapshot`);

/**
 * A ref's node: its backend id, the session of the process it lives in, and the chain of <iframe> elements from the
 * tab's frame down to its frame, each with the session it is read in.
 */
function resolve(tabId, ref) {
  const match = /^(?:f(\d+))?e?(\d+)$/.exec(String(ref ?? "").trim());
  if (!match) throw new BridgeError("invalid_argument", `Invalid ref ${JSON.stringify(ref)}; use an [e123] ref from browser_snapshot`);
  const sender = (frameId) => {
    if (!frameId) return (method, params) => cdp(tabId, method, params);
    const expectedSession = frameSession(tabId, frameId);
    if (!expectedSession) throw gone(ref);
    return (method, params) => frameCDP(tabId, frameId, method, params, { expectedSession });
  };
  const owners = [];
  let frameId = null;
  for (let ordinal = Number(match[1] ?? 0); ordinal; ) {
    const frame = isolatedFrame(tabId, ordinal);
    // Without its session, a frame's backend ids would be looked up in the tab's own process, where they name other nodes.
    if (!frame?.session || frame.session !== frameSession(tabId, frame.frameId)) throw gone(ref);
    frameId ??= frame.frameId;
    const parentFrameId = isolatedFrame(tabId, frame.parent)?.frameId;
    owners.unshift({ id: frame.owner, send: sender(parentFrameId), frameId: parentFrameId });
    ordinal = frame.parent;
  }
  return { id: Number(match[2]), send: sender(frameId), owners, frameId };
}

/** Where each frame of the ref's chain starts in the tab's viewport, from the tab's frame down to the ref's own. */
async function frameOrigins(owners) {
  const origins = [{ x: 0, y: 0 }];
  for (const owner of owners) {
    const { model } = await owner.send("DOM.getBoxModel", { backendNodeId: owner.id });
    const last = origins.at(-1);
    origins.push({ x: last.x + model.content[0], y: last.y + model.content[1] });
  }
  return origins;
}

const frameOffset = async (owners) => (await frameOrigins(owners)).at(-1);

// Called on the node a click must reach, with the node hit at the click point: null when the click reaches it (the
// hit is it, inside it, or its label), else what is in the way, as `role "text"` or `tag "text"`. Called on the hit
// node alone, it describes that node.
const COVERING = `function (hit) {
  const up = (n) => n.parentNode || n.host;
  let el = hit ?? this;
  el = el.nodeType === 1 ? el : el.parentElement;
  if (!el) return null;
  if (hit) {
    for (let n = hit; n; n = up(n)) if (n === this) return null;
    if (el.closest("#second-cursor-overlay") || el.closest("label")?.control === this) return null;
    const target = new Set();
    for (let n = this; n; n = up(n)) target.add(n);
    while (up(el)?.nodeType === 1 && !target.has(up(el))) el = up(el);
  }
  const text = (el.getAttribute("aria-label") || el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
  const what = el.getAttribute("role") || el.localName;
  if (text) return what + ' "' + (text.length > 60 ? text.slice(0, 60) + "…" : text) + '"';
  const box = el.getBoundingClientRect();
  if (box.width * box.height >= 0.5 * innerWidth * innerHeight)
    return "an empty " + what + " over the page, usually the backdrop of an open menu or dialog (press Escape or finish choosing in it)";
  return what + (el.id ? "#" + el.id : "");
}`;

/**
 * What covers a click point, or null. `levels` go from the tab's process down to the target's: in each, the node
 * the point must hit (an <iframe> of the next level, or the target), in the coordinates of that session's root frame.
 * Layers with pointer-events: none, the agent's cursor among them, let clicks through and are skipped.
 */
async function coveredBy(levels) {
  for (const { send, id, x, y } of levels) {
    const objectGroup = "second-cursor-hit";
    try {
      // It takes document coordinates, so a scrolled frame's scroll offset is added to the viewport point.
      const { cssLayoutViewport: view } = await send("Page.getLayoutMetrics");
      const hit = await send("DOM.getNodeForLocation", { x: Math.round(x + view.pageX), y: Math.round(y + view.pageY) });
      if (hit.backendNodeId === id) continue;
      const [target, node] = await Promise.all(
        [id, hit.backendNodeId].map((backendNodeId) => send("DOM.resolveNode", { backendNodeId, objectGroup })),
      );
      const call = (objectId, args) =>
        send("Runtime.callFunctionOn", { objectId, functionDeclaration: COVERING, arguments: args, returnByValue: true });
      // A hit in another frame than the target's lives in another JavaScript world, which only covering explains.
      const { result } = await call(target.object.objectId, [{ objectId: node.object.objectId }]).catch(() =>
        call(node.object.objectId, []),
      );
      if (result.value) return result.value;
    } catch (error) {
      // A failed hit test cannot establish that the point still belongs to the target. Sending anyway could hit a
      // replacement control; return a useful refusal and let the caller observe the new page.
      throw new BridgeError("failed", `Could not verify the target's hit area; input was not sent: ${error.message ?? error}`);
    } finally {
      send("Runtime.releaseObjectGroup", { objectGroup }).catch(() => {});
    }
  }
  return null;
}

const HIT_WAIT_MS = 2000;

/**
 * The point to click on a target, once nothing covers it there. `locate` scrolls the target into view and returns
 * { point, levels } (see coveredBy); it runs again on every try, since the target may move while a toast or an
 * animation clears. After HIT_WAIT_MS the click is refused, without dispatching, naming what covers the target.
 */
export async function reachablePoint(what, locate) {
  const began = Date.now();
  for (;;) {
    const { point, levels } = await locate();
    const cover = await coveredBy(levels);
    if (!cover) return point;
    if (Date.now() - began >= HIT_WAIT_MS)
      throw new BridgeError(
        "failed",
        `${what} is covered by ${cover}, so the click was not sent; wait for it to go away or close it, then click again`,
      );
    await sleep(100);
  }
}

function refError(ref, error) {
  if (/no node|could not find node|does not belong/i.test(error.message)) return gone(ref);
  if (/layout object|box model/i.test(error.message)) {
    return new BridgeError("failed", `Ref ${ref} is not visible on the page`);
  }
  return error;
}

const center = (quad) => ({ x: (quad[0] + quad[2] + quad[4] + quad[6]) / 4, y: (quad[1] + quad[3] + quad[5] + quad[7]) / 4 });
const shift = (point, offset) => ({ x: point.x + offset.x, y: point.y + offset.y });

/**
 * Throws unless the ref's node is in the page. The page a tab left can stay alive in the back/forward cache, where its
 * nodes still report boxes and take focus, so a click or typing would land on whatever the current page has there.
 * Such a node cannot be resolved into the page.
 */
async function requireLive(send, id, ref) {
  const objectGroup = "second-cursor-ref";
  await send("DOM.resolveNode", { backendNodeId: id, objectGroup }).then(
    () => send("Runtime.releaseObjectGroup", { objectGroup }).catch(() => {}),
    () => { throw gone(ref); },
  );
}

async function refTarget(tabId, ref) {
  const { id, send, owners } = resolve(tabId, ref);
  let local;
  await requireLive(send, id, ref);
  try {
    for (const owner of owners) await owner.send("DOM.scrollIntoViewIfNeeded", { backendNodeId: owner.id });
    await send("DOM.scrollIntoViewIfNeeded", { backendNodeId: id });
    local = center((await send("DOM.getBoxModel", { backendNodeId: id })).model.content);
  } catch (error) {
    const quads = await send("DOM.getContentQuads", { backendNodeId: id }).then((r) => r.quads, () => []);
    if (!quads.length) throw refError(ref, error);
    local = center(quads[0]);
  }
  const origins = await frameOrigins(owners);
  const point = shift(local, origins.at(-1));
  const levels = [...owners, { id, send }].map((level, i) => ({
    ...level, x: point.x - origins[i].x, y: point.y - origins[i].y,
  }));
  return { point, levels };
}

export async function click(tabId, { ref, x, y }, guardObservation, guardInput) {
  let point;
  if (ref != null && ref !== "")
    point = await timed(tabId, "locate", () => reachablePoint(`Ref ${ref}`, () => refTarget(tabId, ref)));
  else if (Number.isFinite(x) && Number.isFinite(y)) point = { x, y };
  else throw new BridgeError("invalid_argument", "click needs a ref or both x and y");

  await timed(tabId, "cursor", () => pointTo(tabId, point, 1));
  if (ref != null && ref !== "") point = await reachablePoint(`Ref ${ref}`, () => refTarget(tabId, ref));
  else await guardObservation?.();
  const mouse = async (params) => {
    if (params.type !== "mouseReleased") await guardInput?.();
    return cdp(tabId, "Input.dispatchMouseEvent", { x: point.x, y: point.y, modifiers: 0, ...params });
  };
  await timed(tabId, "input", async () => {
    await mouse({ type: "mouseMoved", button: "none", buttons: 0 });
    // Hover handlers can open menus or move the target before mouseDown. Re-hit-test the live ref at that point.
    if (ref != null && ref !== "") point = await reachablePoint(`Ref ${ref}`, () => refTarget(tabId, ref));
    else await guardObservation?.();
    await mouse({ type: "mousePressed", button: "left", buttons: 1, clickCount: 1 });
    await mouse({ type: "mouseReleased", button: "left", buttons: 0, clickCount: 1 });
  });
}

// Where a text cursor would sit in a field: near its left edge, vertically centered.
function fieldPoint(quad) {
  const xs = [quad[0], quad[2], quad[4], quad[6]], ys = [quad[1], quad[3], quad[5], quad[7]];
  const left = Math.min(...xs), width = Math.max(...xs) - left;
  return { x: left + Math.min(24, width / 2), y: (Math.min(...ys) + Math.max(...ys)) / 2 };
}

/**
 * Whether typing into the field may bring up suggestions, which arrive after a debounce and a request. Fields that show
 * their own say so (a combobox or search role, aria-autocomplete, a datalist, or autocomplete="off" to keep the
 * browser's list out of the way); a name or email field does not, and neither does anything that is not a plain field.
 */
async function suggests(send, id) {
  const { node } = await send("DOM.describeNode", { backendNodeId: id });
  if (!["input", "textarea"].includes(node.localName)) return true;
  const attributes = {};
  for (let i = 0; i + 1 < (node.attributes?.length ?? 0); i += 2) attributes[node.attributes[i]] = node.attributes[i + 1];
  return ["combobox", "searchbox"].includes(attributes.role) || attributes.type === "search"
    || attributes.autocomplete === "off" || ["aria-autocomplete", "aria-controls", "aria-owns", "list"].some((a) => a in attributes);
}

// These programs only inspect DOM state. Text always goes through Input.insertText, never a page value setter.
// A successful DOM.focus is not evidence of focus: Chrome can leave the previous field active for a <select>,
// a disabled field, or a page focus handler that immediately redirects it.
const TYPING_STATE = `function (checkFocus, frameBoundaryKnown, ownerOnly) {
  const parent = (node) => node.parentElement || node.getRootNode()?.host;
  if (!this.isConnected) return "the target is no longer on the page";
  const blocked = (element) => {
    for (let node = element; node; node = parent(node)) {
      if (node.inert || node.getAttribute("aria-disabled") === "true") return "the target is disabled or inert";
      if (node.getAttribute("aria-readonly") === "true") return "the target is read-only";
    }
    if (element.disabled || element.matches(":disabled")) return "the target is disabled";
    if (element.readOnly) return "the target is read-only";
    return null;
  };
  const initialBlock = blocked(this);
  if (initialBlock) return initialBlock;
  if (!ownerOnly) {
    const tag = this.localName;
    if (tag === "select") return "the target is a dropdown; use browser_command operation=select_option";
    const textInput = tag === "input" && ["text", "search", "email", "url", "tel", "password", "number"].includes(this.type);
    if (!textInput && tag !== "textarea" && !this.isContentEditable)
      return "the target is not an editable text field; use a ref for its editable input";
  }
  if (!checkFocus) return null;
  let node = this;
  for (;;) {
    const ownerBlock = node !== this && blocked(node);
    if (ownerBlock) return ownerBlock;
    let root = node.getRootNode();
    const active = root.activeElement;
    // Even an editable descendant may be a separate editor; focus redirection is not ownership of this ref.
    if (active !== node) return "focus moved to a different element";
    if (node.shadowRoot?.activeElement) return "focus moved inside a different shadow editor; use its editable ref";
    while (root.host) {
      node = root.host;
      root = node.getRootNode();
      if (root.activeElement !== node) return "focus moved outside the target's shadow root";
    }
    const view = root.defaultView;
    if (!view) return "the target's document is unavailable";
    if (view === view.top) return null;
    let frame;
    try { frame = view.frameElement; } catch (_) {}
    // Cross-origin/process boundaries are checked separately against the iframe owner refs in their sessions.
    if (!frame) return frameBoundaryKnown ? { boundary: true } : "the parent frame's focus cannot be verified; use a fresh browser_snapshot ref";
    node = frame;
  }
}`;

const FOCUSED_TYPING_TARGET = `(() => {
  let node = document.activeElement;
  for (;;) {
    if (node?.shadowRoot?.activeElement) { node = node.shadowRoot.activeElement; continue; }
    if (["iframe", "frame"].includes(node?.localName)) {
      try {
        if (!node.contentDocument) return null;
        node = node.contentDocument.activeElement;
        continue;
      } catch (_) { return null; }
    }
    return node;
  }
})()`;

async function checkTypingTarget(send, objectId, { focus = false, frameId, owner = false, objectGroup } = {}) {
  if (focus && !owner) {
    const { node } = await send("DOM.describeNode", { objectId });
    // Closed author roots hide their activeElement from page inspection. Native inputs' user-agent roots are
    // different: the input itself owns the editing contract. A ref inside a closed root remains inspectable.
    if (node?.shadowRoots?.some(root => root.shadowRootType === "closed"))
      throw new BridgeError("failed", "Typing was not sent: a closed shadow host cannot prove editable focus; use the editable ref inside it");
  }
  const response = await send("Runtime.callFunctionOn", {
    objectId, functionDeclaration: TYPING_STATE,
    arguments: [focus, !!frameId, owner].map(value => ({ value })), returnByValue: true,
  });
  if (!response.exceptionDetails && response.result?.value?.boundary) {
    // A ref can also be in a same-process child of an isolated frame. Only accept the opaque boundary whose
    // owner was verified; an additional cross-origin ancestor must never inherit that proof accidentally.
    const { result, exceptionDetails } = await send("Runtime.callFunctionOn", {
      objectId, objectGroup, functionDeclaration: `function () {
        let doc = this.ownerDocument;
        while (doc.defaultView !== doc.defaultView.top) {
          let frame;
          try { frame = doc.defaultView.frameElement; } catch (_) {}
          if (!frame) break;
          doc = frame.ownerDocument;
        }
        return doc.documentElement;
      }`,
    });
    const description = !exceptionDetails && result?.objectId
      ? await send("DOM.describeNode", { objectId: result.objectId }) : null;
    if (description?.node?.frameId === frameId) return;
    throw new BridgeError("failed", "Typing was not sent: an unverified parent frame owns focus; take a new browser_snapshot");
  }
  if (response.exceptionDetails || response.result?.value !== null)
    throw new BridgeError("failed", `Typing was not sent: ${response.result?.value || "the target's editable focus could not be verified"}`);
}

let typingOperation = 0;

export async function type(tabId, { ref, text, submit }, guardInput) {
  const objectGroup = `second-cursor-typing-${++typingOperation}`;
  const releases = new Set();
  try {
    const hasRef = ref != null && ref !== "";
    const target = hasRef ? resolve(tabId, ref) : { send: (method, params) => cdp(tabId, method, params), owners: [] };
    const { id, send, owners, frameId } = target;
    releases.add(send);
    const response = hasRef
      ? await send("DOM.resolveNode", { backendNodeId: id, objectGroup }).catch(error => { throw refError(ref, error); })
      : await send("Runtime.evaluate", { expression: FOCUSED_TYPING_TARGET, objectGroup });
    const objectId = (response.object ?? response.result)?.objectId;
    if (!objectId || response.exceptionDetails)
      throw new BridgeError("failed", "Typing was not sent: no editable focus could be identified; use a fresh browser_snapshot ref");
    await checkTypingTarget(send, objectId);
    if (hasRef) {
      await guardInput?.();
      await send("DOM.focus", { backendNodeId: id }).catch(error => { throw refError(ref, error); });
      if (text && !submit && !(await suggests(send, id).catch(() => true))) expectQuickReaction(tabId, QUICK_REACT_MS);
      pointTo(tabId, async () => {
        const { model } = await send("DOM.getBoxModel", { backendNodeId: id });
        return shift(fieldPoint(model.border), await frameOffset(owners));
      });
    } else show(tabId);
    const verifyFocus = async () => {
      for (let index = 0; index < owners.length; index++) {
        const owner = owners[index];
        releases.add(owner.send);
        const { object } = await owner.send("DOM.resolveNode", { backendNodeId: owner.id, objectGroup });
        await checkTypingTarget(owner.send, object.objectId, { focus: true, frameId: owner.frameId, owner: true, objectGroup });
      }
      await checkTypingTarget(send, objectId, { focus: true, frameId, objectGroup });
    };
    // Do all cursor/metadata work first, then recheck the live target immediately before each input dispatch.
    await verifyFocus();
    if (text) {
      await guardInput?.();
      await cdp(tabId, "Input.insertText", { text: String(text) });
    }
    if (submit) {
      if (text) await verifyFocus().catch(error => {
        throw new BridgeError("failed", `Text was inserted, but Enter was not sent: ${String(error.message ?? error).replace(/^Typing was not sent: /, "")}`);
      });
      await pressKey(tabId, "Enter", guardInput);
    }
  } finally {
    await Promise.all([...releases].map(send => send("Runtime.releaseObjectGroup", { objectGroup }).catch(() => {})));
  }
}

/** `approvals` are the call's local approvals; only the user's clipboard approval lets a key reach the clipboard. */
export async function pressKey(tabId, keys, guardInput, approvals = []) {
  const { modifiers, key, command, clipboard } = parseChord(keys);
  if (clipboard && !approvals?.includes("clipboard"))
    throw new BridgeError(
      "not_allowed",
      clipboard === "read"
        ? `${keys} pastes from the system clipboard, which needs the user's approval, so no key was sent; read it with browser_command operation=clipboard action=read (the user is asked), then type the text`
        : `${keys} writes the system clipboard, which needs the user's approval, so no key was sent; set it with browser_command operation=clipboard action=write and the text (the user is asked)`,
    );
  show(tabId);
  if (FOCUS_KEYS.has(key.key) && modifiers.every((m) => m.key === "Shift")) expectQuickReaction(tabId, QUICK_REACT_MS);
  const send = async (params) => {
    if (params.type !== "keyUp") await guardInput?.();
    return cdp(tabId, "Input.dispatchKeyEvent", params);
  };
  let bits = 0;
  const held = [];
  let mainDown = false;
  let inputError;
  try {
    for (const m of modifiers) {
      bits |= m.bit;
      await send({ type: "rawKeyDown", modifiers: bits, key: m.key, code: m.code, windowsVirtualKeyCode: m.keyCode });
      held.push(m);
    }
    const base = { modifiers: bits, key: key.key, code: key.code, windowsVirtualKeyCode: key.keyCode };
    await send({ ...base, type: "rawKeyDown", ...(command ? { commands: [command] } : {}) });
    mainDown = true;
    if (key.text && !(bits & (2 | 4)))
      await send({ ...base, type: "char", text: key.text, unmodifiedText: key.text });
  } catch (error) {
    inputError = error;
    throw error;
  } finally {
    // Human takeover between keyDown and char must not leave a modifier held for the person's next keystroke.
    let releaseError;
    if (mainDown) await send({ type: "keyUp", modifiers: bits, key: key.key, code: key.code,
      windowsVirtualKeyCode: key.keyCode }).catch(error => { releaseError = error; });
    for (const m of held.reverse()) {
      bits &= ~m.bit;
      await send({ type: "keyUp", modifiers: bits, key: m.key, code: m.code, windowsVirtualKeyCode: m.keyCode })
        .catch(error => { releaseError = error; });
    }
    if (releaseError && !inputError) throw releaseError;
  }
}

export async function scroll(tabId, direction, amount, guardInput) {
  const distance = 100 * Math.min(Math.max(Number(amount) || 3, 1), 50);
  const delta = { up: [0, -distance], down: [0, distance], left: [-distance, 0], right: [distance, 0] }[direction];
  if (!delta) throw new BridgeError("invalid_argument", "direction must be up, down, left or right");
  const { cssLayoutViewport: viewport } = await cdp(tabId, "Page.getLayoutMetrics");
  const point = { x: viewport.clientWidth / 2, y: viewport.clientHeight / 2 };
  await pointTo(tabId, point);
  await guardInput?.();
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseWheel", ...point, deltaX: delta[0], deltaY: delta[1] });
}

function pngSize(base64) {
  const bytes = atob(base64.slice(0, 32));
  const u32 = (o) => ((bytes.charCodeAt(o) << 24) | (bytes.charCodeAt(o + 1) << 16) | (bytes.charCodeAt(o + 2) << 8) | bytes.charCodeAt(o + 3)) >>> 0;
  return { width: u32(16), height: u32(20) };
}

// Captured at 1 image pixel per CSS pixel so screenshot coordinates are directly clickable.
export async function screenshot(tabId, guard) {
  await guard?.();
  const { cssVisualViewport: vp } = await cdp(tabId, "Page.getLayoutMetrics");
  await guard?.();
  const { result } = await cdp(tabId, "Runtime.evaluate", { expression: "window.devicePixelRatio", returnByValue: true });
  const dpr = Number(result?.value) || 1;
  await guard?.();
  const { data } = await concealDuring(tabId, async () => {
    await guard?.();
    return withTimeout(
      cdp(tabId, "Page.captureScreenshot", {
        format: "png",
        clip: { x: vp.pageX, y: vp.pageY, width: vp.clientWidth, height: vp.clientHeight, scale: 1 / dpr },
      }),
      15000,
      "Screenshot timed out; Chrome is not rendering this tab",
    );
  });
  const { width, height } = pngSize(data);
  return { data, width, height, scale: 1 };
}
