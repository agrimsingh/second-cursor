import { attach, cdp, networkIdle } from "./cdp.js";
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
export function noteInput(tabId) {
  inputAt.set(tabId, Date.now());
  quickReactions.delete(tabId);
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

// Evaluated in its own isolated world, so the page never sees the observer.
const SETTLE_WORLD = "SecondCursorSettle";
const settleContexts = new Map();
const SETTLE_PROBE = `(() => {
  let s = globalThis.__secondCursorSettle;
  if (!s || s.doc !== document) {
    s = { doc: document, last: performance.now(), mutated: false };
    // Most attribute changes are left out: animations and timers change them without the content changing. The ones
    // that say what an element is or shows (a menu expanding, a tab selected, a panel unhidden) are how many pages
    // react to a click, with no node added. The agent's own cursor (cursor.js), added when the agent starts work on
    // the page, never counts.
    const cursorOnly = (r) => r.target.id === "second-cursor-overlay" || (r.addedNodes.length + r.removedNodes.length > 0
      && [...r.addedNodes, ...r.removedNodes].every((n) => n.id === "second-cursor-overlay"));
    const changed = (records) => {
      if (records.every(cursorOnly)) return;
      s.last = performance.now();
      s.mutated = true;
    };
    new MutationObserver(changed).observe(document, { subtree: true, childList: true, characterData: true });
    new MutationObserver(changed).observe(document, { subtree: true, attributeFilter: ["aria-expanded", "aria-selected",
      "aria-checked", "aria-pressed", "aria-current", "aria-hidden", "aria-disabled", "aria-activedescendant", "hidden",
      "open", "disabled"] });
    // Native checkbox/radio checked is a property, not an attribute. Count its committed change as a reaction;
    // counting every text input would incorrectly finish before debounced search results arrive.
    document.addEventListener("change", event => {
      if (!event.target?.matches?.("input[type=checkbox],input[type=radio]")) return;
      s.last = performance.now();
      s.mutated = true;
    }, true);
    globalThis.__secondCursorSettle = s;
  }
  return { quiet: performance.now() - s.last, mutated: s.mutated, ready: document.readyState };
})()`;

async function settleProbe(tabId) {
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
        expression: SETTLE_PROBE,
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

/**
 * Input that may make the page fetch and render something (a filter, a search, "load more"), or (`load`) a page load.
 * `at` must be when the input was dispatched: a page that re-renders during the action has already reacted, and
 * stamping the end of the action instead makes the next read wait out all of `reactMs` for a reaction that already
 * happened.
 */
export function noteAction(tabId, at = Date.now(), load = false) {
  lastAction.set(tabId, { at, load, reactMs: quickReactions.get(tabId) });
  quickReactions.delete(tabId);
}

/**
 * Waits until the page's content has not changed for `quietMs`, at most `timeoutMs`. Within `reactMs` of an action
 * (or of the load, which counts as one), a page that has not changed since is usually still waiting for a request
 * (search results, a filter, "load more"), so the read waits for the change to start first. While the last action is
 * a load, a page whose network has gone idle has no request left to wait for, so every read stops waiting for one.
 * A document the action started loading counts as such a load, and is waited for until it finishes (at most
 * ACTION_LOAD_MS), however long that takes.
 * The first probe of a document starts its clock, so an idle page costs `quietMs` once, and nothing on later reads.
 */
export async function settled(tabId, { quietMs = 300, timeoutMs = 2000, reactMs = 1200 } = {}) {
  const began = Date.now();
  const { at: actedAt = 0, reactMs: quick } = lastAction.get(tabId) ?? {};
  let { load = false } = lastAction.get(tabId) ?? {};
  const reactWithin = Math.min(reactMs, quick ?? reactMs);
  let deadline = began + timeoutMs;
  // Split out the wait for a reaction that has not started from the wait for the page to go quiet.
  let reacting = 0;
  let loading = 0;
  try {
    while (Date.now() < deadline) {
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
      const state = await settleProbe(tabId);
      if (!state) return;
      const sinceAction = Date.now() - actedAt;
      let awaitingReaction = sinceAction < reactWithin && (!state.mutated || state.quiet >= sinceAction);
      // The idle signal covers the page's first load only, so actions keep the fixed window.
      if (awaitingReaction && load && (await networkIdle(tabId).catch(() => false))) awaitingReaction = false;
      if (state.ready === "complete" && state.quiet >= quietMs && !awaitingReaction) return;
      const pause = awaitingReaction ? 50 : Math.min(100, Math.max(20, quietMs - state.quiet));
      if (awaitingReaction) reacting += pause;
      await sleep(pause);
    }
  } finally {
    addTiming(tabId, "settled", Date.now() - began);
    if (reacting) addTiming(tabId, "settled_awaiting_reaction", reacting);
    if (loading) addTiming(tabId, "settled_loading", loading);
  }
}

export async function listTabs(session = null) {
  const tabs = [];
  for (const id of [...(await agentTabSet())]) {
    if (session && (await ownership())[id]?.session !== session) continue;
    const tab = await chrome.tabs.get(id).catch(() => null);
    if (tab) tabs.push(describe(tab));
    else await updateAgentTabs((set) => set.delete(id));
  }
  return tabs;
}

export async function openTab(rawUrl, session = "legacy") {
  await requireSessionInput(session);
  const url = normalizeUrl(rawUrl);
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
  await timed(tab.id, "load", () => settleAround(tab.id, async () => {}, { quietMs: 0, timeoutMs: 20000 }));
  await attach(tab.id);
  show(tab.id, session);
  noteAction(tab.id, Date.now(), true);
  await settled(tab.id, { quietMs: 500, timeoutMs: 6000, reactMs: 1500 });
  return describe(await chrome.tabs.get(tab.id));
}

export async function navigateTab(tabId, rawUrl) {
  await requireAgentTab(tabId);
  const url = normalizeUrl(rawUrl);
  await settleAround(tabId, async () => {
    await requireAgentInput(tabId);
    return chrome.tabs.update(tabId, { url });
  }, {
    quietMs: 1000,
    timeoutMs: 20000,
  });
  noteAction(tabId, Date.now(), true);
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
