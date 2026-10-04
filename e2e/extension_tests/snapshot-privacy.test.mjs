import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = (await readFile(new URL("../../extension/snapshot.js", import.meta.url), "utf8"))
  .replace(/^import .*;\n/gm, "").replace(/export /g, "");
const SECRET = "SYNTHETIC_SNAPSHOT_SECRET_";
const noSecrets = output => assert(!JSON.stringify(output).includes(SECRET), JSON.stringify(output));
const from = (value, extra = {}) => ({ type: "relatedElement", value: { type: "computedString", value }, ...extra });

// CaptureSnapshot wire-format double: DOM nodes, AX nodes, and layout are
// separate so sensitivity must join by backend ID, not by index or AX name.
function fixture(specs, { factsFail = false } = {}) {
  const strings = [];
  const string = value => { let i = strings.indexOf(value); if (i < 0) { i = strings.length; strings.push(value); } return i; };
  const nodes = { parentIndex: [], nodeType: [], nodeName: [], nodeValue: [], backendNodeId: [], attributes: [], optionSelected: { index: [] }, shadowRootType: { index: [], value: [] } };
  const ax = [{ nodeId: "root", role: { value: "RootWebArea" }, name: { value: "Privacy fixture" }, childIds: [] }];
  const layout = { nodeIndex: [], styles: [], bounds: [] };
  const byID = new Map();
  function add(spec, parent = -1, axParent = ax[0], shadowType = undefined) {
    const index = nodes.parentIndex.length, backend = 100 + index;
    nodes.parentIndex.push(parent); nodes.backendNodeId.push(backend);
    nodes.nodeType.push(spec.nodeType ?? (spec.tag ? 1 : 3)); nodes.nodeName.push(string(spec.tag?.toUpperCase() ?? "#text"));
    nodes.nodeValue.push(string(spec.text ?? ""));
    nodes.attributes.push(Object.entries({ ...(spec.id ? { id: spec.id } : {}), ...spec.attrs }).flatMap(([k, v]) => [string(k), string(v)]));
    if (shadowType) { nodes.shadowRootType.index.push(index); nodes.shadowRootType.value.push(string(shadowType)); }
    if (spec.selected) nodes.optionSelected.index.push(index);
    if (spec.layout !== false) { layout.nodeIndex.push(index); layout.styles.push([string("auto"), string("visible"), string("visible")]); layout.bounds.push([0, index * 10, 100, 10]); }
    let axNode = axParent;
    if (spec.role) {
      axNode = { nodeId: String(backend), parentId: axParent.nodeId, backendDOMNodeId: backend, role: { value: spec.role },
        name: { value: spec.name ?? "", ...(spec.sources ? { sources: spec.sources } : {}) },
        ...(spec.value !== undefined ? { value: { value: spec.value } } : {}), properties: spec.properties ?? [], childIds: [] };
      axParent.childIds.push(axNode.nodeId); ax.push(axNode);
    }
    if (spec.id) byID.set(spec.id, { backend, index, ax: axNode });
    for (const child of spec.children ?? []) add(child, index, axNode, spec.shadow ?? shadowType);
  }
  specs.forEach(spec => add(spec));
  const cdp = async (_tab, method) => {
    if (method === "DOMSnapshot.captureSnapshot") { if (factsFail) throw Error("detached"); return { documents: [{ nodes, layout }], strings }; }
    if (method === "Accessibility.getFullAXTree") return { nodes: structuredClone(ax) };
    throw Error("Unexpected CDP call " + method);
  };
  const context = vm.createContext({ cdp, chrome: { tabs: { onRemoved: { addListener() {} } } } });
  vm.runInContext(`${source}\nglobalThis.api = { snapshot, structuredSnapshot };`, context);
  return { ...context.api, byID };
}
const nativeLabel = (id, text) => ({ tag: "label", attrs: { for: id }, children: [{ text }] });
const field = (id, rest = {}) => ({ tag: "input", id, role: "textbox", name: "Public name", value: SECRET + id, attrs: { type: "text", "aria-label": "Public name" }, ...rest });
const option = (id, { selected = false, ordinary = false, bullet = false } = {}) => ({ tag: "option", id, role: "option", selected,
  name: ordinary ? "Standard shipping" : SECRET + id + "_NAME", value: ordinary ? "standard" : undefined,
  attrs: { value: bullet ? "••••" : ordinary ? "standard" : SECRET + id + "_VALUE" },
  children: [{ role: "StaticText", text: ordinary ? "Standard shipping" : SECRET + id + "_TEXT", name: ordinary ? "Standard shipping" : SECRET + id + "_TEXT" }] });

