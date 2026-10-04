import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../extension/extended.js", import.meta.url), "utf8");
const operationSource = source.slice(source.indexOf("function domOperation("), source.indexOf("\nasync function contextFor("));
const snapshotSource = await readFile(new URL("../../extension/snapshot.js", import.meta.url), "utf8");
const secretSource = snapshotSource.slice(snapshotSource.indexOf("export function isSecretField("), snapshotSource.indexOf("\n// Pages make elements clickable"));
const isSecretField = vm.runInNewContext(`(${secretSource.replace("export ", "")})`);
const PRIVATE = "SYNTHETIC_PRIVATE_";

// DOM doubles include native-label associations and real SELECT -> OPTION
// ancestry. Options are nodes in snapshots, not merely select metadata objects.
function fixture(specs) {
  const all = [], events = [];
  function make(spec, parent = null) {
    const attrs = { ...(spec.attrs || {}), ...(spec.id ? { id: spec.id } : {}) };
    const own = spec.text || "";
    const el = {
      tagName: spec.tag.toUpperCase(), localName: spec.tag.toLowerCase(), parentElement: parent,
      id: attrs.id || "", labels: [], disabled: false, isConnected: true, isContentEditable: false,
      attributes: Object.entries(attrs).map(([name, value]) => ({ name, value })),
      get innerText() { return [own, ...this.children.map(child => child.innerText)].filter(Boolean).join("\n"); },
      get textContent() { return own + this.children.map(child => child.textContent).join(""); },
      getAttribute: key => Object.hasOwn(attrs, key) ? attrs[key] : null,
      hasAttribute: key => Object.hasOwn(attrs, key),
      getClientRects: () => [{}], getBoundingClientRect: () => ({ x: 10, y: 10, width: 150, height: 30 }),
      checkVisibility: () => true,
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
      closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; },
      querySelectorAll(selector) { return this.children.flatMap(child => [child, ...child.querySelectorAll("*")]).filter(child => child.matches(selector)); },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
      cloneNode() { return { textContent: own, querySelectorAll: () => [] }; },
      compareDocumentPosition(other) { return all.indexOf(this) < all.indexOf(other) ? 4 : 2; },
      dispatchEvent(event) { events.push({ id: this.id, type: event.type }); },
      focus() {}, setSelectionRange() {},
    };
    all.push(el);
    el.children = (spec.children || []).map(child => make(child, el));
    if (["INPUT", "TEXTAREA"].includes(el.tagName)) {
      el.type = attrs.type || (el.tagName === "TEXTAREA" ? "textarea" : "text");
      el.value = spec.value ?? attrs.value ?? own;
    }
    if (el.tagName === "OPTION") {
      el.label = attrs.label ?? own;
      el.value = attrs.value ?? own;
      el.selected = Object.hasOwn(attrs, "selected");
    }
    if (el.tagName === "SELECT") {
      el.multiple = Object.hasOwn(attrs, "multiple"); el.size = Number(attrs.size || 0);
      el.options = el.querySelectorAll("option");
      if (!el.options.some(option => option.selected) && el.options.length) el.options[0].selected = true;
      Object.defineProperty(el, "selectedOptions", { get: () => el.options.filter(option => option.selected) });
      Object.defineProperty(el, "value", { get: () => el.selectedOptions[0]?.value || "" });
    }
    return el;
  }
  specs.forEach(spec => make(spec));
  for (const el of all) if (el.matches("input,textarea,select"))
    el.labels = all.filter(candidate => candidate.tagName === "LABEL" && candidate.getAttribute("for") === el.id);
  const document = { title: "Privacy fixture", activeElement: null,
    querySelectorAll: selector => all.filter(el => selector === "body *" || el.matches(selector)),
    getElementById: id => all.find(el => el.id === id) || null };
  const operate = vm.runInNewContext(`(${operationSource})`, { document, location: { href: "http://fixture.invalid/" },
    getComputedStyle: () => ({ visibility: "visible" }), Node: { DOCUMENT_POSITION_FOLLOWING: 4 }, Event });
  return {
    run: (operation, locator, args = {}) => operate({ operation, ...(locator ? { locator } : {}), ...args }, isSecretField),
    all, events,
  };
}
const byID = (id, chained = false) => chained ? { chain: [{ css: "#" + id }] } : { selector: "#" + id };
const byRole = (role, name, chained = false) => chained ? { chain: [{ role, name, exact: true }] } : { role, name, exact: true };
const noSecret = result => assert(!JSON.stringify(result).includes(PRIVATE), JSON.stringify(result));
const option = (id, { selected = false, ordinary = false } = {}) => ({ tag: "option", id,
  attrs: { value: ordinary ? "public-value" : PRIVATE + id + "_VALUE", label: ordinary ? "Public choice" : PRIVATE + id + "_LABEL",
    "aria-label": ordinary ? "Public choice" : PRIVATE + id + "_ARIA", ...(selected ? { selected: "" } : {}) },
  text: ordinary ? "Public choice" : PRIVATE + id + "_TEXT" });

