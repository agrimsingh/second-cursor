import { cdp, ensureDomain, frameCDP, frameSession } from "./cdp.js";
import { concealDuring, dragTo, pointTo, setCaption, show } from "./cursor.js";
import { exportContent } from "./exports.js";
import { BridgeError, sleep } from "./errors.js";
import { click, pressKey, reachablePoint, scroll, type } from "./input.js";
import { isSecretField } from "./snapshot.js";
import {
  requireAgentTab,
  requireAgentInput,
  userTabs,
  claimTab,
  releaseTab,
  report,
  retainTab,
  settleAround,
} from "./tabs.js";

const events = new Map();
chrome.tabs.onRemoved.addListener((id) => events.delete(id));
// Frames that started and stopped loading, per tab, so an input can wait for the navigation it began.
const loads = new Map();
chrome.tabs.onRemoved.addListener((id) => loads.delete(id));
const loadsOf = (id) => loads.get(id) ?? loads.set(id, { started: 0, stopped: 0 }).get(id);
// Waits for a load the input started: one begun within 30 ms, until every started load has stopped (at most 15 s).
async function afterInput(tabId, before) {
  const counts = loadsOf(tabId);
  for (const until = Date.now() + 30; counts.started === before && Date.now() < until; ) await sleep(10);
  for (const until = Date.now() + 15000; counts.stopped < counts.started && Date.now() < until; ) await sleep(25);
}
function state(id) {
  if (!events.has(id))
    events.set(id, {
      logs: [],
      downloads: [],
      assets: new Map(),
      dialog: null,
      chooser: null,
      dialogWaiters: new Set(),
      naturalSize: null,
    });
  return events.get(id);
}
/** The JavaScript dialog open in the tab, if any. */
export const openDialog = (tabId) => state(tabId).dialog;
export function recordEvent(source, method, p) {
  if (method === "Page.frameStartedLoading") loadsOf(source.tabId).started++;
  if (method === "Page.frameStoppedLoading") loadsOf(source.tabId).stopped++;
  const s = state(source.tabId);
  if (method === "Runtime.consoleAPICalled") {
    s.logs.push({
      level: p.type,
      message: p.args.map((a) => a.value ?? a.description ?? "").join(" "),
      timestamp: p.timestamp,
    });
    if (s.logs.length > 500) s.logs.shift();
  }
  if (method === "Runtime.exceptionThrown")
    s.logs.push({
      level: "error",
      message: p.exceptionDetails.text,
      timestamp: p.timestamp,
    });
  if (method === "Page.javascriptDialogOpening") {
    s.dialog = p;
    for (const wake of s.dialogWaiters) wake(p);
    report(
      source.tabId,
      `dialog opened in tab ${source.tabId}: ${p.type} ${JSON.stringify(p.message.slice(0, 200))}; answer it with browser_command operation=dialog`,
    );
  }
  if (method === "Page.javascriptDialogClosed") {
    s.dialog = null;
    report(source.tabId, `dialog closed in tab ${source.tabId}: ${p.result ? "accepted" : "dismissed"}`);
  }
  if (method === "Page.fileChooserOpened") s.chooser = p;
  if (method === "Page.downloadWillBegin") {
    s.downloads.push(p);
    if (s.downloads.length > 100) s.downloads.shift();
    report(source.tabId, `download started in tab ${source.tabId}: ${p.suggestedFilename}`);
  }
  if (method === "Page.downloadProgress" && p.state !== "inProgress") {
    const name = s.downloads.find((d) => d.guid === p.guid)?.suggestedFilename ?? p.guid;
    report(source.tabId, `download ${p.state === "completed" ? "finished" : "canceled"} in tab ${source.tabId}: ${name}`);
  }
  if (method === "Network.responseReceived") {
    s.assets.set(p.requestId, {
      id: p.requestId,
      url: p.response.url,
      mime_type: p.response.mimeType,
      status: p.response.status,
    });
    if (s.assets.size > 500) s.assets.delete(s.assets.keys().next().value);
  }
}

