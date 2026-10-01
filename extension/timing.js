// Milliseconds per phase of the call running on each tab, collected only when the app asks (recordTimings). Calls on
// one tab run one at a time (background.js enqueue), so the tab id identifies the call. Calls without a tab (opening
// one) collect under "browser", which also catches phases timed under the tab they create.
const active = new Map();

export function begin(key) {
  const phases = {};
  active.set(key, phases);
  return phases;
}

export function end(key, phases) {
  if (active.get(key) === phases) active.delete(key);
}

export function add(tabId, phase, ms) {
  const phases = active.get(tabId) ?? active.get("browser");
  if (phases) phases[phase] = (phases[phase] ?? 0) + ms;
}

export async function timed(tabId, phase, work) {
  if (!active.has(tabId) && !active.has("browser")) return work();
  const started = performance.now();
  try {
    return await work();
  } finally {
    add(tabId, phase, performance.now() - started);
  }
}
