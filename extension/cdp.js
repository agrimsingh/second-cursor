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
// committed. Action-relative requests are tracked separately, only once agent input needs them.
const lifecycles = new Map();
// No URLs, headers or bodies are retained here. Keep this bounded even on a page that continually polls.
const requestTracking = new Map();
const REQUEST_LIMIT = 2048;
const REQUEST_RETENTION_MS = 30000;
const SETTLING_REQUEST_TYPES = new Set(["Document", "XHR", "Fetch"]);

// A renderer whose main thread never yields answers no command again: a page script spinning (a retry loop whose
// fetches fail at once after its tab closed, a runaway loop). Chrome also gives the first new tab of that site the
// same renderer, which never commits its navigation. A command unanswered for RESPONSIVE_MS gets as long again for
// a trivial one to come back, so a slow command on a live page still finishes. Keys are `${tabId}:${sessionId}`.
// A JavaScript dialog also blocks the renderer, and the command that opened it, until it is answered; that is not
// a hung renderer.
const RESPONSIVE_MS = 5000;
const unresponsive = new Set();
const dialogs = new Set();

function unresponsiveError(tabId) {
  return Object.assign(new BridgeError("failed", `Tab ${tabId} is not responding: its renderer is stuck in a page script `
    + "that never yields, so it cannot load or answer anything. Close it with browser_close and open the page again "
    + "with browser_open."),
  { unresponsive: true });
}

function answersWithin(promise, ms) {
  let timer;
  return Promise.race([promise.then(() => true, () => true), new Promise(resolve => { timer = setTimeout(resolve, ms, false); })])
    .finally(() => clearTimeout(timer));
}

async function send(target, method, params) {
  const key = `${target.tabId}:${target.sessionId ?? ""}`;
  if (unresponsive.has(key)) throw unresponsiveError(target.tabId);
  const command = chrome.debugger.sendCommand(target, method, params);
  if (await answersWithin(command, RESPONSIVE_MS)) return command;
  const probe = chrome.debugger.sendCommand(target, "Page.getFrameTree", {});
  if (await answersWithin(Promise.race([command, probe]), RESPONSIVE_MS) || dialogs.has(target.tabId)) return command;
  unresponsive.add(key);
  throw unresponsiveError(target.tabId);
}

async function expectAnswer(tabId, method) {
  if (await answersWithin(chrome.debugger.sendCommand({ tabId }, method, {}), RESPONSIVE_MS) || dialogs.has(tabId))
    return void unresponsive.delete(`${tabId}:`);
  unresponsive.add(`${tabId}:`);
  throw unresponsiveError(tabId);
}

/** Throws unless the attached tab's renderer answers a trivial command within RESPONSIVE_MS. */
export const requireResponsive = tabId => expectAnswer(tabId, "Page.getFrameTree");

async function trackNetworkSession(tabId, sessionId = "") {
  const state = requestTracking.get(tabId);
  if (!state) return;
  if (state.sessions.has(sessionId)) return state.sessions.get(sessionId);
  const pending = (sessionId
    ? send({ tabId, sessionId }, "Network.enable", {})
    : ensureDomain(tabId, "Network"))
    .then(() => true, () => false);
  state.sessions.set(sessionId, pending);
  const ready = await pending;
  // A detached/replaced session must not be resurrected by its in-flight enable command.
  if (state.sessions.get(sessionId) === pending) state.sessions.set(sessionId, ready);
  return ready;
}

/** Enable request events before dispatch, including frames Chrome runs in separate processes. */
export async function ensureActionNetwork(tabId) {
  await attach(tabId);
  let state = requestTracking.get(tabId);
  if (!state) {
    state = { requests: new Map(), sessions: new Map(), overflowAt: -Infinity, background: new Set() };
    requestTracking.set(tabId, state);
  }
  // Failed enables may be retried before a later action, but never retry the action itself.
  for (const [sessionId, ready] of state.sessions) if (ready === false) state.sessions.delete(sessionId);
  await Promise.all([trackNetworkSession(tabId), ...[...frameSessions]
    .filter(([key]) => key.startsWith(`${tabId}:`)).map(([, sessionId]) => trackNetworkSession(tabId, sessionId))]);
}

