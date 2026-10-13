import { attach, cdp, networkIdle, ensureActionNetwork, actionNetworkState, noteBackgroundRequests, requireResponsive, trackRequests } from "./cdp.js";
import { removeCursor, show } from "./cursor.js";
import { BridgeError, sleep } from "./errors.js";
import { add as addTiming, timed } from "./timing.js";

const GROUP_TITLE = "Second Cursor";
// Matches the native lease: a claimed personal tab is released after this long without use.
const CLAIM_IDLE_MS = 5 * 60 * 1000;
const CLAIM_SWEEP_ALARM = "second-cursor-claim-sweep";

let agentTabsPromise = null;
let ownershipPromise = null;
const ownership = () =>
  (ownershipPromise ??= chrome.storage.session
    .get("ownership")
    .then((x) => x.ownership ?? {}));
const saveOwnership = async () =>
  chrome.storage.session.set({ ownership: await ownership() });
let controlsPromise = null;
const controls = () => (controlsPromise ??= chrome.storage.session.get("sessionControls").then(x => x.sessionControls ?? {}));

/** Persist pause across service-worker restarts. Closing is terminal, so already queued work cannot recreate tabs. */
export async function setSessionControl(session, state, generation) {
  const all = await controls();
  const previous = all[session];
  if (Number.isInteger(generation) && generation <= (previous?.generation ?? -1))
    return { ...previous, changed: false };
  if (all[session]?.state === "closed" && state !== "closed")
    throw new BridgeError("not_allowed", "The browser session is closed");
  all[session] = { state, generation: Number.isInteger(generation) ? generation : (previous?.generation ?? 0) + 1 };
  await chrome.storage.session.set({ sessionControls: all });
  return { ...all[session], changed: true };
}

export const sessionControlGeneration = async session => (await controls())[session]?.generation ?? 0;
const activeInputs = new Map();
export async function beginAgentInput(tabId, session, generation) {
  await requireSessionInput(session);
  if (generation !== await sessionControlGeneration(session))
    throw new BridgeError("not_allowed", "Browser task control changed while input was queued; read the page and submit it again");
  const token = { session, generation };
  if (tabId != null) activeInputs.set(tabId, token);
  return token;
}
export function endAgentInput(tabId, token) {
  if (activeInputs.get(tabId) === token) activeInputs.delete(tabId);
}

export async function sessionIsActive(session) {
  return !["paused", "closed"].includes((await controls())[session]?.state);
}

export async function requireSessionInput(session) {
  const state = (await controls())[session]?.state;
  if (state === "paused" || state === "closed")
    throw new BridgeError("not_allowed", `Browser session is ${state}; ${state === "paused" ? "resume the task before sending input" : "open a new task"}`);
}

/** Rechecked at each CDP input, after cursor animation and between drag/key events, not only at dispatch. */
export async function requireAgentInput(tabId, session = null) {
  await requireAgentTab(tabId, session);
  const owner = (await ownership())[tabId]?.session ?? session ?? "legacy";
  await requireSessionInput(owner);
  const active = activeInputs.get(tabId);
  if (active && (active.session !== owner || active.generation !== await sessionControlGeneration(owner)))
    throw new BridgeError("not_allowed", "Browser task control changed during input; read the page and submit it again");
}
let persistQueue = Promise.resolve();
let groupQueue = Promise.resolve();

function agentTabSet() {
  agentTabsPromise ??= chrome.storage.session
    .get("agentTabs")
    .then(({ agentTabs = [] }) => new Set(agentTabs));
  return agentTabsPromise;
}

async function updateAgentTabs(change) {
  const set = await agentTabSet();
  change(set);
  persistQueue = persistQueue.then(() =>
    chrome.storage.session.set({ agentTabs: [...set] }),
  );
  return persistQueue;
}

const reports = new Map();
/** Queues a line for the model about an agent tab; the next reply to the tab's session carries it. */
export async function report(tabId, line) {
  const session = (await ownership())[tabId]?.session;
  if (!session) return;
  reports.set(session, [...(reports.get(session) ?? []), `[system] ${line}`].slice(-50));
}
export function takeReports(session) {
  const lines = reports.get(session) ?? [];
  reports.delete(session);
  return lines;
}

