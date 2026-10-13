import { attach } from "./cdp.js";
import { hideSession, setCaption, show } from "./cursor.js";
import { BridgeError, sleep } from "./errors.js";
import { click, pressKey, screenshot, scroll, type } from "./input.js";
import { snapshot, structuredSnapshot } from "./snapshot.js";
import { browserRevision } from "./freshness.js";
import { cancelExpectations, expectationOutcome, failedExpectation, finishBatchExpectation, prepareBatchExpectation, prepareExpectation, waitExpectation } from "./expectations.js";
import {
  closeTab,
  describe,
  listTabs,
  navigateTab,
  openTab,
  noteAction,
  noteInput,
  requireAgentTab,
  beginAgentInput,
  endAgentInput,
  requireSessionInput,
  sessionIsActive,
  sessionControlGeneration,
  setSessionControl,
  setTrackedQuietMs,
  settleAround,
  settled,
  takeReports,
} from "./tabs.js";

import { extended, openDialog, recordEvent, withDialog } from "./extended.js";
import { closeSession, endTurn } from "./tabs.js";
import * as timing from "./timing.js";

const HOST = "dev.secondcursor.host";
const VERSION = chrome.runtime.getManifest().version;
const MIN_RETRY_MS = 500;
// Retrying (an extension API call) more often than every 30s also keeps this worker alive.
const MAX_RETRY_MS = 15000;
const SUPERSEDED_RETRY_MS = 60000;
const HEALTHY_AFTER_MS = 10000;
const RECONNECT_ALARM = "reconnect";

const observations = new Map();
function invalidateSession(session) {
  cancelExpectations(session);
  for (const key of observations.keys()) if (key.startsWith(session + ":")) observations.delete(key);
}
async function validateObservation(params) {
  if (!params.observation_id) return;
  const observed = observations.get(`${params.session_id}:${params.tabId}`);
  if (observed?.id !== params.observation_id)
    throw new BridgeError("failed", "Stale browser observation; read browser_snapshot again");
  if (!observed.current || observed.revision !== await browserRevision(params.tabId))
    throw new BridgeError("failed", "Stale browser observation: the document or viewport changed; read browser_snapshot again");
}
chrome.tabs.onRemoved.addListener((id) => {
  for (const key of observations.keys())
    if (key.endsWith(":" + id)) observations.delete(key);
});
const handlers = {
  extended: params => extended(params, () => validateObservation(params)),
  // Sent just before the action it describes, which must not wait for the overlay.
  "cursor.caption": async ({ tabId, caption, session_id }) => {
    if (await sessionIsActive(session_id)) setCaption(tabId, caption, session_id);
    return {};
  },
  "session.close": async ({ session_id, generation }) => {
    const control = await setSessionControl(session_id, "closed", generation);
    if (!control.changed) return { closed: control.state === "closed", ...control };
    invalidateSession(session_id);
    await hideSession(session_id);
    return closeSession(session_id);
  },
  "session.pause": async ({ session_id, generation }) => {
    const control = await setSessionControl(session_id, "paused", generation);
    if (!control.changed) return { paused: control.state === "paused", ...control };
    invalidateSession(session_id);
    await hideSession(session_id);
    return { paused: true, ...control };
  },
  "session.takeover": async ({ session_id, generation }) => {
    const control = await setSessionControl(session_id, "paused", generation);
    // Immediate pause and post-drain takeover deliberately share one generation. Repeating takeover only releases
    // any remaining personal claims; an older takeover must never release claims from a later resumed task.
    if (control.state !== "paused" || (Number.isInteger(generation) && control.generation !== generation))
      return { taken_over: false, ...control };
    invalidateSession(session_id);
    await hideSession(session_id);
    const released = await endTurn(session_id);
    return { taken_over: true, requires_reclaim: released.released > 0, ...released, ...control };
  },
  "session.resume": async ({ session_id, generation }) => {
    const control = await setSessionControl(session_id, "active", generation);
    if (!control.changed) return { resumed: control.state === "active", ...control };
    invalidateSession(session_id);
    return { resumed: true, requires_observation: true, ...control };
  },
  // Only for the app's local approval prompt text; tab ids never come from here to the model.
  "tab.info": async ({ tabId }) => {
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url || tab.pendingUrl || "", title: tab.title || "" };
  },
  "session.end_turn": async ({ session_id }) => {
    invalidateSession(session_id);
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
  "tab.navigate": async ({ tabId, url, session_id }) => {
    await attach(tabId);
    show(tabId, session_id);
    const tab = await navigateTab(tabId, url);
    return { tab };
  },
  "tab.snapshot": params => tabSnapshot(params),
  "tab.expectPrepare": params => prepareBatchExpectation(params),
  "tab.expectFinish": async params => {
    const generation = await sessionControlGeneration(params.session_id ?? "legacy");
    const expect = await finishBatchExpectation(params);
    const reply = { expect, outcome: expectationOutcome(expect) };
    return appendObservation(params, reply, false, generation);
  },
  "tab.click": async (params) => {
    const { tabId, ref, x, y } = params;
    await requireAgentTab(tabId);
    return act(params, guard => click(tabId, { ref, x, y }, () => validateObservation(params), guard), { quietMs: INPUT_NAVIGATION_MS });
  },
  "tab.type": async (params) => {
    const { tabId, ref, text, submit } = params;
    await requireAgentTab(tabId);
    return act(params, guard => type(tabId, { ref, text, submit }, guard), { quietMs: submit ? INPUT_NAVIGATION_MS : 0 });
  },
  "tab.pressKey": async (params) => {
    const { tabId, keys } = params;
    await requireAgentTab(tabId);
    return act(params, guard => pressKey(tabId, keys, guard), { quietMs: INPUT_NAVIGATION_MS });
  },
  "tab.scroll": async (params) => {
    const { tabId, direction, amount } = params;
    await requireAgentTab(tabId);
    return act(params, guard => scroll(tabId, direction, amount, guard), null);
  },
  "tab.screenshot": async ({ tabId }) => {
    await requireAgentTab(tabId);
    return await timing.timed(tabId, "screenshot", () => screenshot(tabId));
  },
};