/**
 * Starts tracking an attached tab's requests if nothing has. It never attaches (a tab released meanwhile stays
 * released), and a failed enable is retried only before input.
 */
export async function trackRequests(tabId) {
  if (attached.has(tabId) && !requestTracking.has(tabId)) await ensureActionNetwork(tabId);
}

/**
 * Request timing is a readiness hint, never evidence that the requested mutation succeeded. Requests started from
 * `since` count, except the page's own (noteBackgroundRequests) and any started from `until`, once the agent was idle.
 * Unfinished ones started before `outlivedBefore` are counted as outlived instead of pending.
 */
export function actionNetworkState(tabId, since, until = Infinity, outlivedBefore = -Infinity) {
  const state = requestTracking.get(tabId);
  if (!state) return { available: false, pending: 0, outlived: 0, quiet: Infinity, failed: 0 };
  let pending = 0, outlived = 0, finishedAt = -Infinity, failed = 0, inferred = 0;
  for (const request of state.requests.values()) {
    if (request.startedAt < since || request.startedAt >= until || state.background.has(request.key)) continue;
    const completedAt = request.finishedAt ?? request.bodyObservedAt;
    if (completedAt != null) finishedAt = Math.max(finishedAt, completedAt);
    else if (request.startedAt < outlivedBefore) outlived++;
    else pending++;
    if (request.failed) failed++;
    if (request.finishedAt == null && request.bodyObservedAt != null) inferred++;
  }
  const enabling = [...state.sessions.values()].some(value => typeof value !== "boolean");
  const incomplete = state.overflowAt >= since;
  const available = state.sessions.get("") === true && ![...state.sessions.values()].includes(false) && !incomplete;
  return { available, pending, outlived, quiet: Date.now() - finishedAt, failed, inferred, enabling, incomplete };
}

/**
 * A request the page started while the agent was idle (`from`..`until`), or one in flight when the agent acted at
 * `actedAt`, is the page's own: a poll, a heartbeat, a long poll. The same request (method, URL and body) started
 * after a later action is not waited for. A request the action makes with a new URL or body always is.
 */
export function noteBackgroundRequests(tabId, actedAt, from, until) {
  const state = requestTracking.get(tabId);
  if (!state || !actedAt) return;
  if (state.background.size > REQUEST_LIMIT) state.background.clear();
  for (const request of state.requests.values()) {
    const completedAt = request.finishedAt ?? request.bodyObservedAt ?? Infinity;
    if ((request.startedAt < actedAt && completedAt > actedAt) || (request.startedAt >= from && request.startedAt < until))
      state.background.add(request.key);
  }
}

// FNV-1a, so the request's identity is kept without its URL or body.
function requestKey(request = {}) {
  let hash = 0x811c9dc5;
  const text = `${request.method} ${request.url}\n${request.postData ?? ""}`;
  for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
  return hash >>> 0;
}

// Chrome can omit loadingFinished when page code awaits Fetch headers but never consumes its response body.
// For an uncached, unencoded response only, observed bytes equalling its declared length are a bounded readiness
// hint. Never infer from headers alone, unknown/chunked lengths, decoded compressed bytes, or bodyless responses.
function declaredResponseBytes(request, response) {
  // ExtraInfo events can cross redirect boundaries. Keep redirects on the authoritative finish path instead of
  // guessing which response's cache status and byte bounds an event describes.
  if (!request.bodyAllowed || request.redirected || request.cached || response.fromDiskCache || response.fromPrefetchCache
      || response.fromServiceWorker || !Number.isInteger(response.status) || response.status < 200
      || response.status >= 300 || response.status === 204 || response.status === 205) return null;
  const headers = Object.entries(response.headers ?? {});
  const lengths = headers.filter(([name]) => name.toLowerCase() === "content-length");
  const encodings = headers.filter(([name]) => name.toLowerCase() === "content-encoding");
  if (lengths.length !== 1 || typeof lengths[0][1] !== "string" || !/^[1-9]\d*$/.test(lengths[0][1].trim())
      || headers.some(([name]) => name.toLowerCase() === "transfer-encoding") || encodings.length > 1
      || (encodings.length && (typeof encodings[0][1] !== "string" || encodings[0][1].trim().toLowerCase() !== "identity")))
    return null;
  const bytes = Number(lengths[0][1]);
  return Number.isSafeInteger(bytes) ? bytes : null;
}