const closing = new Set();
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const agent = (await agentTabSet()).has(tabId);
  if (!closing.delete(tabId) && agent)
    await report(tabId, `tab ${tabId} closed (by the page or the user); its id no longer works`);
  await updateAgentTabs((set) => set.delete(tabId));
});

// What each window showed before its current tab, to hand the user's view back from a tab an agent tab opened.
const viewed = new Map();
chrome.tabs.onActivated.addListener(({ tabId, windowId }) =>
  viewed.set(windowId, { current: tabId, previous: viewed.get(windowId)?.current }),
);
chrome.windows.onRemoved.addListener((windowId) => viewed.delete(windowId));
chrome.tabs.query({ active: true }).then((tabs) => {
  for (const t of tabs) if (!viewed.has(t.windowId)) viewed.set(t.windowId, { current: t.id });
});

const inputAt = new Map();
chrome.tabs.onRemoved.addListener((tabId) => inputAt.delete(tabId));
/** The agent is about to send input to the tab. */
export async function noteInput(tabId) {
  await ensureActionNetwork(tabId);
  inputAt.set(tabId, Date.now());
  quickReactions.delete(tabId);
  // A dispatch that later throws may still have started a request. The next read must observe that action too.
  noteAction(tabId, inputAt.get(tabId));
}

const quickReactions = new Map();

/** The input being dispatched on this tab cannot start a request worth waiting for, so reads after it wait `ms`. */
export function expectQuickReaction(tabId, ms) {
  quickReactions.set(tabId, ms);
}

/**
 * A tab or window an agent tab opened (a target=_blank link, window.open) joins the opener's session. openerTabId
 * cannot tell: for a tab opened into the same window, Chrome names the window's active tab, which is the user's.
 * One the user opens from an agent tab (one handed off to them, say) stays theirs: a page can only open a window
 * within the 5 s its last input keeps it activated, so only a tab opened that soon after the agent's input counts.
 */
async function adoptOpened({ sourceTabId, tabId, url }) {
  const owner = (await ownership())[sourceTabId];
  if (!owner?.created || !(await agentTabSet()).has(sourceTabId)) return;
  if (Date.now() - (inputAt.get(sourceTabId) ?? 0) > 5000) return;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) return;
  // Chrome activates a new tab in the user's window; every moment it shows is taken from the user. A popup window
  // is its own window, and nothing here can stop it taking focus.
  if (tab.active) {
    const { current, previous } = viewed.get(tab.windowId) ?? {};
    const back = current === tabId ? previous : current;
    if (back != null) await chrome.tabs.update(back, { active: true }).catch(() => {});
  }
  await updateAgentTabs((set) => set.add(tabId));
  (await ownership())[tabId] = { session: owner.session, created: true, mode: "ephemeral" };
  await saveOwnership();
  await report(tabId, `popup opened: tab ${tabId} ${url} (from tab ${sourceTabId})`);
  await chrome.tabs.update(tabId, { autoDiscardable: false }).catch(() => {});
  if ((await chrome.windows.get(tab.windowId)).type === "normal") await addToGroup(tab);
  await attach(tabId).catch(() => {});
}
chrome.webNavigation.onCreatedNavigationTarget.addListener(adoptOpened);

// The alarm wakes the service worker, so an abandoned claim is released (and the "being debugged"
// bar goes away) even if the agent never calls again.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === CLAIM_SWEEP_ALARM) sweepClaims();
});

const isClaim = (entry) => entry?.created === false;
const claimExpired = (entry) =>
  Date.now() - (entry.lastUsed ?? 0) >= CLAIM_IDLE_MS;

// Releasing never closes the user's tab.
async function dropTab(tabId) {
  await removeCursor(tabId);
  await chrome.debugger.detach({ tabId }).catch(() => {});
  delete (await ownership())[tabId];
  await updateAgentTabs((s) => s.delete(tabId));
  await saveOwnership();
}

