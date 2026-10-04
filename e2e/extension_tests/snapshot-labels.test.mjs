import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = (await readFile(new URL("../../extension/snapshot.js", import.meta.url), "utf8"))
  .replace(/^import .*;\n/gm, "").replace(/export /g, "");
const context = vm.createContext({ chrome: { tabs: { onRemoved: { addListener() {} } } } });
vm.runInContext(`${source}\nglobalThis.render = renderOutline;`, context);

function outline(controls) {
  const nodes = [{ nodeId: "root", role: { value: "RootWebArea" }, name: { value: "Fixture" }, childIds: [] }];
  for (const [index, spec] of controls.entries()) {
    const id = `control-${index}`;
    nodes[0].childIds.push(id);
    nodes.push({
      nodeId: id, parentId: "root", backendDOMNodeId: index + 1,
      role: { value: spec.role || "button" },
      name: { value: spec.name, ...(spec.sources ? { sources: spec.sources } : {}) },
      childIds: (spec.texts || []).map((_, n) => `${id}-text-${n}`),
      ...(spec.value ? { value: { value: spec.value } } : {}),
      ...(spec.properties ? { properties: spec.properties } : {}),
    });
    for (const [n, text] of (spec.texts || []).entries()) {
      nodes.push({ nodeId: `${id}-text-${n}`, parentId: id, role: { value: "StaticText" }, name: { value: text } });
    }
  }
  return context.render(nodes);
}
const from = (type, value, rest = {}) => ({ type, value: { type: "computedString", value }, ...rest });

test("aria-label keeps distinct message subjects when the visible buttons share a short action", () => {
  const names = ["Open message Earlier receiving advice", "Open message Updated receiving advice"];
  const text = outline(names.map(name => ({ name, texts: ["Open message"], sources: [from("attribute", name, { attribute: "aria-label" })] })));
  names.forEach((name, index) => assert(text.includes(`[e${index + 1}] button "${name}"`), text));
  assert(!text.includes('button "Open message"'), text);
});

test("aria-labelledby preserves the selected name and ignores a superseded aria-label", () => {
  const name = "Open policy Invoice reconciliation policy";
  const text = outline([{ name, texts: ["Open policy"], sources: [
    from("relatedElement", name, { attribute: "aria-labelledby" }),
    from("attribute", "Old policy name", { attribute: "aria-label", superseded: true }),
    from("contents", "Open policy", { superseded: true }),
  ] }]);
  assert(text.includes(`button "${name}"`), text);
  assert(!text.includes("Old policy name"), text);
});

test("content-based cards still compact icon alt noise and keep title, price, rating, and delivery", () => {
  const name = "Blue kettle photo Blue kettle 1.7L $24.90 star-icon 4.8 Arrives tomorrow";
  const text = outline([{ role: "link", name, texts: ["Blue kettle 1.7L", "$", "24.90", "4.8", "Arrives tomorrow"], sources: [
    from("attribute", name, { attribute: "aria-label", superseded: true }),
    from("relatedElement", name, { attribute: "aria-labelledby", invalid: true }),
    from("contents", name),
  ] }]);
  assert(text.includes('link "Blue kettle 1.7L · $24.90 · 4.8 · Arrives tomorrow"'), text);
  assert(!text.includes("photo") && !text.includes("star-icon"), text);
});

test("explicit labels retain visible card details that the accessible name does not cover", () => {
  const name = "View kettle details";
  const text = outline([{ role: "link", name, texts: ["Blue kettle 1.7L", "$", "24.90"], sources: [from("attribute", name, { attribute: "aria-label" })] }]);
  assert(text.includes('link "View kettle details · Blue kettle 1.7L · $24.90"'), text);
});

test("missing AX name-source metadata conservatively preserves a more informative name", () => {
  const text = outline([{ name: "Open message Latest handoff for TKT-1234", texts: ["Open message"] }]);
  assert(text.includes('button "Open message Latest handoff for TKT-1234"'), text);
});

test("equivalent split text and icon-only controls keep their compact names", () => {
  const text = outline([
    { role: "link", name: "[19]", texts: ["[", "19", "]"], sources: [from("contents", "[19]")] },
    { name: "Close dialog", texts: [], sources: [from("attribute", "Close dialog", { attribute: "aria-label" })] },
  ]);
  assert(text.includes('link "[19]"'), text);
  assert(text.includes('button "Close dialog"'), text);
});

test("explicit labels never restore password or payment field values into the outline", () => {
  const text = outline([
    { role: "textbox", name: "Password", value: "private-password-731", texts: ["private-password-731"], properties: [{ name: "editable", value: { value: "plaintext" } }], sources: [from("attribute", "Password", { attribute: "aria-label" })] },
    { role: "textbox", name: "Card number", value: "4242424242424242", texts: ["4242424242424242"], properties: [{ name: "editable", value: { value: "plaintext" } }], sources: [from("relatedElement", "Card number", { attribute: "aria-labelledby" })] },
  ]);
  assert(text.includes('textbox "Password" value="[redacted]"'), text);
  assert(text.includes('textbox "Card number" value="[redacted]"'), text);
  assert(!text.includes("private-password-731") && !text.includes("4242424242424242"), text);
});
