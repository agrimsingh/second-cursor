# Text batching and workflow recovery verification

Previous work was verified clean and pushed before implementation: local `main` and
GitHub `main` both identified `7f9b9d51f12aeadeb121bce3283279cac171303a`. This work
was developed on `codex/text-batching-and-workflow-recovery`.

The native candidate and installed app have executable SHA-256
`6de59480fde5ad6cf670de8d767bf1344d18e46c930380f24fbda8ac3893e76e`. The release build
and deep strict signature verification passed. The installed app's doctor check
confirmed Accessibility, Screen Recording, extension connectivity, and the new
`select_text` batch schema. The previous installed bundle was preserved under
`build/installed-backups/20261003T202303Z-workflow/SecondCursor.app`.

Raw artifacts below are local and Git-ignored. The acceptance documents and test
sources are tracked; the artifact paths are evidence references for this checkout,
not files published on GitHub.

## Native selection and existing editing

All nine new native cases passed across the initial run and the corrected control
test. They exercise the public MCP dispatcher against disposable apps, including
the deliberately faulty AX editor that acknowledges a selection without applying
it. That case confirmed that an unverified selection stops the batch before typing.

| Coverage | Result | Local evidence |
| --- | --- | --- |
| Schema, Unicode, caret insertion, upfront validation, ambiguous/missing matches, overlapping matches, stale targets, unconfirmed AX selection | 8 passed | `e2e/artifacts/20261004-041742/report.md` |
| Pause, takeover, cancellation during a queued batch | Passed | `e2e/artifacts/20261004-041952/report.md` |
| Existing native rich editing | Passed | `e2e/artifacts/20261004-042003/report.md` |
| Existing native semantic editing | Passed | `e2e/artifacts/20261004-042008/report.md` |

The initial control test failed when its inspection used a snapshot correctly
invalidated by pause. The test now obtains a new observation before inspecting the
document. Its corrected run checks the actual control refusal, skipped input,
unchanged text/selection, and drained in-flight accounting. No production change
was needed for that test correction.

The focus probes around native verification recorded zero activation, key-window,
or first-responder losses. Evidence:
`e2e/artifacts/workflow-probe-20261003T201741.023615Z/result.json` and
`e2e/artifacts/workflow-probe-20261003T201951.206380Z/result.json`.

## Actual model execution

`gpt-6.1-sol` at `high` reasoning completed Calculator → TextEdit, preserving the
original note and appending exactly `\nTotal: 42`. It independently chose
`select_text` plus `type_text` in a single batch with an exact final expectation.

| Same fixed task and model configuration | Previous build | New build |
| --- | ---: | ---: |
| Strict task result | PASS | PASS |
| Tool calls | 5 | 4 |
| Harness-measured adapter wall time | 22.271 s | 21.726 s |
| Adapter-reported tool intervals | 1.335 s | 1.230 s |

The runner, fixture, and task fingerprints match. This is one sample per build:
it demonstrates one fewer model tool call, not a statistically established latency
improvement. Time outside tools includes model, host, and orchestration work; it is
not a measurement of pure inference time.

Previous task evidence:
`e2e/artifacts/best-in-class-tasks-20261003T195922.725369Z/tasks.json`.
New task and model trace:
`e2e/artifacts/best-in-class-tasks-20261003T201822.415993Z/tasks.json` and
`trials/native_notes-1/model-trace.jsonl` beneath that directory.
The attended wrapper also recorded zero focus-counter changes:
`e2e/artifacts/native-model-attended-20261003T201821.291852Z/result.json`.

## Interrupted invoice workflow

The live deterministic adapter used real Second Cursor browser tools against 20
synthetic invoices. After the first of three target invoices was durably submitted,
a proxy withheld the runtime's completed batch response and the harness killed the
SDK process with SIGKILL. A different process resumed from the private checkpoint,
reconciled the original operation in the original session, obtained a full current
snapshot, and completed the remaining two invoices.

The independent ledger confirmed exactly three submission attempts, three accepted
entries with correct net/tax/gross amounts and accounts, and all 17 unrelated
invoices unchanged. The transcript also confirmed only one dispatch of the saved
operation, ordering by request-start/completion timestamps, and a completed final
checkpoint with no pending operation or queued calls.

The first run's task assertions passed but its cursor invariant was inconclusive
because of observed user mouse movement:
`e2e/artifacts/20261004-041854/report.md`. The quiet repeat passed all checks in
6.2 seconds: `e2e/artifacts/20261004-042011/report.md`. Its surrounding focus probe
recorded zero focus losses.

After the final SDK target-discovery review, the complete invoice scenario was run
again from the installed bundle with the finished SDK. All checks passed in
6.3 seconds: `e2e/artifacts/20261004-042358/report.md`. The wrapper at
`e2e/artifacts/workflow-probe-20261003T202357.032805Z/result.json` again recorded zero
focus-counter changes. Recovery coverage includes native targets in nested batch
arguments and targets resolved by the runtime after an interrupted call; browser
batches retain their actual top-level tab identity.

This proves SDK-process recovery while the runtime still holds the completed
operation result. A runtime restart, expired journal result, unknown outcome, or
paused/interrupted task stops for host recovery; it is never treated as permission
to resubmit. The invoice adapter is deterministic and does not measure model
reasoning quality.

## Offline and cleanup checks

- Existing benchmark harness: 144 tests passed.
- Invoice fixture and response gate: 8 tests passed.
- Existing SDK HTTP/MCP transport regressions and all 20 durable workflow tests passed.
- Strict TypeScript package-consumer compilation passed with NodeNext resolution.
- `git diff --check` passed.
- All 37 owned SDK child, runtime, native fixture, browser, and focus-probe processes from the
  runs above were independently confirmed absent; see
  `build/workflow-verification-cleanup.json`.

The final native/SDK source fingerprints and installed executable identity are
recorded in `build/workflow-verification-manifest.json`. Installed diagnostics are
in `build/installed-workflow-doctor.json`.

These checks do not demonstrate simultaneous physical human typing, all apps or
macOS configurations, or competitive superiority over Cua. Cursor/Space endpoint
checks and continuous focus counters are distinct evidence; neither is presented
as continuous cursor/Space monitoring.
