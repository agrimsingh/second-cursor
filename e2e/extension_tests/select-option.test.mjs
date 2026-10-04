import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

// Exercise the exact self-contained function serialized into the isolated page world.
// The small DOM doubles model select/option state; browser E2E covers the actual browser boundary.
const source = await readFile(new URL("../../extension/extended.js", import.meta.url), "utf8");
const operationSource = source.slice(source.indexOf("function domOperation("), source.indexOf("\nasync function contextFor("));
const snapshotSource = await readFile(new URL("../../extension/snapshot.js", import.meta.url), "utf8");
const secretSource = snapshotSource.slice(snapshotSource.indexOf("export function isSecretField("), snapshotSource.indexOf("\n// Pages make elements clickable"));
const isSecretField = vm.runInNewContext(`(${secretSource.replace("export ", "")})`);

function fixture(specs, { multiple = false, name = "Route", disabled = false, fieldsetDisabled = false } = {}) {
  const events = [];
  const element = {
    tagName: "SELECT", id: "choice", multiple, disabled, isConnected: true,
    attributes: [{ name: "aria-label", value: name }], labels: [],
    getAttribute(key) { return key === "aria-label" ? name : null; },
    hasAttribute(key) { return key === "aria-label"; },
    getClientRects() { return [{}]; },
    getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; },
    closest(selector) { return selector === "fieldset[disabled]" && fieldsetDisabled ? {} : null; },
    matches(selector) { return selector.split(",").some((s) => s.trim().toUpperCase() === this.tagName); },
    dispatchEvent(event) { events.push(event.type); this.onEvent?.(event); },
    get selectedOptions() { return this.options.filter((o) => o.selected); },
    get value() { return this.selectedOptions[0]?.value ?? ""; },
  };
  element.options = specs.map((spec, i) => {
    let selected = spec.selected ?? i === 0;
    return {
      value: spec.value, label: spec.label, disabled: spec.disabled ?? false,
      closest(selector) { return selector === "optgroup[disabled]" && spec.groupDisabled ? {} : null; },
      get selected() { return selected; },
      set selected(value) {
        if (value && !multiple) for (const other of element.options) if (other !== this) other.selected = false;
        selected = value;
      },
    };
  });
  const document = { querySelectorAll: () => [element], title: "Fixture", activeElement: null };
  const operate = vm.runInNewContext(`(${operationSource})`, {
    document, location: { href: "http://fixture.invalid/" },
    getComputedStyle: () => ({ visibility: "visible" }), Event,
  });
  const run = (operation, args = {}, chained = false) => operate({ operation, locator: chained ? { chain: [{ css: "#choice" }] } : { selector: "#choice" }, ...args }, isSecretField);
  const plain = (value) => JSON.parse(JSON.stringify(value));
  return { element, events, run, plain };
}

for (const chained of [false, true]) {
  const path = chained ? "chained" : "ordinary";
  test(`${path}: top-level ref is rejected without mutation even alongside a valid locator`, () => {
    const f = fixture([{ value: "a", label: "Alpha" }, { value: "b", label: "Beta" }]);
    for (const operation of ["select_option", "check"]) {
      assert.throws(() => f.run(operation, { ref: "private-untrusted-ref", value: "b", checked: true }, chained),
        (error) => error.message.includes("require locator") && error.message.includes("top-level ref") && !error.message.includes("private-untrusted-ref"));
    }
    assert.equal(f.element.value, "a");
    assert.deepEqual(f.events, []);
  });

  test(`${path}: check still accepts a valid locator`, () => {
    const f = fixture([]);
    f.element.tagName = "INPUT";
    f.element.type = "checkbox";
    f.element.checked = false;
    f.element.click = () => { f.element.checked = !f.element.checked; f.events.push("click"); };
    const result = f.run("check", { checked: true }, chained);
    assert.equal(result.checked, true);
    assert.equal(result.outcome.verified, true);
    assert.deepEqual(f.events, ["click"]);
  });

  test(`${path}: exact values take precedence and exact labels normalize whitespace`, () => {
    const f = fixture([{ value: "priority", label: "Standard" }, { value: "express", label: "Priority   route" }, { value: "standard", label: "priority" }]);
    assert.equal(f.run("select_option", { value: "priority" }, chained).value, "priority");
    const result = f.run("select_option", { value: " Priority\nroute " }, chained);
    assert.equal(result.value, "express");
    assert.equal(result.outcome.verified, true);
    assert.deepEqual(f.plain(result.outcome.expected), ["express"]);
    assert.deepEqual(f.events, ["input", "change", "input", "change"]);
  });

  test(`${path}: ambiguous labels and duplicate values fail before mutation`, () => {
    for (const specs of [
      [{ value: "first", label: "Same label" }, { value: "second", label: "Same  label" }],
      [{ value: "Same label", label: "First" }, { value: "Same label", label: "Second" }],
    ]) {
      const f = fixture(specs);
      assert.throws(() => f.run("select_option", { value: "Same label" }, chained), /ambiguous/);
      assert.deepEqual(f.element.selectedOptions, [f.element.options[0]]);
      assert.deepEqual(f.events, []);
    }
  });

  test(`${path}: missing and disabled options reject the entire multi-select request`, () => {
    for (const option of [{ value: "blocked", label: "Blocked", disabled: true }, { value: "blocked", label: "Blocked", groupDisabled: true }]) {
      const f = fixture([{ value: "initial", label: "Initial" }, { value: "next", label: "Next" }, option], { multiple: true });
      assert.throws(() => f.run("select_option", { value: ["next", "blocked"] }, chained), /disabled/);
      assert.throws(() => f.run("select_option", { value: ["next", "private requested value"] }, chained), (error) => /Option not found/.test(error.message) && !error.message.includes("private requested value"));
      assert.equal(f.element.value, "initial");
      assert.deepEqual(f.events, []);
    }
  });

  test(`${path}: multi-select accepts mixed values and labels and supports clearing`, () => {
    const f = fixture([{ value: "a", label: "Alpha" }, { value: "b", label: "Beta" }, { value: "c", label: "Gamma" }], { multiple: true });
    const result = f.run("select_option", { value: ["Beta", "c", "Beta"] }, chained);
    assert.deepEqual(f.plain(result.outcome.actual), ["b", "c"]);
    assert.equal(result.outcome.verified, true);
    assert.equal(f.run("select_option", { value: [] }, chained).outcome.verified, true);
    assert.deepEqual(f.element.selectedOptions, []);
  });
}

