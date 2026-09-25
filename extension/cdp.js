import { BridgeError } from "./errors.js";

const attached = new Set();

chrome.debugger.onDetach.addListener(({ tabId }) => attached.delete(tabId));

async function attach(tabId) {
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
  const target = { tabId };
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