for (const chained of [false, true]) {
  const path = chained ? "chained" : "flat";
  test(`${path}: native sensitivity survives benign ARIA names and empty live values`, () => {
    const f = fixture([
      { tag: "label", attrs: { for: "input" }, text: "Password" },
      { tag: "span", id: "public-name", text: "Account code" },
      { tag: "input", id: "input", attrs: { type: "text", "aria-label": "Old public name", "aria-labelledby": "public-name", value: PRIVATE + "OLD_INPUT" }, value: "" },
      { tag: "label", attrs: { for: "note" }, text: "Password" },
      { tag: "textarea", id: "note", attrs: { "aria-label": "Recovery note" }, text: PRIVATE + "OLD_NOTE", value: "" },
    ]);
    for (const [id, name] of [["input", "Account code"], ["note", "Recovery note"]]) {
      for (const kind of [undefined, "inner", "content"]) {
        const result = f.run("inspect", byRole("textbox", name, chained), kind ? { text_kind: kind } : {});
        assert.equal(result.attributes.id, id); assert.equal(result.name, name); noSecret(result);
        if (kind && id === "note") { assert.equal(result.text, "[redacted]"); assert.equal(result.total_length, 10); }
      }
    }
    noSecret(f.run("dom_snapshot"));
    noSecret(f.run("query", byID("note", true), { texts: "content" }));
  });

  test(`${path}: selected and unselected option reads inherit owner sensitivity on every serialized field`, () => {
    const f = fixture([{ tag: "label", attrs: { for: "select" }, text: "Card number" },
      { tag: "select", id: "select", attrs: { "aria-label": "Payment choice", size: "2" }, children: [option("first", { selected: true }), option("second")] }]);
    for (const id of ["select", "first", "second"]) {
      for (const kind of [undefined, "inner", "content"]) {
        const result = f.run("inspect", byID(id, chained), kind ? { text_kind: kind } : {});
        noSecret(result); assert.equal(result.text, "[redacted]"); assert.equal(result.total_length, 10);
        assert.equal(result.attributes.id, id);
        assert.equal(result.name, id === "select" ? "Payment choice" : "[redacted]");
      }
      for (const texts of ["inner", "content"]) noSecret(f.run("query", byID(id, true), { texts }));
    }
    // Read masking must not rewrite the raw names used to address an option.
    noSecret(f.run("inspect", byRole("option", PRIVATE + "first_ARIA", chained)));
    const snapshot = f.run("dom_snapshot"); noSecret(snapshot);
    for (const row of snapshot.elements.filter(item => item.role === "option")) {
      assert.equal(row.name, "[redacted]"); assert.equal(row.value, "[redacted]"); assert.equal(row.value_length, 10);
    }
    assert.throws(() => f.run("select_text", byID("first", chained), { text: "guess" }),
      error => /not available on password and payment card fields/.test(error.message) && !error.message.includes(PRIVATE));
  });

  test(`${path}: mixed select preserves ordinary selected values while masking aggregate text`, () => {
    const masked = option("private"); masked.attrs.value = "••••";
    const f = fixture([{ tag: "select", id: "mixed", attrs: { "aria-label": "Shipping route", size: "2" }, children: [option("public", { selected: true, ordinary: true }), masked] }]);
    const result = f.run("inspect", byID("mixed", chained));
    noSecret(result); assert.equal(result.text, "public-value"); assert.equal(result.name, "Shipping route");
    assert.equal(result.options[0].value, "public-value"); assert.equal(result.options[0].label, "Public choice");
    assert.equal(result.options[1].value, "[redacted]");
    for (const kind of ["inner", "content"]) {
      const aggregate = f.run("inspect", byID("mixed", chained), { text_kind: kind });
      noSecret(aggregate); assert.equal(aggregate.text, "[redacted]"); assert.equal(aggregate.total_length, 10);
    }
    const publicOption = f.run("inspect", byID("public", chained));
    assert.equal(publicOption.name, "Public choice"); assert.equal(publicOption.text, "Public choice");
    assert.equal(publicOption.attributes.value, "public-value");
    noSecret(f.run("dom_snapshot"));
    const selected = f.run("select_option", byID("mixed", chained), { value: "public-value" });
    assert.equal(selected.value, "public-value"); assert.equal(selected.outcome.verified, true);
    assert.equal(selected.outcome.actual[0], "public-value");
  });

  test(`${path}: semantic landmark names never fall back to descendant option text`, () => {
    const sensitive = { tag: "select", attrs: { "aria-label": "Card number", size: "2" }, children: [option("hidden-value", { selected: true })] };
    const f = fixture([{ tag: "main", id: "main", children: [{ tag: "form", id: "form", children: [sensitive] }] },
      { tag: "article", id: "article", attrs: { "aria-label": "Saved payment choices" }, children: [{ ...sensitive, children: [option("other-value")] }] }]);
    for (const [role, name, id] of [["main", "", "main"], ["form", "", "form"], ["article", "Saved payment choices", "article"]]) {
      const detail = f.run("inspect", byRole(role, name, chained));
      assert.equal(detail.attributes.id, id); assert.equal(detail.name, name);
      // Arbitrary ancestor text inspection is not part of this masking contract.
    }
    noSecret(f.run("dom_snapshot"));
  });
}

