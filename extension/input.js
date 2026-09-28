import { cdp, frameCDP, frameSession } from "./cdp.js";
import { concealDuring, pointTo, show } from "./cursor.js";
import { BridgeError, sleep, withTimeout } from "./errors.js";
import { parseChord } from "./keys.js";
import { isolatedFrame } from "./snapshot.js";
import { expectQuickReaction } from "./tabs.js";

// Long enough for a field that checks what was typed after a short debounce.
const PLAIN_FIELD_REACT_MS = 300;

const gone = (ref) => new BridgeError("invalid_argument", `Ref ${ref} is no longer on the page; take a new browser_snapshot`);

/**
 * A ref's node: its backend id, the session of the process it lives in, and the chain of <iframe> elements from the
 * tab's frame down to its frame, each with the session it is read in.
 */
function resolve(tabId, ref) {
  const match = /^(?:f(\d+))?e?(\d+)$/.exec(String(ref ?? "").trim());
  if (!match) throw new BridgeError("invalid_argument", `Invalid ref ${JSON.stringify(ref)}; use an [e123] ref from browser_snapshot`);
  const sender = (frameId) => (frameId ? (method, params) => frameCDP(tabId, frameId, method, params) : (method, params) => cdp(tabId, method, params));
  const owners = [];
  let frameId = null;
  for (let ordinal = Number(match[1] ?? 0); ordinal; ) {
    const frame = isolatedFrame(tabId, ordinal);
    // Without its session, a frame's backend ids would be looked up in the tab's own process, where they name other nodes.
    if (!frame || !frameSession(tabId, frame.frameId)) throw gone(ref);
    frameId ??= frame.frameId;
    owners.unshift({ id: frame.owner, send: sender(isolatedFrame(tabId, frame.parent)?.frameId) });
    ordinal = frame.parent;
  }
  return { id: Number(match[2]), send: sender(frameId), owners };
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
  return text ? what + ' "' + (text.length > 60 ? text.slice(0, 60) + "…" : text) + '"' : what;
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
    } catch {
      // Nothing at the point, or a node gone mid-check: the click goes ahead as it always did.
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

async function refTarget(tabId, ref) {
  const { id, send, owners } = resolve(tabId, ref);
  let local;
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

export async function click(tabId, { ref, x, y }) {
  let point;
  if (ref != null && ref !== "") point = await reachablePoint(`Ref ${ref}`, () => refTarget(tabId, ref));
  else if (Number.isFinite(x) && Number.isFinite(y)) point = { x, y };
  else throw new BridgeError("invalid_argument", "click needs a ref or both x and y");

  await pointTo(tabId, point, 1);
  const mouse = (params) => cdp(tabId, "Input.dispatchMouseEvent", { x: point.x, y: point.y, modifiers: 0, ...params });
  await mouse({ type: "mouseMoved", button: "none", buttons: 0 });
  await mouse({ type: "mousePressed", button: "left", buttons: 1, clickCount: 1 });
  await mouse({ type: "mouseReleased", button: "left", buttons: 0, clickCount: 1 });
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

export async function type(tabId, { ref, text, submit }) {
  if (ref != null && ref !== "") {
    const { id, send, owners } = resolve(tabId, ref);
    await send("DOM.focus", { backendNodeId: id }).catch((error) => {
      throw refError(ref, error);
    });
    if (text && !submit && !(await suggests(send, id).catch(() => true))) expectQuickReaction(tabId, PLAIN_FIELD_REACT_MS);
    pointTo(tabId, async () => {
      const { model } = await send("DOM.getBoxModel", { backendNodeId: id });
      return shift(fieldPoint(model.border), await frameOffset(owners));
    });
  } else show(tabId);
  if (text) await cdp(tabId, "Input.insertText", { text: String(text) });
  if (submit) await pressKey(tabId, "Enter");
}

export async function pressKey(tabId, keys) {
  const { modifiers, key, command } = parseChord(keys);
  show(tabId);
  const send = (params) => cdp(tabId, "Input.dispatchKeyEvent", params);
  let bits = 0;
  for (const m of modifiers) {
    bits |= m.bit;
    await send({ type: "rawKeyDown", modifiers: bits, key: m.key, code: m.code, windowsVirtualKeyCode: m.keyCode });
  }
  const base = { modifiers: bits, key: key.key, code: key.code, windowsVirtualKeyCode: key.keyCode };
  await send({ ...base, type: "rawKeyDown", ...(command ? { commands: [command] } : {}) });
  if (key.text && !(bits & (2 | 4))) {
    await send({ ...base, type: "char", text: key.text, unmodifiedText: key.text });
  }
  await send({ ...base, type: "keyUp" });
  for (const m of [...modifiers].reverse()) {
    bits &= ~m.bit;
    await send({ type: "keyUp", modifiers: bits, key: m.key, code: m.code, windowsVirtualKeyCode: m.keyCode });
  }
}

export async function scroll(tabId, direction, amount) {
  const distance = 100 * Math.min(Math.max(Number(amount) || 3, 1), 50);
  const delta = { up: [0, -distance], down: [0, distance], left: [-distance, 0], right: [distance, 0] }[direction];
  if (!delta) throw new BridgeError("invalid_argument", "direction must be up, down, left or right");
  const { cssLayoutViewport: viewport } = await cdp(tabId, "Page.getLayoutMetrics");
  const point = { x: viewport.clientWidth / 2, y: viewport.clientHeight / 2 };
  await pointTo(tabId, point);
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseWheel", ...point, deltaX: delta[0], deltaY: delta[1] });
}

function pngSize(base64) {
  const bytes = atob(base64.slice(0, 32));
  const u32 = (o) => ((bytes.charCodeAt(o) << 24) | (bytes.charCodeAt(o + 1) << 16) | (bytes.charCodeAt(o + 2) << 8) | bytes.charCodeAt(o + 3)) >>> 0;
  return { width: u32(16), height: u32(20) };
}

// Captured at 1 image pixel per CSS pixel so screenshot coordinates are directly clickable.
export async function screenshot(tabId) {
  const { cssVisualViewport: vp } = await cdp(tabId, "Page.getLayoutMetrics");
  const { result } = await cdp(tabId, "Runtime.evaluate", { expression: "window.devicePixelRatio", returnByValue: true });
  const dpr = Number(result?.value) || 1;
  const { data } = await concealDuring(tabId, () =>
    withTimeout(
      cdp(tabId, "Page.captureScreenshot", {
        format: "png",
        clip: { x: vp.pageX, y: vp.pageY, width: vp.clientWidth, height: vp.clientHeight, scale: 1 / dpr },
      }),
      15000,
      "Screenshot timed out; Chrome is not rendering this tab",
    ),
  );
  const { width, height } = pngSize(data);
  return { data, width, height, scale: 1 };
}
