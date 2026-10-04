import { BridgeError } from "./errors.js";
import { requireAgentInput, requireAgentTab } from "./tabs.js";

const attached = new Set();
const MUTATING_COMMANDS = new Set([
  "Page.navigate", "Page.navigateToHistoryEntry", "Page.reload", "Page.handleJavaScriptDialog",
  "DOM.focus", "DOM.scrollIntoViewIfNeeded", "DOM.setFileInputFiles",
  "Emulation.setDeviceMetricsOverride", "Emulation.clearDeviceMetricsOverride",
]);
const frameSessions = new Map();
// A missing session for a frame known to be isolated must not turn its backend node IDs into main-renderer IDs.
// Keep these tombstones across debugger detachment; tab closure is the point at which old refs cannot return.
const isolatedFrameIds = new Set();
// Page lifecycle events ("load", "networkIdle", …) of each tab's top-level frames, for the document each loader
// committed. They come from the Page domain, so knowing whether a page is still fetching needs no Network.enable.
const lifecycles = new Map();
chrome.debugger.onEvent.addListener((source,method,p)=>{
  if(method === "Target.attachedToTarget") {
    const key = `${source.tabId}:${p.targetInfo.targetId}`;
    isolatedFrameIds.add(key);
    frameSessions.set(key,p.sessionId);
    chrome.debugger.sendCommand({tabId:source.tabId,sessionId:p.sessionId},"Target.setAutoAttach",{autoAttach:true,waitForDebuggerOnStart:false,flatten:true}).catch(()=>{});
  }
  if(method === "Target.detachedFromTarget")for(const [k,v] of frameSessions)if(k.startsWith(`${source.tabId}:`)&&v===p.sessionId)frameSessions.delete(k);
  if (method === "Page.lifecycleEvent" && !source.sessionId) {
    const frames = lifecycles.get(source.tabId) ?? new Map();
    lifecycles.set(source.tabId, frames);
    if (frames.get(p.frameId)?.loaderId !== p.loaderId) frames.set(p.frameId, { loaderId: p.loaderId, names: new Set() });
    frames.get(p.frameId).names.add(p.name);
  }
});

function detachedTab(tabId) {
  attached.delete(tabId);
  lifecycles.delete(tabId);
  for (const key of enabled) if (key.startsWith(`${tabId}:`)) enabled.delete(key);
  for (const key of frameSessions.keys()) if (key.startsWith(`${tabId}:`)) frameSessions.delete(key);
}

chrome.debugger.onDetach.addListener(({ tabId }) => detachedTab(tabId));
chrome.tabs.onRemoved.addListener(tabId => {
  detachedTab(tabId);
  for (const key of isolatedFrameIds) if (key.startsWith(`${tabId}:`)) isolatedFrameIds.delete(key);
});

/** True once the tab's current document has gone 500 ms without a network request in flight. */
export async function networkIdle(tabId) {
  const { frameTree } = await cdp(tabId, "Page.getFrameTree");
  const lifecycle = lifecycles.get(tabId)?.get(frameTree.frame.id);
  return lifecycle?.loaderId === frameTree.frame.loaderId && lifecycle.names.has("networkIdle");
}

// Runtime.enable is the best-known sign of an automated page to bot checks, and Network.enable reports every
// request; like Codex, they are switched on only for the operations that read their events.
const enabled = new Set();
export async function ensureDomain(tabId, domain) {
  const key = `${tabId}:${domain}`;
  if (enabled.has(key)) return;
  await cdp(tabId, `${domain}.enable`, {});
  enabled.add(key);
}

