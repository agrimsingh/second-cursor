import { withTimeout } from "./errors.js";

// The agent's cursor, drawn inside agent tabs. Keep the design in step with the native overlay
// (Sources/SecondCursor/AgentCursorOverlay.swift). extended.js and exports.js skip its host element by its
// id, "second-cursor-overlay".
const WORLD = "SecondCursorOverlay";
const IDLE_MS = 60000;
// Pointer input waits for the cursor to arrive, at most this long.
const WAIT_MS = 1500;
// A caption arrives just before its action; the action, not the caption, decides where the cursor appears.
const CAPTION_GRACE_MS = 250;

const tabs = new Map();

function state(tabId) {
  let s = tabs.get(tabId);
  if (!s) {
    s = {
      x: null,
      y: null,
      caption: "",
      visible: false,
      lastUpdate: 0,
      session: null,
      concealed: 0,
      contextId: null,
      chain: Promise.resolve(),
      idle: null,
      pending: null,
    };
    tabs.set(tabId, s);
  }
  return s;
}

function stopTimers(s) {
  clearTimeout(s.idle);
  clearTimeout(s.pending);
  s.idle = s.pending = null;
}

chrome.tabs.onRemoved.addListener((tabId) => {
  const s = tabs.get(tabId);
  if (s) stopTimers(s);
  tabs.delete(tabId);
});

chrome.debugger.onDetach.addListener(({ tabId }) => {
  const s = tabs.get(tabId);
  if (!s) return;
  stopTimers(s);
  s.visible = false;
  s.contextId = null;
});

chrome.debugger.onEvent.addListener(({ tabId, sessionId }, method, p) => {
  const s = tabs.get(tabId);
  if (!s || sessionId) return;
  if (
    method === "Runtime.executionContextsCleared" ||
    (method === "Runtime.executionContextDestroyed" &&
      p.executionContextId === s.contextId)
  )
    s.contextId = null;
  const mainFrame = method === "Page.frameNavigated" && !p.frame.parentId;
  if (mainFrame) s.contextId = null;
  // A new document has no overlay; put the cursor back where it was, without animating.
  if (
    (mainFrame || method === "Page.loadEventFired") &&
    s.visible &&
    Date.now() - s.lastUpdate < IDLE_MS
  )
    send(tabId, () => command(s, { instant: true }));
});

const command = (s, extra = {}) => ({
  op: "sync",
  x: s.x,
  y: s.y,
  caption: s.caption,
  concealed: s.concealed > 0,
  idle_ms: IDLE_MS - (Date.now() - s.lastUpdate),
  ...extra,
});

// Overlay updates run in order per tab. Resolves with the update's result, or null if it failed.
function send(tabId, make) {
  const s = state(tabId);
  const update = s.chain.then(() =>
    withTimeout(
      Promise.resolve(make()).then((c) => c && evaluate(tabId, s, c)),
      2000,
      "overlay update timed out",
    ).catch(() => null),
  );
  s.chain = update.then((end) => {
    if (end) ({ x: s.x, y: s.y } = end);
  });
  return update;
}

// Outside the update chain, so a newer move can go through and cut this wait short.
function arrival(tabId, contextId) {
  return chrome.debugger
    .sendCommand({ tabId }, "Runtime.evaluate", {
      expression: "globalThis.__secondCursorOverlay?.arrival",
      contextId,
      awaitPromise: true,
    })
    .catch(() => {});
}

// Never attaches the debugger itself: a tab the actions have not attached to has no cursor to draw.
async function evaluate(tabId, s, c) {
  const target = { tabId };
  const expression = `(${overlayProgram})(${JSON.stringify(c)})`;
  for (let retry = 0; ; retry++) {
    try {
      if (s.contextId == null) {
        const { frameTree } = await chrome.debugger.sendCommand(
          target,
          "Page.getFrameTree",
        );
        s.contextId = (
          await chrome.debugger.sendCommand(target, "Page.createIsolatedWorld", {
            frameId: frameTree.frame.id,
            worldName: WORLD,
          })
        ).executionContextId;
      }
      const { result, exceptionDetails } = await chrome.debugger.sendCommand(
        target,
        "Runtime.evaluate",
        { expression, contextId: s.contextId, returnByValue: true },
      );
      return exceptionDetails ? null : result.value;
    } catch (e) {
      s.contextId = null;
      if (retry || !/context/i.test(e.message)) throw e;
    }
  }
}