// This function executes in an isolated page world and only operates on DOM elements. `anchor` is the element an
// aria-ref locator resolved to.
function domOperation(p, isSecretField, anchor) {
  // Keep this guard in the isolated world as well as before cursor/locator work in operate().
  if (["select_option", "check"].includes(p.operation) &&
      (p.ref !== undefined || !p.locator || typeof p.locator !== "object" ||
       Array.isArray(p.locator) || Object.keys(p.locator).length === 0))
    throw Error('select_option and check require locator, for example {"label":"Route"}; top-level ref is unsupported');
  let doc = document;
  for (const selector of Array.isArray(p.locator?.frame)
    ? p.locator.frame
    : p.locator?.frame
      ? [p.locator.frame]
      : []) {
    const frames = [...doc.querySelectorAll(selector)];
    if (frames.length !== 1)
      throw Error("Frame selector must match exactly one frame");
    doc = frames[0].contentDocument;
    if (!doc)
      throw Error("Frame is cross-origin; resolve it through a frame context");
  }
  const root = doc;
  const role = (e) =>
    e.getAttribute("role") ||
    {
      BUTTON: "button",
      A: "link",
      TEXTAREA: "textbox",
      SELECT: "combobox",
      IMG: "img",
      H1: "heading",
      H2: "heading",
      IFRAME: "iframe",
    }[e.tagName] ||
    (e.tagName === "INPUT"
      ? {
          checkbox: "checkbox",
          radio: "radio",
          range: "slider",
          number: "spinbutton",
          search: "searchbox",
          button: "button",
          submit: "button",
        }[e.type] || "textbox"
      : "");
  const labelText = (l) => {
    const clone = l.cloneNode(true);
    clone
      .querySelectorAll("input,select,textarea,button")
      .forEach((e) => e.remove());
    return clone.textContent.trim();
  };
  const name = (e) =>
    e.getAttribute("aria-label") ||
    (e.getAttribute("aria-labelledby") || "")
      .split(/\s+/)
      .map((id) => doc.getElementById(id)?.textContent || "")
      .join(" ")
      .trim() ||
    [...(e.labels ?? [])].map(labelText).join(" ").trim() ||
    e.getAttribute("alt") ||
    e.getAttribute("title") ||
    (e.matches("input[type=button],input[type=submit]") ? e.value : "") ||
    e.innerText ||
    "";
  const visible = (e) =>
    !!e.getClientRects().length && getComputedStyle(e).visibility !== "hidden";
  // Password and payment card values stay out of reads, as in browser_snapshot.
  const secret = (e, value = e.value) =>
    e.matches("input,textarea,select") &&
    isSecretField({
      type: e.type,
      autocomplete: e.getAttribute("autocomplete"),
      name: name(e) || e.getAttribute("placeholder"),
      value,
    });
  const optionSecret = (e, o) => secret(e, o.value) || secret(e, o.label);
  const valueOf = (e) =>
    e.value && (secret(e) || (e.tagName === "SELECT" && [...e.selectedOptions].some((o) => optionSecret(e, o))))
      ? "[redacted]" : String(e.value);
  const matches = (a, b) =>
    p.locator?.exact === false ? a.includes(b) : a.trim() === b;
  // Chained locators (browser_repl) follow Playwright: text is case-insensitive substring unless exact, exact is
  // case-sensitive and whole, whitespace is normalized, and each step searches below or narrows the previous set.
  const chained = !!p.locator?.chain;
  const normalized = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
  const optionDisabled = (o) => o.disabled || !!o.closest("optgroup[disabled]");
  const selectMetadata = (e) => {
    if (e.tagName !== "SELECT") return {};
    const options = [...e.options];
    return {
      multiple: e.multiple,
      option_count: options.length,
      options_truncated: options.length > 100,
      options: options.slice(0, 100).map((o) => {
        const redacted = optionSecret(e, o);
        return {
          label: redacted ? "[redacted]" : o.label.slice(0, 300),
          value: redacted ? "[redacted]" : o.value.slice(0, 300),
          selected: o.selected,
          disabled: optionDisabled(o),
          ...(!redacted && o.label.length > 300 ? { label_truncated: true } : {}),
          ...(!redacted && o.value.length > 300 ? { value_truncated: true } : {}),
        };
      }),
    };
  };
  const textMatches = (value, want, exact) => {
    const text = normalized(value);
    if (want && typeof want === "object")
      return new RegExp(want.regex.source, want.regex.flags).test(text);
    return exact
      ? text === normalized(want)
      : text.toLowerCase().includes(normalized(want).toLowerCase());
  };
  const HEADINGS = { H1: 1, H2: 2, H3: 3, H4: 4, H5: 5, H6: 6 };
  const ariaRole = (e) => {
    const explicit = (e.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (explicit) return explicit;
    const tag = e.tagName;
    if (HEADINGS[tag]) return "heading";
    if (tag === "INPUT") {
      const type = (e.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "hidden") return "";
      if (e.hasAttribute("list")) return "combobox";
      return (
        {
          checkbox: "checkbox",
          radio: "radio",
          range: "slider",
          number: "spinbutton",
          search: "searchbox",
        }[type] || "textbox"
      );
    }
    if (tag === "A" || tag === "AREA") return e.hasAttribute("href") ? "link" : "";
    if (tag === "HEADER" || tag === "FOOTER")
      return e.closest("article,aside,main,nav,section")
        ? ""
        : tag === "HEADER"
          ? "banner"
          : "contentinfo";
    if (tag === "SECTION")
      return e.hasAttribute("aria-label") || e.hasAttribute("aria-labelledby")
        ? "region"
        : "";
    if (tag === "SELECT") return e.multiple || e.size > 1 ? "listbox" : "combobox";
    if (tag === "IMG") return e.getAttribute("alt") === "" ? "presentation" : "img";
    return (
      {
        ARTICLE: "article",
        ASIDE: "complementary",
        BUTTON: "button",
        DD: "definition",
        DETAILS: "group",
        DIALOG: "dialog",
        DT: "term",
        FIELDSET: "group",
        FIGURE: "figure",
        FORM: "form",
        HR: "separator",
        LI: "listitem",
        MAIN: "main",
        MENU: "list",
        NAV: "navigation",
        OL: "list",
        OPTION: "option",
        OUTPUT: "status",
        P: "paragraph",
        PROGRESS: "progressbar",
        TABLE: "table",
        TBODY: "rowgroup",
        TD: "cell",
        TEXTAREA: "textbox",
        TFOOT: "rowgroup",
        TH: "columnheader",
        THEAD: "rowgroup",
        TR: "row",
        UL: "list",
      }[tag] || ""
    );
  };
  const NAME_FROM_CONTENT = new Set([
    "button",
    "cell",
    "checkbox",
    "columnheader",
    "gridcell",
    "heading",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "radio",
    "row",
    "rowheader",
    "switch",
    "tab",
    "term",
    "tooltip",
    "treeitem",
  ]);
  const labelOf = (e) =>
    normalized(
      (e.getAttribute("aria-labelledby") || "")
        .split(/\s+/)
        .filter(Boolean)
        .map((id) => doc.getElementById(id)?.textContent || "")
        .join(" "),
    ) ||
    normalized(e.getAttribute("aria-label")) ||
    normalized([...(e.labels ?? [])].map(labelText).join(" "));
  const accName = (e, r) => {
    const label = labelOf(e);
    if (label) return label;
    if (e.matches("img,area,input[type=image]") && e.getAttribute("alt"))
      return e.getAttribute("alt");
    if (e.matches("input[type=button],input[type=submit],input[type=reset]"))
      return e.value || { submit: "Submit", reset: "Reset" }[e.type] || "";
    const caption =
      e.tagName === "FIELDSET"
        ? e.querySelector(":scope > legend")
        : e.tagName === "TABLE"
          ? e.querySelector(":scope > caption")
          : null;
    if (caption) return normalized(caption.textContent);
    if (NAME_FROM_CONTENT.has(r)) {
      const text = normalized(e.innerText ?? e.textContent);
      if (text) return text;
    }
    return (
      e.getAttribute("title") ||
      (["textbox", "searchbox", "combobox"].includes(r)
        ? e.getAttribute("placeholder") || ""
        : "")
    );
  };
  const rendered = (e) =>
    e.checkVisibility
      ? e.checkVisibility({ visibilityProperty: true })
      : visible(e);
  // Playwright's visibility: a non-empty box and not visibility:hidden.
  const shown = (e) => {
    if (!rendered(e)) return false;
    const b = e.getBoundingClientRect();
    return b.width > 0 && b.height > 0;
  };
  const ariaHidden = (e) =>
    !!e.closest('[aria-hidden="true"]') ||
    !rendered(e.tagName === "OPTION" ? (e.closest("select") ?? e) : e);
  const enabled = (e) =>
    !(
      e.disabled ||
      e.closest('[aria-disabled="true"]') ||
      (e.closest("fieldset[disabled]") && !e.closest("legend"))
    );
  const roleStates = (e, s) =>
    (s.level === undefined ||
      (Number(e.getAttribute("aria-level")) || HEADINGS[e.tagName] || 0) ===
        s.level) &&
    (s.checked === undefined ||
      (e.matches("input[type=checkbox],input[type=radio]")
        ? e.checked
        : e.getAttribute("aria-checked") === "true") === s.checked) &&
    (s.disabled === undefined || !enabled(e) === s.disabled) &&
    (s.expanded === undefined ||
      (e.getAttribute("aria-expanded") === "true") === s.expanded) &&
    (s.pressed === undefined ||
      (e.getAttribute("aria-pressed") === "true") === s.pressed) &&
    (s.selected === undefined ||
      (e.tagName === "OPTION"
        ? e.selected
        : e.getAttribute("aria-selected") === "true") === s.selected);
  const ownText = (e) =>
    e.matches("input[type=button],input[type=submit],input[type=reset]")
      ? e.value
      : (e.innerText ?? e.textContent ?? "");
  const describeElement = (e) => {
    const attributes = ["id", "type", "role", "aria-label", "data-testid", "href"]
      .filter((a) => e.hasAttribute(a))
      .slice(0, 3)
      .map((a) => ` ${a}="${e.getAttribute(a).slice(0, 40)}"`)
      .join("");
    const text = normalized(e.innerText ?? e.textContent).slice(0, 80);
    return `<${e.localName}${attributes}>${text ? ` "${text}"` : ""}`;
  };
  const inOrder = (list) =>
    list.length < 2
      ? list
      : list.sort((a, b) =>
          a === b
            ? 0
            : a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING
              ? -1
              : 1,
        );
  const notOverlay = (e) => e.id !== "second-cursor-overlay";
  function chain(steps, set) {
    const below = (keep) => {
      const found = new Set();
      for (const el of set ?? [root])
        for (const e of el.querySelectorAll("*"))
          if (notOverlay(e) && keep(e)) found.add(e);
      return inOrder([...found]);
    };
    for (const s of steps) {
      if (s.ref !== undefined) continue;
      if (s.css !== undefined)
        set = inOrder([
          ...new Set(
            (set ?? [root]).flatMap((el) => [...el.querySelectorAll(s.css)]),
          ),
        ]).filter(notOverlay);
      else if (s.xpath !== undefined) {
        const found = new Set();
        for (const el of set ?? [root]) {
          const r = doc.evaluate(s.xpath, el, null, 7, null);
          for (let i = 0; i < r.snapshotLength; i++)
            if (r.snapshotItem(i).nodeType === 1) found.add(r.snapshotItem(i));
        }
        set = inOrder([...found]);
      } else if (s.role !== undefined)
        set = below(
          (e) =>
            ariaRole(e) === s.role &&
            (s.include_hidden || !ariaHidden(e)) &&
            roleStates(e, s) &&
            (s.name === undefined || textMatches(accName(e, s.role), s.name, s.exact)),
        );
      else if (s.text !== undefined)
        set = below(
          (e) =>
            !e.closest("head,script,style,noscript,template") &&
            textMatches(ownText(e), s.text, s.exact) &&
            ![...e.children].some((c) => textMatches(ownText(c), s.text, s.exact)),
        );
      else if (s.label !== undefined)
        set = below((e) => {
          const label = labelOf(e);
          return !!label && textMatches(label, s.label, s.exact);
        });
      else if (s.attribute !== undefined)
        set = below(
          (e) =>
            e.hasAttribute(s.attribute) &&
            (s.attribute === "data-testid"
              ? e.getAttribute(s.attribute) === s.value
              : textMatches(e.getAttribute(s.attribute), s.value, s.exact)),
        );
      else {
        set ??= [...root.querySelectorAll("*")].filter(notOverlay);
        if (s.has_text !== undefined)
          set = set.filter((e) => textMatches(ownText(e), s.has_text, false));
        else if (s.has_not_text !== undefined)
          set = set.filter((e) => !textMatches(ownText(e), s.has_not_text, false));
        else if (s.has !== undefined)
          set = set.filter((e) => chain(s.has, [e]).length > 0);
        else if (s.has_not !== undefined)
          set = set.filter((e) => chain(s.has_not, [e]).length === 0);
        else if (s.visible !== undefined)
          set = set.filter((e) => shown(e) === s.visible);
        else if (s.nth !== undefined) {
          const i = s.nth < 0 ? set.length + s.nth : s.nth;
          set = i >= 0 && i < set.length ? [set[i]] : [];
        } else if (s.and !== undefined) {
          const other = new Set(chain(s.and, null));
          set = set.filter((e) => other.has(e));
        } else if (s.or !== undefined)
          set = inOrder([...new Set([...set, ...chain(s.or, null)])]);
        else throw Error(`Unknown locator step ${JSON.stringify(s)}`);
      }
    }
    return set ?? [];
  }
  function locate() {
    const l = p.locator || {};
    if (chained) return chain(l.chain, anchor ? [anchor] : l.chain[0]?.ref !== undefined ? [] : null);
    // The agent's own cursor (cursor.js) is not part of the page.
    let found = [...root.querySelectorAll(l.selector || "*")].filter(
      (e) => e.id !== "second-cursor-overlay",
    );
    if (l.role) found = found.filter((e) => role(e) === l.role);
    if (l.name !== undefined)
      found = found.filter((e) => matches(name(e), l.name));
    if (l.label !== undefined)
      found = found.filter((e) =>
        matches(
          e.getAttribute("aria-label") ||
            [...(e.labels ?? [])].map(labelText).join(" ").trim(),
          l.label,
        ),
      );
    if (l.placeholder !== undefined)
      found = found.filter((e) =>
        matches(e.getAttribute("placeholder") || "", l.placeholder),
      );
    if (l.test_id !== undefined)
      found = found.filter((e) => e.getAttribute("data-testid") === l.test_id);
    if (l.text !== undefined)
      found = found.filter(
        (e) =>
          !e.closest("head,script,style,noscript,template") &&
          matches(e.innerText || "", l.text) &&
          ![...e.children].some((c) => matches(c.innerText || "", l.text)),
      );
    if (l.visible !== undefined)
      found = found.filter((e) => visible(e) === l.visible);
    if (Number.isInteger(l.nth)) found = found.slice(l.nth, l.nth + 1);
    return found;
  }
  if (p.operation === "dom_snapshot") {
    const walk = (d, depth = 0) =>
      [...d.querySelectorAll("body *")].flatMap((e) => {
        if (["SCRIPT", "STYLE", "NOSCRIPT"].includes(e.tagName)) return [];
        if (e.tagName === "IFRAME")
          return [
            {
              role: "iframe",
              name: name(e),
              src: e.src,
              children:
                e.contentDocument && depth < 8
                  ? walk(e.contentDocument, depth + 1)
                  : [],
              cross_origin: !e.contentDocument,
            },
          ];
        const r = role(e),
          n = name(e);
        if (
          !r &&
          !e.matches(
            "p,li,td,th,[contenteditable],[draggable=true],[tabindex],[onclick]",
          )
        )
          return [];
        const b = e.getBoundingClientRect(),
          value = "value" in e ? valueOf(e) : null;
        return [
          {
            role: r || e.tagName.toLowerCase(),
            name: n.slice(0, 300),
            value: value?.slice(0, 300) ?? null,
            value_length: value?.length ?? null,
            enabled: !e.disabled,
            focused: d.activeElement === e,
            checked: "checked" in e ? e.checked : null,
            selected: e.getAttribute("aria-selected"),
            expanded: e.getAttribute("aria-expanded"),
            editable: e.isContentEditable || e.matches("input,textarea"),
            ...selectMetadata(e),
            visible: visible(e),
            bounds: { x: b.x, y: b.y, width: b.width, height: b.height },
            actions: e.disabled
              ? []
              : [
                  "click",
                  "focus",
                  "scroll_into_view",
                  ...(e.hasAttribute("aria-expanded")
                    ? [
                        e.getAttribute("aria-expanded") === "true"
                          ? "collapse"
                          : "expand",
                      ]
                    : []),
                  ...(e.matches("input[type=number],input[type=range]")
                    ? ["increment", "decrement"]
                    : []),
                  ...(e.matches("input,textarea,[contenteditable]")
                    ? ["fill", "type", "select_text"]
                    : []),
                  ...(e.tagName === "SELECT" ? ["select_option"] : []),
                ],
          },
        ];
      });
    return {
      url: location.href,
      title: doc.title,
      elements: walk(doc).slice(0, 3000),
      truncated: doc.querySelectorAll("body *").length > 3000,
    };
  }
  const found = locate();
  if (p.operation === "query") {
    if (!chained)
      return {
        count: found.length,
        visible: found.length === 1 && visible(found[0]),
      };
    return {
      count: found.length,
      visible: found.length === 1 && shown(found[0]),
      enabled: found.length === 1 && enabled(found[0]),
      ...(p.texts
        ? {
            texts: found
              .slice(0, 1000)
              .map((e) =>
                (p.texts === "content" ? e.textContent : ownText(e)).slice(0, 100000),
              ),
          }
        : {}),
    };
  }
  if (chained && found.length > 1)
    throw Error(
      `strict mode violation: resolved to ${found.length} elements:\n${found
        .slice(0, 5)
        .map((e, i) => `  ${i + 1}) ${describeElement(e)}`)
        .join("\n")}${found.length > 5 ? `\n  … and ${found.length - 5} more` : ""}`,
    );
  if (found.length !== 1)
    throw Error(
      `Locator matched ${found.length} elements; refine it to exactly one`,
    );
  const e = found[0];
  if (p.operation === "node") return e;
  if (p.operation === "inspect") {
    const text =
        p.text_kind === "content"
          ? e.textContent || ""
          : p.text_kind === "inner"
            ? (e.innerText ?? e.textContent ?? "")
            : e.matches("input,textarea,select")
              ? valueOf(e)
              : e.innerText || e.textContent || "",
      offset = p.offset ?? 0,
      length = p.length ?? 4000;
    if (offset < 0 || length < 0 || length > 100000)
      throw Error("Invalid text range");
    const b = e.getBoundingClientRect();
    return {
      bounds: { x: b.x, y: b.y, width: b.width, height: b.height },
      text: text.slice(offset, offset + length),
      total_length: text.length,
      has_more: offset + length < text.length,
      role: chained ? ariaRole(e) : role(e),
      name: chained ? accName(e, ariaRole(e)) : name(e),
      enabled: chained ? enabled(e) : !e.disabled,
      visible: chained ? shown(e) : visible(e),
      checked: "checked" in e ? e.checked : null,
      ...selectMetadata(e),
      attributes: Object.fromEntries(
        [...e.attributes].map((x) => [
          x.name,
          x.name === "value" && x.value && secret(e) ? "[redacted]" : x.value,
        ]),
      ),
    };
  }
  // Playwright's actionability: acted-on elements must be visible and enabled; these errors are retried.
  if (
    chained &&
    !p.force &&
    ["center", "fill", "focus", "check", "select_option", "select_text", "perform_action"].includes(
      p.operation,
    )
  ) {
    if (!shown(e)) throw Error("Element is not visible");
    if (!enabled(e)) throw Error("Element is disabled");
    if (p.operation === "fill" && !(e.isContentEditable || e.matches("input,textarea")))
      throw Error("Element is not an <input>, <textarea> or [contenteditable] element");
    if (p.operation === "fill" && e.readOnly) throw Error("Element is not editable");
  }
  if (e.disabled) throw Error("Element is disabled");
  if (p.operation === "focus") {
    e.focus();
    return { focused: doc.activeElement === e };
  }
  if (p.operation === "perform_action") {
    const actions = [
      "focus",
      "scroll_into_view",
      ...(e.hasAttribute("aria-expanded")
        ? [e.getAttribute("aria-expanded") === "true" ? "collapse" : "expand"]
        : []),
      ...(e.matches("input[type=number],input[type=range]")
        ? ["increment", "decrement"]
        : []),
    ];
    if (!actions.includes(p.action))
      throw Error(
        "Action is not exposed by this element: " + actions.join(", "),
      );
    if (p.action === "focus") e.focus();
    else if (p.action === "scroll_into_view")
      e.scrollIntoView({ block: "nearest" });
    else if (p.action === "expand" || p.action === "collapse") e.click();
    else {
      p.action === "increment" ? e.stepUp() : e.stepDown();
      e.dispatchEvent(new Event("input", { bubbles: true }));
      e.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return { ok: true };
  }
  if (p.operation === "fill") {
    e.focus();
    if (e.isContentEditable) e.textContent = p.text;
    else {
      const proto =
        e.tagName === "TEXTAREA"
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(e, p.text);
    }
    e.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: p.text,
      }),
    );
    e.dispatchEvent(new Event("change", { bubbles: true }));
    const actual = "value" in e ? String(e.value) : e.textContent;
    const redacted = secret(e);
    const verified = e.isConnected && actual === p.text;
    return { value: "value" in e ? valueOf(e) : e.textContent,
      outcome: { state: verified ? "verified" : "uncertain", verified, verification: "value",
        reason: verified ? "The connected control has the requested value after its input handlers returned."
          : "The control was replaced or did not retain the requested value; inspect it before continuing.",
        expected: redacted ? "[redacted]" : p.text, actual: redacted ? "[redacted]" : actual } };
  }
  if (p.operation === "select_option") {
    if (e.tagName !== "SELECT") throw Error("select_option requires a native <select> control");
    if (!enabled(e)) throw Error("Element is disabled");
    const opts = [...e.options];
    const requested = Array.isArray(p.value) ? p.value : [p.value];
    if (!requested.every((v) => typeof v === "string"))
      throw Error("select_option value must be a string or an array of strings");
    // Both locator forms prefer exact values, then an exact case-sensitive label with normalized whitespace.
    // Resolve the entire request before mutating; a duplicate or disabled match must not select a different option.
    const selected = [...new Set(requested.map((v) => {
      let matches = opts.filter((o) => o.value === v);
      if (!matches.length) matches = opts.filter((o) => normalized(o.label) === normalized(v));
      if (!matches.length) throw Error("Option not found; inspect the control for available option labels and values");
      if (matches.length !== 1) throw Error("Option is ambiguous; use a unique option value or label from inspect");
      if (optionDisabled(matches[0])) throw Error("Requested option is disabled");
      return matches[0];
    }))];
    if (!e.multiple && selected.length > 1)
      throw Error("Multiple options require a <select multiple> control");
    const values = selected.map((o) => o.value);
    const expectedRedacted = selected.some((o) => optionSecret(e, o));
    for (const o of opts) o.selected = selected.includes(o);
    e.dispatchEvent(new Event("input", { bubbles: true }));
    e.dispatchEvent(new Event("change", { bubbles: true }));
    const actualOptions = [...e.selectedOptions];
    const actual = actualOptions.map((o) => o.value);
    const verified = e.isConnected && actualOptions.length === selected.length &&
      actualOptions.every((o) => selected.includes(o)) && selected.every((o, i) => o.value === values[i]);
    const redacted = expectedRedacted || actualOptions.some((o) => optionSecret(e, o));
    return { value: redacted ? "[redacted]" : valueOf(e),
      outcome: { state: verified ? "verified" : "uncertain", verified,
        reason: verified ? "The connected control has the requested selection after its input handlers returned."
          : "The control was replaced or did not retain the requested selection; inspect it before continuing.",
        verification: "selected_values", expected: redacted ? values.map(() => "[redacted]") : values,
        actual: redacted ? actual.map(() => "[redacted]") : actual } };
  }
  if (p.operation === "check") {
    if (!["checkbox", "radio"].includes(e.type))
      throw Error("Element is not checkbox/radio");
    if (e.checked !== (p.checked !== false)) e.click();
    const expected = p.checked !== false;
    const verified = e.isConnected && e.checked === expected;
    return { checked: e.checked, outcome: { state: verified ? "verified" : "uncertain", verified,
      reason: verified ? "The connected control has the requested checked state after its input handlers returned."
        : "The control was replaced or did not retain the requested checked state; inspect it before continuing.",
      verification: "checked", expected, actual: e.checked } };
  }
  if (p.operation === "select_text") {
    // How often a text matches would reveal a redacted value a guess at a time.
    if (secret(e))
      throw Error("select_text is not available on password and payment card fields");
    const text = "value" in e ? e.value : e.textContent,
      needle = p.text;
    if (!needle) throw Error("Nonempty text required");
    const indexes = [];
    for (
      let i = text.indexOf(needle);
      i >= 0;
      i = text.indexOf(needle, i + needle.length)
    )
      if (
        (p.prefix === undefined || text.slice(0, i).endsWith(p.prefix)) &&
        (p.suffix === undefined ||
          text.slice(i + needle.length).startsWith(p.suffix))
      )
        indexes.push(i);
    if (indexes.length !== 1)
      throw Error(`Text matched ${indexes.length} times; use prefix/suffix`);
    let start = indexes[0],
      end = start + needle.length;
    if (p.selection_type === "cursor_before") end = start;
    else if (p.selection_type === "cursor_after") start = end;
    e.focus();
    if (e.setSelectionRange) e.setSelectionRange(start, end);
    else {
      const walker = doc.createTreeWalker(e, NodeFilter.SHOW_TEXT),
        nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      const point = (offset) => {
        for (const n of nodes) {
          if (offset <= n.length) return [n, offset];
          offset -= n.length;
        }
        return [e, e.childNodes.length];
      };
      const range = doc.createRange();
      range.setStart(...point(start));
      range.setEnd(...point(end));
      const selection = doc.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    return { start, end };
  }
  if (p.operation === "paste") {
    e.focus();
    const transfer = new DataTransfer();
    transfer.setData("text/plain", p.text);
    if (p.format === "html") transfer.setData("text/html", p.text);
    const event = new ClipboardEvent("paste", {
      clipboardData: transfer,
      bubbles: true,
      cancelable: true,
    });
    if (e.dispatchEvent(event))
      doc.execCommand(
        p.format === "html" ? "insertHTML" : "insertText",
        false,
        p.text,
      );
    return { ok: true };
  }
  if (p.operation === "scroll") {
    const n = (p.amount ?? 3) * 100;
    const [x, y] =
      { up: [0, -n], down: [0, n], left: [-n, 0], right: [n, 0] }[
        p.direction
      ] || [];
    if (x === undefined) throw Error("Invalid direction");
    e.scrollBy(x, y);
    return { scroll_top: e.scrollTop, scroll_left: e.scrollLeft };
  }
  if (p.operation === "center") {
    if (p.scroll === "if_needed") {
      // Only as far as focusing the element would: the cursor must not move the page more than the action does.
      const r = e.getBoundingClientRect(),
        view = doc.defaultView;
      if (
        r.bottom < 0 ||
        r.right < 0 ||
        r.top > view.innerHeight ||
        r.left > view.innerWidth
      )
        e.scrollIntoView({ block: "nearest", inline: "nearest" });
    } else e.scrollIntoView({ block: "center", inline: "center" });
    const b = e.getBoundingClientRect();
    let x = b.x + b.width / 2,
      y = b.y + b.height / 2;
    let view = doc.defaultView;
    while (view !== window && view.frameElement) {
      const r = view.frameElement.getBoundingClientRect();
      x += r.x;
      y += r.y;
      view = view.parent;
    }
    return { x, y };
  }
  throw Error("Unsupported DOM operation");
}