async function sweepClaims() {
  for (const [id, entry] of Object.entries(await ownership()))
    if (isClaim(entry) && claimExpired(entry)) await dropTab(Number(id));
  // Re-check after the awaits above: a claim made meanwhile still needs the alarm. The check and the clear
  // run in one synchronous step, so a later claimTab's alarms.create is always issued after this clear.
  if (!Object.values(await ownership()).some(isClaim))
    await chrome.alarms.clear(CLAIM_SWEEP_ALARM);
}

let lastTouchSave = 0;
async function touchClaim(entry) {
  entry.lastUsed = Date.now();
  if (entry.lastUsed - lastTouchSave < 10000) return;
  lastTouchSave = entry.lastUsed;
  await saveOwnership();
}

export function describe(tab) {
  return {
    id: tab.id,
    url: tab.url || tab.pendingUrl || "",
    title: tab.title || "",
    isAgentTab: true,
    is_agent_tab: true,
  };
}

export async function requireAgentTab(tabId, session = null) {
  if (!Number.isInteger(tabId))
    throw new BridgeError("invalid_argument", "tabId must be an integer");
  if (!(await agentTabSet()).has(tabId)) {
    throw new BridgeError(
      "not_allowed",
      `Tab ${tabId} is not a Second Cursor tab; only tabs opened with browser_open, or opened by such a tab, can be driven`,
    );
  }
  const owner = (await ownership())[tabId];
  if (isClaim(owner) && claimExpired(owner)) {
    await dropTab(tabId);
    throw new BridgeError(
      "not_allowed",
      "claim expired; claim again (requires approval)",
    );
  }
  if (!owner)
    throw new BridgeError("not_allowed", "Tab has no current session owner; claim it again before use");
  if (session && owner.session !== session)
    throw new BridgeError("not_allowed", "Tab belongs to another session");
  if (isClaim(owner)) await touchClaim(owner);
  try {
    return await chrome.tabs.get(tabId);
  } catch {
    await updateAgentTabs((set) => set.delete(tabId));
    throw new BridgeError("invalid_argument", `Tab ${tabId} no longer exists`);
  }
}

function normalizeUrl(url) {
  if (typeof url !== "string" || !url.trim())
    throw new BridgeError("invalid_argument", "url is required");
  url = url.trim();
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url) && !/^(about|data):/i.test(url)) {
    url =
      (/^(localhost|127\.|\[::1\])/i.test(url) ? "http://" : "https://") + url;
  }
  if (!/^(https?|file|about|data):/i.test(url)) {
    throw new BridgeError(
      "invalid_argument",
      "Only http, https, file, data and about URLs can be opened",
    );
  }
  return url;
}

async function targetWindowId() {
  try {
    const win = await chrome.windows.getLastFocused({
      windowTypes: ["normal"],
    });
    if (win?.type === "normal") return win.id;
  } catch {}
  const [win] = await chrome.windows.getAll({ windowTypes: ["normal"] });
  return win?.id;
}

function addToGroup(tab) {
  groupQueue = groupQueue
    .then(async () => {
      const [group] = await chrome.tabGroups.query({
        windowId: tab.windowId,
        title: GROUP_TITLE,
      });
      if (group) {
        await chrome.tabs.group({ groupId: group.id, tabIds: [tab.id] });
        return;
      }
      const groupId = await chrome.tabs.group({
        tabIds: [tab.id],
        createProperties: { windowId: tab.windowId },
      });
      await chrome.tabGroups.update(groupId, {
        title: GROUP_TITLE,
        color: "orange",
      });
    })
    .catch((e) => console.warn("Second Cursor: could not group tab", e));
  return groupQueue;
}

/**
 * Runs `action`, then waits for any navigation it started to finish. A navigation counts as
 * started if the tab reports "loading" within `quietMs`.
 */