function sync(tabId, target, extra, wait = false) {
  const s = state(tabId);
  stopTimers(s);
  s.visible = true;
  s.lastUpdate = Date.now();
  s.idle = setTimeout(() => hide(tabId), IDLE_MS);
  const update = send(tabId, async () => {
    const point =
      typeof target === "function" ? await target().catch(() => null) : target;
    if (!s.visible) return null;
    return command(s, {
      ...(point ? { x: point.x, y: point.y } : {}),
      ...extra,
      wait,
    });
  });
  if (!wait) return update;
  return withTimeout(
    update.then((end) => end && s.contextId != null && arrival(tabId, s.contextId)),
    WAIT_MS,
    "",
  ).catch(() => {});
}

function hide(tabId, now = false) {
  const s = tabs.get(tabId);
  if (!s) return Promise.resolve();
  stopTimers(s);
  const shown = s.visible;
  s.visible = false;
  return shown || now ? send(tabId, () => ({ op: "hide", now })) : s.chain;
}

/** `caption` is one line; "" clears it. Shows the cursor only if no action follows. */
export function setCaption(tabId, caption, session) {
  const s = state(tabId);
  s.session = session ?? s.session;
  s.caption = typeof caption === "string" ? caption : "";
  clearTimeout(s.pending);
  s.pending = null;
  if (s.visible) sync(tabId, null);
  else if (s.caption)
    s.pending = setTimeout(() => sync(tabId, null), CAPTION_GRACE_MS);
}

/**
 * Moves the cursor to a viewport point. The promise resolves once a person watching the tab would see the cursor
 * arrive (at once in a hidden tab), so pointer input can follow it. A `target()` function is resolved later, and
 * nothing waits for it.
 */
export function pointTo(tabId, target, clicks = 0) {
  return sync(
    tabId,
    target,
    clicks ? { click: clicks } : {},
    typeof target !== "function",
  );
}

/** Resolves when the cursor reaches `from`; it then slides to `to` alongside the drag. */
export function dragTo(tabId, from, to) {
  return sync(tabId, from, { then: to }, true);
}

/** An action without a target: the cursor shows where it rests, or appears at 58% x 55% of the viewport. */
export function show(tabId) {
  sync(tabId, null);
}

/** Runs `capture` with the cursor hidden, so the agent never sees its own cursor. */
export async function concealDuring(tabId, capture) {
  const s = tabs.get(tabId);
  if (!s?.visible && !s?.pending) return capture();
  const conceal = () => ({ op: "conceal", concealed: s.concealed > 0 });
  s.concealed++;
  try {
    await withTimeout(send(tabId, conceal), 300, "").catch(() => {});
    return await capture();
  } finally {
    s.concealed--;
    send(tabId, conceal);
  }
}

/** Fades out the cursors a session drew; one with no known session goes with any session's turn. */
export async function hideSession(session) {
  const hiding = [...tabs]
    .filter(
      ([, s]) => (s.visible || s.pending) && (s.session ?? session) === session,
    )
    .map(([tabId]) => hide(tabId));
  await withTimeout(Promise.all(hiding), 500, "").catch(() => {});
}

/** Removes the cursor at once, before the debugger detaches and can no longer reach the page. */
export async function removeCursor(tabId) {
  if (tabs.has(tabId))
    await withTimeout(hide(tabId, true), 500, "").catch(() => {});
}

