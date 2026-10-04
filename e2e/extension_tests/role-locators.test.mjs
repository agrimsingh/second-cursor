import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

// Exercise the actual isolated-world operation, with DOM doubles that provide
// element/attribute/ancestor behavior but do not compute accessibility roles.
const source = await readFile(new URL("../../extension/extended.js", import.meta.url), "utf8");
const operationSource = source.slice(source.indexOf("function domOperation("), source.indexOf("\nasync function contextFor("));
const snapshotSource = await readFile(new URL("../../extension/snapshot.js", import.meta.url), "utf8");
const secretSource = snapshotSource.slice(snapshotSource.indexOf("export function isSecretField("), snapshotSource.indexOf("\n// Pages make elements clickable"));
const isSecretField = vm.runInNewContext(`(${secretSource.replace("export ", "")})`);

function fixture(specs) {
  const all = [];
  function make(spec, parent = null) {
    const attrs = { ...(spec.attrs || {}), ...(spec.id ? { id: spec.id } : {}) };
    const el = {
      tagName: spec.tag.toUpperCase(), localName: spec.tag.toLowerCase(), parentElement: parent,
      id: attrs.id || "", innerText: spec.text || "", textContent: spec.text || "", labels: [],
      disabled: false, isConnected: true, isContentEditable: false,
      attributes: Object.entries(attrs).map(([name, value]) => ({ name, value })),
      getAttribute: key => Object.hasOwn(attrs, key) ? attrs[key] : null,
      hasAttribute: key => Object.hasOwn(attrs, key),
      getClientRects: () => spec.hidden ? [] : [{}],
      getBoundingClientRect: () => ({ x: 10, y: 10, width: 150, height: 30 }),
      checkVisibility: () => !spec.hidden,
      matches(selector) {
        return selector.split(",").some(part => {
          const s = part.trim();
          if (s === "*") return true;
          if (s.startsWith("#")) return this.id === s.slice(1);
          const match = /^([a-z0-9]+)?(?:\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\])?$/i.exec(s);
          return !!match && (!match[1] || this.localName === match[1].toLowerCase()) &&
            (!match[2] || (Object.hasOwn(attrs, match[2]) && (match[3] === undefined || attrs[match[2]] === match[3])));
        });
      },
      closest(selector) {
        for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
        return null;
      },
      querySelectorAll(selector) {
        const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll("*")]);
        return descendants.filter(child => child.matches(selector));
      },
      querySelector(selector) {
        if (selector.startsWith(":scope > ")) return this.children.find(child => child.matches(selector.slice(9))) || null;
        return this.querySelectorAll(selector)[0] || null;
      },
      compareDocumentPosition(other) { return all.indexOf(this) < all.indexOf(other) ? 4 : 2; },
    };
    all.push(el);
    el.children = (spec.children || []).map(child => make(child, el));
    if (el.tagName === "INPUT") {
      el.type = attrs.type || "text";
      el.value = attrs.value || "";
    }
    if (el.tagName === "SELECT") {
      el.multiple = Object.hasOwn(attrs, "multiple");
      el.size = Number(attrs.size || 0);
      el.options = spec.options || [];
      el.selectedOptions = el.options.filter(option => option.selected);
      el.value = el.selectedOptions[0]?.value || "";
    }
    if (el.tagName === "IFRAME") { el.src = attrs.src || ""; el.contentDocument = null; }
    return el;
  }
  specs.forEach(spec => make(spec));
  const document = {
    title: "Role fixture", activeElement: null,
    querySelectorAll: selector => all.filter(el => selector === "body *" || el.matches(selector)),
    getElementById: id => all.find(el => el.id === id) || null,
  };
  const operate = vm.runInNewContext(`(${operationSource})`, {
    document, location: { href: "http://fixture.invalid/" },
    getComputedStyle: () => ({ visibility: "visible" }),
    Node: { DOCUMENT_POSITION_FOLLOWING: 4 },
  });
  const run = (operation, locator, args = {}) => operate({ operation, ...(locator ? { locator } : {}), ...args }, isSecretField);
  const locator = (role, name, chained) => chained
    ? { chain: [{ role, ...(name === undefined ? {} : { name, exact: true }) }] }
    : { role, ...(name === undefined ? {} : { name }) };
  return { run, locator };
}