export async function settleAround(
  tabId,
  action,
  { quietMs = 300, timeoutMs = 10000 } = {},
) {
  let sawLoading = false;
  const listener = (id, info) => {
    if (id === tabId && info.status === "loading") sawLoading = true;
  };
  chrome.tabs.onUpdated.addListener(listener);
  try {
    const result = await timed(tabId, "action", action);
    const began = Date.now();
    while (Date.now() - began < timeoutMs) {
      const tab = await chrome.tabs.get(tabId).catch(() => null);
      if (!tab) break;
      if (tab.status === "loading") sawLoading = true;
      else if (sawLoading || Date.now() - began > quietMs) break;
      await sleep(50);
    }
    addTiming(tabId, sawLoading ? "after_action_load" : "after_action", Date.now() - began);
    return result;
  } finally {
    chrome.tabs.onUpdated.removeListener(listener);
  }
}

/** settleAround for a load, failing as soon as the attached tab's renderer stops answering: that load never ends. */
async function settleLoad(tabId, action, options) {
  const load = settleAround(tabId, action, options);
  let loading = true;
  const watch = (async () => {
    while (loading) {
      await requireResponsive(tabId);
      await sleep(500);
    }
  })();
  watch.catch(() => {});
  try {
    return await Promise.race([load, watch.then(() => load)]);
  } finally {
    loading = false;
  }
}

// Evaluated in its own isolated world, so the page never sees the observer. Its times are milliseconds ago (null when
// unknown): when the agent acted, when a read first settled that action, and [from, until] windows the agent was idle.
const SETTLE_WORLD = "SecondCursorSettle";
const settleContexts = new Map();
const SETTLE_PROBE = (times) => `((times) => {
  const LOG_MS = 30000, BUSY = '[aria-busy="true"],[role="progressbar"]:not([aria-valuenow])';
  let s = globalThis.__secondCursorSettle;
  if (!s || s.doc !== document) {
    // Each change is logged as [time, node, kind]; a null node (the document starting, a checkbox) always counts.
    s = { doc: document, log: [[performance.now(), null, ""]], busy: new WeakMap() };
    // Most attribute changes are left out: animations and timers change them without the content changing. The ones
    // that say what an element is or shows (a menu expanding, a tab selected, a panel unhidden) are how many pages
    // react to a click, with no node added. The agent's own cursor (cursor.js), added when the agent starts work on
    // the page, never counts.
    const cursorOnly = (r) => r.target.id === "second-cursor-overlay" || (r.addedNodes.length + r.removedNodes.length > 0
      && [...r.addedNodes, ...r.removedNodes].every((n) => n.id === "second-cursor-overlay"));
    const changed = (records) => {
      const now = performance.now(), seen = new Map();
      for (const r of records) {
        if (cursorOnly(r)) continue;
        const node = r.type === "characterData" ? r.target.parentNode : r.target;
        const kind = r.type === "attributes" ? r.attributeName
          : [...r.addedNodes, ...r.removedNodes].every((n) => n.nodeType === Node.TEXT_NODE) ? "text" : "tree";
        const kinds = seen.get(node) ?? new Set();
        if (!kinds.has(kind)) s.log.push([now, node, kind]);
        seen.set(node, kinds.add(kind));
        for (const added of r.addedNodes)
          if (added.nodeType === Node.ELEMENT_NODE)
            for (const el of [added, ...added.querySelectorAll(BUSY)]) if (el.matches(BUSY)) s.busy.set(el, now);
        if (r.attributeName === "aria-busy" && r.target.matches(BUSY)) s.busy.set(r.target, now);
      }
      const old = s.log.findIndex(([t]) => t >= now - LOG_MS);
      s.log.splice(0, old < 0 ? s.log.length : Math.max(old, s.log.length - 5000));
    };
    new MutationObserver(changed).observe(document, { subtree: true, childList: true, characterData: true });
    new MutationObserver(changed).observe(document, { subtree: true, attributeFilter: ["aria-expanded", "aria-selected",
      "aria-checked", "aria-pressed", "aria-current", "aria-hidden", "aria-disabled", "aria-activedescendant", "hidden",
      "open", "disabled", "aria-busy"] });
    // Native checkbox/radio checked is a property, not an attribute. Count its committed change as a reaction;
    // counting every text input would incorrectly finish before debounced search results arrive.
    document.addEventListener("change", event => {
      if (event.target?.matches?.("input[type=checkbox],input[type=radio]")) s.log.push([performance.now(), null, "change"]);
    }, true);
    globalThis.__secondCursorSettle = s;
  }
  const now = performance.now(), at = (ago) => (ago == null ? Infinity : now - ago);
  // A node that changed the same way at least twice while the agent was idle changes on its own (a clock, a carousel,
  // a poll's counter). Its changes are neither the action's reaction nor a reason the page is not yet quiet.
  const idle = new Map();
  for (const [t, node, kind] of s.log)
    if (node && times.idle.some(([from, until]) => t >= at(from) && t < at(until))) {
      const kinds = idle.get(node) ?? new Map();
      idle.set(node, kinds.set(kind, (kinds.get(kind) ?? 0) + 1));
    }
  const counted = s.log.filter(([, node, kind]) => !(idle.get(node)?.get(kind) >= 2));
  // A spinner or busy region the action made (before its read settled) means its result is still coming.
  const busy = [...document.querySelectorAll(BUSY)].some((el) => s.busy.get(el) >= at(times.acted)
    && s.busy.get(el) < at(times.read) && el.getClientRects().length > 0);
  return { quiet: now - (counted.at(-1)?.[0] ?? now - LOG_MS), mutated: counted.some(([, , kind]) => kind !== ""), busy,
    ready: document.readyState };
})(${JSON.stringify(times)})`;

