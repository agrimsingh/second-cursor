import { BridgeError } from "./errors.js";

const attached = new Set();
const frameSessions = new Map();
// Page lifecycle events ("load", "networkIdle", …) of each tab's top-level frames, for the document each loader
// committed. They come from the Page domain, so knowing whether a page is still fetching needs no Network.enable.
const lifecycles = new Map();
chrome.debugger.onEvent.addListener((source,method,p)=>{
  if(method === "Target.attachedToTarget") {
    frameSessions.set(`${source.tabId}:${p.targetInfo.targetId}`,p.sessionId);
    chrome.debugger.sendCommand({tabId:source.tabId,sessionId:p.sessionId},"Target.setAutoAttach",{autoAttach:true,waitForDebuggerOnStart:false,flatten:true}).catch(()=>{});
  }
  if(method === "Target.detachedFromTarget")for(const [k,v] of frameSessions)if(v===p.sessionId)frameSessions.delete(k);
  if (method === "Page.lifecycleEvent" && !source.sessionId) {
    const frames = lifecycles.get(source.tabId) ?? new Map();
    lifecycles.set(source.tabId, frames);
    if (frames.get(p.frameId)?.loaderId !== p.loaderId) frames.set(p.frameId, { loaderId: p.loaderId, names: new Set() });
    frames.get(p.frameId).names.add(p.name);
  }
});

chrome.debugger.onDetach.addListener(({ tabId }) => {
  attached.delete(tabId);
  lifecycles.delete(tabId);
  for (const key of enabled) if (key.startsWith(`${tabId}:`)) enabled.delete(key);
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

export async function frameCDP(tabId,frameId,method,params={}) {
  await attach(tabId);
  const sessionId=frameSessions.get(`${tabId}:${frameId}`);
  try{return await chrome.debugger.sendCommand({tabId,...(sessionId?{sessionId}:{})},method,params);}
  catch(e){throw new BridgeError("failed",`${method}: ${protocolMessage(e)}`);}
}
