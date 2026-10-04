# Model-driven cross-app verification — 2026-10-05

The connected AI host owns the model loop. Second Cursor provides computer-use tools over MCP, including read-only operation reconciliation after an uncertain write. The runtime has no dependency on the model provider. This verification uses an isolated Codex host forwarding its dynamic-tool callbacks through the candidate app's real MCP connection; it does not establish every AI application's native MCP configuration.

## Change and acceptance boundary

The public default, full and compact catalogs now advertise `operation_status`, its required `operation_id`, and a read-only annotation. Session creation, ownership and operation cancellation remain host-managed. This lets an ordinary model discover how to inspect an uncertain operation without a benchmark-only recovery schema.

The new goal-only harness combines the seeded invoice recovery fixture, an existing saved TextEdit document and two foreground canary editors. The model chooses how to gather context, handle stale browser data and missing save confirmation, recover from native task pause/resume, preserve an external note, reconcile a lost native-write acknowledgement and save the final summary. All model tools are projected from live MCP discovery and restricted to the disposable tab and document. Batch tools are excluded so each native mutation has an independent before/after receipt.

The independent judges read the browser ledger, native accessibility state, saved file and foreground event history. They require reconciliation before the next native mutation after acknowledgement loss. Host telemetry must account for every tool request and callback, and runtime/source fingerprints must remain unchanged. See [the acceptance contract](acceptance-cross-app-model.md).

## Development pilot, retained as a failure

Artifact: `e2e/artifacts/20261005-025135/` (seed 100, version 1).

The pilot completed in 172.410 seconds of model-host wall time, using 43 calls and 18.362 seconds summed across tool callbacks. All 43 calls were correlated with host lifecycle telemetry. The remaining time is not a measurement of model-only inference.

All eight browser checks passed. A late receiving update rejected the model's initial $395 decision; it read the new message, recalculated the correct $360 amount, selected Standard routing and saved the required `RCPT-8473` memo. The ledger records two submission attempts, one rejected conflict and one committed save, followed by persisted-state inspection after unavailable confirmation.

The native trace shows real recovery: after a task-resume refusal, the model checked operation status and reread the newly edited document. It selected after the external note and issued one content-changing insertion. When that acknowledgement was suppressed, it checked the matching operation journal, reread the document and then saved. The original and external notes survived, and live text matched disk. The strict final-state checks failed because the model omitted the required terminating period, despite claiming an exact summary in its final answer.

All eight planned disruption identifiers fired. The foreground canary recorded 973 synthetic insertions and 2,944 events, with no unexpected text or focus changes. The three tool errors were the two injected native faults and an unnecessary adapter rejection of `Cmd+End`.

Before freezing version 2, the adapter allowed bounded native document-navigation shortcuts, the prompt made its literal summary punctuation unambiguous, and negative judge tests closed a loophole that previously allowed an observation only after another uncertain mutation. No expected business values or recovery recipe were added to the goal. The pilot remains a failure outside the five-seed cohort.

## Frozen version 2 cohort

Command:

```bash
SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_model.py --takeover --seed 101 --rounds 5
```

Profile: `cross-app-model/2`, `gpt-6.1-sol`, high reasoning, `codex-cli 0.159.0-alpha.12.1`, 100 calls and 420 seconds per trial. Each seed receives a fresh isolated model conversation, with shell, filesystem, search and other tool routes disabled. The visible fixture uses Chrome for Testing 152.0.7977.75. Artifact: `e2e/artifacts/20261005-025611/`.

All five frozen trials passed, with 175 independent outcome checks and 256 total scenario checks. The retained pilot is not part of this denominator.

| Seed | Result | Model-host wall time | Tool calls | Callback time | Injected tool errors |
| --- | --- | ---: | ---: | ---: | ---: |
| 101 | PASS | 179.095 s | 41 | 18.407 s | 2 |
| 102 | PASS | 176.144 s | 42 | 19.372 s | 2 |
| 103 | PASS | 174.532 s | 41 | 17.805 s | 2 |
| 104 | PASS | 185.459 s | 39 | 18.437 s | 2 |
| 105 | PASS | 160.179 s | 39 | 17.856 s | 2 |

Median model-host wall time was 176.144 seconds. The 202 model calls had complete lifecycle/callback correlation, with no unmatched or unknown host outcomes. All ten tool errors were the deliberately injected native freshness refusal and acknowledgement loss; browser stale revisions and unavailable save confirmation were additional application-level events, not counted as MCP tool errors.

Every trial produced the exact browser ledger and saved document, preserved the original and external notes and unrelated browser records, committed the invoice once, and reconciled the uncertain native write before its next native mutation. The model discovered and called the public `operation_status` tool in every trial. The foreground judges covered 4,921 synthetic insertions across 14,888 events, with no unexpected text or focus changes. Every planned disruption fired once, and the recorded source/runtime fingerprints were unchanged throughout the cohort.

All five canary processes, five scratch TextEdit processes, the isolated browser and the candidate runtime had exited after cleanup. Each scenario also passed its frontmost-app, active-Space and cursor invariants. The tested candidate binary SHA-256 was `a5cbc3dbdc32f44febbbf0edc840ab2ca60dfb6a232cacfd65efd56da5d50e83`; the profile and per-trial files retain the remaining source hashes and model configuration.

## Other verification and known limits

- All 370 benchmark Python tests and 61 root fixture Python tests passed, including 23 scoped-engine tests, 27 model-support negative controls and seven public-catalog tests. The candidate build, Python compilation and whitespace checks passed.
- Standard MCP discovery and real process-restart operation reconciliation passed in `e2e/artifacts/20261005-024840/`.
- The live default and compact surface scenarios passed in `e2e/artifacts/20261005-025535/`. The host-parameter scenario passed its ownership, recovery-schema, retained-request, stale-observation and expired-deadline checks, but failed the existing 42,000-character catalog budget. The catalog is 46,819 characters; the committed baseline was already 46,292. Exposing recovery adds 527 characters. The budget has not been raised, and this is not a fully passing release gate.

Synthetic in-process foreground activity does not establish physical human-input detection. Native mutation receipts observe boundaries rather than continuous TextEdit history. The lost acknowledgement is injected after a successful dispatch; real transport/process restart recovery is a separate test. These results cover an ordinary desktop, one browser workflow and TextEdit, not arbitrary apps or full-screen Spaces. The recovery fixture's late adjustment always ends below its approval threshold, so the final routing branch is Standard by construction; this cohort does not cover every policy branch. Five trials cannot establish universal reliability or rank model configurations. Provider cost and model-only inference latency are unmeasured. The candidate app was tested without replacing the installed package.
