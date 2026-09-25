/** Dependency-free SDK. All actions use the same authenticated dispatcher as MCP. */
export class SecondCursor {
  constructor({
    url = "http://127.0.0.1:8787",
    token,
    sessionId,
    transport = "http",
    onResult,
  } = {}) {
    this.url = url.replace(/\/$/, "");
    this.token = token;
    this.sessionId = sessionId;
    this.transport = transport;
    this.onResult = onResult;
    this.nextId = 0;
  }
  static async local(options = {}) {
    const { readFile } = await import("node:fs/promises"),
      { homedir } = await import("node:os");
    const config = JSON.parse(
      await readFile(
        `${process.env.SC_HOME || `${homedir()}/Library/Application Support/SecondCursor`}/config.json`,
        "utf8",
      ),
    );
    const client = new SecondCursor({
      url: `http://127.0.0.1:${config.port}`,
      token: config.token,
      ...options,
    });
    await client.connect();
    return client;
  }
  async connect() {
    if (!this.sessionId)
      this.sessionId = (
        await this.call("session", { action: "create" })
      ).session_id;
    return this;
  }
  async call(tool, args = {}) {
    const operationId = args.operation_id ?? crypto.randomUUID();
    const params = {
      ...args,
      ...(this.sessionId ? { session_id: this.sessionId } : {}),
      operation_id: operationId,
    };
    const mcp = this.transport === "mcp";
    const response = await fetch(this.url + (mcp ? "/mcp" : "/api"), {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
        Accept: "application/json",
        "MCP-Protocol-Version": "2025-06-18",
      },
      body: JSON.stringify(
        mcp
          ? {
              jsonrpc: "2.0",
              id: ++this.nextId,
              method: "tools/call",
              params: { name: tool, arguments: params },
            }
          : { tool, args: params },
      ),
    });
    let result = await response.json();
    if (mcp) result = result.result ?? result;
    if (!response.ok || result.error || result.isError) {
      const error = new Error(
        result.content?.find((x) => x.type === "text")?.text ??
          JSON.stringify(result.error ?? result),
      );
      error.operationId = operationId;
      throw error;
    }
    const data = JSON.parse(result.content.find((x) => x.type === "text").text);
    const images = result.content.filter((x) => x.type === "image");
    if (images.length) data.images = images;
    await this.onResult?.({ tool, args: params, data });
    return data;
  }
  async state() {
    const [apps, windows, browsers] = await Promise.all([
      this.call("list_apps"),
      this.call("list_windows"),
      this.call("browser_instances"),
    ]);
    return { ...apps, ...windows, ...browsers };
  }
  async app(query, { windowId, newInstance = false } = {}) {
    let windows = (await this.call("list_windows", { app: query })).windows;
    if (!windows.length)
      windows = (
        await this.call("launch_app", { app: query, new_instance: newInstance })
      ).windows;
    const chosen = windowId
      ? windows.find((w) => w.id === windowId)
      : windows.length === 1
        ? windows[0]
        : null;
    if (!chosen)
      throw new Error(
        "Select an explicit windowId; application has zero or multiple windows",
      );
    const handle = new App(this, chosen.id);
    await handle.observe();
    return handle;
  }
  async window(id) {
    const handle = new App(this, id);
    await handle.observe();
    return handle;
  }
  browser(id) {
    return new Browser(this, id);
  }
  status(id) {
    return this.call("operation_status", { operation_id: id });
  }
  cancel(id) {
    return this.call("cancel_operation", { operation_id: id });
  }
  async close() {
    if (this.sessionId) {
      await this.call("session", { action: "close" });
      this.sessionId = null;
    }
  }
}
export class App {
  constructor(client, id) {
    this.client = client;
    this.id = id;
    this.observation = null;
  }
  async observe(options = {}) {
    this.observation = await this.client.call("get_window_state", {
      window_id: this.id,
      structured: true,
      ...options,
    });
    return this.observation;
  }
  async act(tool, args = {}) {
    const result = await this.client.call(tool, {
      window_id: this.id,
      ...(this.observation
        ? { observation_id: this.observation.snapshot_id }
        : {}),
      ...args,
    });
    if (result.observation?.snapshot_id) this.observation = result.observation;
    return result;
  }
  click(target, options = {}) {
    return this.act("click", {
      ...(typeof target === "number"
        ? { element_index: target }
        : { x: target[0], y: target[1] }),
      ...options,
    });
  }
  typeText(text, index) {
    return this.act("type_text", { text, element_index: index });
  }
  pressKey(keys) {
    return this.act("press_key", { keys });
  }
  setValue(index, value) {
    return this.act("set_value", {
      element_index: index,
      value: String(value),
    });
  }
  selectText(index, text, options = {}) {
    return this.act("select_text", { element_index: index, text, ...options });
  }
  paste(text, format = "text") {
    return this.act("paste", { text, format });
  }
  inspect(index, options = {}) {
    return this.client.call("inspect_element", {
      window_id: this.id,
      element_index: index,
      ...options,
    });
  }
  scroll(target, direction, amount = 3) {
    return this.act("scroll", {
      ...(typeof target === "number"
        ? { element_index: target }
        : { x: target[0], y: target[1] }),
      direction,
      amount,
    });
  }
  drag(from, to) {
    return this.act("drag", {
      from_x: from[0],
      from_y: from[1],
      to_x: to[0],
      to_y: to[1],
    });
  }
  performAction(index, action) {
    return this.act("perform_action", { element_index: index, action });
  }
  screenshot() {
    return this.client.call("screenshot", { window_id: this.id });
  }
  batch(steps, options = {}) {
    return this.act("batch", { steps, ...options });
  }
  release() {
    return this.client.call("bind_target", {
      target: `window:${this.id}`,
      action: "release",
    });
  }
}
export class Browser {
  constructor(client, id) {
    this.client = client;
    this.id = id;
  }
  call(tool, args = {}) {
    return this.client.call(tool, { browser_id: this.id, ...args });
  }
  command(operation, args = {}) {
    return this.call("browser_command", { operation, ...args });
  }
  tabs() {
    return this.call("browser_tabs");
  }
  userTabs() {
    return this.command("user_tabs");
  }
  history(options = {}) {
    return this.command("history", options);
  }
  async open(url) {
    const { tab, observation } = await this.call("browser_open", { url });
    const handle = new Tab(this, tab.id);
    if (observation) handle.observation = observation;
    else await handle.observe();
    return handle;
  }
  async claim(id) {
    await this.command("claim", { tab_id: id });
    const handle = new Tab(this, id);
    await handle.observe();
    return handle;
  }
  async tab(id) {
    const handle = new Tab(this, id);
    await handle.observe();
    return handle;
  }
}
export class Tab {
  constructor(browser, id) {
    this.browser = browser;
    this.id = id;
    this.observation = null;
  }
  command(operation, args = {}) {
    return this.browser.command(operation, { tab_id: this.id, ...args });
  }
  async observe(options = {}) {
    this.observation = await this.browser.call("browser_snapshot", {
      tab_id: this.id,
      ...options,
    });
    return this.observation;
  }
  domSnapshot() {
    return this.command("dom_snapshot");
  }
  locator(spec) {
    return new Locator(
      this,
      typeof spec === "string" ? { selector: spec } : spec,
    );
  }
  getByRole(role, { name, ...options } = {}) {
    return this.locator({ role, name, ...options });
  }
  getByLabel(label, options = {}) {
    return this.locator({ label, ...options });
  }
  getByText(text, options = {}) {
    return this.locator({ text, ...options });
  }
  getByPlaceholder(placeholder, options = {}) {
    return this.locator({ placeholder, ...options });
  }
  getByTestId(test_id) {
    return this.locator({ test_id });
  }
  goto(url) {
    return this.browser.call("browser_navigate", { tab_id: this.id, url });
  }
  back() {
    return this.command("back");
  }
  forward() {
    return this.command("forward");
  }
  reload() {
    return this.command("reload");
  }
  close() {
    return this.browser.call("browser_close", { tab_id: this.id });
  }
  release() {
    return this.command("release");
  }
  markDeliverable() {
    return this.command("retain", { mode: "deliverable" });
  }
  markHandoff() {
    return this.command("retain", { mode: "handoff" });
  }
  screenshot(options = {}) {
    return this.command("screenshot", options);
  }
  logs(options = {}) {
    return this.command("logs", options);
  }
  viewport(options) {
    return this.command("viewport", options);
  }
  dialog(options = {}) {
    return this.command("dialog", options);
  }
  wait(options) {
    return this.command("wait", options);
  }
  export(format = "text") {
    return this.command("export", { format });
  }
  assets(options = {}) {
    return this.command("assets", options);
  }
  cdp(method, params = {}) {
    return this.command("cdp", { method, params });
  }
  clipboard(action = "read", text) {
    return this.command("clipboard", { action, text });
  }
  batch(steps) {
    return this.command("batch", { steps });
  }
  click(point, options = {}) {
    return this.command("click", { x: point[0], y: point[1], ...options });
  }
  drag(from, to) {
    return this.command("drag", {
      from_x: from[0],
      from_y: from[1],
      to_x: to[0],
      to_y: to[1],
    });
  }
  pressKey(keys) {
    return this.command("press_key", { keys });
  }
  typeText(text) {
    return this.command("type", { text });
  }
}
export class Locator {
  constructor(tab, spec) {
    this.tab = tab;
    this.spec = spec;
  }
  command(operation, args = {}) {
    return this.tab.command(operation, { locator: this.spec, ...args });
  }
  frame(frame) {
    return new Locator(this.tab, { ...this.spec, frame });
  }
  nth(nth) {
    return new Locator(this.tab, { ...this.spec, nth });
  }
  click(options = {}) {
    return this.command("click", options);
  }
  dblclick(options = {}) {
    return this.click({ ...options, count: 2 });
  }
  fill(text) {
    return this.command("fill", { text });
  }
  type(text) {
    return this.command("type", { text });
  }
  press(keys) {
    return this.command("press_key", { keys });
  }
  check(checked = true) {
    return this.command("check", { checked });
  }
  uncheck() {
    return this.check(false);
  }
  selectOption(value) {
    return this.command("select_option", { value });
  }
  selectText(text, options = {}) {
    return this.command("select_text", { text, ...options });
  }
  paste(text, format = "text") {
    return this.command("paste", { text, format });
  }
  inspect(options = {}) {
    return this.command("inspect", options);
  }
  async innerText() {
    return (await this.inspect()).text;
  }
  async getAttribute(name) {
    return (await this.inspect()).attributes[name] ?? null;
  }
  performAction(action) {
    return this.command("perform_action", { action });
  }
  setFiles(files) {
    return this.command("upload", {
      files: Array.isArray(files) ? files : [files],
    });
  }
  waitFor(state = "visible", timeout_ms = 10000) {
    return this.command("wait", { state, timeout_ms });
  }
  scroll(direction, amount = 3) {
    return this.command("scroll", { direction, amount });
  }
}
