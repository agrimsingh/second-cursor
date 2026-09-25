// E2E: start the stdio adapter, negotiate MCP, discover tools, and call the live app.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const dir = new URL(`../e2e/artifacts/stdio-${Date.now()}/`, import.meta.url);
await mkdir(dir, { recursive: true });
const child = spawn(
  process.execPath,
  [fileURLToPath(new URL("./mcp-stdio.mjs", import.meta.url))],
  { stdio: ["pipe", "pipe", "pipe"] },
);
const pending = new Map(),
  transcript = [];
let id = 0,
  stderr = "";
child.stderr.on("data", (chunk) => (stderr += chunk));
child.on("exit", (code) => {
  for (const waiter of pending.values()) {
    clearTimeout(waiter.timer);
    waiter.reject(new Error(`Adapter exited (${code}): ${stderr}`));
  }
  pending.clear();
});
createInterface({ input: child.stdout }).on("line", (line) => {
  const result = JSON.parse(line);
  transcript.push(result);
  const waiter = pending.get(result.id);
  if (!waiter) return;
  pending.delete(result.id);
  clearTimeout(waiter.timer);
  if (result.error) waiter.reject(new Error(JSON.stringify(result.error)));
  else waiter.resolve(result.result);
});
const request = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const key = ++id,
      timer = setTimeout(() => {
        pending.delete(key);
        reject(new Error(`Timeout: ${method}`));
      }, 10000);
    pending.set(key, { resolve, reject, timer });
    child.stdin.write(
      JSON.stringify({ jsonrpc: "2.0", id: key, method, params }) + "\n",
    );
  });
try {
  const initialized = await request("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "second-cursor-e2e", version: "1" },
  });
  if (!initialized.serverInfo) throw Error("Missing server identity");
  child.stdin.write(
    JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) +
      "\n",
  );
  const catalog = await request("tools/list");
  if (!catalog.tools.some((t) => t.name === "browser_command"))
    throw Error("Extended browser tool missing");
  const status = await request("tools/call", { name: "status", arguments: {} });
  if (
    status.isError ||
    !JSON.parse(status.content.find((x) => x.type === "text").text).permissions
  )
    throw Error("Live status unavailable");
  await writeFile(
    new URL("report.md", dir),
    "PASS: stdio process, MCP initialization, tool discovery, and live runtime status.\nRerun: node sdk/stdio-smoke.mjs\n",
  );
} finally {
  child.kill();
  for (const waiter of pending.values()) clearTimeout(waiter.timer);
  await writeFile(
    new URL("transcript.json", dir),
    JSON.stringify({ responses: transcript, stderr }, null, 2),
  );
  console.log(fileURLToPath(dir));
}