export async function attach(tabId) {
  if (attached.has(tabId)) return;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (e) {
    // A previous service worker instance may have attached; the session outlives it.
    if (!/already attached/i.test(e.message)) {
      throw new BridgeError("failed", `Could not attach to tab ${tabId}: ${e.message}`);
    }
  }
  attached.add(tabId);
  await chrome.debugger.sendCommand({ tabId }, "Page.enable", {}).catch(() => {});
  // Enabling also reports the lifecycle events the current document has already reached.
  await chrome.debugger.sendCommand({ tabId }, "Page.setLifecycleEventsEnabled", { enabled: true }).catch(() => {});
  const target = { tabId };
  await chrome.debugger.sendCommand(target,"Page.setInterceptFileChooserDialog",{enabled:true}).catch(()=>{});
  await chrome.debugger.sendCommand(target,"Target.setAutoAttach",{autoAttach:true,waitForDebuggerOnStart:false,flatten:true}).catch(()=>{});
  // Background tabs report blur and may be frozen; make them behave like the focused tab.
  await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true }).catch(() => {});
  await chrome.debugger.sendCommand(target, "Page.setWebLifecycleState", { state: "active" }).catch(() => {});
}

export async function cdp(tabId, method, params = {}) {
  // Button/key release must always run after takeover, otherwise a held drag/modifier could reach human input.
  const releasing = params.type === "mouseReleased" || params.type === "keyUp";
  if (method.startsWith("Input.")) await (releasing ? requireAgentTab(tabId) : requireAgentInput(tabId));
  else if (MUTATING_COMMANDS.has(method)) await requireAgentInput(tabId);
  await attach(tabId);
  try {
    return await chrome.debugger.sendCommand({ tabId }, method, params);
  } catch (e) {
    if (/not attached/i.test(e.message)) {
      attached.delete(tabId);
      await attach(tabId);
      return await chrome.debugger.sendCommand({ tabId }, method, params);
    }
    throw new BridgeError("failed", `${method}: ${protocolMessage(e)}`);
  }
}

// chrome.debugger reports protocol errors as a JSON string of {code, message}.
export function protocolMessage(error) {
  try {
    return JSON.parse(error.message).message ?? error.message;
  } catch {
    return error.message;
  }
}

/** The session of a frame Chrome isolated in its own process; undefined for frames in the tab's process. */
export const frameSession = (tabId, frameId) => frameSessions.get(`${tabId}:${frameId}`);

/** Root/same-process frame IDs deliberately use the tab session. Callers retaining isolated-frame backend IDs
 * can pin expectedSession so a replacement renderer cannot reinterpret those IDs between awaited commands. */
export async function frameCDP(tabId,frameId,method,params={}, { expectedSession } = {}) {
  const key = `${tabId}:${frameId}`;
  const initialSession = frameSessions.get(key);
  // A new frame context may establish that a former isolated frame now lives in the tab renderer. This command
  // names the frame explicitly and cannot send stale backend-node IDs or input to the main document.
  const proveLocalFrame = !expectedSession && !initialSession && isolatedFrameIds.has(key)
    && method === "Page.createIsolatedWorld" && params.frameId === frameId;
  const unavailable = () => new BridgeError("failed", "The isolated browser frame changed or detached; take a new browser_snapshot before acting again");
  if ((isolatedFrameIds.has(key) && !initialSession && !proveLocalFrame) || (expectedSession && initialSession !== expectedSession))
    throw unavailable();
  const releasing = params.type === "mouseReleased" || params.type === "keyUp";
  if (method.startsWith("Input.")) await (releasing ? requireAgentTab(tabId) : requireAgentInput(tabId));
  else if (MUTATING_COMMANDS.has(method)) await requireAgentInput(tabId);
  await attach(tabId);
  const sessionId=frameSessions.get(key);
  if (sessionId !== initialSession || (isolatedFrameIds.has(key) && !sessionId && !proveLocalFrame)) throw unavailable();
  try {
    const result = await chrome.debugger.sendCommand({tabId,...(sessionId?{sessionId}:{})},method,params);
    if (proveLocalFrame) {
      if (!attached.has(tabId) || frameSessions.get(key) || !Number.isInteger(result?.executionContextId)) throw unavailable();
      isolatedFrameIds.delete(key);
    }
    return result;
  }
  catch(e){throw new BridgeError("failed",`${method}: ${protocolMessage(e)}`);}
}