test("missing or empty locators refuse control mutations while root reads remain available", () => {
  const f = fixture([{ value: "a", label: "Alpha" }, { value: "b", label: "Beta" }]);
  for (const operation of ["select_option", "check"]) {
    for (const locator of [undefined, null, {}, []]) {
      assert.throws(() => f.run(operation, { locator, value: "b", checked: true }), /require locator/);
      assert.throws(() => f.run(operation, { locator, ref: "e1", value: "b", checked: true }), /top-level ref/);
    }
  }
  assert.equal(f.element.value, "a");
  assert.deepEqual(f.events, []);
  assert.equal(f.run("query", { locator: undefined }).count, 1);
  assert.equal(f.run("dom_snapshot", { locator: undefined }).elements.length, 1);
});

test("single selects reject multi-selection and invalid value types without side effects", () => {
  const f = fixture([{ value: "a", label: "Alpha" }, { value: "b", label: "Beta" }]);
  assert.throws(() => f.run("select_option", { value: ["a", "b"] }), /select multiple/);
  for (const value of [undefined, null, 3, { label: "Beta" }]) assert.throws(() => f.run("select_option", { value }), /string/);
  assert.equal(f.element.value, "a");
  assert.deepEqual(f.events, []);
});

test("unique labels select one option even when another option shares its value", () => {
  const f = fixture([{ value: "same", label: "First" }, { value: "same", label: "Second" }], { multiple: true });
  assert.equal(f.run("select_option", { value: "Second" }).outcome.verified, true);
  assert.deepEqual(f.element.selectedOptions, [f.element.options[1]]);
});

test("a change handler that changes or replaces the selected option makes the result uncertain", () => {
  const f = fixture([{ value: "same", label: "First" }, { value: "same", label: "Second" }]);
  f.element.onEvent = () => { f.element.options[0].selected = true; };
  assert.equal(f.run("select_option", { value: "Second" }).outcome.verified, false);
  delete f.element.onEvent;
  f.element.isConnected = false;
  assert.equal(f.run("select_option", { value: "Second" }).outcome.verified, false);
});

test("a handler that rewrites a selected option value makes the result uncertain", () => {
  const f = fixture([{ value: "initial", label: "Initial" }, { value: "wanted", label: "Wanted" }]);
  f.element.onEvent = () => { f.element.options[1].value = "rewritten"; };
  const result = f.run("select_option", { value: "Wanted" });
  assert.equal(result.outcome.verified, false);
  assert.deepEqual(f.plain(result.outcome.expected), ["wanted"]);
  assert.deepEqual(f.plain(result.outcome.actual), ["rewritten"]);
});

test("non-select targets and disabled fieldsets reject before events", () => {
  const f = fixture([{ value: "a", label: "Alpha" }], { fieldsetDisabled: true });
  assert.throws(() => f.run("select_option", { value: "a" }), /disabled/);
  f.element.tagName = "INPUT";
  assert.throws(() => f.run("select_option", { value: "a" }), /native <select>/);
  assert.deepEqual(f.events, []);
});