async function tabSnapshot({ tabId, session_id, full, structured }, settle = true, guard = async () => {}) {
    await guard();
    await requireAgentTab(tabId);
    // The page runs no script, and so cannot be read, until its dialog is answered.
    if (openDialog(tabId))
      throw new BridgeError(
        "failed",
        `A JavaScript dialog is open in tab ${tabId}; answer it with browser_command operation=dialog, then read the page`,
      );
    // Like Codex, the cursor is on the tab from the moment the agent works in it, not only once it acts.
    await timing.timed(tabId, "attach", () => attach(tabId));
    if (await sessionIsActive(session_id)) show(tabId, session_id);
    const settling = settle ? await settled(tabId) : undefined;
    const key = `${session_id}:${tabId}`;
    const previous = observations.get(key);
    const beforeRevision = await browserRevision(tabId, guard);
    const capturedAt = Date.now();
    const snapshot = await timing.timed(tabId, "collect", () => structuredSnapshot(tabId, guard));
    const revision = await browserRevision(tabId, guard);
    await guard();
    const current = beforeRevision === revision;
    const id = crypto.randomUUID();
    observations.set(key, { id, outline: snapshot.outline, revision, current });
    let outline = snapshot.outline;
    let is_diff = false;
    if (full === false && previous) {
      const diffStarted = performance.now();
      const changed = changedLines(
        previous.outline.split("\n"),
        outline.split("\n"),
      );
      timing.add(tabId, "diff", performance.now() - diffStarted);
      const diff =
        changed && (changed.length ? changed.join("\n") : "(no changes)");
      // Large edits return the full tree.
      if (diff && diff.length < outline.length * 0.6) {
        outline = diff;
        is_diff = true;
      }
    }
    return {
      tab: describe(await chrome.tabs.get(tabId)),
      outline,
      ...(structured && !is_diff ? { elements: snapshot.elements } : {}),
      snapshot_id: id,
      ...(settling ? { settling } : {}),
      freshness: { revision, captured_at_ms: capturedAt, age_ms: Date.now() - capturedAt,
        status: current ? "current" : "changed_during_capture" },
      is_diff,
      diff_base: is_diff ? previous.id : null,
    };
}

