import { SecondCursor } from "./index.mjs";
import { runAgent } from "./agent.mjs";
import { mkdir, writeFile } from "node:fs/promises";
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
    if ((await tab.getByLabel("Name").inspect()).text !== "SDK " + transport)
      throw Error("SDK fill did not persist");
    const shot = await tab.screenshot();
    await writeFile(
      new URL(`${transport}.png`, path),
      Buffer.from(shot.images[0].data, "base64"),
    );
    await tab.close();
    await client.close();
  }
  await writeFile(
    new URL("report.md", path),
    "PASS: MCP and HTTP SDK sessions, discovery, browser binding, DOM fill, observation, screenshot, cleanup, and model-independent loop with a deterministic adapter calling the live runtime.\nRerun: node sdk/smoke.mjs\n",
  );
} finally {
  for (const c of clients) await c.close().catch(() => {});
  await writeFile(
    new URL("transcript.json", path),
    JSON.stringify(log, null, 2),
  );
  console.log(path.pathname);
}