for (const chained of [false, true]) {
  const path = chained ? "chained" : "flat";
  test(`${path}: inspect discovers the workspace's named section as a region`, () => {
    const f = fixture([{ tag: "section", id: "details", attrs: { "aria-label": "Record details" }, text: "Invoice ID: INV-1234" }]);
    const locator = chained ? { chain: [{ role: "region", name: "Record details", exact: false }] } : { role: "region", name: "Record details", exact: false };
    const result = f.run("inspect", locator);
    assert.equal(result.role, "region");
    assert.equal(result.name, "Record details");
    assert.equal(result.text, "Invoice ID: INV-1234");
    assert.equal(f.run("inspect", chained ? { chain: [{ css: "#details" }] } : { selector: "#details" }).role, "region");
    assert.equal(f.run("dom_snapshot").elements.find(el => el.name === "Record details").role, "region");
  });

  test(`${path}: aria-labelledby names regions; absent, empty, and unresolved names do not`, () => {
    const f = fixture([
      { tag: "h2", id: "region-label", text: "Order history" },
      { tag: "section", attrs: { "aria-labelledby": "region-label" } },
      { tag: "section" },
      { tag: "section", attrs: { "aria-label": "   " } },
      { tag: "section", attrs: { "aria-labelledby": "missing-label" } },
    ]);
    assert.equal(f.run("query", f.locator("region", undefined, chained)).count, 1);
    assert.equal(f.run("inspect", f.locator("region", "Order history", chained)).role, "region");
  });

  test(`${path}: explicit roles override named-section and native-button roles`, () => {
    const f = fixture([
      { tag: "section", attrs: { role: "group", "aria-label": "Record details" } },
      { tag: "button", attrs: { role: "link", "aria-label": "Open account" } },
    ]);
    assert.equal(f.run("query", f.locator("region", undefined, chained)).count, 0);
    assert.equal(f.run("query", f.locator("button", undefined, chained)).count, 0);
    assert.equal(f.run("inspect", f.locator("group", "Record details", chained)).role, "group");
    assert.equal(f.run("inspect", f.locator("link", "Open account", chained)).role, "link");
  });

  test(`${path}: duplicate named regions still refuse ambiguous inspection`, () => {
    const f = fixture([
      { tag: "section", attrs: { "aria-label": "Record details" } },
      { tag: "section", attrs: { "aria-label": "Record details" } },
    ]);
    assert.throws(() => f.run("inspect", f.locator("region", "Record details", chained)),
      /(?:Locator matched 2 elements|strict mode violation: resolved to 2 elements)/);
  });

  test(`${path}: all heading levels and common structural roles resolve through the same map`, () => {
    for (const [tag, role] of [...Array.from({ length: 6 }, (_, index) => [`h${index + 1}`, "heading"]),
      ["main", "main"], ["nav", "navigation"], ["aside", "complementary"], ["article", "article"],
      ["dialog", "dialog"], ["ul", "list"], ["li", "listitem"], ["table", "table"], ["tr", "row"], ["td", "cell"]]) {
      const f = fixture([{ tag, text: "Fixture content" }]);
      assert.equal(f.run("inspect", f.locator(role, undefined, chained)).role, role, tag);
    }
  });

  test(`${path}: multi-select and sized select are listboxes; ordinary select is a combobox`, () => {
    const f = fixture([
      { tag: "select", attrs: { multiple: "", "aria-label": "Channels" } },
      { tag: "select", attrs: { size: "4", "aria-label": "Regions" } },
      { tag: "select", attrs: { "aria-label": "Route" } },
    ]);
    assert.equal(f.run("query", f.locator("listbox", undefined, chained)).count, 2);
    assert.equal(f.run("inspect", f.locator("listbox", "Channels", chained)).role, "listbox");
    assert.equal(f.run("inspect", f.locator("combobox", "Route", chained)).role, "combobox");
    assert.equal(f.run("dom_snapshot").elements.filter(el => el.role === "listbox").length, 2);
  });

  test(`${path}: only top-level header/footer produce banner/contentinfo landmarks`, () => {
    const f = fixture([
      { tag: "header", attrs: { "aria-label": "Site header" } },
      { tag: "footer", attrs: { "aria-label": "Site footer" } },
      { tag: "article", children: [{ tag: "header", text: "Article header" }, { tag: "footer", text: "Article footer" }] },
    ]);
    assert.equal(f.run("query", f.locator("banner", undefined, chained)).count, 1);
    assert.equal(f.run("query", f.locator("contentinfo", undefined, chained)).count, 1);
    assert.equal(f.run("inspect", f.locator("banner", "Site header", chained)).name, "Site header");
  });

  test(`${path}: iframe inspection and DOM snapshot keep existing frame metadata`, () => {
    const f = fixture([{ tag: "iframe", attrs: { "aria-label": "Billing widget", src: "https://widget.invalid/" } }]);
    assert.equal(f.run("inspect", f.locator("iframe", "Billing widget", chained)).role, "iframe");
    const frame = f.run("dom_snapshot").elements[0];
    assert.equal(frame.role, "iframe");
    assert.equal(frame.name, "Billing widget");
    assert.equal(frame.src, "https://widget.invalid/");
    assert.equal(frame.cross_origin, true);
  });

  test(`${path}: role-based reads preserve password and payment redaction`, () => {
    const f = fixture([
      { tag: "input", attrs: { type: "password", "aria-label": "Password", value: "private-password-902" } },
      { tag: "input", attrs: { type: "text", autocomplete: "cc-number", "aria-label": "Card number", value: "4242424242424242" } },
    ]);
    for (const name of ["Password", "Card number"]) {
      const inspected = f.run("inspect", f.locator("textbox", name, chained));
      assert.equal(inspected.text, "[redacted]");
      assert.equal(inspected.attributes.value, "[redacted]");
    }
    const snapshot = JSON.stringify(f.run("dom_snapshot"));
    assert(!snapshot.includes("private-password-902") && !snapshot.includes("4242424242424242"));
  });
}

