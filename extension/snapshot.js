import { cdp } from "./cdp.js";

const MAX_LINES = 2000;
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

function clean(text) {
  if (text == null) return "";
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > MAX_TEXT ? `${flat.slice(0, MAX_TEXT)}…` : flat;
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

/**
 * Renders the accessibility tree as an indented outline. Interactive nodes carry `[e<backendDOMNodeId>]`
 * refs, which stay valid for as long as the DOM node exists.
 */
export function renderOutline(nodes) {
  const byId = new Map(nodes.map((n) => [n.nodeId, n]));
  const root = nodes.find((n) => !n.parentId) ?? nodes[0];
  const lines = [];
  let omitted = 0;

  // `context` is the name and value of the nearest printed ancestor; text repeating it is dropped.
  const visit = (node, depth, context) => {
    const role = node.role?.value ?? "";
    if (SKIPPED.has(role)) return;
    const name = clean(node.name?.value);
    let childDepth = depth;
    let childContext = context;

    if (!node.ignored) {
      const interactive = isInteractive(node, role);
      const print = role === "StaticText"
        ? name && !context.includes(name)
        : interactive || name || STRUCTURAL.has(role);
      if (print) {
        const value = clean(node.value?.value);
        if (lines.length >= MAX_LINES) {
          omitted++;
        } else {
          const ref = interactive ? `[e${node.backendDOMNodeId}] ` : "";
          let line = `${"  ".repeat(depth)}${ref}${ROLE_NAMES[role] ?? role}`;
          if (name) line += ` "${quote(name)}"`;
          if (value && value !== name) line += ` value="${quote(value)}"`;
          lines.push(line + (node === root ? "" : states(node)));
        }
        childDepth = depth + 1;
        childContext = `${name}\n${value}`;
      }
    }
    for (const id of node.childIds ?? []) {
      const child = byId.get(id);
      if (child) visit(child, childDepth, childContext);
    }
  };

  if (root) visit(root, 0, "");
  if (omitted) lines.push(`… ${omitted} more nodes omitted`);
  return lines.join("\n");
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
