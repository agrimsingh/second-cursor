import { cdp, frameCDP, frameSession } from "./cdp.js";
import { concealDuring, pointTo, show } from "./cursor.js";
import { BridgeError, withTimeout } from "./errors.js";
import { parseChord } from "./keys.js";
import { isolatedFrame } from "./snapshot.js";

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

/** Where the ref's frame's viewport starts in the tab's viewport. */
async function frameOffset(owners) {
  let x = 0, y = 0;
  for (const owner of owners) {
    const { model } = await owner.send("DOM.getBoxModel", { backendNodeId: owner.id });
    x += model.content[0];
    y += model.content[1];
  }
  return { x, y };
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

async function refCenter(tabId, ref) {
  const { id, send, owners } = resolve(tabId, ref);
  try {
    for (const owner of owners) await owner.send("DOM.scrollIntoViewIfNeeded", { backendNodeId: owner.id });
    await send("DOM.scrollIntoViewIfNeeded", { backendNodeId: id });
    const { model } = await send("DOM.getBoxModel", { backendNodeId: id });
    return shift(center(model.content), await frameOffset(owners));
  } catch (error) {
    const quads = await send("DOM.getContentQuads", { backendNodeId: id }).then((r) => r.quads, () => []);
    if (quads.length) return shift(center(quads[0]), await frameOffset(owners));
    throw refError(ref, error);
  }
}

export async function click(tabId, { ref, x, y }) {
  let point;
  if (ref != null && ref !== "") point = await refCenter(tabId, ref);
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

export async function type(tabId, { ref, text, submit }) {
  if (ref != null && ref !== "") {
    const { id, send, owners } = resolve(tabId, ref);
    await send("DOM.focus", { backendNodeId: id }).catch((error) => {
      throw refError(ref, error);
    });
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
