import { cdp, frameCDP } from "./cdp.js";
import { BridgeError } from "./errors.js";

// Separate from settling: even an animation's attribute change can invalidate screenshot coordinates. Values stay
// inside the isolated world; the host receives only an opaque revision, never another copy of a private field.
const contexts = new Map();
const PROBE = `(() => {
  let s = globalThis.__secondCursorFreshness;
  if (!s || s.doc !== document) {
    s = { doc: document, epoch: String(Date.now()) + "-" + Math.random(), revision: 0, fields: null,
      active: document.activeElement, roots: new WeakSet() };
    const overlay = n => n?.nodeType === 1 && (n.id === "second-cursor-overlay" || n.closest?.("#second-cursor-overlay"));
    s.observer = new MutationObserver(records => {
      if (records.some(r => !overlay(r.target) && !(r.type === "childList" &&
          [...r.addedNodes, ...r.removedNodes].length && [...r.addedNodes, ...r.removedNodes].every(overlay))))
        s.revision++;
    });
    for (const event of ["input", "change", "scroll", "resize"])
      globalThis.addEventListener(event, () => s.revision++, true);
    globalThis.__secondCursorFreshness = s;
  }
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    const root = roots[i];
    if (!s.roots.has(root)) {
      s.roots.add(root);
      s.revision++;
      s.observer.observe(root, { subtree: true, childList: true, characterData: true, attributes: true });
    }
    for (const e of root.querySelectorAll("*"))
      if (e.shadowRoot && e.id !== "second-cursor-overlay") roots.push(e.shadowRoot);
  }
  const fields = JSON.stringify(roots.flatMap(root => [...root.querySelectorAll("input,textarea,select")]
    .map(e => [e.value, e.checked, e.selectedIndex])));
  if (s.fields !== null && fields !== s.fields) s.revision++;
  s.fields = fields;
  let active = document.activeElement;
  while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
  if (s.active !== active) { s.active = active; s.revision++; }
  const v = globalThis.visualViewport;
  return [s.epoch, s.revision, scrollX, scrollY, innerWidth, innerHeight,
    v?.offsetLeft, v?.offsetTop, v?.scale].join(":");
})()`;

chrome.tabs.onRemoved.addListener(tabId => {
  for (const key of contexts.keys()) if (key.startsWith(tabId + ":")) contexts.delete(key);
});
chrome.debugger.onDetach.addListener(({ tabId }) => {
  for (const key of contexts.keys()) if (key.startsWith(tabId + ":")) contexts.delete(key);
});

async function frameRevision(tabId, frameId) {
  const key = `${tabId}:${frameId}`;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      let contextId = contexts.get(key);
      if (contextId == null) {
        ({ executionContextId: contextId } = await frameCDP(tabId, frameId, "Page.createIsolatedWorld", {
          frameId, worldName: "SecondCursorFreshness",
        }));
        contexts.set(key, contextId);
      }
      const { result, exceptionDetails } = await frameCDP(tabId, frameId, "Runtime.evaluate", {
        expression: PROBE, contextId, returnByValue: true,
      });
      if (!exceptionDetails && typeof result?.value === "string") return result.value;
    } catch {}
    contexts.delete(key);
  }
  throw new BridgeError("failed", "Could not establish browser observation freshness; read the page again");
}

/** Covers the DOM, form properties and viewport of every document Chrome exposes, including isolated frames.
 * Canvas/video paint and render-only layout are not DOM revisions; live targets still need actionability checks.
 */
export async function browserRevision(tabId) {
  const { frameTree } = await cdp(tabId, "Page.getFrameTree");
  const frames = [];
  const walk = tree => { frames.push(tree.frame.id); for (const child of tree.childFrames ?? []) walk(child); };
  walk(frameTree);
  const revisions = await Promise.all(frames.sort().map(async id => `${id}:${await frameRevision(tabId, id)}`));
  return revisions.join("|");
}