async function settleProbe(tabId, { actedAt, settledAt, idle = [] } = {}) {
  const ago = (at) => (at == null ? null : Date.now() - at);
  const times = { acted: actedAt ? ago(actedAt) : null, read: ago(settledAt),
    idle: idle.map(([from, until]) => [ago(from), ago(until)]) };
  for (let retry = 0; retry < 2; retry++) {
    try {
      let contextId = settleContexts.get(tabId);
      if (contextId == null) {
        const { frameTree } = await cdp(tabId, "Page.getFrameTree");
        ({ executionContextId: contextId } = await cdp(tabId, "Page.createIsolatedWorld", {
          frameId: frameTree.frame.id,
          worldName: SETTLE_WORLD,
        }));
        settleContexts.set(tabId, contextId);
      }
      const { result, exceptionDetails } = await cdp(tabId, "Runtime.evaluate", {
        expression: SETTLE_PROBE(times),
        contextId,
        returnByValue: true,
      });
      if (!exceptionDetails) return result.value;
    } catch {
      // A navigation replaced the document and its world; make a new one.
    }
    settleContexts.delete(tabId);
  }
  return null;
}
chrome.tabs.onRemoved.addListener((tabId) => settleContexts.delete(tabId));

const lastAction = new Map();
chrome.tabs.onRemoved.addListener((tabId) => lastAction.delete(tabId));

// When each tab last began loading a document, and whether it has finished, so a read that follows an action does not
// return the page the action is navigating away from.
const loads = new Map();
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading") loads.set(tabId, { at: Date.now(), done: false });
  else if (info.status === "complete" && loads.has(tabId)) loads.get(tabId).done = true;
});
chrome.tabs.onRemoved.addListener((tabId) => loads.delete(tabId));
// A load the last action started may take this long before the read gives up waiting for it.
const ACTION_LOAD_MS = 10000;

// The app's browserQuietMs, sent with every message. It replaces the default quiet period only while the action's
// requests are tracked: without tracking, that period is all that stands in for a request the read cannot see.
let trackedQuietMs = null;
export function setTrackedQuietMs(ms) {
  trackedQuietMs = Number.isInteger(ms) && ms >= 0 && ms <= 2000 ? ms : null;
}

