# Cursor model host verification — 2026-10-04

Cursor's SDK successfully ran Grok 4.7 Medium Fast through the installed Second
Cursor driver. The native app binary was unchanged
(`6de59480fde5ad6cf670de8d767bf1344d18e46c930380f24fbda8ac3893e76e`).

## Native pilot

Artifact: `e2e/artifacts/model-pilot-20261003T210739.583718Z/second-cursor.tasks.json`.
Focus evidence: `e2e/artifacts/workflow-probe-20261003T210738.331180Z/result.json`.

- Exact selection: `grok-4.7`, `reasoning_effort=medium`, `fast=true`, `context=256k`.
- One scheduled native task, one strict PASS. The independent judge confirmed
  Calculator displayed 42, the original note was preserved with `Total: 42`
  appended, and the underlying file was unchanged.
- Harness wall time: 27,508.2 ms. Exact final tool postcondition: 25,200.4 ms,
  subsequently confirmed by the independent judge. This does not measure the
  first instant at which the goal became true.
- Two upfront reads and two model-selected native batches; tool time 1,302.5 ms.
- Focus probe observed zero losses of app activity, key window, or first responder.
  This is not a test of simultaneous human typing or continuous pointer tracking.
- Model usage and host identity were recorded. Dollar cost was not measured.

This one run is an integration result, not a model ranking. The previous Sol
cohort used a different host and runner fingerprint; its 16.5-second median is
historical context, not a controlled head-to-head result.

## Transport and cleanup

The initial probe intentionally refused its tool callback because the SDK emitted
no `system/init` inventory event. Source inspection confirmed that the local SDK
does not emit one. Isolation evidence now explicitly reports the documented SDK
allowlist plus pinned-source audit, with inventory observation false. It does not
claim an OS sandbox or disabled local executor implementations.

A following synthetic nonce round trip passed. After the native pilot, callback
session validation, MCP target validation, cancellation checks, terminal EOF/exit
checks, response milestone timing, and owned transcript storage were tightened.
The final Grok 4.7 synthetic wrapper test also passed:
`e2e/artifacts/cursor-host-smoke-20261003T211001.093523Z/result.json`.
The native pilot predates those final guard changes; no second desktop run is
claimed for the final revision.

The SDK writes a second transcript outside its configured custom store by default.
Trials now set `CURSOR_DATA_DIR` to a sibling inside the parent-owned temporary
directory, so process termination is followed by removal of all owned storage.
Three project namespaces left by the initial probes/pilot were removed explicitly;
evidence is in `build/cursor-probe-transcript-cleanup.json`.

The live account catalog also exposes Grok 4.5 Fast using `effort` plus `fast`,
with low, medium, and high available. It has no context parameter. The adapter
preserves this distinct parameter shape and rejects ambiguous effort aliases.
Grok 4.5 Medium Fast passed the final synthetic callback/nonce test at
`e2e/artifacts/cursor-grok45-smoke-20261003T211145.866815Z/result.json`. This verifies
model selection and the tool connection; it is not a native task timing result.
Neither final smoke test left a trial namespace in the normal Cursor project
directory, confirming the owned `CURSOR_DATA_DIR` behavior on the live SDK.

Final offline verification passed 246 Python benchmark tests and eight Node
bridge tests, including embedded resource preservation and SDK lifecycle checks.

See `e2e/cursor_host/README.md` for setup, commands, isolation limits, and comparison
rules. Independent task correctness and latency remain separate from claims about
general reasoning quality.
