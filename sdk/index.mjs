/** Dependency-free SDK. All actions use the same authenticated dispatcher as MCP. */
export class SecondCursorError extends Error {
  constructor(message, { code, operationId, sessionId, tool, outcomeUncertain = false, status, result, cause } = {}) {
    super(message, { cause });
    this.name = "SecondCursorError";
    Object.assign(this, { code, operationId, sessionId, tool, outcomeUncertain, status, result });
    this.recovery = tool === "task" ? "task_state" : ["session", "initialize"].includes(tool) ? "session_state" : tool === "bind_target" ? "target_state" : "operation_status";
  }
}

function responseMessage(result) {
  return (Array.isArray(result?.content) ? result.content.find((item) => item?.type === "text")?.text : undefined) ??
    result?.error?.message ?? JSON.stringify(result?.error ?? result) ?? "Unknown host error";
}

export class SecondCursor {
  constructor({
    url = "http://127.0.0.1:8787",
    token,
    sessionId,
    transport = "http",
    onResult,
    timeoutMs = 65_000,
  } = {}) {
    this.url = url.replace(/\/$/, "");
    this.token = token;
    this.sessionId = sessionId;
    this.transport = transport;
    this.onResult = onResult;
    this.timeoutMs = timeoutMs;
    this.nextId = 0;
    this._connecting = null;
    this._connected = false;
    this._observations = new Map();
    this._invalidObservations = new Set();
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
  async connect(options = {}) {
    const context = { operationId: crypto.randomUUID(), sessionId: this.sessionId, tool: this.transport === "mcp" ? "initialize" : "session" };
    const timeoutMs = options.timeoutMs ?? this.timeoutMs;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0)
      throw new SecondCursorError("timeoutMs must be a positive finite number", { ...context, code: "INVALID_ARGUMENT" });
    if (options.signal?.aborted)
      throw new SecondCursorError("Connection wait aborted before dispatch", { ...context, code: "ABORTED", cause: options.signal.reason });
    if (this._connected) return this;
    if (!this._connecting) {
      this._connecting = (async () => {
        if (this.transport === "mcp") {
          await this._request("initialize", {}, {}, true);
        } else if (!this.sessionId) {
          const operationId = crypto.randomUUID();
          const result = await this._request("session", { action: "create", operation_id: operationId }, {}, false, false);
          if (typeof result.session_id !== "string" || !result.session_id)
            throw new SecondCursorError("Host did not return a session identifier", { code: "INVALID_RESPONSE", tool: "session", operationId, sessionId: this.sessionId });
          this.sessionId = result.session_id;
        }
        this._connected = true;
      })();
      const pending = this._connecting;
      pending.finally(() => { if (this._connecting === pending) this._connecting = null; }).catch(() => {});
    }
    // Each caller may stop waiting independently; one aborted caller must not destroy
    // another caller's in-progress host session negotiation.
    let timer, abort;
    try {
      await Promise.race([
        this._connecting,
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new SecondCursorError("Connection wait timed out before tool dispatch", { ...context, code: "TIMEOUT" })), timeoutMs);
          abort = () => reject(new SecondCursorError("Connection wait aborted before tool dispatch", { ...context, code: "ABORTED", cause: options.signal.reason }));
          options.signal?.addEventListener("abort", abort, { once: true });
        }),
      ]);
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
    }
    return this;
  }
  async call(tool, args = {}, options = {}) {
    const startedAt = Date.now();
    const timeoutMs = options.timeoutMs ?? this.timeoutMs;
    if (options.signal?.aborted)
      throw new SecondCursorError("Request aborted before dispatch", { code: "ABORTED", tool, operationId: args.operation_id ?? crypto.randomUUID(), sessionId: this.sessionId, cause: options.signal.reason });
    await this.connect(options);
    const remainingMs = timeoutMs - (Date.now() - startedAt);
    if (remainingMs <= 0)
      throw new SecondCursorError("Request timed out before tool dispatch", { code: "TIMEOUT", tool, operationId: args.operation_id ?? crypto.randomUUID(), sessionId: this.sessionId });
    return this._request(tool, args, { ...options, timeoutMs: remainingMs });
  }
  async _request(tool, args = {}, options = {}, initialize = false, notifyResult = true) {
    const operationId = args.operation_id ?? crypto.randomUUID();
    const sessionId = this.sessionId;
    const context = { operationId, sessionId, tool };
    const timeoutMs = options.timeoutMs ?? this.timeoutMs;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0)
      throw new SecondCursorError("timeoutMs must be a positive finite number", { ...context, code: "INVALID_ARGUMENT" });
    if (options.signal?.aborted)
      throw new SecondCursorError("Request aborted before dispatch", { ...context, code: "ABORTED", cause: options.signal.reason });
    const params = {
      ...args,
      ...(sessionId ? { session_id: sessionId } : {}),
      operation_id: operationId,
    };
    const mcp = this.transport === "mcp";
    let body;
    try {
      body = JSON.stringify(mcp ? {
        jsonrpc: "2.0", id: ++this.nextId,
        method: initialize ? "initialize" : "tools/call",
        params: initialize ? { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "second-cursor-sdk", version: "0.3.0" } } : { name: tool, arguments: params },
      } : { tool, args: params });
    } catch (cause) {
      throw new SecondCursorError("Request arguments must be JSON serializable", { ...context, code: "INVALID_ARGUMENT", cause });
    }
    const controller = new AbortController();
    let timedOut = false, data;
    const abort = () => controller.abort(options.signal.reason);
    options.signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const response = await fetch(this.url + (mcp ? "/mcp" : "/api"), {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
          Accept: "application/json",
          "MCP-Protocol-Version": "2025-06-18",
          ...(mcp && sessionId ? { "Mcp-Session-Id": sessionId } : {}),
        },
        body,
        signal: controller.signal,
      });
      // Preserve an issued identity even if the handshake response body is damaged.
      const hostSession = initialize ? response.headers.get("Mcp-Session-Id") : null;
      if (hostSession) { this.sessionId = hostSession; context.sessionId = hostSession; }
      const raw = await response.text();
      let result;
      try { result = JSON.parse(raw); } catch (cause) {
        throw new SecondCursorError(response.ok ? "Host returned invalid JSON" : `HTTP ${response.status}: ${raw.slice(0, 500)}`, { ...context, code: response.ok ? "INVALID_RESPONSE" : "HTTP_ERROR", outcomeUncertain: !initialize && (response.ok || ![400, 401, 403, 404, 405, 413, 415].includes(response.status)), status: response.status, cause });
      }
      if (!response.ok)
        throw new SecondCursorError(`HTTP ${response.status}: ${responseMessage(result)}`, { ...context, code: "HTTP_ERROR", outcomeUncertain: !initialize && ![400, 401, 403, 404, 405, 413, 415].includes(response.status), status: response.status, result });
      if (mcp && result?.error)
        throw new SecondCursorError(responseMessage(result), { ...context, code: "RPC_ERROR", outcomeUncertain: ![-32600, -32601, -32602].includes(result.error.code), result });
      if (mcp) result = result?.result;
      if (initialize) {
        if (!hostSession || !result?.serverInfo)
          throw new SecondCursorError("MCP initialization did not return a host session and server identity", { ...context, code: "INVALID_RESPONSE" });
        this.sessionId = hostSession;
        return result;
      }
      if (result?.error || result?.isError)
        throw new SecondCursorError(responseMessage(result), { ...context, code: "TOOL_ERROR", outcomeUncertain: true, result });
      try {
        if (!Array.isArray(result?.content)) throw new Error("Missing result content");
        const text = result.content.find((item) => item.type === "text")?.text;
        if (typeof text !== "string") throw new Error("Missing JSON text result");
        data = JSON.parse(text);
        if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Expected object result");
        const images = result.content.filter((item) => item.type === "image");
        if (images.length) data.images = images;
      } catch (cause) {
        throw new SecondCursorError("Host returned an invalid tool result", { ...context, code: "INVALID_RESPONSE", outcomeUncertain: true, cause });
      }
    } catch (cause) {
      if (cause instanceof SecondCursorError) throw cause;
      throw new SecondCursorError(timedOut ? `Request timed out after ${timeoutMs} ms; reconcile before retrying` : controller.signal.aborted ? "Request aborted; reconcile before retrying" : "Connection failed; reconcile before retrying", {
        ...context, code: timedOut ? "TIMEOUT" : controller.signal.aborted ? "ABORTED" : "NETWORK_ERROR", outcomeUncertain: !initialize, cause,
      });
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
    }
    try { if (notifyResult) await this.onResult?.({ tool, args: params, data }); } catch (cause) {
      throw new SecondCursorError("Action completed but onResult failed", { ...context, code: "CALLBACK_ERROR", result: data, cause });
    }
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
  status(id, options = {}) {
    return this.call("operation_status", { operation_id: id }, options);
  }
  cancel(id, options = {}) {
    return this.call("cancel_operation", { operation_id: id }, options);
  }
  reconcile(operation, options = {}) {
    const id = typeof operation === "string" ? operation : operation?.operationId;
    if (!id) return Promise.reject(new SecondCursorError("Reconciliation requires an operationId", { code: "INVALID_ARGUMENT" }));
    if (typeof operation !== "string" && operation.sessionId != null && operation.sessionId !== this.sessionId)
      return Promise.reject(new SecondCursorError("Reconcile with the client that owns the operation's session", { code: "SESSION_MISMATCH", operationId: id, sessionId: operation.sessionId }));
    if (operation instanceof SecondCursorError && operation.recovery !== "operation_status") {
      const guidance = { task_state: "Use task().get() to inspect the current task state", session_state: "Validate the existing session before starting another session", target_state: "Read the target's current ownership before changing its binding" }[operation.recovery];
      return Promise.reject(new SecondCursorError(`${guidance}; this control is not stored in the operation journal`, { code: "UNTRACKED_OPERATION", operationId: id, sessionId: this.sessionId, tool: operation.tool }));
    }
    return this.status(id, options);
  }
  async tasks(options = {}) {
    return (await this.call("task", { ...options, action: "list" })).tasks;
  }
  async createTask(options = {}) {
    const { task } = await this.call("task", { ...options, action: "create" });
    return new Task(this, task.id, task);
  }
  async bindTask(id) {
    const { task } = await this.call("task", { action: "bind", task_id: id });
    this._invalidateObservations();
    return new Task(this, task.id, task);
  }
  _invalidateObservations() {
    for (const key of this._observations.keys()) this._invalidObservations.add(key);
    this._observations.clear();
  }
  _requireObservation(key, tool, operationId) {
    if (this._invalidObservations.has(key))
      throw new SecondCursorError("Call observe() again before acting on this target", { code: "STALE_OBSERVATION", tool, operationId: operationId ?? crypto.randomUUID(), sessionId: this.sessionId });
  }
  task(id) {
    return new Task(this, id);
  }
  async close() {
    if (this.sessionId) {
      let callbackError;
      try { await this.call("session", { action: "close" }); }
      catch (error) {
        if (error.code !== "CALLBACK_ERROR" || error.result?.closed !== true) throw error;
        callbackError = error;
      }
      this.sessionId = null;
      this._connected = false;
      this._invalidateObservations();
      if (callbackError) throw callbackError;
    }
  }
}
export class Task {
  constructor(client, id, snapshot = null) {
    this.client = client;
    this.id = id;
    this.snapshot = snapshot;
  }
  async command(action, args = {}, options = {}) {
    if (["pause", "takeover", "resume", "cancel"].includes(action) || (action === "update" && ["completed", "failed", "cancelled"].includes(args.status))) this.client._invalidateObservations();
    const result = await this.client.call("task", { ...args, action, ...(this.id ? { task_id: this.id } : {}) }, options);
    this.snapshot = result.task;
    if (result.task?.id) this.id = result.task.id;
    return this.snapshot;
  }
  get(options) { return this.command("get", {}, options); }
  update(update, options) { return this.command("update", update, options); }
  pause(options) { return this.command("pause", {}, options); }
  takeover(options) { return this.command("takeover", {}, options); }
  resume(options) { return this.command("resume", {}, options); }
  cancel(options) { return this.command("cancel", {}, options); }
}

