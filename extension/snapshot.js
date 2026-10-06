import { cdp, frameCDP, frameSession } from "./cdp.js";

const MAX_LINES = 2000;
// Lines vary in length (a control's line holds all its text), so a long page is also cut by size.
const MAX_CHARS = 80000;
const MAX_TEXT = 150;

const INTERACTIVE = new Set([
  "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox", "option",
  "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider", "spinbutton", "treeitem",
  "MenuListOption", "PopUpButton", "ToggleButton", "DisclosureTriangle", "ColorWell", "Date", "DateTime",
  "InputTime",
]);

// Printed even without a name because they carry page structure.
const STRUCTURAL = new Set([
  "RootWebArea", "heading", "list", "listitem", "table", "row", "cell", "columnheader", "rowheader", "grid",
  "gridcell", "image", "img", "figure", "form", "main", "navigation", "banner", "contentinfo", "complementary",
  "region", "search", "dialog", "alertdialog", "alert", "article", "tablist", "tabpanel", "menu", "menubar",
  "toolbar", "tree", "Iframe",
]);

const SKIPPED = new Set(["InlineTextBox", "LineBreak", "ListMarker"]);

const NOT_REFERENCEABLE = new Set(["RootWebArea", "Iframe"]);

const ROLE_NAMES = {
  RootWebArea: "document",
  StaticText: "text",
  MenuListOption: "option",
  PopUpButton: "combobox",
  Iframe: "iframe",
};

// A control's own line carries its visible text (a product card: title, price, rating, delivery), so it may run longer.
const MAX_CONTROL_TEXT = 260;

function clean(text, max = MAX_TEXT) {
  if (text == null) return "";
  const flat = String(text).replace(/\s+/g, " ").trim();
  // A URL inside other text (an image's src in an alt, a tracking link) costs many tokens and tells nothing; a
  // text that is only a URL is what the page shows, so it stays.
  const words = flat.replace(/\S*https?:\/\/\S*/g, "").replace(/\s+/g, " ").trim();
  const kept = words || flat;
  return kept.length > max ? `${kept.slice(0, max)}…` : kept;
}

