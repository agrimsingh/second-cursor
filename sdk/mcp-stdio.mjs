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
const input = createInterface({ input: process.stdin });
input.on("line", async (line) => {
  let request;
  try {
    request = JSON.parse(line);
    const response = await fetch(`http://127.0.0.1:${config.port}/mcp`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.token}`,
        "Content-Type": "application/json",
        Accept: "application/json",
        "MCP-Protocol-Version": "2025-06-18",
      },
      body: line,
    });
    const body = await response.text();
    if (body && request.id !== undefined) process.stdout.write(body + "\n");
  } catch (error) {
    if (request?.id !== undefined)
      process.stdout.write(
        JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          error: { code: -32603, message: error.message },
        }) + "\n",
      );
  }
});