test("inspect and dom_snapshot expose the same bounded option metadata and select action", () => {
  const specs = Array.from({ length: 102 }, (_, i) => ({ value: `v${i}`, label: `Label ${i}`, groupDisabled: i === 1 }));
  specs[2] = { value: "v".repeat(301), label: "L".repeat(301) };
  const f = fixture(specs, { multiple: true });
  const inspected = f.run("inspect");
  const snap = f.run("dom_snapshot").elements[0];
  assert.deepEqual(f.plain(snap.options), f.plain(inspected.options));
  assert.equal(inspected.option_count, 102);
  assert.equal(inspected.options_truncated, true);
  assert.equal(inspected.options.length, 100);
  assert.equal(inspected.multiple, true);
  assert.equal(inspected.options[0].selected, true);
  assert.equal(inspected.options[1].disabled, true);
  assert.equal(inspected.options[2].label.length, 300);
  assert.equal(inspected.options[2].value.length, 300);
  assert.equal(inspected.options[2].label_truncated, true);
  assert.equal(inspected.options[2].value_truncated, true);
  assert(snap.actions.includes("select_option"));
});

test("sensitive select values stay redacted in metadata and selection outcomes", () => {
  const f = fixture([{ value: "1234", label: "1234" }, { value: "5678", label: "5678" }], { name: "Credit card number" });
  const inspected = f.plain(f.run("inspect"));
  assert.equal(inspected.text, "[redacted]");
  assert(inspected.options.every((o) => o.value === "[redacted]" && o.label === "[redacted]"));
  const result = f.run("select_option", { value: "5678" });
  assert.equal(result.value, "[redacted]");
  assert.deepEqual(f.plain(result.outcome.expected), ["[redacted]"]);
  assert.deepEqual(f.plain(result.outcome.actual), ["[redacted]"]);
});

test("mixed masked and ordinary options preserve redaction in multi-select outcomes", () => {
  const f = fixture([{ value: "••••", label: "Masked" }, { value: "normal", label: "Normal" }], { multiple: true });
  const result = f.run("select_option", { value: ["Masked", "Normal"] });
  assert.equal(result.outcome.verified, true);
  assert.deepEqual(f.plain(result.outcome.expected), ["[redacted]", "[redacted]"]);
  assert.deepEqual(f.plain(result.outcome.actual), ["[redacted]", "[redacted]"]);
  assert.equal(f.run("inspect").options[0].value, "[redacted]");
});

test("a masked option label protects its underlying value", () => {
  const f = fixture([{ value: "private-value", label: "••••" }, { value: "normal", label: "Normal" }]);
  const inspected = f.run("inspect");
  assert.equal(inspected.text, "[redacted]");
  assert.equal(inspected.options[0].value, "[redacted]");
  assert.equal(inspected.options[0].label, "[redacted]");
  const result = f.run("select_option", { value: "private-value" });
  assert.equal(result.value, "[redacted]");
  assert.deepEqual(f.plain(result.outcome.expected), ["[redacted]"]);
});

for (const chained of [false, true]) {
  const path = chained ? "chained" : "ordinary";
  test(`${path}: a handler removing a masked label cannot expose the selected value`, () => {
    const f = fixture([{ value: "dummy-sensitive-value", label: "••••" }, { value: "normal", label: "Normal" }]);
    f.element.onEvent = (event) => {
      if (event.type === "change") f.element.options[0].label = "Selected option";
    };
    const result = f.run("select_option", { value: "dummy-sensitive-value" }, chained);
    assert.equal(result.outcome.verified, true);
    assert.equal(result.value, "[redacted]");
    assert.deepEqual(f.plain(result.outcome.expected), ["[redacted]"]);
    assert.deepEqual(f.plain(result.outcome.actual), ["[redacted]"]);
    assert(!JSON.stringify(result).includes("dummy-sensitive-value"));
  });

  test(`${path}: a handler relabeling an ordinary option preserves its public value`, () => {
    const f = fixture([{ value: "standard", label: "Standard" }, { value: "priority", label: "Priority" }]);
    f.element.onEvent = (event) => {
      if (event.type === "change") f.element.options[1].label = "Selected option";
    };
    const result = f.run("select_option", { value: "Priority" }, chained);
    assert.equal(result.outcome.verified, true);
    assert.equal(result.value, "priority");
    assert.deepEqual(f.plain(result.outcome.expected), ["priority"]);
    assert.deepEqual(f.plain(result.outcome.actual), ["priority"]);
  });
}