test("flat and chained names share aria-labelledby precedence without changing flat exact/substr matching", () => {
  const f = fixture([
    { tag: "h2", id: "preferred-label", text: "Linked label" },
    { tag: "section", attrs: { "aria-label": "Flat label", "aria-labelledby": "preferred-label" } },
  ]);
  assert.equal(f.run("inspect", { role: "region", name: "Linked label" }).name, "Linked label");
  assert.equal(f.run("query", { role: "region", name: "Flat label" }).count, 0);
  assert.equal(f.run("query", { role: "region", name: "Linked" }).count, 0);
  assert.equal(f.run("query", { role: "region", name: "Linked", exact: false }).count, 1);
  assert.equal(f.run("query", { role: "region", name: "linked", exact: false }).count, 0);
  assert.equal(f.run("inspect", { chain: [{ role: "region", name: "Linked label", exact: true }] }).name, "Linked label");
});

for (const chained of [false, true]) {
  const path = chained ? "chained" : "flat";
  test(`${path}: competing button labels match the selected accessible name`, () => {
    const f = fixture([
      { tag: "span", id: "first-label", text: "Property case" },
      { tag: "span", id: "second-label", text: "Cedar handoff" },
      { tag: "button", id: "target", attrs: { "aria-label": "Alternative name", "aria-labelledby": "first-label second-label" }, text: "Open record" },
    ]);
    const result = f.run("inspect", f.locator("button", "Property case Cedar handoff", chained));
    assert.equal(result.attributes.id, "target");
    assert.equal(result.name, "Property case Cedar handoff");
    assert.equal(result.text, "Open record");
    assert.equal(f.run("query", f.locator("button", "Alternative name", chained)).count, 0);
  });

  test(`${path}: resolved labels normalize whitespace and unresolved references fall back to aria-label`, () => {
    const f = fixture([
      { tag: "span", id: "first-label", text: "  Current \n " },
      { tag: "span", id: "second-label", text: "  invoice\tqueue  " },
      { tag: "button", id: "linked", attrs: { "aria-labelledby": " first-label  missing-label\tsecond-label " } },
      { tag: "button", id: "fallback", attrs: { "aria-labelledby": "unknown", "aria-label": " \nAccount\t summary  " } },
    ]);
    assert.equal(f.run("inspect", f.locator("button", "Current invoice queue", chained)).attributes.id, "linked");
    assert.equal(f.run("inspect", f.locator("button", "Account summary", chained)).attributes.id, "fallback");
    const names = f.run("dom_snapshot").elements.filter(el => el.role === "button").map(el => el.name);
    assert.equal(JSON.stringify(names), JSON.stringify(["Current invoice queue", "Account summary"]));
  });

  test(`${path}: correcting label precedence protects both actual and previously secret labels`, () => {
    const f = fixture([
      { tag: "span", id: "public-label", text: "Account token" },
      { tag: "span", id: "secret-label", text: "Card number" },
      { tag: "input", attrs: { type: "text", "aria-label": "Password", "aria-labelledby": "public-label", value: "private-token-836" } },
      { tag: "input", attrs: { type: "text", "aria-label": "Account input", "aria-labelledby": "secret-label", value: "4242424242424242" } },
    ]);
    for (const name of ["Account token", "Card number"]) {
      const result = f.run("inspect", f.locator("textbox", name, chained));
      assert.equal(result.name, name);
      assert.equal(result.text, "[redacted]");
      assert.equal(result.attributes.value, "[redacted]");
    }
    const snapshot = JSON.stringify(f.run("dom_snapshot"));
    assert(!snapshot.includes("private-token-836") && !snapshot.includes("4242424242424242"));
  });
}

test("sharing roles preserves flat visibility filters and chained hidden-element defaults", () => {
  const f = fixture([
    { tag: "section", attrs: { "aria-label": "Record details" } },
    { tag: "section", attrs: { "aria-label": "Record details" }, hidden: true },
  ]);
  assert.equal(f.run("query", { role: "region" }).count, 2);
  assert.equal(f.run("query", { role: "region", visible: true }).count, 1);
  assert.equal(f.run("query", { chain: [{ role: "region" }] }).count, 1);
  assert.equal(f.run("query", { chain: [{ role: "region", include_hidden: true }] }).count, 2);
});