async function contextFor(tabId, p) {
  const frames = Array.isArray(p.locator?.frame)
    ? p.locator.frame
    : p.locator?.frame
      ? [p.locator.frame]
      : [];
  if (!frames.length)
    return {
      send: (method, params) => cdp(tabId, method, params),
      offset: { x: 0, y: 0 },
      root: { x: 0, y: 0 },
      owners: [],
    };
  // owners: the <iframe> of each frame Chrome runs in its own process, in its parent's session; root: where the
  // frame whose session the target is read in starts, since hit tests take that frame's coordinates.
  let frameId = (await cdp(tabId, "Page.getFrameTree")).frameTree.frame.id,
    offset = { x: 0, y: 0 },
    root = { x: 0, y: 0 },
    contextId;
  const owners = [];
  for (const selector of frames) {
    contextId = (
      await frameCDP(tabId, frameId, "Page.createIsolatedWorld", {
        frameId,
        worldName: "SecondCursor",
      })
    ).executionContextId;
    const expression = `(()=>{const es=[...document.querySelectorAll(${JSON.stringify(selector)})];if(es.length!==1)throw Error('Frame selector must match exactly one frame');return es[0];})()`;
    const node = await frameCDP(tabId, frameId, "Runtime.evaluate", {
      expression,
      contextId,
    });
    if (node.exceptionDetails || !node.result.objectId)
      throw new BridgeError("failed", "Frame selector did not resolve");
    const rect = await frameCDP(tabId, frameId, "Runtime.callFunctionOn", {
      objectId: node.result.objectId,
      functionDeclaration:
        'function(){this.scrollIntoView({block:"nearest"});const b=this.getBoundingClientRect();return {x:b.x+this.clientLeft,y:b.y+this.clientTop}}',
      returnByValue: true,
    });
    offset.x += rect.result.value.x;
    offset.y += rect.result.value.y;
    const description = await frameCDP(tabId, frameId, "DOM.describeNode", {
      objectId: node.result.objectId,
    });
    await frameCDP(tabId, frameId, "Runtime.releaseObject", {
      objectId: node.result.objectId,
    });
    const child =
      description.node.contentDocument?.frameId ?? description.node.frameId;
    if (!child || child === frameId)
      throw new BridgeError("failed", "Frame has not loaded");
    if (frameSession(tabId, child)) {
      const parent = frameId;
      owners.push({
        send: (method, params) => frameCDP(tabId, parent, method, params),
        id: description.node.backendNodeId,
        origin: root,
      });
      root = { ...offset };
    }
    frameId = child;
  }
  contextId = (
    await frameCDP(tabId, frameId, "Page.createIsolatedWorld", {
      frameId,
      worldName: "SecondCursor",
    })
  ).executionContextId;
  return {
    send: (method, params) => frameCDP(tabId, frameId, method, params),
    contextId,
    offset,
    root,
    owners,
  };
}
/** A locator's click point and hit-test levels, for reachablePoint (input.js). */
async function locatorTarget(tabId, p) {
  const point = await dom(tabId, { ...p, operation: "center" });
  const { objectId, send, context } = await dom(tabId, { ...p, operation: "node" }, true);
  const { node } = await send("DOM.describeNode", { objectId });
  await send("Runtime.releaseObject", { objectId }).catch(() => {});
  const levels = [...context.owners, { send, id: node.backendNodeId, origin: context.root }].map(
    ({ send, id, origin }) => ({ send, id, x: point.x - origin.x, y: point.y - origin.y }),
  );
  return { point, levels };
}
// A chain that starts at an aria-ref (a snapshot's [e123], the node's backendNodeId) runs on that node.
async function refDom(tabId, p, ref, object) {
  const id = /^e?(\d+)$/.exec(String(ref).trim())?.[1];
  if (!id)
    throw new BridgeError(
      "invalid_argument",
      `Invalid ref ${JSON.stringify(ref)}; use an [e123] ref from the page's snapshot`,
    );
  const resolved = await cdp(tabId, "DOM.resolveNode", {
    backendNodeId: Number(id),
  }).catch(() => null);
  const objectId = resolved?.object?.objectId;
  if (!objectId)
    throw new BridgeError(
      "failed",
      `Ref ${ref} is no longer on the page; take a new snapshot`,
    );
  try {
    if (!["query", "inspect", "node", "dom_snapshot"].includes(p.operation)) await requireAgentInput(tabId, p.session_id);
    const result = await cdp(tabId, "Runtime.callFunctionOn", {
      objectId,
      functionDeclaration: `function (p) { return (${domOperation.toString()})(p, ${isSecretField.toString()}, this); }`,
      arguments: [{ value: p }],
      returnByValue: !object,
      awaitPromise: true,
    });
    if (result.exceptionDetails)
      throw new BridgeError(
        "failed",
        result.exceptionDetails.exception?.description ??
          result.exceptionDetails.text,
      );
    if (object) {
      const send = (method, params) => cdp(tabId, method, params);
      return { ...result.result, send, context: { send, root: { x: 0, y: 0 }, owners: [] } };
    }
    return result.result.value;
  } finally {
    cdp(tabId, "Runtime.releaseObject", { objectId }).catch(() => {});
  }
}
async function dom(tabId, p, object = false) {
  // A pause can arrive while an action animates its cursor or resolves a frame. Check again at the DOM mutation.
  if (!["query", "inspect", "node", "dom_snapshot"].includes(p.operation)) await requireAgentInput(tabId, p.session_id);
  const ref = p.locator?.chain?.[0]?.ref;
  if (ref !== undefined) return refDom(tabId, p, ref, object);
  const context = await contextFor(tabId, p);
  const arg = { ...p, locator: { ...p.locator, frame: undefined } };
  if (!["query", "inspect", "node", "dom_snapshot"].includes(p.operation)) await requireAgentInput(tabId, p.session_id);
  const result = await context.send("Runtime.evaluate", {
    expression: `(${domOperation.toString()})(${JSON.stringify(arg)}, ${isSecretField.toString()})`,
    returnByValue: !object,
    awaitPromise: true,
    ...(context.contextId ? { contextId: context.contextId } : {}),
  });
  if (result.exceptionDetails)
    throw new BridgeError(
      "failed",
      result.exceptionDetails.exception?.description ??
        result.exceptionDetails.text,
    );
  if (object) return { ...result.result, send: context.send, context };
  const value = result.result.value;
  if (p.operation === "center") {
    value.x += context.offset.x;
    value.y += context.offset.y;
  }
  return value;
}
const ok = () => ({ ok: true });
// Operations that act on the page; each sets the cursor's caption, or clears it when it has none.
const CURSOR_OPERATIONS = new Set([
  "click",
  "hover",
  "drag",
  "scroll",
  "fill",
  "type",
  "paste",
  "select_text",
  "check",
  "select_option",
  "perform_action",
  "press_key",
  "back",
  "forward",
  "reload",
]);
// Moves the cursor to the element before the action and resolves when it arrives. A locator that matches nothing
// is left for the action itself to report.
const pointAtLocator = async (tabId, p) => {
  const point = await dom(tabId, {
    ...p,
    operation: "center",
    scroll: "if_needed",
  }).catch(() => null);
  if (point) await pointTo(tabId, point);
};
const PRIVATE_OPERATIONS = new Set([
  "user_tabs",
  "claim",
  "history",
  "clipboard",
  "cdp",
  "upload",
]);
const CDP_ALLOWLIST = new Set([
  "Page.getLayoutMetrics",
  "Page.getNavigationHistory",
  "DOM.getDocument",
  "Accessibility.getFullAXTree",
]);
const OPERATION_ARGUMENTS = {
  user_tabs: "No arguments. Local approval required.",
  claim:
    "tab_id. Local approval required. Control lasts until release, session close, turn end (default session), or 5 idle minutes. browser_close on a claimed tab only releases it.",
  release: "tab_id. Release ownership without closing the tab.",
  retain: "mode: ephemeral | deliverable | handoff",
  dom_snapshot: "tab_id",
  inspect: "locator; optional offset, length in UTF-16 units",
  click:
    "locator or x,y; optional button (left/right/middle), count (1..3), modifiers array",
  hover: "locator or x,y; optional modifiers array. Dispatches trusted CDP mouse movement without clicking.",
  fill: "locator, text",
  type: "text; optional locator",
  paste:
    "text; optional locator, format (text/md/html). HTML requires locator.",
  select_text:
    "locator, text; optional prefix, suffix, selection_type (text/cursor_before/cursor_after)",
  perform_action: "locator, action from the observed actions",
  check: "locator, checked boolean",
  select_option: "locator, value (exact option value or exact label; values take precedence). An array selects multiple options on <select multiple>. inspect/dom_snapshot expose bounded options with labels, values, selected and disabled states.",
  press_key: "keys; optional locator",
  scroll: "direction, amount; optional locator",
  drag: "from_x, from_y, to_x, to_y in viewport CSS pixels. Left-button pointer drag; no HTML5 DataTransfer synthesis.",
  back: "tab_id",
  forward: "tab_id",
  reload: "tab_id",
  wait: "url substring, or locator and state (visible/hidden/attached/detached), or event (download/filechooser/navigation); timeout_ms",
  dialog:
    "omit accept to inspect; accept boolean and optional prompt_text to answer",
  upload:
    "files array of absolute paths; optional locator, otherwise intercepted chooser. Local approval required.",
  downloads: "tab_id",
  logs: "tab_id",
  history:
    "optional query, from/to (Unix milliseconds), limit. Local approval required.",
  export: "format (text/html/md/pdf/xlsx/csv/docx/pptx/youtube_transcript)",
  assets: "omit action to list; action:bundle and asset_ids array for content",
  viewport: "width, height or reset:true",
  screenshot: "optional full_page boolean or clip:{x,y,width,height}",
  cdp: "method from cdp_allowlist; optional params object. Local approval required.",
  batch:
    "steps array of {operation,...fields}, or {operation,arguments:{...fields}} like the call itself. One tab, 1..50 steps, no nested batches. Executed by the native runtime.",
  clipboard: "action read/write; text for write. Local approval required.",
};

