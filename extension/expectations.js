import { BridgeError, sleep } from "./errors.js";
import { conditionSnapshot } from "./snapshot.js";
import { requireAgentTab, requireSessionInput, sessionControlGeneration } from "./tabs.js";
import { openDialog } from "./extended.js";

const fields = new Set(["text", "ref", "value", "gone", "timeout_ms"]);
const records = new Map();
const MAX_PENDING = 128;
const details = {
  verified: "The requested postcondition became true after input was dispatched.",
  preexisting: "The postcondition already held before input; this does not prove the input caused it.",
  timeout: "The postcondition was not observed before its deadline. Input was already dispatched; inspect the result before retrying.",
  unavailable: "The complete public page state could not be checked. Input may have taken effect; inspect before retrying.",
  target_changed: "The referenced element or its document changed. Input may have taken effect; take a fresh observation before continuing.",
  control_changed: "The tab's ownership or task control changed; the postcondition was not verified.",
  dialog_open: "A JavaScript dialog interrupted verification. Handle the dialog, then inspect the result before retrying input.",
  batch_incomplete: "The batch did not complete every step; its requested postcondition was not verified.",
  token_unavailable: "The batch verification token is missing, expired, or belongs to another target; inspect the result before retrying.",
  deadline: "The call deadline expired during verification. Input may have taken effect; inspect before retrying.",
};
class CheckStopped extends Error {
  constructor(reason) { super(reason); this.reason = reason; }
}

export function parseExpectation(raw) {
  if (raw == null) return null;
  if (!raw || typeof raw !== "object" || Array.isArray(raw))
    throw new BridgeError("invalid_argument", "expect must be an object");
  if (Object.keys(raw).some(key => !fields.has(key)))
    throw new BridgeError("invalid_argument", "expect accepts only text, ref, value, gone and timeout_ms");
  for (const key of ["text", "ref", "value"])
    if (raw[key] !== undefined && typeof raw[key] !== "string")
      throw new BridgeError("invalid_argument", `expect.${key} must be a string`);
  if (raw.text === "" || raw.ref === "")
    throw new BridgeError("invalid_argument", "expect.text and expect.ref must not be empty");
  if ((raw.ref === undefined) !== (raw.value === undefined))
    throw new BridgeError("invalid_argument", "expect.ref and expect.value must be supplied together");
  if (raw.text === undefined && raw.ref === undefined)
    throw new BridgeError("invalid_argument", "expect requires text, or ref with value");
  if (raw.ref !== undefined && !/^(?:f\d+)?e\d+$/.test(raw.ref))
    throw new BridgeError("invalid_argument", "expect.ref must be a ref from browser_snapshot");
  if (raw.gone !== undefined && typeof raw.gone !== "boolean")
    throw new BridgeError("invalid_argument", "expect.gone must be a boolean");
  if (raw.timeout_ms !== undefined && (!Number.isInteger(raw.timeout_ms) || raw.timeout_ms < 0 || raw.timeout_ms > 10000))
    throw new BridgeError("invalid_argument", "expect.timeout_ms must be an integer from 0 to 10000");
  return { ...raw, gone: raw.gone ?? false, timeout_ms: raw.timeout_ms ?? 2000 };
}

export const failedExpectation = (reason, waited = 0) => ({ status: "failed", reason,
  waited_ms: Math.max(0, Math.round(waited)), detail: details[reason] ?? details.unavailable });
export const expectationOutcome = expect => ({
  state: expect.status === "verified" ? "verified" : expect.status === "preexisting" ? "dispatched" : "uncertain",
  verified: expect.status === "verified", verification: "postcondition", reason: expect.detail,
});

function sweep() {
  for (const [token, record] of records)
    if (record.expires <= Date.now() || record.cancelled) { record.cancelled = true; records.delete(token); }
}
export function cancelExpectations(session, tabId) {
  for (const [token, record] of records) {
    if ((session == null || record.session === session) && (tabId == null || record.tabId === tabId)) {
      record.cancelled = true;
      records.delete(token);
    }
  }
}
chrome.tabs.onRemoved.addListener(tabId => cancelExpectations(null, tabId));

async function guard(record) {
  if (record.cancelled) throw new CheckStopped("control_changed");
  if (Date.now() >= record.expires) throw new CheckStopped("deadline");
  if (openDialog(record.tabId)) throw new CheckStopped("dialog_open");
  try {
    await requireAgentTab(record.tabId, record.session);
    await requireSessionInput(record.session);
    if (record.generation !== await sessionControlGeneration(record.session)) throw Error("control changed");
  } catch { throw new CheckStopped("control_changed"); }
  if (record.cancelled) throw new CheckStopped("control_changed");
}