export class App {
  constructor(client, id) {
    this.client = client;
    this.id = id;
    this._observationKey = `window:${id}`;
  }
  get observation() { return this.client._observations.get(this._observationKey) ?? null; }
  set observation(value) {
    if (value) {
      this.client._observations.set(this._observationKey, value);
      this.client._invalidObservations.delete(this._observationKey);
    } else {
      this.client._observations.delete(this._observationKey);
      this.client._invalidObservations.add(this._observationKey);
    }
  }
  async observe(options = {}, requestOptions = {}) {
    this.observation = null;
    this.observation = await this.client.call("get_window_state", {
      window_id: this.id,
      structured: true,
      ...options,
    }, requestOptions);
    return this.observation;
  }
  async act(tool, args = {}, options = {}) {
    this.client._requireObservation(this._observationKey, tool, args.operation_id);
    try {
      const result = await this.client.call(tool, {
        window_id: this.id,
        ...(this.observation?.snapshot_id ? { observation_id: this.observation.snapshot_id } : {}),
        ...args,
      }, options);
      this.observation = result.observation?.snapshot_id ? result.observation : null;
      return result;
    } catch (error) {
      this.observation = error.code === "CALLBACK_ERROR" ? error.result?.observation ?? null : null;
      throw error;
    }
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
  call(tool, args = {}, options = {}) {
    return this.client.call(tool, { browser_id: this.id, ...args }, options);
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
    this._observationKey = JSON.stringify(["tab", browser.id, id]);
  }
  get observation() { return this.browser.client._observations.get(this._observationKey) ?? null; }
  set observation(value) {
    if (value) {
      this.browser.client._observations.set(this._observationKey, value);
      this.browser.client._invalidObservations.delete(this._observationKey);
    } else {
      this.browser.client._observations.delete(this._observationKey);
      this.browser.client._invalidObservations.add(this._observationKey);
    }
  }
  async _act(tool, args, options = {}) {
    const readOnly = tool === "browser_command" && ["inspect", "screenshot", "logs", "export", "assets", "dom_snapshot", "history", "user_tabs", "retain", "release"].includes(args.operation);
    const releasesTarget = tool === "browser_close" || (tool === "browser_command" && args.operation === "release");
    if (!readOnly && tool !== "browser_close") this.browser.client._requireObservation(this._observationKey, tool, args.operation_id);
    try {
      const result = await this.browser.client.call(tool, {
        browser_id: this.browser.id, tab_id: this.id,
        ...(!readOnly && !releasesTarget && this.observation?.snapshot_id ? { observation_id: this.observation.snapshot_id } : {}),
        ...args,
      }, options);
      if (result.observation?.snapshot_id) this.observation = result.observation;
      else if (result.snapshot_id) this.observation = result;
      else if (!readOnly) {
        this.observation = null;
        // Extended browser operations can verify a control's value without returning
        // a document snapshot. Read the resulting state once; never replay the input.
        if (tool !== "browser_close" && args.observe !== "none" && !result.dialog_open) {
          try { result.observation = await this.observe({}, options); }
          catch (error) {
            result.observation = null;
            result.observation_error = error.message;
            result.observation_error_code = error.code;
          }
        }
      }
      if (releasesTarget) this.observation = null;
      return result;
    } catch (error) {
      this.observation = error.code === "CALLBACK_ERROR" ? error.result?.observation ?? null : null;
      throw error;
    }
  }
  command(operation, args = {}, options = {}) {
    return this._act("browser_command", { operation, ...args }, options);
  }
  async observe(options = {}, requestOptions = {}) {
    this.observation = null;
    this.observation = await this.browser.call("browser_snapshot", {
      tab_id: this.id,
      full: true,
      ...options,
    }, requestOptions);
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
    return this._act("browser_navigate", { url });
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
    return this._act("browser_close", {});
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
  hover(point, options = {}) {
    return this.command("hover", { x: point[0], y: point[1], ...options });
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
  hover(options = {}) {
    return this.command("hover", options);
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