// browser_repl asks for the tab's URL with every result, so page.url() follows navigations its actions cause.
export async function extended(p, guardObservation) {
  const result = await operate(p, guardObservation);
  if (p.report_url && Number.isInteger(p.tabId) && result && typeof result === "object") {
    const tab = await chrome.tabs.get(p.tabId).catch(() => null);
    if (tab) result.url = tab.url || tab.pendingUrl || "";
  }
  return result;
}

async function operate(p, guardObservation) {
  const { tabId, operation: op } = p,
    session = p.session_id ?? "legacy";
  if (
    ["fill", "type", "paste", "select_text"].includes(op) &&
    typeof p.text !== "string"
  )
    throw new BridgeError("invalid_argument", "text must be a string");
  if (
    p.selection_type &&
    !["text", "cursor_before", "cursor_after"].includes(p.selection_type)
  )
    throw new BridgeError("invalid_argument", "Invalid selection_type");
  if (p.button && !["left", "right", "middle"].includes(p.button))
    throw new BridgeError("invalid_argument", "Invalid mouse button");
  if (p.button !== undefined && op !== "click")
    throw new BridgeError("invalid_argument", "button is supported only by click");
  if (p.modifiers !== undefined && !["click", "hover"].includes(op))
    throw new BridgeError("invalid_argument", "modifiers are supported only by click and hover");
  if (p.modifiers !== undefined && !Array.isArray(p.modifiers))
    throw new BridgeError("invalid_argument", "modifiers must be an array");
  if (p.count !== undefined && op !== "click")
    throw new BridgeError("invalid_argument", "count is supported only by click");
  if (
    (p.modifiers ?? []).some(
      (m) => !["alt", "ctrl", "cmd", "shift"].includes(m),
    )
  )
    throw new BridgeError("invalid_argument", "Unsupported modifier");
  if (
    p.count !== undefined &&
    (!Number.isInteger(p.count) || p.count < 1 || p.count > 3)
  )
    throw new BridgeError("invalid_argument", "count must be 1, 2 or 3");
  if (op === "capabilities")
    return {
      operations: Object.keys(OPERATION_ARGUMENTS),
      arguments: OPERATION_ARGUMENTS,
      cdp_allowlist: [...CDP_ALLOWLIST],
      local_approval_required: [...PRIVATE_OPERATIONS],
      input: { backend: "chrome_debugger_cdp", background: true, hover: true, pointer_drag: true,
        html5_drag_data_transfer: false, modifier_click: true, touch: false },
      observations: { revision_guard: "observation_id", coverage: ["dom", "form_properties", "viewport", "frames"],
        excludes: ["canvas_or_video_paint", "render_only_layout", "closed_shadow_roots"],
        outcomes: ["dispatched", "verified", "uncertain"], verified_postconditions: ["value", "checked", "selected_values"] },
      locator_fields: [
        "selector",
        "role",
        "name",
        "label",
        "text",
        "placeholder",
        "test_id",
        "frame",
        "nth",
        "visible",
        "exact",
      ],
    };
  if (op === "cdp" && !CDP_ALLOWLIST.has(p.method)) {
    throw new BridgeError(
      "not_allowed",
      "CDP method is outside the tab-scoped allowlist",
    );
  }
  if (PRIVATE_OPERATIONS.has(op) && !p.local_approvals?.includes(op)) {
    throw new BridgeError("not_allowed", "Local approval required");
  }
  if (op === "user_tabs") return userTabs();
  if (op === "history")
    return {
      history: await chrome.history.search({
        text: p.query ?? "",
        startTime: p.from ?? Date.now() - 86400000,
        endTime: p.to ?? Date.now(),
        maxResults: Math.min(p.limit ?? 100, 1000),
      }),
    };
  if (op === "claim") return claimTab(tabId, session);
  await requireAgentTab(tabId, session);
  if (op === "release") return releaseTab(tabId, session);
  if (op === "retain") return retainTab(tabId, session, p.mode);
  if (CURSOR_OPERATIONS.has(op)) {
    await requireAgentInput(tabId, session);
    setCaption(tabId, p.caption ?? "", session);
  }
  if (op === "dom_snapshot" || op === "inspect" || op === "query")
    return dom(tabId, p);
  if (
    ["fill", "check", "select_option", "select_text", "perform_action"].includes(
      op,
    )
  ) {
    if (["select_option", "check"].includes(op) &&
        (p.ref !== undefined || !p.locator || typeof p.locator !== "object" ||
         Array.isArray(p.locator) || Object.keys(p.locator).length === 0))
      throw new BridgeError("invalid_argument", 'select_option and check require locator, for example {"label":"Route"}; top-level ref is unsupported');
    await pointAtLocator(tabId, p);
    return dom(tabId, p);
  }
  if (op === "click" || op === "hover") {
    let point = p.locator
      ? await reachablePoint("The locator's element", () => locatorTarget(tabId, p))
      : { x: p.x, y: p.y };
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y))
      throw new BridgeError(
        "invalid_argument",
        `${op} needs locator or coordinates`,
      );
    const modifiers = (p.modifiers ?? []).reduce(
        (n, m) => n | ({ alt: 1, ctrl: 2, cmd: 4, shift: 8 }[m] ?? 0),
        0,
      ),
      button = p.button ?? "left";
    const send = (type) =>
      cdp(tabId, "Input.dispatchMouseEvent", {
        type,
        ...point,
        button,
        clickCount: p.count ?? 1,
        modifiers,
      });
    await pointTo(tabId, point, op === "hover" ? 0 : p.count ?? 1);
    // Resolve again after the cursor glide, so a moving/replaced locator cannot receive input at its old position.
    if (p.locator) point = await reachablePoint("The locator's element", () => locatorTarget(tabId, p));
    else await guardObservation?.();
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", ...point, button: "none", buttons: 0, modifiers });
    if (op === "hover") return ok();
    if (p.locator) point = await reachablePoint("The locator's element", () => locatorTarget(tabId, p));
    else await guardObservation?.();
    const before = loadsOf(tabId).started;
    // Do not wait for navigation while a JS dialog is open.
    await send("mousePressed");
    await send("mouseReleased");
    if (p.wait_navigation) await afterInput(tabId, before);
    return ok();
  }
  if (op === "type" || op === "press_key") {
    if (p.locator) {
      if (op === "type") await pointAtLocator(tabId, p);
      await dom(tabId, { ...p, operation: "focus" });
    }
    const before = loadsOf(tabId).started;
    if (op === "type") await type(tabId, { text: p.text });
    else await pressKey(tabId, p.keys);
    if (p.wait_navigation) await afterInput(tabId, before);
    return ok();
  }
  if (op === "paste") {
    if (!["text", "md", "html"].includes(p.format ?? "text"))
      throw new BridgeError("invalid_argument", "Unsupported paste format");
    if (p.locator) {
      await pointAtLocator(tabId, p);
      return dom(tabId, p);
    }
    if (p.format === "html")
      throw new BridgeError(
        "invalid_argument",
        "HTML paste requires a locator",
      );
    show(tabId);
    await cdp(tabId, "Input.insertText", { text: p.text });
    return ok();
  }
  if (op === "scroll") {
    if (p.locator) {
      await pointAtLocator(tabId, p);
      return dom(tabId, p);
    }
    await scroll(tabId, p.direction, p.amount);
    return ok();
  }
  if (op === "drag") {
    const values = [p.from_x, p.from_y, p.to_x, p.to_y];
    if (!values.every(Number.isFinite))
      throw new BridgeError(
        "invalid_argument",
        "Four drag coordinates required",
      );
    await dragTo(tabId, { x: p.from_x, y: p.from_y }, { x: p.to_x, y: p.to_y });
    await guardObservation?.();
    await cdp(tabId, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: p.from_x,
      y: p.from_y,
      button: "left",
      buttons: 1,
      clickCount: 1,
    });
    try {
      for (let i = 1; i <= 20; i++)
        await cdp(tabId, "Input.dispatchMouseEvent", {
          type: "mouseMoved",
          x: p.from_x + ((p.to_x - p.from_x) * i) / 20,
          y: p.from_y + ((p.to_y - p.from_y) * i) / 20,
          button: "left",
          buttons: 1,
        });
    } finally {
      await cdp(tabId, "Input.dispatchMouseEvent", {
        type: "mouseReleased",
        x: p.to_x,
        y: p.to_y,
        button: "left",
        buttons: 0,
        clickCount: 1,
      });
    }
    return ok();
  }
  if (op === "back" || op === "forward") {
    const history = await cdp(tabId, "Page.getNavigationHistory");
    const entry =
      history.entries[history.currentIndex + (op === "back" ? -1 : 1)];
    if (!entry) throw new BridgeError("failed", "No history entry");
    await settleAround(tabId, () =>
      cdp(tabId, "Page.navigateToHistoryEntry", { entryId: entry.id }),
    );
    show(tabId);
    return ok();
  }
  if (op === "reload") {
    await settleAround(tabId, async () => {
      await requireAgentInput(tabId, session);
      return chrome.tabs.reload(tabId);
    });
    show(tabId);
    return ok();
  }
  if (op === "dialog") {
    const dialog = state(tabId).dialog;
    if (p.accept === undefined) return { dialog };
    if (!dialog) throw new BridgeError("failed", "No JavaScript dialog");
    await cdp(tabId, "Page.handleJavaScriptDialog", {
      accept: p.accept,
      promptText: p.prompt_text ?? "",
    });
    state(tabId).dialog = null;
    return ok();
  }
  if (op === "upload") {
    if (
      !Array.isArray(p.files) ||
      !p.files.length ||
      p.files.some((x) => !x.startsWith("/"))
    )
      throw new BridgeError("invalid_argument", "Absolute file paths required");
    if (!p.locator) {
      const chooser = state(tabId).chooser;
      if (!chooser) throw new BridgeError("failed", "No active file chooser");
      await cdp(tabId, "DOM.setFileInputFiles", {
        backendNodeId: chooser.backendNodeId,
        files: p.files,
      });
      state(tabId).chooser = null;
      return ok();
    }
    const node = await dom(tabId, { ...p, operation: "node" }, true);
    try {
      await node.send("DOM.setFileInputFiles", {
        objectId: node.objectId,
        files: p.files,
      });
    } finally {
      await node.send("Runtime.releaseObject", { objectId: node.objectId });
    }
    return ok();
  }
  if (op === "wait") {
    const until = Date.now() + Math.min(p.timeout_ms ?? 10000, 60000);

    while (Date.now() < until) {
      if (p.event === "download" && state(tabId).downloads.length) {
        const event = state(tabId).downloads.shift();
        const matches = await chrome.downloads.search({
          url: event.url,
          orderBy: ["-startTime"],
          limit: 1,
        });
        return { download: { ...event, ...matches[0] } };
      }
      if (p.event === "filechooser" && state(tabId).chooser)
        return { filechooser: state(tabId).chooser };
      const t = await chrome.tabs.get(tabId);
      if (p.url && t.url?.includes(p.url) && t.status === "complete")
        return { url: t.url };
      if (p.event === "navigation" && t.status === "complete")
        return { url: t.url };
      if (p.locator) {
        const q = await dom(tabId, { ...p, operation: "query" });
        if (
          {
            attached: q.count > 0,
            detached: q.count === 0,
            visible: q.visible,
            hidden: !q.visible,
          }[p.state ?? "visible"]
        )
          return q;
      }
      await sleep(50);
    }
    throw new BridgeError("failed", "Wait deadline expired");
  }
  if (op === "downloads") return { downloads: state(tabId).downloads };
  if (op === "logs") {
    // Runtime.enable replays the messages the page logged before it, as events that precede its reply.
    await ensureDomain(tabId, "Runtime");
    return { logs: state(tabId).logs.slice(-(p.limit ?? 100)) };
  }
  if (op === "viewport") {
    const s = state(tabId);
    const size = async () =>
      (
        await cdp(tabId, "Runtime.evaluate", {
          expression: "[innerWidth, innerHeight]",
          returnByValue: true,
        })
      ).result.value;
    let want;
    if (p.reset) {
      await cdp(tabId, "Emulation.clearDeviceMetricsOverride");
      [want, s.naturalSize] = [s.naturalSize, null];
    } else {
      if (
        !Number.isInteger(p.width) ||
        !Number.isInteger(p.height) ||
        p.width < 1 ||
        p.height < 1
      )
        throw new BridgeError(
          "invalid_argument",
          "Positive width and height required",
        );
      s.naturalSize ??= await size();
      await cdp(tabId, "Emulation.setDeviceMetricsOverride", {
        width: p.width,
        height: p.height,
        deviceScaleFactor: 1,
        mobile: false,
      });
      want = [p.width, p.height];
    }
    // The page resizes some time after the call returns; an action before then aims at the old layout.
    const until = Date.now() + 1000;
    while (want && Date.now() < until) {
      const [width, height] = await size();
      if (width === want[0] && height === want[1]) break;
      await sleep(20);
    }
    return ok();
  }
  if (op === "screenshot") {
    const metrics = await cdp(tabId, "Page.getLayoutMetrics"),
      v = metrics.cssVisualViewport;
    const clip =
      p.clip ??
      (p.full_page
        ? {
            x: 0,
            y: 0,
            width: metrics.cssContentSize.width,
            height: metrics.cssContentSize.height,
          }
        : {
            x: v.pageX,
            y: v.pageY,
            width: v.clientWidth,
            height: v.clientHeight,
          });
    if (
      ![clip.x, clip.y, clip.width, clip.height].every(Number.isFinite) ||
      clip.width <= 0 ||
      clip.height <= 0 ||
      clip.width * clip.height > 40e6
    )
      throw new BridgeError(
        "invalid_argument",
        "Invalid or excessively large screenshot",
      );
    const { result } = await cdp(tabId, "Runtime.evaluate", {
      expression: "devicePixelRatio",
      returnByValue: true,
    });
    // Beyond-viewport capture can temporarily resize the renderer, firing page resize handlers even when the
    // requested clip already fits. Use the ordinary screenshot path for viewport/contained clips; only expand
    // the renderer for an explicitly requested region outside the current viewport.
    const beyondViewport = clip.x < v.pageX || clip.y < v.pageY ||
      clip.x + clip.width > v.pageX + v.clientWidth || clip.y + clip.height > v.pageY + v.clientHeight;
    const { data } = await concealDuring(tabId, () =>
      cdp(tabId, "Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: beyondViewport,
        clip: { ...clip, scale: 1 / (result.value || 1) },
      }),
    );
    const bytes = atob(data.slice(0, 32)),
      u32 = (o) =>
        ((bytes.charCodeAt(o) << 24) |
          (bytes.charCodeAt(o + 1) << 16) |
          (bytes.charCodeAt(o + 2) << 8) |
          bytes.charCodeAt(o + 3)) >>>
        0;
    return {
      data,
      width: u32(16),
      height: u32(20),
      scale: 1,
      origin: { x: clip.x, y: clip.y },
      coordinate_space: "document CSS pixels",
    };
  }
  if (op === "export") return exportContent(tabId, p);
  if (op === "assets") {
    // Responses before this call are found through the page below; later ones are also recorded with their bodies.
    await ensureDomain(tabId, "Network");
    const { result } = await cdp(tabId, "Runtime.evaluate", {
      expression:
        '[location.href,...performance.getEntriesByType("resource").map(x=>x.name),...[...document.querySelectorAll("img[src],video[src],audio[src],source[src],link[href]")].map(x=>x.src||x.href)]',
      returnByValue: true,
    });
    for (const url of result.value ?? [])
      if (
        /^https?:/.test(url) &&
        ![...state(tabId).assets.values()].some((a) => a.url === url)
      )
        state(tabId).assets.set("url:" + url, {
          id: "url:" + url,
          url,
          mime_type: null,
          source: "page_observation",
        });
    const assets = [...state(tabId).assets.values()];
    if (p.action !== "bundle") return { assets };
    const selected = p.asset_ids ?? [];
    if (selected.length > 30)
      throw new BridgeError("invalid_argument", "Maximum 30 assets per bundle");
    return {
      assets: await Promise.all(
        selected.map(async (id) => {
          const item = state(tabId).assets.get(id);
          if (!item)
            throw new BridgeError("invalid_argument", "Unknown observed asset");
          if (!id.startsWith("url:"))
            return {
              ...item,
              ...(await cdp(tabId, "Network.getResponseBody", {
                requestId: id,
              })),
            };
          const expression = `(async()=>{const r=await fetch(${JSON.stringify(item.url)});if(!r.ok)throw Error('Asset fetch failed '+r.status);const bytes=new Uint8Array(await r.arrayBuffer());if(bytes.length>8388608)throw Error('Asset exceeds 8MB');let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));return {body:btoa(binary),base64Encoded:true,mime_type:r.headers.get('content-type')}})()`;
          const response = await cdp(tabId, "Runtime.evaluate", {
            expression,
            awaitPromise: true,
            returnByValue: true,
          });
          if (response.exceptionDetails)
            throw new BridgeError(
              "failed",
              "Could not read observed asset: " + item.url,
            );
          return { ...item, ...response.result.value };
        }),
      ),
    };
  }
  if (op === "cdp") {
    return { result: await cdp(tabId, p.method, p.params ?? {}) };
  }
  if (op === "clipboard") {
    if (p.action === "write") await requireAgentInput(tabId, session);
    const expression =
      p.action === "write"
        ? `navigator.clipboard.writeText(${JSON.stringify(p.text)}).then(()=>({ok:true}))`
        : "navigator.clipboard.readText().then(text=>({text}))";
    const result = await cdp(tabId, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails)
      throw new BridgeError("failed", "Clipboard unavailable on this origin");
    return result.result.value;
  }

  throw new BridgeError("invalid_argument", `Unknown operation ${op}`);
}

// A click that opens a modal dialog may leave its CDP command pending until the dialog
// is answered. Return the observed dialog so the client can answer it without deadlock.
export async function withDialog(tabId, work) {
  const s = state(tabId);
  let wake;
  const dialog = new Promise((resolve) => {
    wake = (p) => resolve({ dialog_open: true, dialog: p });
    s.dialogWaiters.add(wake);
  });
  try {
    return await Promise.race([work(), dialog]);
  } finally {
    s.dialogWaiters.delete(wake);
  }
}