// Evaluated in the page's "SecondCursorOverlay" isolated world; its live objects stay on that world's
// globalThis, so page scripts see only the host element, and never what its closed shadow root holds.
function overlayProgram(c) {
  const ID = "second-cursor-overlay";
  let o = globalThis.__secondCursorOverlay;
  if (o && o.doc !== document) o = null;
  const found = document.getElementById(ID);
  // Drawn by another world (a restarted extension) that this one cannot drive: rebuild from `c`.
  if (found && found !== o?.host) found.remove();
  if (c.op !== "sync") {
    if (!o) return null;
    if (c.op === "conceal") o.conceal(c.concealed);
    else o.vanish(c.now);
    return o.end();
  }
  if (!document.documentElement) {
    document.addEventListener("DOMContentLoaded", () => overlayProgram(c), {
      once: true,
    });
    return null;
  }
  return (o ?? build()).apply(c);

  function build() {
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const host = document.createElement(ID);
    host.id = ID;
    host.setAttribute("aria-hidden", "true");
    for (const [name, value] of [
      ["all", "initial"],
      ["position", "fixed"],
      ["inset", "0"],
      ["z-index", "2147483647"],
      ["pointer-events", "none"],
      ["contain", "layout style"],
    ])
      host.style.setProperty(name, value, "important");
    const shadow = host.attachShadow({ mode: "closed" });
    // Constructed stylesheets are exempt from the page's style-src CSP; a <style> element is not.
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(`
      .root { position: absolute; inset: 0; opacity: 0; pointer-events: none; }
      .cursor { position: absolute; left: 0; top: 0; width: 0; height: 0; will-change: transform; }
      .breath { position: absolute; left: -8px; top: -8px; width: 32px; height: 32px; border-radius: 50%;
        background: radial-gradient(rgba(245,78,0,0.5), rgba(245,78,0,0) 70%); opacity: 0; transform: scale(0.8); }
      .ring { position: absolute; left: -22px; top: -22px; width: 44px; height: 44px; box-sizing: border-box;
        border: 2.5px solid #f54e00; border-radius: 50%; opacity: 0; }
      .pointer, .tilt { position: absolute; left: -3px; top: -3px; width: 24px; height: 24px; transform-origin: 3px 3px; }
      .tilt { left: 0; top: 0; }
      svg { display: block; overflow: visible;
        filter: drop-shadow(0 0 5px rgba(245,78,0,0.85)) drop-shadow(0 0 14px rgba(245,78,0,0.45)); }
      .pill { position: absolute; left: 0; top: 0; box-sizing: border-box; max-width: 320px; padding: 5px 9px;
        border-radius: 8px; background: rgba(20,18,11,0.94); color: #edecec;
        font: 500 12px/16px -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; opacity: 0;
        box-shadow: 0 2px 10px rgba(0,0,0,0.25), 0 0 0 1px rgba(255,255,255,0.08); }
      .caret { display: none; width: 1.5px; height: 13px; margin-left: 1px; vertical-align: -2px; background: currentColor; }
      .typing .caret { display: inline-block; }
      .resting .breath { animation: breathe 1.4s ease-in-out infinite alternate; }
      .resting .tilt { animation: wobble 3.6s ease-in-out 1.2s infinite; }
      @keyframes breathe { from { opacity: 0.15; transform: scale(0.8); } to { opacity: 0.35; transform: scale(1); } }
      @keyframes wobble { 0% { transform: rotate(0deg); } 8% { transform: rotate(9deg); } 17% { transform: rotate(-7deg); }
        26% { transform: rotate(5deg); } 33% { transform: rotate(-3deg); } 39%, 100% { transform: rotate(0deg); } }
      @media (prefers-reduced-motion: reduce) { .resting .breath, .resting .tilt { animation: none; } }
      @media print { :host, .root { display: none !important; } }
    `);
    shadow.adoptedStyleSheets = [sheet];
    const add = (parent, className, tag = "div") => {
      const e = document.createElement(tag);
      e.className = className;
      parent.append(e);
      return e;
    };
    const root = add(shadow, "root");
    const cursor = add(root, "cursor");
    add(cursor, "breath");
    const ring = add(cursor, "ring");
    const pointer = add(cursor, "pointer");
    const tilt = add(pointer, "tilt");
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    const path = document.createElementNS(NS, "path");
    for (const [el, attrs] of [
      [svg, { width: 24, height: 24, viewBox: "0 0 24 24" }],
      [
        path,
        {
          d: "M 3 3 L 21 10.2 L 12.6 12.6 L 10.2 21 Z",
          fill: "#14120b",
          stroke: "#ffffff",
          "stroke-width": 1.5,
          "stroke-linejoin": "round",
        },
      ],
    ])
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.append(path);
    tilt.append(svg);
    const pill = add(cursor, "pill");
    const text = add(pill, "text", "span");
    add(pill, "caret", "span");
    document.documentElement.append(host);

    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
    const ease = (u) => (u < 0.5 ? 2 * u * u : 1 - 2 * (1 - u) * (1 - u));
    const now = () => performance.now();
    const viewport = () => ({ w: innerWidth, h: innerHeight });
    const o = {
      doc: document,
      host,
      pos: null,
      to: null,
      from: null,
      ctrl: null,
      kind: "spring",
      p: 1,
      v: 0,
      r: 0.2,
      dur: 0,
      moving: false,
      arrived: true,
      queue: [],
      speedX: 0,
      lean: 0,
      vis: 0,
      visV: 0,
      leaving: null,
      leaveFrom: 1,
      clicks: 0,
      clickAt: -1e9,
      clickDur: 0.32,
      caption: "",
      cap: null,
      pill: 0,
      pillW: 0,
      pillH: 0,
      last: now(),
      raf: 0,
      idle: 0,
    };

    function aim(target, slide = false) {
      const from = { ...o.pos },
        d = Math.hypot(target.x - from.x, target.y - from.y);
      Object.assign(o, { from, to: target, ctrl: null, p: 0, v: 0, arrived: false });
      if (d < 0.5) {
        Object.assign(o, { p: 1, moving: false, pos: { ...target } });
        return arrive();
      }
      o.moving = true;
      if (reduced) Object.assign(o, { kind: "linear", dur: 0.12 });
      else if (slide) Object.assign(o, { kind: "slide", dur: 0.25 });
      else if (d < 200) Object.assign(o, { kind: "spring", r: 0.2 });
      else {
        Object.assign(o, { kind: "spring", r: clamp(0.28 + d / 4000, 0.28, 0.5) });
        // A quarter of the distance off the midpoint, on the side that stays on screen, bowing upward if both do.
        const { w, h } = viewport();
        const mid = { x: (from.x + target.x) / 2, y: (from.y + target.y) / 2 };
        const n = { x: -(target.y - from.y) / d, y: (target.x - from.x) / d };
        o.ctrl =
          [1, -1]
            .map((side) => ({ x: mid.x + n.x * d * 0.25 * side, y: mid.y + n.y * d * 0.25 * side }))
            .sort((a, b) => a.y - b.y)
            .find((q) => q.x >= 8 && q.y >= 8 && q.x <= w - 8 && q.y <= h - 8) ?? null;
      }
    }

    function along() {
      const { from: a, to: b, ctrl: q, p } = o;
      const t = o.kind === "slide" ? ease(p) : p;
      if (!q) return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
      // Past the end, an overshoot continues along the curve's end tangent.
      if (t > 1) return { x: b.x + 2 * (b.x - q.x) * (t - 1), y: b.y + 2 * (b.y - q.y) * (t - 1) };
      const u = 1 - t;
      return {
        x: u * u * a.x + 2 * u * t * q.x + t * t * b.x,
        y: u * u * a.y + 2 * u * t * q.y + t * t * b.y,
      };
    }

    function arrive() {
      o.arrived = true;
      o.release();
      if (o.clicks) {
        o.clickDur = o.clicks > 1 ? 0.42 : 0.32;
        o.clickAt = now();
        o.clicks = 0;
      }
      if (o.queue.length) aim(o.queue.shift(), true);
    }

    function spring(value, velocity, goal, response, dt) {
      const w = (2 * Math.PI) / response;
      for (let left = dt; left > 1e-9; left -= 1 / 240) {
        const h = Math.min(left, 1 / 240);
        velocity += (-w * w * (value - goal) - 2 * 0.86 * w * velocity) * h;
        value += velocity * h;
      }
      return [value, velocity];
    }

    function setCaption(next, instant) {
      if (next === o.caption) return;
      o.caption = next;
      if (!next) {
        o.cap = o.cap || o.pill > 0 ? { clearing: now(), from: o.pill } : null;
        if (!o.cap) text.textContent = "";
        return;
      }
      text.textContent = next;
      pill.classList.remove("typing");
      pill.style.width = "";
      // The pill takes its final width at once, so it does not grow while the text types out.
      // Rounded down, a fraction of a pixel short would cut the text off with an ellipsis.
      const box = pill.getBoundingClientRect();
      o.pillW = Math.ceil(box.width);
      o.pillH = Math.ceil(box.height);
      pill.style.width = `${o.pillW}px`;
      const chars = Array.from(next);
      const typing = !instant && !reduced && chars.length > 1;
      o.cap = { chars, t0: now(), dur: Math.min(400, 18 * chars.length), typing, from: instant ? 1 : o.pill };
      if (typing) {
        text.textContent = "";
        pill.classList.add("typing");
      }
    }

    function stepCaption(t) {
      const cap = o.cap;
      if (!cap) return;
      if (cap.clearing != null) {
        const u = (t - cap.clearing) / 150;
        o.pill = cap.from * (1 - clamp(u, 0, 1));
        if (u >= 1) {
          text.textContent = "";
          pill.style.width = "";
          o.cap = null;
        }
        return;
      }
      o.pill = cap.from + (1 - cap.from) * clamp((t - cap.t0) / 120, 0, 1);
      if (!cap.typing) return;
      const shown = Math.ceil(cap.chars.length * clamp((t - cap.t0) / cap.dur, 0, 1));
      text.textContent = cap.chars.slice(0, shown).join("");
      if (shown >= cap.chars.length) {
        cap.typing = false;
        pill.classList.remove("typing");
      }
    }

    // Everything jumps to where it is going: a hidden tab paints nothing, and a throttled one catches up.
    function settle() {
      if (o.queue.length) o.to = o.queue.pop();
      o.queue = [];
      Object.assign(o, { pos: { ...o.to }, p: 1, v: 0, moving: false, arrived: true, lean: 0, clicks: 0, clickAt: -1e9 });
      o.release();
      if (o.leaving != null) return remove();
      Object.assign(o, { vis: 1, visV: 0 });
      if (o.cap) {
        if (o.cap.clearing != null) o.cap.clearing = -1e9;
        else o.cap.t0 = -1e9;
        stepCaption(now());
      }
    }

    function step(t) {
      const gap = t - o.last;
      o.last = t;
      if (gap > 500) return settle();
      const dt = Math.min(gap, 50) / 1000;
      if (o.moving) {
        const prev = o.pos;
        if (o.kind === "spring") [o.p, o.v] = spring(o.p, o.v, 1, o.r, dt);
        else o.p = Math.min(1, o.p + dt / o.dur);
        o.pos = along();
        o.speedX = dt ? (o.pos.x - prev.x) / dt : 0;
        // Arrival is the first time the tip reaches the target, not when the spring stops settling.
        if (!o.arrived && o.p >= 1) arrive();
        const done = o.kind === "spring" ? Math.abs(o.p - 1) < 1e-3 && Math.abs(o.v) < 1e-2 : o.p >= 1;
        if (o.moving && done) {
          o.moving = false;
          o.pos = { ...o.to };
          if (!o.arrived) arrive();
        }
      }
      // Leans up to 10 degrees toward horizontal travel, clockwise when moving right, back to 0 at rest.
      const lean = o.moving && !reduced ? clamp(o.speedX / 150, -10, 10) : 0;
      o.lean += (lean - o.lean) * Math.min(1, dt * 14);
      if (Math.abs(o.lean) < 0.01) o.lean = 0;
      if (o.leaving != null) {
        const u = (t - o.leaving) / 300;
        o.vis = o.leaveFrom * (1 - ease(clamp(u, 0, 1)));
        if (u >= 1) return remove();
      } else if (reduced) o.vis = Math.min(1, o.vis + dt / 0.18);
      else [o.vis, o.visV] = spring(o.vis, o.visV, 1, 0.42, dt);
      stepCaption(t);
    }

    function render(t) {
      const v = clamp(o.vis, 0, 1);
      // Under reduced motion a move is a short linear slide that dips in opacity.
      const fade = o.moving && o.kind === "linear" ? 1 - 0.5 * Math.sin(Math.PI * o.p) : 1;
      root.style.opacity = String(v * fade);
      cursor.style.transform = `translate3d(${o.pos.x}px, ${o.pos.y}px, 0)`;
      const since = (t - o.clickAt) / 1000;
      let dip = 1;
      if (!reduced && since < 0.18) {
        const k = since / 0.18;
        dip = k < 0.35 ? 1 - 0.12 * ease(k / 0.35) : 0.88 + 0.12 * ease((k - 0.35) / 0.65);
      }
      const scale = (reduced ? 1 : 0.6 + 0.4 * Math.max(0, o.vis)) * dip;
      pointer.style.transform = `rotate(${o.lean}deg) scale(${scale})`;
      const blur = reduced ? 0 : 5 * (1 - v);
      pointer.style.filter = blur > 0.05 ? `blur(${blur}px)` : "";
      const ringU = since / o.clickDur;
      if (ringU >= 0 && ringU < 1) {
        const e = 1 - (1 - ringU) * (1 - ringU);
        ring.style.opacity = String(0.9 * (1 - e));
        ring.style.transform = `scale(${0.4 + 0.6 * e})`;
      } else ring.style.opacity = "0";
      // Placed for where the cursor is going, so the pill does not jump sides mid-flight.
      const { w, h } = viewport();
      const end = o.end();
      const ox = end.x + 14 + o.pillW > w - 4 ? -14 - o.pillW : 14;
      const oy = end.y + 20 + o.pillH > h - 4 ? -20 - o.pillH : 20;
      pill.style.transform = `translate(${ox}px, ${oy}px)`;
      pill.style.opacity = String(o.pill);
      root.classList.toggle("resting", !o.moving && !o.queue.length && o.leaving == null && !reduced);
    }

    function busy(t) {
      return (
        o.moving ||
        o.queue.length > 0 ||
        o.leaving != null ||
        Math.abs(o.vis - 1) > 1e-3 ||
        Math.abs(o.visV) > 1e-3 ||
        o.lean !== 0 ||
        t - o.clickAt < 450 ||
        (o.cap != null && (o.cap.typing || o.cap.clearing != null || o.pill < 1))
      );
    }

    function frame(t) {
      o.raf = 0;
      try {
        step(t);
        if (globalThis.__secondCursorOverlay !== o) return;
        render(t);
        if (busy(t)) o.raf = requestAnimationFrame(frame);
      } catch {
        // Uncaught, it would land in the page's console log, which the agent reads.
      }
    }

    function schedule() {
      if (document.hidden) settle();
      if (globalThis.__secondCursorOverlay !== o) return;
      render(now());
      if (!o.raf && busy(now())) o.raf = requestAnimationFrame(frame);
    }

    function remove() {
      o.release();
      cancelAnimationFrame(o.raf);
      clearTimeout(o.idle);
      host.remove();
      if (globalThis.__secondCursorOverlay === o) delete globalThis.__secondCursorOverlay;
    }

    o.apply = (c) => {
      if (!host.isConnected) document.documentElement.append(host);
      o.conceal(c.concealed);
      // A fallback for an extension that stopped sending updates; it normally hides the cursor itself.
      clearTimeout(o.idle);
      o.idle = setTimeout(() => o.vanish(false), Math.max(0, c.idle_ms ?? 60000) + 2000);
      const appearing = o.pos == null || o.leaving != null;
      // Reinstalling after a navigation: this document already has the cursor, so leave its motion alone.
      if (c.instant && !appearing) return o.end();
      const { w, h } = viewport();
      const target =
        Number.isFinite(c.x) && Number.isFinite(c.y) ? { x: c.x, y: c.y } : o.to ?? { x: w * 0.58, y: h * 0.55 };
      // A newer move supersedes the older one's wait.
      o.release();
      Object.assign(o, { leaving: null, last: now(), queue: [], clicks: c.click ?? 0 });
      if (appearing) {
        Object.assign(o, { pos: { ...target }, to: { ...target }, moving: false, visV: 0 });
        o.vis = c.instant ? 1 : Math.min(o.vis, 0.3);
        arrive();
      } else if (target.x !== o.to.x || target.y !== o.to.y || !o.moving) aim(target);
      // Decided before a drag's second leg starts: the drag waits for its first leg, an appear for its fade-in.
      const wait = !c.wait || document.visibilityState !== "visible" ? 0 : appearing ? 180 : o.arrived ? 0 : 1500;
      if (c.then) {
        o.queue = [c.then];
        if (o.arrived) arrive();
      }
      o.arrival = wait
        ? new Promise((resolve) => {
            const done = () => {
              if (o.waiter === done) o.waiter = null;
              resolve();
            };
            o.waiter = done;
            setTimeout(done, wait);
          })
        : null;
      setCaption(c.caption ?? "", c.instant);
      if (c.instant) settle();
      schedule();
      return o.end();
    };
    o.release = () => o.waiter?.();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") o.release();
    });
    o.conceal = (hidden) => host.style.setProperty("visibility", hidden ? "hidden" : "visible", "important");
    o.vanish = (immediately) => {
      if (immediately) return remove();
      if (o.leaving == null) Object.assign(o, { leaving: now(), leaveFrom: clamp(o.vis, 0, 1), last: now() });
      schedule();
    };
    o.end = () => {
      const end = o.queue.at(-1) ?? o.to;
      return end && { x: end.x, y: end.y };
    };
    globalThis.__secondCursorOverlay = o;
    return o;
  }
}