/**
 * Input that may make the page fetch and render something (a filter, a search, "load more"), or (`load`) a page load.
 * `at` must be when the input was dispatched: a page that re-renders during the action has already reacted, and
 * stamping the end of the action instead makes the next read wait out all of `reactMs` for a reaction that already
 * happened.
 */
export function noteAction(tabId, at = Date.now(), load = false) {
  at = Math.max(at, inputAt.get(tabId) ?? 0);
  // The agent was idle from the read that settled its previous action until this input. Input that no read has
  // settled since (observe=none, a batch, this same input noted twice) keeps the window that preceded it.
  const previous = lastAction.get(tabId);
  const idle = !previous ? {} : previous.settledAt != null ? { idleFrom: previous.settledAt, idleUntil: at }
    : { idleFrom: previous.idleFrom, idleUntil: previous.idleUntil };
  lastAction.set(tabId, { at, load, reactMs: quickReactions.get(tabId), ...idle });
  quickReactions.delete(tabId);
}

/**
 * Waits until the page's content has not changed for `quietMs`, at most `timeoutMs`. Within `reactMs` of an action
 * (or of the load, which counts as one), a page that has not changed since is usually still waiting for a request
 * (search results, a filter, "load more"), so the read waits for the change to start first. While the last action is
 * a load, a page whose network has gone idle has no request left to wait for, so every read stops waiting for one.
 * A document the action started loading counts as such a load, and is waited for until it finishes (at most
 * ACTION_LOAD_MS). Document/XHR/Fetch requests started after input also hold the observation, bounded by that
 * same action deadline. Pre-existing requests do not, nor do the page's own (noteBackgroundRequests). Changes the
 * page made on its own while the agent was idle do not count either, and a spinner or aria-busy region the action
 * made holds the read like a request. Quietness is a readiness hint, not a verified postcondition.
 * The first probe of a document starts its clock, so an idle page costs `quietMs` once, and nothing on later reads.
 */
export async function settled(tabId, { quietMs, timeoutMs = 2000, reactMs = 1200, requestTimeoutMs = ACTION_LOAD_MS } = {}) {
  const began = Date.now();
  const action = lastAction.get(tabId);
  const { at: actedAt = 0, reactMs: quick, idleFrom, idleUntil, settledAt } = action ?? {};
  let { load = false } = action ?? {};
  const reactWithin = Math.min(reactMs, quick ?? reactMs);
  let deadline = began + timeoutMs;
  // Split out the wait for a reaction that has not started from the wait for the page to go quiet.
  let reacting = 0;
  let loading = 0;
  let fetching = 0;
  let busy = 0;
  // Once a read has settled the action, the agent is idle again, so what the page does from then on is its own.
  const idle = [[idleFrom, idleUntil], [settledAt, null]].filter(([from]) => from != null);
  noteBackgroundRequests(tabId, actedAt, idleFrom, idleUntil);
  const requests = () => actionNetworkState(tabId, actedAt, settledAt);
  let network = requests();
  const result = (state, reason) => ({ state, ...(reason ? { reason } : {}),
    network: network.available ? "observed" : "unavailable", pending_requests: network.pending,
    ...(network.inferred ? { body_completion_inferred: network.inferred } : {}),
    failed_requests: network.failed, waited_ms: Date.now() - began });
  try {
    while (Date.now() < deadline) {
      network = requests();
      if (actedAt && (network.pending || network.enabling)) {
        deadline = Math.max(deadline, actedAt + requestTimeoutMs);
        fetching += 50;
        await sleep(50);
        continue;
      }
      const navigation = loads.get(tabId);
      if (navigation && navigation.at >= actedAt) {
        load = true;
        if (!navigation.done && (await chrome.tabs.get(tabId).catch(() => null))?.status === "loading") {
          deadline = Math.max(deadline, navigation.at + ACTION_LOAD_MS);
          loading += 50;
          await sleep(50);
          continue;
        }
        navigation.done = true;
      }
      const state = await settleProbe(tabId, { actedAt, settledAt, idle });
      if (!state) return result("unavailable", "The document could not be observed while settling.");
      // CDP events can arrive while the DOM probe is in flight. Recheck before declaring it ready.
      network = requests();
      if (actedAt && (network.pending || network.enabling)) continue;
      if (actedAt && state.busy) {
        deadline = Math.max(deadline, actedAt + requestTimeoutMs);
        busy += 50;
        await sleep(50);
        continue;
      }
      const sinceAction = Date.now() - actedAt;
      let awaitingReaction = sinceAction < reactWithin && (!state.mutated || state.quiet >= sinceAction);
      // Retain a bounded reaction window for timers/debounces that have not started a request yet.
      if (awaitingReaction && load && (await networkIdle(tabId).catch(() => false))) awaitingReaction = false;
      network = requests();
      if (actedAt && (network.pending || network.enabling)) continue;
      const quietFor = quietMs ?? (network.available && trackedQuietMs != null ? trackedQuietMs : 300);
      if (state.ready === "complete" && state.quiet >= quietFor && network.quiet >= quietFor && !awaitingReaction)
        return network.incomplete
          ? result("unavailable", "Request tracking exceeded its bounded capacity; pending work may remain. Observe the intended postcondition before retrying.")
          : result("quiet");
      const pause = awaitingReaction ? 50 : Math.min(100, Math.max(20, quietFor - state.quiet));
      if (awaitingReaction) reacting += pause;
      await sleep(pause);
    }
    network = requests();
    return result("timed_out", "The readiness deadline expired; the action may still be running. Observe again before retrying a mutation.");
  } finally {
    addTiming(tabId, "settled", Date.now() - began);
    if (reacting) addTiming(tabId, "settled_awaiting_reaction", reacting);
    if (loading) addTiming(tabId, "settled_loading", loading);
    if (fetching) addTiming(tabId, "settled_requests", fetching);
    if (busy) addTiming(tabId, "settled_busy", busy);
    if (action && lastAction.get(tabId) === action && action.settledAt == null) {
      action.settledAt = Date.now();
      // Requests tracked from now on let the next action tell the page's own requests from the ones it causes.
      await trackRequests(tabId).catch(() => {});
    }
  }
}

