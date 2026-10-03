#!/usr/bin/env node
/** Stdio adapter for MCP hosts that cannot connect over HTTP. The app's native
 * `SecondCursor mcp` does the same without Node and also starts the app; prefer it. */
import { createInterface } from "node:readline";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";

const home =
  process.env.SC_HOME ||
  `${homedir()}/Library/Application Support/SecondCursor`;
const config = JSON.parse(await readFile(`${home}/config.json`, "utf8"));
const endpoint = `http://127.0.0.1:${config.port}/mcp`;
const inFlight = new Set();
let state = "uninitialized";
let sessionID;
let protocolVersion = "2025-06-18";
let initializationFailure;
let pending = [];

// A host may close its read end without draining our replies. Cleanup must still
// finish, and no diagnostic may contaminate the JSON-RPC stream on stdout.
process.stdout.on("error", (error) => {
  if (error.code !== "EPIPE") console.error(`Second Cursor mcp: ${error.message}`);
});

function write(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

function isRequest(message) {
  return message.id !== undefined && message.id !== null;
}

function reject(message, reason) {
  if (isRequest(message)) write(rpcError(message.id, -32000, reason));
}

function headers(session) {
  return {
    Authorization: `Bearer ${config.token}`,
    "Content-Type": "application/json",
    Accept: "application/json",
    "MCP-Protocol-Version": protocolVersion,
    ...(session ? { "Mcp-Session-Id": session } : {}),
  };
}

async function post(message, session) {
  // A mutating request is never retried: a dropped response cannot establish
  // whether its action ran. Recovery requires an explicit initialize from the host.
  const response = await fetch(endpoint, {
    method: "POST",
    headers: headers(session),
    body: JSON.stringify(message),
    signal: AbortSignal.timeout(90_000),
  });
  let body = await response.text();
  if (response.headers.get("content-type")?.includes("text/event-stream")) {
    body = body.split(/\r?\n/).filter((line) => line.startsWith("data:")).at(-1)?.slice(5).trim() ?? body;
  }
  let payload;
  try { payload = JSON.parse(body); } catch {}
  if (response.ok && payload && (payload.result !== undefined || payload.error !== undefined)) {
    return { response, payload };
  }
  const detail = payload?.error?.message ?? body.slice(0, 300);
  return {
    response,
    payload: rpcError(message.id ?? null, payload?.error?.code ?? -32603,
      `HTTP ${response.status} from Second Cursor: ${detail || "empty response"}`),
  };
}

function start(message, session) {
  const work = forward(message, session).finally(() => inFlight.delete(work));
  inFlight.add(work);
}

async function forward(message, session) {
  const isInitialize = message.method === "initialize";
  let payload;
  let initializedSession;
  try {
    const result = await post(message, session);
    payload = result.payload;
    if (isInitialize && result.response.ok && payload.result) {
      initializedSession = result.response.headers.get("Mcp-Session-Id");
      if (initializedSession) {
        // Save the identity before the initialize response reaches the host.
        sessionID = initializedSession;
        protocolVersion = payload.result.protocolVersion ?? protocolVersion;
      } else {
        payload = rpcError(message.id, -32603,
          "Initialize did not return Mcp-Session-Id. Update the Second Cursor app before using this proxy.");
      }
    } else if (!isInitialize && result.response.status === 404 && sessionID === session && state === "ready") {
      state = "invalid";
      sessionID = undefined;
      initializationFailure = "The MCP session expired or the app restarted. Send initialize again; the previous action was not retried.";
    } else if (isInitialize && result.response.status === 404) {
      sessionID = undefined;
    }
  } catch (error) {
    if (!isInitialize && error.cause?.code === "ECONNREFUSED" && sessionID === session && state === "ready") {
      state = "invalid";
      initializationFailure = "The app stopped accepting connections. Send initialize again; the previous action was not retried.";
    }
    payload = rpcError(message.id ?? null, -32603, `Could not reach Second Cursor: ${error.message}`);
  }
  if (isRequest(message)) write(payload);
  if (!isInitialize) return;

  state = initializedSession ? "ready" : "uninitialized";
  initializationFailure = initializedSession ? undefined : payload.error?.message ?? "MCP initialization failed.";
  const waiting = pending;
  pending = [];
  for (const queued of waiting) {
    if (initializedSession) start(queued, initializedSession);
    else reject(queued, initializationFailure);
  }
}

const input = createInterface({ input: process.stdin });
input.on("line", (line) => {
  if (!line.trim()) return;
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    write(rpcError(null, -32700, "Parse error: stdin lines must be JSON-RPC messages"));
    return;
  }
  if (!message || Array.isArray(message) || typeof message !== "object" || typeof message.method !== "string") {
    write(rpcError(null, -32600, "Invalid JSON-RPC request"));
    return;
  }
  if (message.method === "initialize") {
    if (!isRequest(message) || state === "initializing" || state === "ready") {
      reject(message, "An initialize request is already active. Reinitialize only after initialization fails or the session expires.");
      return;
    }
    state = "initializing";
    initializationFailure = undefined;
    // A refused connection may recover the same persisted session after restart.
    // Only an explicit unknown-session response discards its operation history.
    start(message, sessionID);
  } else if (state === "initializing") {
    pending.push(message);
  } else if (state === "ready") {
    start(message, sessionID);
  } else {
    reject(message, initializationFailure ?? "Send initialize before using this MCP connection.");
  }
});

input.on("close", async () => {
  // Initialize may release more requests while this wait is in progress.
  while (inFlight.size) await Promise.allSettled([...inFlight]);
  if (!sessionID) return;
  try {
    const response = await fetch(endpoint, {
      method: "DELETE",
      headers: headers(sessionID),
      signal: AbortSignal.timeout(5_000),
    });
    await response.arrayBuffer();
    if (!response.ok && response.status !== 404) console.error(`Second Cursor mcp: session cleanup returned HTTP ${response.status}`);
  } catch (error) {
    console.error(`Second Cursor mcp: session cleanup failed: ${error.message}`);
  }
});
