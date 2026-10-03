import assert from "node:assert/strict";
import { SecondCursor, SecondCursorError } from "./index.mjs";
import { runAgent } from "./agent.mjs";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const log = [],
  clients = [];
const path = new URL(`../e2e/artifacts/sdk-${Date.now()}/`, import.meta.url);
await mkdir(path, { recursive: true });
try {
  for (const transport of ["http", "mcp"]) {
    const client = await SecondCursor.local({
      transport,
      onResult: (r) =>
        log.push({
          ...r,
          data: JSON.parse(
            JSON.stringify(r.data, (k, v) => (k === "images" ? "<images>" : v)),
          ),
        }),
    });
    clients.push(client);
    assert(client.sessionId && client.sessionId !== "legacy", "SDK must own a real host session");
    const task = await client.createTask({ title: `SDK ${transport} verification`, client_name: "SDK smoke" });
    assert.equal((await task.get()).id, task.id);
    let turn = 0;
    const run = await runAgent({
      client,
      task: "Check runtime status",
      adapter: {
        next: async ({ history }) => {
          if (turn++ === 0)
            return { calls: [{ id: "status", name: "status", arguments: {} }] };
          if (!history.at(-1).result?.permissions)
            throw Error("Agent loop lost tool result");
          return { text: "Runtime observed" };
        },
      },
    });
    if (run.text !== "Runtime observed")
      throw Error("Agent loop did not complete");
    const state = await client.state();
    if (!state.apps.length) throw Error("No apps returned");
    const instances = (await client.call("browser_instances")).browsers;
    if (!instances.length) throw Error("No extension browser");
    const tab = await client
      .browser(instances[0].id)
      .open(
        "data:text/html,<title>SDK parity</title><label>Name<input></label><button>Save</button>",
      );
    await tab.getByLabel("Name").fill("SDK " + transport);
    assert(tab.observation?.snapshot_id, "Browser mutation must refresh the handle observation");
    if ((await tab.getByLabel("Name").inspect()).text !== "SDK " + transport)
      throw Error("SDK fill did not persist");
    await task.pause();
    assert.equal((await task.get()).status, "paused");
    await assert.rejects(task.update({ status: "thinking" }), "Host heartbeat must not undo pause");
    await task.takeover();
    assert.equal(tab.observation, null, "Takeover must invalidate the cached observation");
    await assert.rejects(tab.getByLabel("Name").fill("unsafe"), (error) => error instanceof SecondCursorError && error.code === "STALE_OBSERVATION");
    await task.resume();
    await tab.observe();
    await tab.getByLabel("Name").fill("Resumed " + transport);
    assert.equal((await tab.getByLabel("Name").inspect()).text, "Resumed " + transport);
    const shot = await tab.screenshot();
    await writeFile(
      new URL(`${transport}.png`, path),
      Buffer.from(shot.images[0].data, "base64"),
    );
    await tab.close();
    await task.update({ status: "completed" });
    assert.equal((await task.get()).status, "completed");
    await client.close();
  }
  await writeFile(
    new URL("report.md", path),
    "PASS: MCP and HTTP SDK host sessions, named tasks, enforced pause, takeover/resume with fresh observations, discovery, browser binding, DOM fill, screenshot, cleanup, and model-independent loop with a deterministic adapter calling the live runtime.\nRerun: node sdk/smoke.mjs\n",
  );
} finally {
  for (const c of clients) await c.close().catch(() => {});
  await writeFile(
    new URL("transcript.json", path),
    JSON.stringify(log, null, 2),
  );
  console.log(fileURLToPath(path));
}