export async function listTabs(session = null) {
  const tabs = [];
  for (const id of [...(await agentTabSet())]) {
    const owner = (await ownership())[id];
    if (session && owner?.session !== session) continue;
    const tab = await chrome.tabs.get(id).catch(() => null);
    if (tab) tabs.push(isClaim(owner) ? { ...describe(tab), claimed: true } : describe(tab));
    else await updateAgentTabs((set) => set.delete(id));
  }
  return tabs;
}

async function createTab(url, session) {
  const windowId = await targetWindowId();
  await requireSessionInput(session);
  const tab =
    windowId === undefined
      ? (await chrome.windows.create({ url, focused: false })).tabs[0]
      : await chrome.tabs.create({ windowId, url, active: false });
  await updateAgentTabs((set) => set.add(tab.id));
  (await ownership())[tab.id] = { session, created: true, mode: "ephemeral" };
  await saveOwnership();
  await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
  await timed(tab.id, "group", () => addToGroup(tab));
  return tab;
}

export async function openTab(rawUrl, session = "legacy") {
  await requireSessionInput(session);
  const url = normalizeUrl(rawUrl);
  let tab;
  for (let attempt = 0; !tab; attempt++) {
    const began = Date.now();
    const created = await createTab(url, session);
    try {
      await attach(created.id);
      await timed(created.id, "load", () => settleLoad(created.id, async () => {}, { quietMs: 0, timeoutMs: 20000 }));
      tab = created;
    } catch (error) {
      // Chrome gives the first new tab of a site a renderer stuck in a page script (see cdp.js), once: the tab never
      // commits, and a new tab gets a new renderer. A page that committed and then hung would only hang again.
      const committed = (await chrome.tabs.get(created.id).catch(() => null))?.url;
      if (!error.unresponsive || committed) throw error;
      closing.add(created.id);
      await chrome.tabs.remove(created.id).catch(() => {});
      await updateAgentTabs((set) => set.delete(created.id));
      addTiming(created.id, "unresponsive_tab_replaced", Date.now() - began);
      if (attempt) throw error;
    }
  }
  show(tab.id, session);
  noteAction(tab.id, Date.now(), true);
  await settled(tab.id, { quietMs: 500, timeoutMs: 6000, reactMs: 1500 });
  return describe(await chrome.tabs.get(tab.id));
}

