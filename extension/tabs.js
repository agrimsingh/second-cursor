import { BridgeError, sleep } from "./errors.js";

const GROUP_TITLE = "Second Cursor";

let agentTabsPromise = null;
let ownershipPromise = null;
const ownership = () =>
  (ownershipPromise ??= chrome.storage.session
    .get("ownership")
    .then((x) => x.ownership ?? {}));
const saveOwnership = async () =>
  chrome.storage.session.set({ ownership: await ownership() });
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

chrome.tabs.onRemoved.addListener((tabId) =>
  updateAgentTabs((set) => set.delete(tabId)),
);

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
      `Tab ${tabId} is not a Second Cursor tab; only tabs opened with browser_open can be driven`,
    );
  }
  const owner = (await ownership())[tabId];
  if (session && owner && owner.session !== session)
    throw new BridgeError("not_allowed", "Tab belongs to another session");
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
    const result = await action();
    const began = Date.now();
    while (Date.now() - began < timeoutMs) {
      const tab = await chrome.tabs.get(tabId).catch(() => null);
      if (!tab) break;
      if (tab.status === "loading") sawLoading = true;
      else if (sawLoading || Date.now() - began > quietMs) break;
      await sleep(50);
    }
    return result;
  } finally {
    chrome.tabs.onUpdated.removeListener(listener);
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
  const url = normalizeUrl(rawUrl);
  const windowId = await targetWindowId();
  const tab =
    windowId === undefined
      ? (await chrome.windows.create({ url, focused: false })).tabs[0]
      : await chrome.tabs.create({ windowId, url, active: false });
  await updateAgentTabs((set) => set.add(tab.id));
  (await ownership())[tab.id] = { session, created: true, mode: "ephemeral" };
  await saveOwnership();
  await chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
  await addToGroup(tab);
  await settleAround(tab.id, async () => {}, { quietMs: 0, timeoutMs: 20000 });
  return describe(await chrome.tabs.get(tab.id));
}

export async function navigateTab(tabId, rawUrl) {
  await requireAgentTab(tabId);
  const url = normalizeUrl(rawUrl);
  await settleAround(tabId, () => chrome.tabs.update(tabId, { url }), {
    quietMs: 1000,
    timeoutMs: 20000,
  });
  return describe(await chrome.tabs.get(tabId));
}

export async function closeTab(tabId) {
  await requireAgentTab(tabId);
  await chrome.tabs.remove(tabId);
  await updateAgentTabs((set) => set.delete(tabId));
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
  const all = await ownership(),
    previous = all[tabId];
  if (previous && previous.session !== session)
    throw new BridgeError(
      "not_allowed",
      "Tab already claimed by another session",
    );
  const tab = await chrome.tabs.get(tabId);
  all[tabId] = previous ?? { session, created: false, mode: "ephemeral" };
  await updateAgentTabs((s) => s.add(tabId));
  await saveOwnership();
  return { tab: describe(tab) };
}
export async function releaseTab(tabId, session) {
  await requireAgentTab(tabId, session);
  await chrome.debugger.detach({ tabId }).catch(() => {});
  delete (await ownership())[tabId];
  await updateAgentTabs((s) => s.delete(tabId));
  await saveOwnership();
  return { released: true };
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
      if (entry.created && entry.mode === "ephemeral")
        await chrome.tabs.remove(Number(id)).catch(() => {});
      await releaseTab(Number(id), session).catch(async () => {
        delete (await ownership())[id];
        await updateAgentTabs((s) => s.delete(Number(id)));
      });
    }
  await saveOwnership();
  return { closed: true };
}
