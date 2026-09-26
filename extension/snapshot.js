import { cdp } from "./cdp.js";

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

function isInteractive(node, role) {
  if (NOT_REFERENCEABLE.has(role) || !node.backendDOMNodeId) return false;
  return INTERACTIVE.has(role) || property(node, "focusable") === true;
}

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

/** A control with no controls inside is one line: its visible text, or its name when it shows none. */
function controlLabel(node, byId) {
  const texts = visibleText(node, byId);
  const name = clean(node.name?.value, Infinity);
  if (!texts.length) return clean(name, MAX_CONTROL_TEXT);
  // The name is the visible text as the page phrases it ("[19]", "v2.1 Latest"): keep it.
  if (name.replace(/\s/g, "") === texts.join("").replace(/\s/g, "")) return clean(name, MAX_CONTROL_TEXT);
  const shown = joinTexts(texts);
  // A name built from the control's content (plus image alt text) says nothing the texts do not.
  const covered = texts.filter((t) => name.includes(t)).length >= Math.ceil(texts.length / 2);
  const label = !name || covered || shown.includes(name) ? shown : `${name} · ${shown}`;
  return clean(label, MAX_CONTROL_TEXT);
}

/**
 * Renders the accessibility tree as an indented outline. Interactive nodes carry `[e<backendDOMNodeId>]`
 * refs, which stay valid for as long as the DOM node exists. A control with no controls inside (a card link, a
 * labelled button) is a single line with its visible text; unnamed images and empty containers are left out, and
 * a wrapper whose whole name is already printed inside it gives up its own line.
 */
export function renderOutline(nodes) {
  const byId = new Map(nodes.map((n) => [n.nodeId, n]));
  const root = nodes.find((n) => !n.parentId) ?? nodes[0];

  const line = (node, role, depth, name, interactive) => {
    const value = clean(node.value?.value);
    const ref = interactive ? `[e${node.backendDOMNodeId}] ` : "";
    let text = `${ref}${ROLE_NAMES[role] ?? role}`;
    if (name) text += ` "${quote(name)}"`;
    if (value && value !== name) text += ` value="${quote(value)}"`;
    return { depth, text: text + (node === root ? "" : states(node)), plain: role === "StaticText", name };
  };

  // Lines are { depth, text }; `context` is the name and value of the nearest printed ancestor, whose text is
  // not repeated below it.
  const visit = (node, depth, context) => {
    const role = node.role?.value ?? "";
    if (SKIPPED.has(role)) return [];
    const inside = (d, c) => mergeText(children(node, byId).flatMap((child) => visit(child, d, c)));
    if (node.ignored) return inside(depth, context);

    const interactive = isInteractive(node, role);
    if (interactive && !hasControlInside(node, byId)) {
      return [line(node, role, depth, controlLabel(node, byId), true)];
    }
    const name = clean(node.name?.value);
    if (role === "StaticText") return name && !context.includes(name) ? [line(node, role, depth, name, false)] : [];
    if ((role === "image" || role === "img") && !name) return [];
    if (!interactive && !name && !STRUCTURAL.has(role)) return inside(depth, context);

    const below = inside(depth + 1, `${name}\n${clean(node.value?.value)}`);
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

export async function snapshot(tabId) {
  const { nodes } = await cdp(tabId, "Accessibility.getFullAXTree", {});
  return renderOutline(nodes ?? []);
}

export async function structuredSnapshot(tabId) {
  const {nodes=[]}=await cdp(tabId,"Accessibility.getFullAXTree",{});
  const elements=nodes.filter(n=>!n.ignored&&n.backendDOMNodeId).map(n=>({
    ref:`e${n.backendDOMNodeId}`,role:n.role?.value??"",name:n.name?.value??"",value:String(n.value?.value??"").slice(0,300),
    value_length:String(n.value?.value??"").length,
    states:Object.fromEntries(["disabled","focused","selected","expanded","checked","editable"].map(k=>[k,property(n,k)??null])),
    actions:isInteractive(n,n.role?.value)?["click",...(property(n,"editable")?["fill","type","select_text"]:[])]:[]
  }));
  return {outline:renderOutline(nodes),elements};
}