export async function navigateTab(tabId, rawUrl) {
  await requireAgentTab(tabId);
  const url = normalizeUrl(rawUrl);
  await noteInput(tabId);
  const at = Date.now();
  await settleLoad(tabId, async () => {
    await requireAgentInput(tabId);
    return chrome.tabs.update(tabId, { url });
  }, {
    quietMs: 1000,
    timeoutMs: 20000,
  });
  noteAction(tabId, at, true);
  await settled(tabId, { quietMs: 500, timeoutMs: 6000, reactMs: 1500 });
  return describe(await chrome.tabs.get(tabId));
}

/** Closes a tab the agent opened. A claimed personal tab is the user's: it is released, never closed. */
export async function closeTab(tabId) {
  await requireAgentInput(tabId);
  if (isClaim((await ownership())[tabId])) {
    await dropTab(tabId);
    return { released: true, closed: false };
  }
  closing.add(tabId);
  await chrome.tabs.remove(tabId);
  await updateAgentTabs((set) => set.delete(tabId));
  return { released: false, closed: true };
}

export async function userTabs() {
  const owned = await agentTabSet();
  return {
    tabs: (await chrome.tabs.query({})).map((t) => ({
      ...describe(t),
      isAgentTab: owned.has(t.id),
      is_agent_tab: owned.has(t.id),
      window_id: t.windowId,
    })),
  };
}
export async function claimTab(tabId, session) {
  await requireSessionInput(session);
  const all = await ownership();
  if (isClaim(all[tabId]) && claimExpired(all[tabId])) await dropTab(tabId);
  const previous = all[tabId];
  if (previous && previous.session !== session)
    throw new BridgeError(
      "not_allowed",
      "Tab already claimed by another session",
    );
  const tab = await chrome.tabs.get(tabId);
  all[tabId] = previous ?? { session, created: false, mode: "ephemeral" };
  if (isClaim(all[tabId])) all[tabId].lastUsed = Date.now();
  await updateAgentTabs((s) => s.add(tabId));
  await saveOwnership();
  if (isClaim(all[tabId]))
    await chrome.alarms.create(CLAIM_SWEEP_ALARM, { periodInMinutes: 1 });
  return { tab: describe(tab) };
}
export async function releaseTab(tabId, session) {
  await requireAgentTab(tabId, session);
  await dropTab(tabId);
  return { released: true };
}
/** Releases the personal tabs a session claimed; tabs the agent opened stay open and owned. */
export async function endTurn(session) {
  let released = 0;
  for (const [id, entry] of Object.entries(await ownership()))
    if (entry.session === session && isClaim(entry)) {
      await dropTab(Number(id));
      released++;
    }
  return { released };
}
export async function retainTab(tabId, session, mode) {
  if (!["ephemeral", "handoff", "deliverable"].includes(mode))
    throw new BridgeError("invalid_argument", "Invalid retention mode");
  await requireAgentTab(tabId, session);
  (await ownership())[tabId].mode = mode;
  await saveOwnership();
  return { mode };
}
export async function closeSession(session) {
  for (const [id, entry] of Object.entries(await ownership()))
    if (entry.session === session) {
      if (entry.created && entry.mode === "ephemeral") {
        closing.add(Number(id));
        await chrome.tabs.remove(Number(id)).catch(() => {});
      }
      await releaseTab(Number(id), session).catch(async () => {
        delete (await ownership())[id];
        await updateAgentTabs((s) => s.delete(Number(id)));
      });
    }
  await saveOwnership();
  reports.delete(session);
  return { closed: true };
}