for (const tag of ["input", "textarea"]) {
  test(`${tag}: native labels survive benign ARIA overrides and absent layout`, async () => {
    const f = fixture([nativeLabel("private", "Password"), field("private", { tag, layout: false, sources: [from("Password", { superseded: true, nativeSource: "labelfor" })] })]);
    for (const output of [await f.snapshot(1), await f.structuredSnapshot(1)]) {
      noSecrets(output); assert(JSON.stringify(output).includes("Public name")); assert(JSON.stringify(output).includes("[redacted]"));
    }
  });
}

test("implicit native labels and multiple explicit labels remain sensitivity evidence", async () => {
  const f = fixture([{ tag: "label", children: [{ text: "Password" }, field("wrapped", { sources: [from("Password", { superseded: true, nativeSource: "labelwrapped" })] })] },
    nativeLabel("multiple", "Account"), nativeLabel("multiple", "Card number"), field("multiple", { sources: [from("Account Card number", { superseded: true, nativeSource: "labelfor" })] })]);
  noSecrets(await f.structuredSnapshot(1));
});

test("ARIA-labelledby wins public naming while superseded ARIA and native labels still protect values", async () => {
  const f = fixture([{ tag: "span", id: "public", children: [{ text: "Account code" }] },
    field("private", { name: "Account code", attrs: { "aria-labelledby": "public", "aria-label": "Password" } }),
    { tag: "span", id: "secret-label", children: [{ text: "Card number" }] },
    field("related", { attrs: { "aria-labelledby": "secret-label", "aria-label": "Public name" }, sources: [from("Card number", { attribute: "aria-labelledby" })] })]);
  noSecrets(await f.structuredSnapshot(1));
});

test("AX superseded sources protect values when DOM facts are unavailable", async () => {
  const f = fixture([
    field("computed", { sources: [from("Public name"), from("Password", { superseded: true, nativeSource: "label" })] }),
    field("related", { sources: [{ type: "relatedElement", superseded: true, nativeSourceValue: { relatedNodes: [{ text: "Card number" }] } }] }),
    field("attribute", { sources: [{ type: "attribute", superseded: true, attribute: "aria-label", attributeValue: { value: "Password" } }] }),
  ], { factsFail: true });
  noSecrets(await f.structuredSnapshot(1));
});

test("invalid AX label candidates do not hide ordinary values", async () => {
  const f = fixture([field("public", { value: "Public value", sources: [from("Password", { invalid: true })] })], { factsFail: true });
  const result = await f.structuredSnapshot(1);
  assert.equal(result.elements.find(row => row.role === "textbox").value, "Public value");
});

test("native labels and labelledby IDs stay within each shadow tree", async () => {
  // Real Chrome captureSnapshot omits nodeType 11 roots entirely, reparents
  // their children to the host, and marks descendants using shadowRootType.
  const shadow = children => ({ tag: "div", shadow: "open", children });
  const f = fixture([
    shadow([nativeLabel("shared", "Password"), field("shared", { name: "Private shadow", sources: [from("Password", { superseded: true, nativeSource: "labelfor" })] }),
      { tag: "span", id: "shared-label", children: [{ text: "Card number" }] },
      field("related", { name: "Private related", attrs: { "aria-labelledby": "shared-label" }, sources: [from("Card number", { attribute: "aria-labelledby" })] })]),
    shadow([nativeLabel("shared", "Display name"), field("shared", { name: "Public shadow", value: "Visible shadow value", sources: [from("Display name", { superseded: true, nativeSource: "labelfor" })] }),
      { tag: "span", id: "shared-label", children: [{ text: "Shipping route" }] },
      field("related", { name: "Public related", value: "Visible related value", attrs: { "aria-labelledby": "shared-label" }, sources: [from("Shipping route", { attribute: "aria-labelledby" })] })]),
    field("shared", { name: "Public document", value: "Visible document value" }),
    { tag: "label", children: [{ text: "Password" }, shadow([field("inside-host", { name: "Public nested shadow", value: "Visible nested value" })])] },
  ]);
  const result = await f.structuredSnapshot(1); noSecrets(result);
  const fields = Object.fromEntries(result.elements.filter(row => row.role === "textbox").map(row => [row.name, row.value]));
  assert.deepEqual(fields, { "Private shadow": "[redacted]", "Private related": "[redacted]",
    "Public shadow": "Visible shadow value", "Public related": "Visible related value", "Public document": "Visible document value",
    "Public nested shadow": "Visible nested value" });
});