const OBSERVING = new Set(["tree", "both", "screenshot"]);
// A navigation that input starts in its own event handler (a link, a form submit, location set in the handler) shows
// as loading within a few ms of the input. Later ones (after a timer or a request) are waited for by the next read.
const INPUT_NAVIGATION_MS = 100;

/**
 * Runs an input action. A call that observes the page (`observe` tree, both or screenshot) reads it in the same
 * message once it settles: settled() waits for the page's reaction, and for a navigation the action started, so the
 * action waits for nothing first. Without a read the next call may act at once, so the action waits out any navigation
 * it starts (settleAround with `settleOptions`; null for input that cannot navigate), and the next read waits for its
 * reaction as an observing action's would. A dialog the action opens answers the call before any read, which could
 * not run until the dialog is answered.
 */
async function act(params, action, settleOptions) {
  const { tabId, observe, session_id } = params;
  if (params.expect != null) {
    const prepared = await prepareExpectation(params);
    const at = Date.now();
    let blocked;
    try {
      // The complete precondition read may take time; the action must still be grounded in the caller's view.
      await validateObservation(params);
      const guardInput = async () => {
        if (prepared.cancelled || Date.now() >= prepared.expires || openDialog(tabId)
          || prepared.generation !== await sessionControlGeneration(session_id ?? "legacy"))
          throw new BridgeError("failed", "The interrupted action cannot send further input; inspect the page before continuing");
      };
      blocked = await withDialog(tabId, () => timing.timed(tabId, "action", () => action(guardInput)));
    } catch (error) {
      prepared.cancelled = true;
      throw error;
    }
    noteAction(tabId, at);
    const expect = blocked?.dialog_open
      ? failedExpectation("dialog_open") : await waitExpectation(prepared);
    prepared.cancelled = true;
    const reply = { ...(blocked ?? {}), expect, outcome: expectationOutcome(expect) };
    return appendObservation(params, reply, false, prepared.generation);
  }
  if (!OBSERVING.has(observe)) {
    const at = Date.now();
    await (settleOptions ? settleAround(tabId, action, settleOptions) : action());
    noteAction(tabId, at);
    return { outcome: { state: "dispatched", verified: false, reason: "Input was dispatched; no postcondition was requested.", verification: "none" } };
  }
  const at = Date.now();
  const blocked = await withDialog(tabId, async () => {
    await timing.timed(tabId, "action", action);
  });
  if (blocked?.dialog_open) return blocked;
  noteAction(tabId, at);
  const reply = { outcome: { state: "dispatched", verified: false, reason: "Input was dispatched; no postcondition was requested.", verification: "none" } };
  return appendObservation(params, reply);
}

async function appendObservation(params, reply, settle = true, expectedGeneration) {
  const { tabId, observe, session_id = "legacy" } = params;
  if (!OBSERVING.has(observe)) return reply;
  if (openDialog(tabId)) {
    reply.observation_error = "A JavaScript dialog is open; handle it before reading the page.";
    return reply;
  }
  if (!settle && ["control_changed", "deadline", "token_unavailable"].includes(reply.expect?.reason)) {
    reply.observation_error = "Task control or the verification deadline changed; take a fresh observation when control is restored.";
    return reply;
  }
  let cancelled = false;
  const deadline = Math.min(params.deadline ?? Infinity, Date.now() + 3000);
  const guard = settle ? async () => {} : async () => {
    if (cancelled || Date.now() >= deadline || openDialog(tabId)) throw Error("Observation interrupted");
    await requireAgentTab(tabId, session_id);
    await requireSessionInput(session_id);
    if (expectedGeneration !== await sessionControlGeneration(session_id)) throw Error("Task control changed");
  };
  const read = async () => {
    await guard();
    await requireAgentTab(tabId, session_id);
    const result = {};
    if (observe === "screenshot") {
      if (settle) result.settling = await settled(tabId);
    } else result.observation = await tabSnapshot({ tabId, session_id, full: params.full ?? false }, settle, guard);
    await guard();
    if (observe !== "tree") result.screenshot = await timing.timed(tabId, "screenshot", () => screenshot(tabId, guard));
    await guard();
    return result;
  };
  try {
    if (settle) Object.assign(reply, await read());
    else {
      let done = false, result, failure;
      const pending = read().then(value => { result = value; done = true; }, error => { failure = error; done = true; });
      while (!done) { await Promise.race([pending, sleep(25)]); await guard(); }
      if (failure) throw failure;
      Object.assign(reply, result);
    }
  } catch (error) {
    reply.observation_error = settle ? error.message ?? String(error)
      : "The postcondition result is retained, but the final observation was interrupted or unavailable; inspect the tab before further input.";
  } finally { cancelled = true; }
  return reply;
}

