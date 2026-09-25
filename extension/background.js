import { BridgeError } from "./errors.js";
import { click, pressKey, screenshot, scroll, type } from "./input.js";
import { snapshot } from "./snapshot.js";
import { closeTab, describe, listTabs, navigateTab, openTab, requireAgentTab, settleAround } from "./tabs.js";

const HOST = "dev.secondcursor.host";
const VERSION = chrome.runtime.getManifest().version;
const MIN_RETRY_MS = 500;
// Retrying (an extension API call) more often than every 30s also keeps this worker alive.
const MAX_RETRY_MS = 15000;
const SUPERSEDED_RETRY_MS = 60000;
const HEALTHY_AFTER_MS = 10000;

const handlers = {
  "tabs.list": async () => ({ tabs: await listTabs() }),
  "tabs.open": async ({ url }) => ({ tab: await openTab(url) }),
  "tabs.close": async ({ tabId }) => {
    await closeTab(tabId);
    return {};
  },
  "tab.navigate": async ({ tabId, url }) => ({ tab: await navigateTab(tabId, url) }),
  "tab.snapshot": async ({ tabId }) => {
    await requireAgentTab(tabId);
    const outline = await snapshot(tabId);
    return { tab: describe(await chrome.tabs.get(tabId)), outline };
  },
  "tab.click": async ({ tabId, ref, x, y }) => {
    await requireAgentTab(tabId);
    await settleAround(tabId, () => click(tabId, { ref, x, y }));
    return {};
  },
  "tab.type": async ({ tabId, ref, text, submit }) => {
    await requireAgentTab(tabId);
    await settleAround(tabId, () => type(tabId, { ref, text, submit }), { quietMs: submit ? 300 : 0 });
    return {};
  },
  "tab.pressKey": async ({ tabId, keys }) => {
    await requireAgentTab(tabId);
    await settleAround(tabId, () => pressKey(tabId, keys));
    return {};
  },
  "tab.scroll": async ({ tabId, direction, amount }) => {
    await requireAgentTab(tabId);
    await scroll(tabId, direction, amount);
    return {};
  },
  "tab.screenshot": async ({ tabId }) => {
    await requireAgentTab(tabId);
    return await screenshot(tabId);
  },
};

let port = null;
let retryTimer = null;
let retryDelay = MIN_RETRY_MS;

async function dispatch(activePort, { id, method, params }) {
  let reply;
  try {
    const handler = handlers[method];
    if (!handler) throw new BridgeError("invalid_argument", `Unknown method ${method}`);
    reply = { id, result: await handler(params ?? {}) };
  } catch (error) {
    reply = { id, error: { code: error.code ?? "failed", message: error.message ?? String(error) } };
  }
  try {
    activePort.postMessage(reply);
  } catch {
    // The host went away mid-call; the app has already failed this request.
  }
}

function scheduleRetry() {
  const delay = retryDelay;
  retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, delay);
}

function connect() {
  if (port || retryTimer) return;
  let current;
  try {
    current = chrome.runtime.connectNative(HOST);
  } catch (error) {
    console.warn("Second Cursor: connectNative failed", error);
    scheduleRetry();
    return;
  }
  const connectedAt = Date.now();
  let superseded = false;
  port = current;

  current.onMessage.addListener((message) => {
    if (message?.type === "superseded") superseded = true;
    else if (message?.type === "error") console.warn("Second Cursor host:", message.message);
    else if (message?.id !== undefined && message.method) dispatch(current, message);
  });
  current.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError?.message;
    if (reason) console.info("Second Cursor: host disconnected:", reason);
    if (port === current) port = null;
    if (superseded) retryDelay = SUPERSEDED_RETRY_MS;
    else if (Date.now() - connectedAt > HEALTHY_AFTER_MS) retryDelay = MIN_RETRY_MS;
    scheduleRetry();
  });
  current.postMessage({ type: "hello", version: VERSION });
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