const quote = (text) => text.replace(/\\/g, "\\\\").replace(/"/g, '\\"');

function property(node, name) {
  return node.properties?.find((p) => p.name === name)?.value?.value;
}

function states(node) {
  const words = [];
  const checked = property(node, "checked");
  if (checked === "true" || checked === true) words.push("checked");
  if (checked === "mixed") words.push("mixed");
  if (property(node, "pressed") === "true") words.push("pressed");
  if (property(node, "selected") === true) words.push("selected");
  const expanded = property(node, "expanded");
  if (expanded === true) words.push("expanded");
  if (expanded === false) words.push("collapsed");
  if (property(node, "disabled") === true) words.push("disabled");
  if (property(node, "focused") === true) words.push("focused");
  return words.length ? ` ${words.join(" ")}` : "";
}

function isNative(node, role) {
  if (NOT_REFERENCEABLE.has(role) || !node.backendDOMNodeId) return false;
  return INTERACTIVE.has(role) || property(node, "focusable") === true;
}

const isInteractive = (node, role) => isNative(node, role) || !!node.hint;

const REDACTED = "[redacted]";

/**
 * Whether a field holds a password or payment card detail, whose value no read returns: by its type or autocomplete,
 * or, for a field with a value, by its accessible name or the bullets Chrome shows for a password. extended.js runs
 * this in the page from its source, so it must not refer to anything outside itself.
 */
export function isSecretField({ type, autocomplete, name, value }) {
  const tokens = ["cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year", "current-password", "new-password"];
  const label = /password|passcode|card ?(?:number|no\b|#)|credit ?card|\bcv[cv2]\b|\bcsc\b|security code|expir(?:y|ation)/i;
  value = String(value ?? "");
  return (
    String(type ?? "").toLowerCase() === "password" ||
    String(autocomplete ?? "").toLowerCase().split(/\s+/).some((t) => tokens.includes(t)) ||
    (!!value && (label.test(String(name ?? "")) || /^•+$/.test(value)))
  );
}

// Pages make elements clickable with a listener and a pointer cursor, and scroll panels with overflow; the
// accessibility tree marks neither. One DOMSnapshot per renderer process supplies both, joined by backend node id.
const FACT_STYLES = ["cursor", "overflow-x", "overflow-y"];
const SCROLLS = new Set(["auto", "scroll", "overlay"]);

/**
 * backendNodeId → { hint, secret, tiny } for the documents of one renderer process. A scroll panel is an overflow
 * auto or scroll box whose content reaches past its edges. Layout bounds are absolute and unclipped, so a scrolled
 * panel's content reaches past its top instead, and content counts only for its nearest box that clips.
 */
async function domFacts(send, strict = false) {
  const { documents = [], strings = [] } = await send("DOMSnapshot.captureSnapshot", { computedStyles: FACT_STYLES });
  if (strict && !documents.length) throw Error("Privacy fact coverage is incomplete");
  const facts = new Map();
  const note = (id, fact) => facts.set(id, { hint: "", ...facts.get(id), ...fact });
  for (const { nodes, layout } of documents) {
    if (strict) for (const id of nodes.backendNodeId) note(id, { observed: true });
    const box = new Map(layout.nodeIndex.map((node, i) => [node, i]));
    const style = (node, k) => (box.has(node) ? strings[layout.styles[box.get(node)][k]] : "");
    const attr = (node, name) => {
      const pairs = nodes.attributes[node] ?? [];
      for (let k = 0; k < pairs.length; k += 2) if (strings[pairs[k]].toLowerCase() === name) return strings[pairs[k + 1]];
    };
    // Option values may be absent from AX. Native/related labels, including
    // superseded ones, come from AX name sources below: Chrome resolves their
    // tree scope correctly, while DOMSnapshot flattens shadow roots away.
    const descendants = nodes.parentIndex.map(() => []);
    for (let node = 0; node < nodes.parentIndex.length; node++) {
      const parent = nodes.parentIndex[node];
      if (parent >= 0) descendants[parent].push(node);
    }
    const textContent = (node) => nodes.nodeType[node] === 3
      ? strings[nodes.nodeValue?.[node]] ?? ""
      : descendants[node].map(textContent).join("");
    const sensitivityName = (node) => [attr(node, "aria-label"), attr(node, "placeholder"), attr(node, "title")]
      .filter(Boolean).join(" ");
    const nativeValues = new Map();
    if (strict) for (const sparse of [nodes.inputValue, nodes.textValue]) {
      for (let i = 0; i < (sparse?.index?.length ?? 0); i++) {
        const at = sparse.index[i], value = sparse.value?.[i];
        if (!Number.isInteger(at) || at < 0 || at >= nodes.backendNodeId.length
          || !Number.isInteger(value) || value < -1 || value >= strings.length)
          throw Error("Native field value coverage is incomplete");
        nativeValues.set(at, value === -1 ? "" : strings[value]);
      }
    }
    if (strict) {
      const overlays = new Set();
      for (let node = 0; node < nodes.parentIndex.length; node++) {
        if (attr(node, "id") === "second-cursor-overlay" || overlays.has(nodes.parentIndex[node])) {
          overlays.add(node);
          note(nodes.backendNodeId[node], { expectExcluded: true });
        }
      }
    }
    const sensitiveSelects = new Set();
    for (let node = 0; node < nodes.parentIndex.length; node++) {
      const tag = strings[nodes.nodeName[node]];
      if (strict && tag === "SELECT") note(nodes.backendNodeId[node], { nativeSelectHost: true });
      if (strict && ["INPUT", "TEXTAREA"].includes(tag)) {
        note(nodes.backendNodeId[node], { nativeValueHost: true,
          ...(nativeValues.has(node) ? { nativeValue: nativeValues.get(node) } : {}) });
        if (nativeValues.has(node) && isSecretField({ type: attr(node, "type"), autocomplete: attr(node, "autocomplete"),
          name: sensitivityName(node), value: nativeValues.get(node) })) note(nodes.backendNodeId[node], { secret: true });
      }
      if (!["INPUT", "TEXTAREA", "SELECT"].includes(tag)) continue;
      if (isSecretField({ type: attr(node, "type"), autocomplete: attr(node, "autocomplete"),
        name: sensitivityName(node), value: "present" })) {
        note(nodes.backendNodeId[node], { secret: true });
        if (tag === "SELECT") sensitiveSelects.add(node);
      }
    }
    const selected = new Set(nodes.optionSelected?.index ?? []);
    for (let node = 0; node < nodes.parentIndex.length; node++) {
      if (strings[nodes.nodeName[node]] !== "OPTION") continue;
      let owner = nodes.parentIndex[node];
      while (owner >= 0 && strings[nodes.nodeName[owner]] !== "SELECT") owner = nodes.parentIndex[owner];
      const value = attr(node, "value") ?? textContent(node);
      if (!sensitiveSelects.has(owner) && !isSecretField({ value })) continue;
      note(nodes.backendNodeId[node], { secret: true });
      // A mixed select keeps its ordinary choices readable; only its selected
      // value is masked when a sensitive option is selected.
      if (owner >= 0 && selected.has(node)) note(nodes.backendNodeId[owner], { secretValue: true });
    }
    const cursor = (node) => {
      for (let n = node; n >= 0; n = nodes.parentIndex[n]) if (box.has(n)) return style(n, 0);
      return "";
    };
    const clips = (node) => box.has(node) && (style(node, 1) !== "visible" || style(node, 2) !== "visible");
    const listens = new Set(nodes.isClickable?.index);
    // Nodes come parent first, so each node's nearest clipping ancestor is known before its children are reached.
    const clipper = new Int32Array(nodes.parentIndex.length).fill(-1);
    const panels = new Map();
    for (let node = 0; node < nodes.parentIndex.length; node++) {
      const parent = nodes.parentIndex[node];
      if (parent >= 0) clipper[node] = clips(parent) ? parent : clipper[parent];
      if (nodes.nodeType[node] !== 1 || !box.has(node)) continue;
      const tag = strings[nodes.nodeName[node]];
      // The cursor is inherited, so only the element that sets it is the target, unless it listens itself.
      if ((style(node, 0) === "pointer" && (listens.has(node) || cursor(parent) !== "pointer")) || attr(node, "onclick") !== undefined)
        note(nodes.backendNodeId[node], { hint: "clickable" });
      const [x, y, width, height] = layout.bounds[box.get(node)];
      if (tag === "IFRAME" && (width <= 2 || height <= 2)) note(nodes.backendNodeId[node], { tiny: true });
      // The page's own scrolling belongs to the viewport, not to <html> or <body>.
      if ((SCROLLS.has(style(node, 1)) || SCROLLS.has(style(node, 2))) && tag !== "HTML" && tag !== "BODY")
        panels.set(node, { edges: [x, y, x + width, y + height], reach: [x, y, x + width, y + height] });
    }
    if (!panels.size) continue;
    layout.nodeIndex.forEach((node, i) => {
      const reach = panels.get(clipper[node])?.reach;
      const [x, y, width, height] = layout.bounds[i];
      if (!reach || (!width && !height)) return;
      reach[0] = Math.min(reach[0], x);
      reach[1] = Math.min(reach[1], y);
      reach[2] = Math.max(reach[2], x + width);
      reach[3] = Math.max(reach[3], y + height);
    });
    for (const [node, { edges, reach }] of panels) {
      const across = SCROLLS.has(style(node, 1)) && (reach[0] < edges[0] - 1 || reach[2] > edges[2] + 1);
      const down = SCROLLS.has(style(node, 2)) && (reach[1] < edges[1] - 1 || reach[3] > edges[3] + 1);
      if (!across && !down) continue;
      const id = nodes.backendNodeId[node];
      note(id, { hint: [facts.get(id)?.hint, "scrollable"].filter(Boolean).join(" ") });
    }
  }
  return facts;
}

/**
 * Drops hints a control's own ref already covers, and marks secret fields: by autocomplete or type (from the DOM),
 * by label, or by the bullets Chrome shows for a password. Nodes inside a secret field are marked too.
 */
function annotate(node, byId, inControl = false, inSecret = false) {
  const role = node.role?.value ?? "";
  const native = isNative(node, role);
  if (node.hint && (node.ignored || inControl || native || NOT_REFERENCEABLE.has(role) || role === "LabelText")) node.hint = "";
  // Keep superseded but valid naming sources as sensitivity evidence. Chrome
  // may omit DOM facts during a detach; the AX sources still protect values.
  const sourceNames = (node.name?.sources ?? []).filter(source => !source.invalid).flatMap(source => [
    source.value?.value, ...(source.value?.relatedNodes ?? []).map(related => related.text),
    ...(source.nativeSourceValue?.relatedNodes ?? []).map(related => related.text),
    source.attribute === "aria-label" ? source.attributeValue?.value : "",
  ]);
  const name = [node.name?.value, ...sourceNames].filter(Boolean).join(" ");
  const field = ["textbox", "searchbox", "combobox", "PopUpButton", "listbox", "spinbutton"].includes(role);
  if (isSecretField({ name, value: node.value?.value }) || (field && isSecretField({ name, value: "present" }))) node.secret = true;
  node.withinSecret = inSecret;
  for (const child of children(node, byId)) annotate(child, byId, inControl || native, inSecret || !!node.secret);
}

const redact = (node, value) => (value && (node.secret || node.secretValue || node.withinSecret) ? REDACTED : value);
const readName = (node, value) => value && node.secret && ["option", "MenuListOption"].includes(node.role?.value) ? REDACTED : value;

// Containers whose line a repeated name may drop; landmarks, lists, tables and headings always keep theirs.
const WRAPPERS = new Set(["group", "generic", "region", "article", "listitem", "figure", "section"]);

const children = (node, byId) => (node.childIds ?? []).map((id) => byId.get(id)).filter(Boolean);

function hasControlInside(node, byId) {
  return children(node, byId).some(
    (c) => (!c.ignored && isInteractive(c, c.role?.value ?? "")) || hasControlInside(c, byId),
  );
}

function visibleText(node, byId, out = []) {
  for (const child of children(node, byId)) {
    if (child.secret || child.withinSecret) continue;
    const role = child.role?.value ?? "";
    if (SKIPPED.has(role)) continue;
    if (role === "StaticText") {
      const text = child.ignored ? "" : clean(child.name?.value, Infinity);
      if (text && out.at(-1) !== text) out.push(text);
    } else visibleText(child, byId, out);
  }
  return out;
}

// A lone currency sign or opening mark ("$", "S$", "€", "[") belongs to the text after it, and a closing mark
// ("]", ")", ",", "%") to the text before it, not in a list of their own.
const OPENS = /^(?:\p{L}{0,3}\p{Sc}|\p{Ps}|\p{Pi}|[#@~+±-])$/u;
const CLOSES = /^(?:\p{Pe}|\p{Pf}|[.,;:!?%'"]){1,2}$/u;
const joinTexts = (texts) =>
  texts.reduce((line, text, i) => {
    if (i === 0) return text;
    return OPENS.test(texts[i - 1]) || CLOSES.test(text) ? line + text : `${line} · ${text}`;
  }, "");

/** A control with no controls inside keeps its accessible name and useful visible text on one line. */
function controlLabel(node, byId) {
  if (node.secret) return readName(node, clean(node.name?.value, MAX_CONTROL_TEXT));
  const texts = visibleText(node, byId);
  const name = clean(node.name?.value, Infinity);
  if (!texts.length) return clean(name, MAX_CONTROL_TEXT);
  // The name is the visible text as the page phrases it ("[19]", "v2.1 Latest"): keep it.
  if (name.replace(/\s/g, "") === texts.join("").replace(/\s/g, "")) return clean(name, MAX_CONTROL_TEXT);
  const shown = joinTexts(texts);
  // Only a name computed from contents may lose extra image-alt fragments. An
  // explicit label can distinguish several controls with the same visible text
  // ("Open message"), so that text covering part of the name is not redundancy.
  // Ignore superseded/invalid candidates; when Chrome omits source metadata,
  // conservatively retain the more informative accessible name.
  const source = node.name?.sources?.find((candidate) =>
    !candidate.superseded && !candidate.invalid && clean(candidate.value?.value, Infinity) === name,
  );
  if (name && source?.type !== "contents") {
    if (texts.every((text) => name.includes(text))) return clean(name, MAX_CONTROL_TEXT);
    if (!shown.includes(name)) return clean(`${name} · ${shown}`, MAX_CONTROL_TEXT);
  }
  // A name built from the control's content (plus image alt text) says nothing the texts do not.
  const covered = texts.filter((t) => name.includes(t)).length >= Math.ceil(texts.length / 2);
  const label = !name || covered || shown.includes(name) ? shown : `${name} · ${shown}`;
  return clean(label, MAX_CONTROL_TEXT);
}

const refOf = (node) => node.ref ?? `e${node.backendDOMNodeId}`;

/**
 * Renders the accessibility tree as an indented outline. Interactive nodes carry `[e<backendDOMNodeId>]`
 * refs (`[f<frame>e<id>]` inside a frame Chrome runs in another process), which stay valid for as long as the DOM
 * node exists. A control with no controls inside (a card link, a labelled button) is a single line retaining explicit
 * accessible labels and useful visible text; unnamed images and empty containers are left out, and a wrapper whose whole name is already printed inside
 * it gives up its own line. Elements only a listener or pointer cursor makes clickable, and scroll panels, carry a
 * `clickable` or `scrollable` hint; password and payment card values are redacted.
 */
export function renderOutline(nodes) {
  const byId = new Map(nodes.map((n) => [n.nodeId, n]));
  const root = nodes.find((n) => !n.parentId) ?? nodes[0];
  if (root) annotate(root, byId);

  const line = (node, role, depth, name, interactive) => {
    const value = redact(node, clean(node.value?.value));
    const ref = interactive ? `[${refOf(node)}] ` : "";
    const bare = node.hint && (role === "generic" || role === "none");
    let text = `${ref}${bare ? node.hint : (ROLE_NAMES[role] ?? role)}`;
    if (name) text += ` "${quote(name)}"`;
    if (value && value !== name) text += ` value="${quote(value)}"`;
    const hint = node.hint && !bare ? ` ${node.hint}` : "";
    return { depth, text: text + (node === root ? "" : states(node)) + hint, plain: role === "StaticText", name };
  };

  // Lines are { depth, text }; `context` is the name and value of the nearest printed ancestor, whose text is
  // not repeated below it.
  const visit = (node, depth, context) => {
    const role = node.role?.value ?? "";
    if (SKIPPED.has(role) || node.withinSecret) return [];
    const inside = (d, c) => mergeText(children(node, byId).flatMap((child) => visit(child, d, c)));
    // A frame's document reads as the content of its iframe line.
    if (node.ignored || (role === "RootWebArea" && node !== root)) return inside(depth, context);

    const interactive = isInteractive(node, role);
    // A scroll panel lists what it holds rather than folding it into one line.
    if (interactive && !node.hint?.includes("scrollable") && !hasControlInside(node, byId)) {
      // A field's visible text is its value, printed once as value="…".
      const label = node.value?.value && property(node, "editable") ? clean(node.name?.value, MAX_CONTROL_TEXT) : controlLabel(node, byId);
      return [line(node, role, depth, label, true)];
    }
    const name = readName(node, clean(node.name?.value));
    if (role === "StaticText") return name && !context.includes(name) ? [line(node, role, depth, name, false)] : [];
    if ((role === "image" || role === "img") && !name) return [];
    if (!interactive && !name && !STRUCTURAL.has(role)) return inside(depth, context);

    const below = inside(depth + 1, `${name}\n${redact(node, clean(node.value?.value))}`);
    if (!interactive && !below.length && (!name || role === "listitem" || role === "list")) return [];
    if (!interactive && WRAPPERS.has(role) && name.length >= 12 && below.some((l) => l.text.includes(quote(name)))) {
      return below.map((l) => ({ ...l, depth: l.depth - 1 }));
    }
    return [line(node, role, depth, name, interactive), ...below];
  };

  const lines = root ? visit(root, 0, "") : [];
  const text = [];
  let size = 0;
  for (const l of lines) {
    const printed = `${"  ".repeat(l.depth)}${l.text}`;
    if (text.length >= MAX_LINES || size + printed.length > MAX_CHARS) break;
    text.push(printed);
    size += printed.length + 1;
  }
  if (text.length < lines.length) text.push(`… ${lines.length - text.length} more lines omitted`);
  return text.join("\n");
}

/** Adjacent text lines at one depth ("1", "/", "14") read as one ("1 / 14"). */
function mergeText(lines) {
  const out = [];
  for (const l of lines) {
    const last = out.at(-1);
    if (last?.plain && l.plain && last.depth === l.depth) {
      const name = `${last.name} ${l.name}`;
      out[out.length - 1] = { ...last, name, text: `text "${quote(name)}"` };
    } else out.push(l);
  }
  return out;
}

// Per tab, refs bind to the renderer session that issued their backend IDs: ordinal → { frameId, session, parent,
// owner }. Replacement sessions get new ordinals so previously returned refs can never retarget a new renderer.
// owner is the <iframe>'s backend node id in the parent frame (ordinal 0 is the tab's own process).
const isolatedFrames = new Map();
chrome.tabs.onRemoved.addListener((tabId) => isolatedFrames.delete(tabId));

function frameOrdinal(tabId, frameId, session, parent, owner) {
  const frames = isolatedFrames.get(tabId) ?? new Map();
  isolatedFrames.set(tabId, frames);
  for (const [n, f] of frames) {
    if (f.frameId !== frameId || f.session !== session || f.parent !== parent || f.owner !== owner) continue;
    return n;
  }
  frames.set(frames.size + 1, { frameId, session, parent, owner });
  return frames.size;
}

export const isolatedFrame = (tabId, ordinal) => isolatedFrames.get(tabId)?.get(ordinal);

/**
 * The tab's accessibility nodes with every frame's document stitched under its iframe node. getFullAXTree stops at
 * iframes, so each frame is read on its own: through its process's session when Chrome isolated it, else by frame id.
 * Node ids are namespaced per frame; each node carries its ref and its DOM facts.
 */
async function collect(tabId, { strict = false, guard = async () => {} } = {}) {
  const all = [];
  let reads = 0;
  const read = async (renderer, frameId, owner) => {
    const key = reads++;
    const [{ nodes = [] }, facts] = await Promise.all([
      renderer.send("Accessibility.getFullAXTree", frameId !== renderer.frameId ? { frameId } : {}),
      renderer.facts,
    ]);
    if (strict && !nodes.some(n => !n.parentId && n.role?.value === "RootWebArea"))
      throw Error("Frame accessibility coverage is incomplete");
    if (strict) {
      // Chrome exposes native input/textarea value renderers in AX but omits those user-agent shadow nodes from
      // DOMSnapshot. They duplicate their DOM-backed host's full AX value and are never independent targets.
      // Exclude that internal subtree instead of trusting uncovered text; an unreadable host cannot establish
      // absence when one of its internal renderers still contains text. Every other AX backend still needs facts.
      const byID = new Map(nodes.map(n => [n.nodeId, n]));
      const coveredModals = new Set(nodes.filter(n => !n.ignored && ["dialog", "alertdialog"].includes(n.role?.value)
        && facts.get(n.backendDOMNodeId)?.observed).map(n => n.backendDOMNodeId));
      const inactiveFor = node => node?.ignored === true && node.role?.value === "none"
        && !node.name?.value && node.value?.value == null
        ? (node.ignoredReasons ?? []).filter(reason => reason.name === "activeModalDialog")
          .flatMap(reason => reason.value?.relatedNodes ?? []).map(related => related.backendDOMNodeId)
          .filter(id => coveredModals.has(id)) : [];
      for (const node of nodes) {
        // aria-owns may place a real suggestion/listbox subtree beneath an input in AX. DOM-backed descendants
        // are ordinary public content, not native internals, and must remain visible to text/gone conditions.
        if (node.backendDOMNodeId && facts.get(node.backendDOMNodeId)?.observed) continue;
        // A native SELECT's empty menu container is exposed only in AX. Its options remain ordinary DOM-backed
        // nodes and must all be present in the captured facts; omit only this structural wrapper, never its text
        // or descendants. This is deliberately distinct from the duplicate INPUT/TEXTAREA value renderers.
        const selectParent = byID.get(node.parentId);
        const nativeSelectPopup = node.role?.type === "internalRole" && node.role.value === "MenuListPopup"
          && !node.name?.value && (node.value?.value == null || node.value.value === "")
          && facts.get(selectParent?.backendDOMNodeId)?.nativeSelectHost
          && (node.childIds ?? []).every(id => {
            const child = byID.get(id);
            return child?.backendDOMNodeId && facts.get(child.backendDOMNodeId)?.observed;
          });
        if (nativeSelectPopup) { node.expectExcluded = true; continue; }
        // showModal reparents Chrome's empty, ignored native renderers outside their hosts. A SELECT wrapper
        // retains its option leaves, so each child must itself be covered, empty, ignored and bound to the same
        // covered modal. Never accept an unknown child, readable content or an unchecked descendant subtree.
        const inactiveModalRenderer = inactiveFor(node).some(modal => (node.childIds ?? []).every(id => {
          const child = byID.get(id);
          return child?.parentId === node.nodeId && facts.get(child.backendDOMNodeId)?.observed
            && !(child.childIds?.length) && inactiveFor(child).includes(modal);
        }));
        if (inactiveModalRenderer) continue;
        const visited = new Set();
        let parent = byID.get(node.parentId), host;
        while (parent && !visited.has(parent.nodeId)) {
          visited.add(parent.nodeId);
          const covered = facts.get(parent.backendDOMNodeId);
          if (covered?.observed) {
            if (covered.nativeValueHost) host = parent;
            break;
          }
          parent = byID.get(parent.parentId);
        }
        if (host) {
          const hostFact = facts.get(host.backendDOMNodeId);
          const hostValue = host.value?.value ?? hostFact.nativeValue;
          // A concurrent aria-owns update can also create an uncovered descendant. Only plain value-rendering
          // nodes whose content is already represented by the known host qualify; distinct text, values or
          // interactive controls are incomplete coverage, never silently absent.
          const duplicateRole = ["generic", "none", "StaticText", "InlineTextBox"].includes(node.role?.value);
          const fragments = [node.name?.value, node.value?.value].filter(v => v != null && String(v) !== "");
          const hostTexts = [hostValue, host.name?.value].filter(v => v != null).map(String);
          if (hostValue == null && fragments.length && !node.ignored)
            throw Error("Native field value coverage is incomplete");
          if (!duplicateRole || fragments.some(v => !hostTexts.some(text => text.includes(String(v)))))
            throw Error("Native field descendant coverage is incomplete");
          node.expectExcluded = true;
        } else if (node.backendDOMNodeId && !facts.get(node.backendDOMNodeId)?.observed) {
          throw Error("Privacy fact coverage is incomplete");
        }
      }
    }
    const id = (nodeId) => `${key}:${nodeId}`;
    const prefix = renderer.ordinal ? `f${renderer.ordinal}` : "";
    const own = nodes.map((n) => ({
      ...n,
      nodeId: id(n.nodeId),
      parentId: n.parentId && id(n.parentId),
      childIds: n.childIds?.map(id),
      ref: n.backendDOMNodeId && `${prefix}e${n.backendDOMNodeId}`,
      ...facts.get(n.backendDOMNodeId),
    }));
    const top = own.find((n) => !n.parentId);
    if (owner && top) {
      top.parentId = owner.nodeId;
      owner.childIds = [...(owner.childIds ?? []), top.nodeId];
    }
    all.push(...own);
    // Hidden and tiny iframes (ads, trackers) show the user nothing, so their documents are not read.
    await Promise.all(
      own
        .filter((n) => n.role?.value === "Iframe" && !n.ignored && !n.tiny && n.backendDOMNodeId && !n.childIds?.length)
        .map(async (iframe) => {
          const { node } = await renderer.send("DOM.describeNode", { backendNodeId: iframe.backendDOMNodeId }).catch(error => {
            if (strict) throw error;
            return {};
          });
          const frameId = node?.frameId;
          if (!frameId) {
            if (strict) throw Error("Frame coverage is incomplete");
            return;
          }
          // A frame that detaches mid-read leaves its iframe line empty rather than failing the snapshot.
          const session = frameSession(tabId, frameId);
          if (!session) return read(renderer, frameId, iframe).catch(error => { if (strict) throw error; });
          const send = async (method, params) => {
            await guard();
            return frameCDP(tabId, frameId, method, params, { expectedSession: session });
          };
          const ordinal = frameOrdinal(tabId, frameId, session, renderer.ordinal, iframe.backendDOMNodeId);
          const facts = domFacts(send, strict).catch(error => { if (strict) throw error; return new Map(); });
          return read({ send, ordinal, frameId, facts }, frameId, iframe).catch(error => { if (strict) throw error; });
        }),
    );
  };
  const send = async (method, params) => { await guard(); return cdp(tabId, method, params); };
  await read({ send, ordinal: 0, frameId: null, facts: domFacts(send, strict).catch(error => { if (strict) throw error; return new Map(); }) }, null, null);
  return all;
}

/** Complete, untruncated public AX content for postconditions, never serialized into a tool reply. A failed frame
 * or privacy-fact read is an unavailable check, not evidence that text disappeared. Document identities exclude
 * URLs and are checked around the read so a navigation cannot combine the old and new page into one sample. */
export async function conditionSnapshot(tabId, guard = async () => {}) {
  const identity = async () => {
    await guard();
    const { frameTree } = await cdp(tabId, "Page.getFrameTree");
    if (!frameTree?.frame?.id || !frameTree.frame.loaderId) throw Error("Document identity unavailable");
    const frames = [];
    const walk = tree => {
      frames.push([tree.frame.id, tree.frame.loaderId ?? "", frameSession(tabId, tree.frame.id) ?? ""]);
      for (const child of tree.childFrames ?? []) walk(child);
    };
    walk(frameTree);
    return { document: `${frameTree.frame.id}:${frameTree.frame.loaderId}`, frames: JSON.stringify(frames.sort()) };
  };
  const before = await identity();
  const nodes = await collect(tabId, { strict: true, guard });
  const byId = new Map(nodes.map(n => [n.nodeId, n]));
  const root = nodes.find(n => !n.parentId);
  if (!root) throw Error("Accessibility coverage is incomplete");
  annotate(root, byId);
  const after = await identity();
  if (before.frames !== after.frames) throw Error("Document changed during the condition check");
  return { document: after.document, elements: nodes.filter(n => !n.ignored && !n.withinSecret && !n.expectExcluded
    && !["RootWebArea", "Iframe", ...SKIPPED].includes(n.role?.value)).map(n => ({
      ref: n.ref, name: readName(n, String(n.name?.value ?? "")),
      value: n.secret || n.secretValue || (n.value?.value == null && n.nativeValue === undefined)
        ? null : String(n.value?.value ?? n.nativeValue),
      secret: !!(n.secret || n.secretValue),
    })) };
}

export async function snapshot(tabId) {
  return renderOutline(await collect(tabId));
}

export async function structuredSnapshot(tabId, guard) {
  const nodes=await collect(tabId, { guard });
  const outline=renderOutline(nodes);
  const elements=nodes.filter(n=>!n.ignored&&n.backendDOMNodeId&&!n.withinSecret).map(n=>{
    const value=redact(n,String(n.value?.value??""));
    return {
      ref:n.ref,role:n.role?.value??"",name:readName(n,n.name?.value??""),value:value.slice(0,300),
      value_length:value.length,
      states:Object.fromEntries(["disabled","focused","selected","expanded","checked","editable"].map(k=>[k,property(n,k)??null])),
      actions:isInteractive(n,n.role?.value)?["click",...(property(n,"editable")?["fill","type","select_text"]:[])]:[]
    };
  });
  return {outline,elements};
}