/**
 * The removed (-) and added (+) lines between two outlines, in order. Lines on a longest common subsequence drop
 * out, so an edit in the header and another in the body do not resend the rows between them, and repeated rows keep
 * their count. Null when the change is a different page, not an edit.
 */
function changedLines(old, now) {
  let head = 0;
  while (head < old.length && head < now.length && old[head] === now[head])
    head++;
  let tail = 0;
  while (
    tail < old.length - head &&
    tail < now.length - head &&
    old[old.length - 1 - tail] === now[now.length - 1 - tail]
  )
    tail++;
  const a = old.slice(head, old.length - tail);
  const b = now.slice(head, now.length - tail);
  if (a.length * b.length > 250000) return null;
  const table = Array.from(
    { length: a.length + 1 },
    () => new Uint32Array(b.length + 1),
  );
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      table[i][j] =
        a[i] === b[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1]);
  const changed = [];
  let i = 0,
    j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      i++;
      j++;
    } else if (
      j === b.length ||
      (i < a.length && table[i + 1][j] >= table[i][j + 1])
    )
      changed.push("-" + a[i++]);
    else changed.push("+" + b[j++]);
  }
  return changed;
}

const ACTIONS = new Set(["tab.click", "tab.type", "tab.pressKey", "tab.scroll"]);
const EXTENDED_ACTIONS = new Set(["click", "hover", "drag", "fill", "check", "select_option", "select_text",
  "perform_action", "paste", "press_key", "scroll", "type", "upload", "back", "forward", "reload", "viewport"]);
// Reads and lifecycle operations address a tab by its owned identity. They must remain usable to inspect or close
// a page after it changed. Freshness is a precondition for input grounded in a prior UI, not for that recovery.
const GROUNDED_INPUT = new Set(["click", "hover", "drag", "fill", "check", "select_option", "select_text",
  "perform_action", "paste", "press_key", "scroll", "type", "upload"]);
// Replies the app hands to the model; queued [system] lines ride on these and on nothing else, or they are lost.
const REPORTING = new Set([...ACTIONS, "tabs.open", "tabs.close", "tab.snapshot", "tab.expectFinish", "extended"]);

let port = null;
let retryTimer = null;
let retryDelay = MIN_RETRY_MS;

