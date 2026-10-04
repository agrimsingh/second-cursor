# Model-driven cross-app acceptance

The host owns model decisions. Second Cursor supplies public MCP tools, session ownership, task controls and operation reconciliation without a model-provider dependency. This track tests that boundary using an isolated Codex model host whose dynamic-tool callbacks forward through the real Second Cursor MCP connection. It does not prove every AI application's native MCP configuration or require MCP sampling.

## Frozen run contract

```bash
SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_model.py --takeover --seed 100 --rounds 1 # development pilot
SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_model.py --takeover --seed 101 --rounds 5 # frozen cohort
```

Defaults are `gpt-6.1-sol`, high reasoning, 100 tool calls and 420 seconds per trial. Each trial starts a fresh conversation with other tool routes disabled, one isolated browser fixture and one existing scratch TextEdit document. The model receives the business goal, exact summary format and permitted targets. It must discover the values and choose its own actions and recovery strategy. No expected business answer, independent judge state or scripted solver is supplied to it.

The adapter derives its schemas from the running server's `tools/list`, restricts them to those two targets, and preserves MCP text, images and errors. It supplies host-owned operation IDs for mutations. Native input is limited to observed text-area handles and bounded keyboard actions, including saving the existing scratch document. Browser calls are limited to the visible local fixture. Batch tools are excluded because native inner steps would bypass the independent mutation hooks in this track.

## Required disruptions and outcome checks

- The browser fixture forces a stale revision and a committed save with unavailable confirmation. The independent durable ledger checks the intended invoice, all unrelated records and duplicate submissions.
- Before the first native mutation, the harness pauses the real runtime task, appends an external note through an independent AX helper, and resumes the task. The pending old plan must be refused; the model must obtain a fresh observation before continuing.
- The harness suppresses the acknowledgement of the first successful content change. The model sees an unknown outcome and the real operation ID. It must inspect the operation or document before continuing, preserve the external note, and finish with one exact summary saved on disk.
- Three seeded foreground-window switches occur at early tool boundaries, with periodic synthetic typing throughout the run in two independent canary editors. Their complete event history, focus counters and content must stay consistent with only the commanded activity. The judge requires typing activity to overlap both browser and native work; it does not require window switches during both phases.
- Each permitted native mutation has independent AX before/after receipts. Every observed state must preserve the original and external notes; the model may revise its own appended draft. A known guard refusal must have no effect. Other failed or uncertain calls retain their effects as evidence rather than being assumed harmless.

Success requires all independent checks, every planned disruption exactly once, a completed isolated model turn, complete tool lifecycle correlation, no unknown or unmatched callbacks, and unchanged source/runtime fingerprints. A final model answer alone never passes the run. Missing evidence or setup faults remain failures in the report.

## Evidence and limits

Each artifact includes the goal prompt, scoped public catalog, model and host identity, source/runtime hashes, fault schedule, raw MCP replies, model-visible replies, host lifecycle telemetry, independent native mutation receipts, browser ledger, saved document and canary event history. Tool time includes local dispatch and harness hooks; the host's total time includes model reasoning and orchestration. Model-only inference latency and provider cost are not established.

Keep the pilot outside the frozen five-seed cohort. Retain failed attempts; changing sources after a failed cohort creates a new cohort and must not erase the original denominator. Five passing synthetic trials would establish bounded end-to-end recovery evidence, not a universal reliability or model ranking claim.

Synthetic typing does not establish physical human-input detection. Local response suppression does not establish process or transport crash recovery; that is exercised separately by `e2e/transport_recovery.py`. Native receipts observe mutation boundaries, not continuous TextEdit history. This track covers a headed browser and TextEdit on an ordinary desktop, not arbitrary apps, full-screen Spaces or protected surfaces.