function updateBodyHint(request) {
  // Revalidated cached responses can expose status 200 here but status 304 in ExtraInfo. If Chrome promises that
  // event, wait for it regardless of ordering, and do not infer through conflicting/duplicate extra responses.
  const extraConfirmed = !request.extraDuplicate && (!request.expectsExtra || request.extraSeen)
    && (!request.extraSeen || request.extraStatus === request.responseStatus);
  request.bodyObservedAt = request.expectedBytes != null && request.receivedBytes === request.expectedBytes && extraConfirmed
    ? request.bodyObservedAt ?? Date.now() : undefined;
}

function networkEvent(source, method, params) {
  const state = requestTracking.get(source.tabId);
  if (!state) return;
  const sessionId = source.sessionId ?? "";
  if (sessionId && !state.sessions.has(sessionId)) return;
  const key = `${sessionId}:${params.requestId}`;
  if (method === "Network.requestWillBeSent" && SETTLING_REQUEST_TYPES.has(params.type)) {
    const now = Date.now();
    // Redirects keep their original start, so an old long poll redirecting after a click stays unrelated.
    const previous = params.redirectResponse && state.requests.get(key);
    const startedAt = previous?.startedAt ?? (Number.isFinite(params.wallTime) ? params.wallTime * 1000 : now);
    for (const [id, request] of state.requests) {
      const completedAt = request.finishedAt ?? request.bodyObservedAt;
      if (completedAt != null && now - completedAt > REQUEST_RETENTION_MS) state.requests.delete(id);
    }
    if (!state.requests.has(key) && state.requests.size >= REQUEST_LIMIT) {
      const oldest = [...state.requests].find(([, request]) => request.finishedAt != null || request.bodyObservedAt != null)
        ?? state.requests.entries().next().value;
      state.requests.delete(oldest[0]);
      // Losing an unfinished request prevents a false all-clear for observations of the affected action.
      if (oldest[1].finishedAt == null && oldest[1].bodyObservedAt == null)
        state.overflowAt = Math.max(state.overflowAt, oldest[1].startedAt);
    }
    state.requests.set(key, { sessionId, frameId: params.frameId, loaderId: params.loaderId, startedAt,
      key: previous?.key ?? requestKey(params.request),
      bodyAllowed: typeof params.request?.method === "string" && params.request.method.toUpperCase() !== "HEAD",
      redirected: Boolean(params.redirectResponse),
      receivedBytes: 0 });
  } else if (method === "Network.requestServedFromCache") {
    const request = state.requests.get(key);
    if (request) Object.assign(request, { cached: true, expectedBytes: null, bodyObservedAt: undefined });
  } else if (method === "Network.responseReceived") {
    const request = state.requests.get(key);
    if (request) {
      // A duplicate response or out-of-order byte event cannot establish the new body's bounds.
      request.expectedBytes = request.responseSeen || request.receivedBytes !== 0
        ? null : declaredResponseBytes(request, params.response ?? {});
      request.responseSeen = true;
      request.responseStatus = params.response?.status;
      request.expectsExtra = params.hasExtraInfo === true;
      request.bodyObservedAt = undefined;
    }
  } else if (method === "Network.responseReceivedExtraInfo") {
    const request = state.requests.get(key);
    if (request) {
      request.extraDuplicate = request.extraSeen === true;
      request.extraSeen = true;
      request.extraStatus = Number.isInteger(params.statusCode) ? params.statusCode : null;
      updateBodyHint(request);
    }
  } else if (method === "Network.dataReceived") {
    const request = state.requests.get(key);
    if (request && request.finishedAt == null) {
      request.receivedBytes = Number.isSafeInteger(params.dataLength) && params.dataLength >= 0
        && Number.isSafeInteger(request.receivedBytes + params.dataLength)
        ? request.receivedBytes + params.dataLength : NaN;
      updateBodyHint(request);
    }
  } else if (method === "Network.loadingFinished" || method === "Network.loadingFailed") {
    const request = state.requests.get(key);
    if (request) Object.assign(request, { finishedAt: Date.now(), failed: method === "Network.loadingFailed" });
  } else if (method === "Page.frameNavigated") {
    const frame = params.frame;
    if (!source.sessionId && !frame.parentId) state.background.clear();
    for (const [id, request] of state.requests) {
      const replaced = !source.sessionId && !frame.parentId
        ? request.loaderId !== frame.loaderId
        : request.frameId === frame.id && request.loaderId !== frame.loaderId;
      if (replaced) state.requests.delete(id);
    }
  } else if (method === "Page.frameDetached" && params.reason !== "swap") {
    for (const [id, request] of state.requests) if (request.frameId === params.frameId) state.requests.delete(id);
  }
}
chrome.debugger.onEvent.addListener((source,method,p)=>{
  if(method === "Target.attachedToTarget") {
    const key = `${source.tabId}:${p.targetInfo.targetId}`;
    isolatedFrameIds.add(key);
    frameSessions.set(key,p.sessionId);
    chrome.debugger.sendCommand({tabId:source.tabId,sessionId:p.sessionId},"Target.setAutoAttach",{autoAttach:true,waitForDebuggerOnStart:false,flatten:true}).catch(()=>{});
    void trackNetworkSession(source.tabId, p.sessionId);
  }
  if(method === "Target.detachedFromTarget") {
    unresponsive.delete(`${source.tabId}:${p.sessionId}`);
    for(const [k,v] of frameSessions)if(k.startsWith(`${source.tabId}:`)&&v===p.sessionId)frameSessions.delete(k);
    const state = requestTracking.get(source.tabId);
    state?.sessions.delete(p.sessionId);
    if (state) for (const [key, request] of state.requests) if (request.sessionId === p.sessionId) state.requests.delete(key);
  }
  networkEvent(source, method, p);
  // A renderer that commits a document is answering again.
  if (method === "Page.frameNavigated" && !source.sessionId && !p.frame.parentId) unresponsive.delete(`${source.tabId}:`);
  if (method === "Page.javascriptDialogOpening") dialogs.add(source.tabId);
  if (method === "Page.javascriptDialogClosed") dialogs.delete(source.tabId);
  if (method === "Page.lifecycleEvent" && !source.sessionId) {
    const frames = lifecycles.get(source.tabId) ?? new Map();
    lifecycles.set(source.tabId, frames);
    if (frames.get(p.frameId)?.loaderId !== p.loaderId) frames.set(p.frameId, { loaderId: p.loaderId, names: new Set() });
    frames.get(p.frameId).names.add(p.name);
  }
});