async function dispatch(activePort, { id, method, params, receivedAt, controlGeneration }) {
  let reply;
  let inputToken;
  const started = performance.now();
  const timingKey = params?.tabId ?? "browser";
  const phases = params?.timings ? timing.begin(timingKey) : null;
  setTrackedQuietMs(params?.quiet_ms);
  try {
    const handler = handlers[method];
    if (!handler)
      throw new BridgeError("invalid_argument", `Unknown method ${method}`);
    params ??= {};
    params.session_id ??= "legacy";
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
    // Ownership precedes freshness: an old cached observation must never reattach a released personal tab.
    if (ACTIONS.has(method) || (method === "extended" && GROUNDED_INPUT.has(params.operation)))
      await validateObservation(params);
    const dispatchedAt = Date.now();
    const acts =
      params.tabId != null &&
      (ACTIONS.has(method) || (method === "extended" && EXTENDED_ACTIONS.has(params.operation)));
    if (acts || ["tabs.open", "tabs.close", "tab.navigate"].includes(method) ||
        (method === "extended" && ["claim", "dialog", "clipboard"].includes(params.operation)))
      inputToken = await beginAgentInput(params.tabId, params.session_id, await controlGeneration);
    if (acts) await noteInput(params.tabId);
    // Explicit postconditions handle dialogs inside their action/wait, preserving failed verification metadata.
    // A second outer race would drop that metadata and abandon a polling loop behind the returned reply.
    const managesExpectation = (ACTIONS.has(method) && params.expect != null)
      || ["tab.expectPrepare", "tab.expectFinish"].includes(method);
    reply = {
      id,
      result: await (params.tabId != null && !managesExpectation
        ? withDialog(params.tabId, () => handler(params))
        : handler(params)),
    };
    if (acts && !reply.result?.outcome)
      reply.result = { ...reply.result, outcome: { state: "dispatched", verified: false,
        reason: "Input was dispatched; no postcondition was requested.", verification: "none" } };
    if (method === "extended" && ["claim", "release"].includes(params.operation))
      observations.delete(`${params.session_id}:${params.tabId}`);
    const events = REPORTING.has(method) ? takeReports(params.session_id ?? "legacy") : [];
    if (events.length) reply.result = { ...reply.result, events };
    // An action that read the page in this message stamped itself before reading (act).
    if (acts && !(ACTIONS.has(method) && OBSERVING.has(params.observe) && !reply.result?.dialog_open))
      noteAction(params.tabId, dispatchedAt);
    if (phases)
      reply.result = {
        ...reply.result,
        timings: { ...phases, queue: started - receivedAt, total: performance.now() - started },
      };
  } catch (error) {
    reply = {
      id,
      error: {
        code: error.code ?? "failed",
        message: error.message ?? String(error),
      },
    };
  } finally {
    if (inputToken) endAgentInput(params?.tabId, inputToken);
    if (phases) timing.end(timingKey, phases);
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
  message.receivedAt = performance.now();
  message.controlGeneration = sessionControlGeneration(message.params?.session_id ?? "legacy");
  // Control has a separate, per-session lane: a pause never waits behind a tab action or slow tabs.open, while
  // pause/takeover/resume retain their generation order even across asynchronous storage and release calls.
  const control = ["session.pause", "session.resume", "session.takeover", "session.close"].includes(message.method);
  const key = control ? `control:${message.params?.session_id ?? "legacy"}` : message.params?.tabId ?? "browser";
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
    .get(["instance_id", "profile_name", "focused_at"])
    .then(async (data) => {
      const instance_id = data.instance_id ?? crypto.randomUUID();
      await chrome.storage.local.set({ instance_id });
      // Chrome has no API for a profile's display name; its signed-in account is what tells profiles apart.
      const account = await chrome.identity
        .getProfileUserInfo({ accountStatus: "ANY" })
        .catch(() => null);
      const focusedNow = (await chrome.windows.getLastFocused().catch(() => null))?.focused;
      const ua = navigator.userAgent;
      try {
        current.postMessage({
          type: "hello",
          version: VERSION,
          instance_id,
          profile_name: data.profile_name ?? (account?.email || "Default"),
          focused_at: focusedNow ? Date.now() : (data.focused_at ?? null),
          name: ua.includes("Edg/")
            ? "Edge"
            : ua.includes("Chrome/")
              ? "Chrome / Chromium"
              : "Chromium",
          capabilities: [
            "browser_expect_v1",
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

// Chrome stops an idle worker, and a stopped worker's retry timer never fires: without this alarm an extension
// that lost the app stays disconnected until Chrome restarts. The alarm itself restarts the worker.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) connect();
});
chrome.alarms.get(RECONNECT_ALARM).then((alarm) => {
  if (!alarm) chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 });
});

// Each profile's extension sees only its own windows, so each reports when the user focuses one; the app sends
// calls without browser_id to the profile focused last.
chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return;
  const at = Date.now();
  chrome.storage.local.set({ focused_at: at });
  try {
    port?.postMessage({ type: "focus", at });
  } catch {
    // The host is gone; the next hello carries focused_at.
  }
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