test("ambiguity diagnostics preserve identity/count while masking sensitive option labels and text", () => {
  const f = fixture([{ tag: "select", id: "private-select", attrs: { "aria-label": "Card number", size: "2" }, children: [option("alpha"), option("beta")] }]);
  assert.throws(() => f.run("inspect", { chain: [{ css: "#private-select" }, { role: "option" }] }), error => {
    assert.match(error.message, /strict mode violation: resolved to 2 elements/);
    assert(error.message.includes('id="alpha"')); assert(error.message.includes('id="beta"'));
    assert(!error.message.includes(PRIVATE), error.message); assert(error.message.includes("[redacted]")); return true;
  });
});

test("masking precedes slicing and exposes only the fixed redacted length", () => {
  const f = fixture([{ tag: "select", attrs: { "aria-label": "Card number" }, children: [option("long-secret")] }]);
  const result = f.run("inspect", byID("long-secret"), { text_kind: "content", offset: 2, length: 3 });
  assert.equal(result.text, "eda"); assert.equal(result.total_length, 10); assert.equal(result.has_more, true); noSecret(result);
});

test("ordinary paragraph/list prose and ordinary fields remain readable", () => {
  const f = fixture([
    { tag: "p", text: "The order is ready for collection." },
    { tag: "li", text: "Priority shipping" },
    { tag: "label", attrs: { for: "public-input" }, text: "Display name" },
    { tag: "input", id: "public-input", attrs: { type: "text", value: "Public display value" } },
    { tag: "label", attrs: { for: "public-note" }, text: "Delivery note" },
    { tag: "textarea", id: "public-note", text: "Leave at reception." },
  ]);
  const rows = f.run("dom_snapshot").elements;
  assert(rows.some(row => row.role === "paragraph" && row.name === "The order is ready for collection."));
  assert(rows.some(row => row.role === "listitem" && row.name === "Priority shipping"));
  for (const chained of [false, true]) {
    assert.equal(f.run("inspect", byRole("textbox", "Display name", chained)).text, "Public display value");
    for (const kind of [undefined, "inner", "content"])
      assert.equal(f.run("inspect", byRole("textbox", "Delivery note", chained), kind ? { text_kind: kind } : {}).text, "Leave at reception.");
  }
});

test("correct semantic names retain old content-based sensitivity for an unnamed select", () => {
  const f = fixture([{ tag: "select", id: "unnamed", children: [
    { tag: "option", id: "unnamed-choice", text: "Card number", attrs: { value: PRIVATE + "UNNAMED_SELECT", selected: "" } },
  ] }]);
  for (const chained of [false, true]) {
    const result = f.run("inspect", byRole("combobox", "", chained));
    assert.equal(result.attributes.id, "unnamed"); assert.equal(result.name, "");
    assert.equal(result.text, "[redacted]"); noSecret(result);
    noSecret(f.run("inspect", byID("unnamed-choice", chained)));
  }
  noSecret(f.run("dom_snapshot"));
});