test("AX sensitive popup sources protect option descendants even with an empty value and no DOM facts", async () => {
  const f = fixture([{ tag: "select", role: "PopUpButton", name: "Payment choice", value: "",
    sources: [from("Card number", { superseded: true, nativeSource: "label" })], children: [option("private")] }], { factsFail: true });
  const result = await f.structuredSnapshot(1); noSecrets(result);
  assert(result.outline.includes('combobox "Payment choice"'));
});

for (const role of ["listbox", "combobox"]) {
  test(`${role}: secret selects hide selected and unselected options and retain their public name`, async () => {
    const f = fixture([nativeLabel("choice", "Card number"), { tag: "select", id: "choice", role, name: "Payment choice",
      attrs: { "aria-label": "Payment choice" }, sources: [from("Card number", { superseded: true, nativeSource: "labelfor" })],
      value: SECRET + "SELECTED", children: [option("selected", { selected: true }), option("unselected")] }]);
    const result = await f.structuredSnapshot(1); noSecrets(result);
    const select = result.elements.find(row => row.role === role);
    assert.equal(select.name, "Payment choice"); assert.equal(select.value, "[redacted]");
    assert.equal(result.elements.filter(row => row.role === "option").length, 0);
  });
}

test("one private unselected option stays masked without hiding the public selected value", async () => {
  const f = fixture([{ tag: "select", role: "listbox", name: "Shipping route", value: "Standard shipping", children: [
    option("ordinary", { selected: true, ordinary: true }), option("private", { bullet: true }),
  ] }]);
  const result = await f.structuredSnapshot(1); noSecrets(result);
  assert.equal(result.elements.find(row => row.role === "listbox").value, "Standard shipping");
  const choices = result.elements.filter(row => row.role === "option");
  assert.deepEqual(Array.from(choices, row => row.name), ["Standard shipping", "[redacted]"]);
  assert(result.outline.includes('option "[redacted]"')); assert(result.outline.includes('option "Standard shipping"'));
});

test("a selected private option masks the select value without hiding ordinary sibling choices", async () => {
  const f = fixture([{ tag: "select", role: "combobox", name: "Shipping route", value: SECRET + "SELECTED", children: [
    option("ordinary", { ordinary: true }), option("private", { selected: true, bullet: true }),
  ] }]);
  const result = await f.structuredSnapshot(1); noSecrets(result);
  assert.equal(result.elements.find(row => row.role === "combobox").value, "[redacted]");
  assert(result.elements.some(row => row.role === "option" && row.name === "Standard shipping"));
});

test("type/autocomplete, password bullets, empty values, and ordinary fields retain their contracts", async () => {
  const f = fixture([field("password", { attrs: { type: "password" } }), field("card", { attrs: { autocomplete: "section-billing cc-number" } }),
    field("bullets", { value: "••••" }), field("empty", { name: "Password", value: "", children: [{ text: SECRET + "OLD", name: SECRET + "OLD", role: "StaticText" }] }),
    field("public", { value: "Visible ordinary value" })]);
  const result = await f.structuredSnapshot(1); noSecrets(result);
  const values = result.elements.filter(row => row.role === "textbox").map(row => row.value);
  assert.deepEqual(Array.from(values), ["[redacted]", "[redacted]", "[redacted]", "", "Visible ordinary value"]);
});