function detachedTab(tabId) {
  attached.delete(tabId);
  dialogs.delete(tabId);
  lifecycles.delete(tabId);
  requestTracking.delete(tabId);
  for (const key of enabled) if (key.startsWith(`${tabId}:`)) enabled.delete(key);
  for (const key of frameSessions.keys()) if (key.startsWith(`${tabId}:`)) frameSessions.delete(key);
}

chrome.debugger.onDetach.addListener(({ tabId }) => detachedTab(tabId));
chrome.tabs.onRemoved.addListener(tabId => {
  detachedTab(tabId);
  for (const key of isolatedFrameIds) if (key.startsWith(`${tabId}:`)) isolatedFrameIds.delete(key);
  for (const key of unresponsive) if (key.startsWith(`${tabId}:`)) unresponsive.delete(key);
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
  try {
    await expectAnswer(tabId, "Page.enable");
  } catch (e) {
    attached.delete(tabId);
    throw e;
  }
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
    return await send({ tabId }, method, params);
  } catch (e) {
    if (e.unresponsive) throw e;
    if (/not attached/i.test(e.message)) {
      attached.delete(tabId);
      await attach(tabId);
      return await send({ tabId }, method, params);
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
    const result = await send({tabId,...(sessionId?{sessionId}:{})},method,params);
    if (proveLocalFrame) {
      if (!attached.has(tabId) || frameSessions.get(key) || !Number.isInteger(result?.executionContextId)) throw unavailable();
      isolatedFrameIds.delete(key);
    }
    return result;
  }
  catch(e){throw e.unresponsive ? e : new BridgeError("failed",`${method}: ${protocolMessage(e)}`);}
}
