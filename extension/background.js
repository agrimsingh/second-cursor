import { hideSession, setCaption, show } from "./cursor.js";
import { BridgeError } from "./errors.js";
import { click, pressKey, screenshot, scroll, type } from "./input.js";
import { snapshot, structuredSnapshot } from "./snapshot.js";
import {
  closeTab,
  describe,
  listTabs,
  navigateTab,
  openTab,
  requireAgentTab,
  settleAround,
} from "./tabs.js";

import { extended, recordEvent, withDialog } from "./extended.js";
import { closeSession, endTurn } from "./tabs.js";

const HOST = "dev.secondcursor.host";
const VERSION = chrome.runtime.getManifest().version;
const MIN_RETRY_MS = 500;
// Retrying (an extension API call) more often than every 30s also keeps this worker alive.
const MAX_RETRY_MS = 15000;
const SUPERSEDED_RETRY_MS = 60000;
const HEALTHY_AFTER_MS = 10000;

const observations = new Map();
chrome.tabs.onRemoved.addListener((id) => {
  for (const key of observations.keys())
    if (key.endsWith(":" + id)) observations.delete(key);
});
const handlers = {
  extended: extended,
  // Sent just before the action it describes, which must not wait for the overlay.
  "cursor.caption": ({ tabId, caption, session_id }) => {
    setCaption(tabId, caption, session_id);
    return {};
  },
  "session.close": async ({ session_id }) => {
    for (const key of observations.keys())
      if (key.startsWith(session_id + ":")) observations.delete(key);
    await hideSession(session_id);
    return closeSession(session_id);
  },
  // Only for the app's local approval prompt text; tab ids never come from here to the model.
  "tab.info": async ({ tabId }) => {
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url || tab.pendingUrl || "", title: tab.title || "" };
  },
  "session.end_turn": async ({ session_id }) => {
    for (const key of observations.keys())
      if (key.startsWith(session_id + ":")) observations.delete(key);
    await hideSession(session_id);
    return endTurn(session_id);
  },
  "tabs.list": async ({ session_id }) => ({ tabs: await listTabs(session_id) }),
  "tabs.open": async ({ url, session_id }) => {
    const tab = await openTab(url, session_id);
    return {
      tab,
      observation: await handlers["tab.snapshot"]({
        tabId: tab.id,
        session_id,
        full: true,
      }),
    };
  },
  "tabs.close": ({ tabId }) => closeTab(tabId),
  "tab.navigate": async ({ tabId, url }) => {
    const tab = await navigateTab(tabId, url);
    show(tabId);
    return { tab };
  },
  "tab.snapshot": async ({ tabId, session_id, full }) => {
    await requireAgentTab(tabId);
    const key = `${session_id}:${tabId}`;
    const previous = observations.get(key);
    const snapshot = await structuredSnapshot(tabId);
    const id = crypto.randomUUID();
    observations.set(key, { id, outline: snapshot.outline });
    let outline = snapshot.outline;
    let is_diff = false;
    if (full === false && previous) {
      const old = previous.outline.split("\n");
      const now = outline.split("\n");
      // A positional splice preserves ordering and repeated rows. Large edits return the full tree.
      let start = 0;
      while (
        start < old.length &&
        start < now.length &&
        old[start] === now[start]
      )
        start++;
      let oldEnd = old.length,
        newEnd = now.length;
      while (
        oldEnd > start &&
        newEnd > start &&
        old[oldEnd - 1] === now[newEnd - 1]
      ) {
        oldEnd--;
        newEnd--;
      }
      const changed = [
        ...old.slice(start, oldEnd).map((line) => "-" + line),
        ...now.slice(start, newEnd).map((line) => "+" + line),
      ];
      const diff = changed.length
        ? `@@ -${start + 1},${oldEnd - start} +${start + 1},${newEnd - start} @@\n${changed.join("\n")}`
        : "(no changes)";
      if (diff.length < outline.length * 0.6) {
        outline = diff;
        is_diff = true;
      }
    }
    return {
      tab: describe(await chrome.tabs.get(tabId)),
      outline,
      ...(is_diff ? {} : { elements: snapshot.elements }),
      snapshot_id: id,
      is_diff,
      diff_base: is_diff ? previous.id : null,
    };
  },
  "tab.click": async ({ tabId, ref, x, y }) => {
    await requireAgentTab(tabId);
    await settleAround(tabId, () => click(tabId, { ref, x, y }));
    return {};
  },
  "tab.type": async ({ tabId, ref, text, submit }) => {
    await requireAgentTab(tabId);
    await settleAround(tabId, () => type(tabId, { ref, text, submit }), {
      quietMs: submit ? 300 : 0,
    });
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
    if (!handler)
      throw new BridgeError("invalid_argument", `Unknown method ${method}`);
    params ??= {};
    if (
      params.observation_id &&
      !["tab.snapshot", "tab.screenshot"].includes(method) &&
      observations.get(`${params.session_id}:${params.tabId}`)?.id !==
        params.observation_id
    )
      throw new BridgeError(
        "failed",
        "Stale browser observation; read browser_snapshot again",
      );
    if (params.deadline && Date.now() >= params.deadline)
      throw new BridgeError(
        "failed",
        "Operation expired before browser dispatch",
      );
    if (
      params.tabId != null &&
      method !== "tab.info" &&
      !(
        method === "extended" &&
        ["claim", "user_tabs", "capabilities"].includes(params.operation)
      )
    )
      await requireAgentTab(params.tabId, params.session_id ?? "legacy");
    reply = {
      id,
      result: await (params.tabId != null
        ? withDialog(params.tabId, () => handler(params))
        : handler(params)),
    };
  } catch (error) {
    reply = {
      id,
      error: {
        code: error.code ?? "failed",
        message: error.message ?? String(error),
      },
    };
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

const queues = new Map();
function enqueue(activePort, message) {
  const key = message.params?.tabId ?? "browser";
  const next = (queues.get(key) ?? Promise.resolve()).then(() =>
    dispatch(activePort, message),
  );
  const tail = next.catch(() => {});
  queues.set(key, tail);
  tail.finally(() => {
    if (queues.get(key) === tail) queues.delete(key);
  });
}
chrome.debugger.onEvent.addListener(recordEvent);

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
    else if (message?.type === "error")
      console.warn("Second Cursor host:", message.message);
    else if (message?.id !== undefined && message.method)
      enqueue(current, message);
  });
  current.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError?.message;
    if (reason) console.info("Second Cursor: host disconnected:", reason);
    if (port === current) port = null;
    if (superseded) retryDelay = SUPERSEDED_RETRY_MS;
    else if (Date.now() - connectedAt > HEALTHY_AFTER_MS)
      retryDelay = MIN_RETRY_MS;
    scheduleRetry();
  });
  chrome.storage.local
    .get(["instance_id", "profile_name"])
    .then(async (data) => {
      const instance_id = data.instance_id ?? crypto.randomUUID();
      await chrome.storage.local.set({ instance_id });
      const ua = navigator.userAgent;
      try {
        current.postMessage({
          type: "hello",
          version: VERSION,
          instance_id,
          profile_name: data.profile_name ?? "Default",
          name: ua.includes("Edg/")
            ? "Edge"
            : ua.includes("Chrome/")
              ? "Chrome / Chromium"
              : "Chromium",
          capabilities: [
            "ax",
            "dom",
            "input",
            "dialogs",
            "files",
            "screenshots",
            "exports",
            "cdp",
          ],
        });
      } catch {
        // The host went away while storage was read (an app restart, or it refused the connection);
        // onDisconnect has already scheduled the retry.
      }
    });
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