// Poll the control lane even when a renderer's CDP read is hung. The read's own guard prevents it issuing any
// more commands after cancellation; the already-issued read is harmless and its result is never adopted later.
async function sample(record, until = record.expires) {
  await guard(record);
  let finished = false, value, error;
  const checking = conditionSnapshot(record.tabId, () => guard(record)).then(
    result => { value = result; finished = true; }, failure => { error = failure; finished = true; });
  while (!finished) {
    await Promise.race([checking, sleep(25)]);
    await guard(record);
    if (!finished && Date.now() >= until) { record.cancelled = true; throw new CheckStopped("unavailable"); }
  }
  if (error) throw error;
  const condition = record.condition;
  let matched = true;
  if (condition.text !== undefined)
    matched = value.elements.some(node => !node.secret &&
      (node.name.includes(condition.text) || node.value?.includes(condition.text)));
  if (condition.ref !== undefined) {
    if (record.document !== undefined && record.document !== value.document) throw new CheckStopped("target_changed");
    const node = value.elements.find(node => node.ref === condition.ref);
    if (!node) throw new CheckStopped("target_changed");
    if (node.secret || node.value == null) throw new CheckStopped("unavailable");
    record.document ??= value.document;
    matched = matched && node.value === condition.value;
  }
  return condition.gone ? !matched : matched;
}

export async function prepareExpectation(params) {
  const condition = parseExpectation(params.expect);
  if (!condition) return null;
  const record = { condition, tabId: params.tabId, session: params.session_id ?? "legacy",
    generation: await sessionControlGeneration(params.session_id ?? "legacy"),
    expires: Math.min(Number.isFinite(params.deadline) ? params.deadline : Infinity, Date.now() + 60000), cancelled: false };
  try {
    record.before = await sample(record, Math.min(record.expires, Date.now() + 3000));
    await guard(record);
  } catch (error) {
    record.cancelled = true;
    throw new BridgeError("invalid_argument", `expect could not be checked before input; input was not sent (${error.reason ?? "unavailable"})`);
  }
  return record;
}

export async function waitExpectation(record, { complete = true } = {}) {
  const began = Date.now();
  if (!complete) { record.cancelled = true; return failedExpectation("batch_incomplete"); }
  const until = Math.min(record.expires, began + record.condition.timeout_ms);
  let lastUnavailable = false;
  try {
    do {
      let met = false;
      try {
        met = await sample(record, Math.min(record.expires, Math.max(until, Date.now() + 500)));
        lastUnavailable = false;
      } catch (error) {
        // A same-tab navigation can briefly have no AX root or privacy facts. Only a complete later sample may
        // satisfy the condition; missing coverage never means gone. Ownership, dialogs and pinned-ref changes
        // are terminal, and a cancelled hung read must not be restarted.
        if (record.cancelled || (error.reason && error.reason !== "unavailable")) throw error;
        lastUnavailable = true;
      }
      if (met) {
        const status = record.before ? "preexisting" : "verified";
        return { status, reason: status, waited_ms: Date.now() - began, detail: details[status] };
      }
      if (Date.now() >= until) break;
      await sleep(Math.min(50, until - Date.now()));
    } while (Date.now() <= until);
    return failedExpectation(lastUnavailable ? "unavailable" : "timeout", Date.now() - began);
  } catch (error) {
    return failedExpectation(error.reason ?? "unavailable", Date.now() - began);
  } finally { record.cancelled = true; }
}

export async function prepareBatchExpectation(params) {
  sweep();
  if (records.size >= MAX_PENDING) throw new BridgeError("failed", "Too many pending browser batch postconditions");
  const record = await prepareExpectation(params);
  if (!record) throw new BridgeError("invalid_argument", "expect is required");
  const token = crypto.randomUUID();
  records.set(token, record);
  return { expect_token: token };
}
export async function finishBatchExpectation(params) {
  sweep();
  const record = records.get(params.expect_token);
  if (!record || record.tabId !== params.tabId || record.session !== (params.session_id ?? "legacy"))
    return failedExpectation("token_unavailable");
  records.delete(params.expect_token);
  return waitExpectation(record, { complete: params.complete === true });
}
