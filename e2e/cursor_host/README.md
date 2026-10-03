# Cursor benchmark host

This optional host lets Cursor models select Second Cursor's scoped fixture tools.
It uses the existing independent judges, callback budgets, and latency evidence.
It does not replace the native app or turn Cursor into a second computer driver.

Install the pinned runtime with Node 22.13 or newer:

```sh
npm ci --prefix e2e/cursor_host --ignore-scripts
node e2e/cursor_host/bridge.mjs --login
node e2e/cursor_host/bridge.mjs --catalog grok-4.5
```

Login opens Cursor's official browser flow and stores its key through the SDK.
Existing `CURSOR_API_KEY` or SDK login also works. IDE/CLI login credentials are
not extracted, copied, or printed. Never put a key in a benchmark command or file.

Run a disposable native task through Second Cursor:

```sh
python3 e2e/model_benchmark.py --engine second-cursor --task native_notes \
  --model-host cursor-sdk --model grok-4.5 --reasoning-effort medium \
  --model-param effort=medium --model-param fast=true \
  --observation-mode upfront --rounds 1 --timeout 90
```

For Grok 4.7, use `--model grok-4.7 --reasoning-effort medium` with
`--model-param reasoning_effort=medium --model-param fast=true --model-param context=256k`.
Grok 4.5 uses `effort` and has no context parameter. The bridge requires every
parameter exposed by the live catalog, rejects unknown settings and fallback
models, and verifies the SDK's returned model selection. Add `--list` to inspect
the prepared schedule without starting the model or desktop fixtures.

The current benchmark contract supports models with an explicit `effort` or
`reasoning_effort` parameter and an explicit Fast setting. Other Cursor parameter
shapes need corresponding identity validation before use.

Each trial has an empty workspace, private store, private `CURSOR_DATA_DIR`, no
ambient settings or MCP servers, and two allowed MCP protocol operations. The
only registered callbacks are the scoped fixture tools. SDK and Node versions
are recorded; the SDK source hash must match the audited 1.0.35 package. Upgrades
fail closed until the allowlist and configuration behavior are audited again.
The SDK enforces tool selection through its backend. Its local runtime retains
built-in executors; this is **not an OS sandbox or an observed complete tool
inventory**, and artifacts state that distinction. Callback session IDs,
observed MCP targets, model identity, deadlines, and process cleanup are also
checked locally.

Cursor uses its own coding system prompt and agent runtime. A Cursor-versus-Codex
measurement compares host/model combinations. Ordinary driver comparison still
requires identical host/model configurations. For a model selection experiment,
interleave fresh trials, retain failures, compare exact task success before
latency, and use harder tasks before inferring reasoning quality from the simple
Calculator-to-TextEdit task. Token totals retain Cursor's accounting semantics;
they are not interchangeable with Codex usage or dollar cost.

Offline checks:

```sh
python3 -m unittest discover -s e2e/benchmarks -p 'test_*.py'
npm test --prefix e2e/cursor_host
```
