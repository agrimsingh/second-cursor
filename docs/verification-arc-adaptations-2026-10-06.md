# Arc-inspired adaptations — 2026-10-06

This record separates development pilots, frozen comparisons, and the behavior each actually establishes. The baseline is Second Cursor `b0a4c22`; the upstream reference is [Arc `6ca19d62c95106732fad28f488ecd458c08e02f4`](https://github.com/shhivv/arc-cua/tree/6ca19d62c95106732fad28f488ecd458c08e02f4). Raw evidence under `build/` and `e2e/artifacts/` is local and ignored by Git.

## Development evidence retained

- Baseline browser regression: `build/arc-eval/baseline/e2e/artifacts/20261006-135403/`. Existing checkbox and delayed-search scenarios passed.
- Baseline cross-app model pilot: `build/arc-eval/baseline/e2e/artifacts/20261006-135431/`, seed 201. Correct final browser/native state, operation reconciliation and foreground canaries passed. This development run is outside the final latency cohort.
- First settling pilot: `e2e/artifacts/20261006-135912/`. Baseline returned early on four-second fetches, the delayed fetch, and the 1.5-second timer. Three native cases failed fixture placement before an action. The fixture had fallen back to the main display before the virtual display appeared; it now requires the exact display and independently confirmed window placement before declaring itself ready. The failures remain in this artifact.
- First SDK model pilot: `e2e/artifacts/20261006-140023/`. Both baseline and optional constrained SDK modes passed seed 201, each using 17 model tool callbacks and 27 SDK calls with no errors. These two successful development runs do not establish a speed advantage.
- First candidate settling pilot: `e2e/artifacts/20261006-140239/`. All six native returned screenshots contained the expected custom-drawn change. Continuous caret animation exhausted the visual probe budget and correctly reported `timed_out`. Browser fetch effects were present, but unread response bodies caused unnecessary ten-second waits and the delayed-fetch case failed its latency bound.
- Upstream Arc smoke: `e2e/artifacts/arc-reference-20261006T060449.583024Z/`. Arc also waited ten seconds for the unread-body fixture and returned before the 800ms dialog appeared. Its long-poll setup failed because our reference harness omitted Arc's required action guard; the harness now constructs setup actions through upstream `materialize_action`. No action retry or upstream runtime patch was added.
- Independent request lifecycle diagnostic: `e2e/artifacts/20261006-140533/`. With an unread Fetch response, Chrome emitted headers and all 11 body bytes but no `Network.loadingFinished`; the UI became ready at 4.246 seconds and the call returned at 10.046 seconds. Consumed Fetch and XHR emitted completion and returned at 4.559 and 4.528 seconds. This established the cause before changing the implementation or fixture.

## Comparison boundaries

Settling probes compare one dispatched action and its returned observation with independent effect evidence. Browser effect timing comes from sampled DOM layout/CSS visibility, not physical display presentation. Native timing comes from the fixture's draw receipt; returned screenshots receive a separate pixel check. Readiness misses remain misses even when an independent observer eventually sees the effect. An intentionally unfinished request is judged on boundedness and truthful readiness metadata, not on producing an effect.

The Arc reference uses its unchanged Python browser backend and execution loop, while Second Cursor uses public MCP, native bridge and extension transport. Both use the same visible Chrome for Testing build and fixture code. This narrow reference cannot establish overall driver superiority or native coexistence.

The SDK experiment uses one messy invoice workflow: a real Sol 6.1 high conversation gathers context and supplies its own literal form values, then the actual SDK runs either raw calls or constrained choices. Both arms use the same candidate app/extension and equivalent observation work. This isolates optional execution policy; independent ledger checks determine success. The separate cross-app model suite tests stale revisions, uncertain writes, native interruption, exact saved text and synthetic foreground activity.

## Final measurements

### Returned observations

The frozen baseline run is `e2e/artifacts/20261006-140814/`; the frozen candidate is `e2e/artifacts/20261006-141205/`. Both used `arc-settling/2`, identical fixture/observer hashes and three repetitions. All before/after manifests matched. The candidate binary SHA-256 is `3eae5c470f201e180d026638514336cec590252312bdb9d79c3310d2f3e1dd77`.

| Measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Expected browser effect present in returned observation | 15/36 | 33/36 |
| Expected native effect present in returned screenshot | 3/18 | 18/18 |
| Immediate browser action, median | 525.30 ms | 532.18 ms |
| Four-second request, returned observation | Early/incomplete, about 0.53–1.27 s | Complete, about 4.53–4.56 s |

The three remaining candidate browser misses are the blind 1.5-second timer. Its response says `quiet` with zero pending requests before the later dialog appears. Quietness therefore remains a bounded readiness heuristic; callers must verify the intended postcondition. The unfinished requests correctly report `timed_out`, one pending request and no ready effect, including all three screenshot-only replies. Native continuous caret animation reaches the probe deadline while still returning the expected completed drawing.

The baseline's 60 characterization checks passed; this does not turn its missing returned effects into readiness successes. The candidate has 59 passing checks and one failure: the first native case changed the foreground app from Chrome for Testing to UserNotificationCenter. The fixture's placement, single dispatch, effect, screenshot, pointer and Space checks passed. Narrow `replayd` evidence in `build/arc-eval/screen-capture-reminder.txt` shows macOS's screen-capture acknowledgement alert at that instant. The failure is retained rather than relabeled as a clean coexistence run.

The fresh native confirmation, `e2e/artifacts/20261006-142013/`, passed all 18 cases with complete returned screenshots and unchanged foreground, Space and cursor. Its full manifest matches the original candidate cohort. The first case started on an ordinary desktop with System Settings frontmost. No OS alert was dismissed by the benchmark; the previous test process had exited before this fresh run.

### Pinned upstream Arc browser reference

`e2e/artifacts/arc-reference-20261006T061652.882051Z/` completed three repetitions with 21/36 expected effects present in returned observations. There were no execution/observer errors or failed fixture checks; the three screenshot-only cases are explicitly unsupported and excluded from that denominator. The visible browser build, viewport (1200×762, DPR 2), fixtures and independent observer match the Second Cursor comparison. All four loaded Arc source hashes still match the manifest.

| Shared case, median | Arc | Second Cursor candidate |
| --- | ---: | ---: |
| Immediate effect | 0.207 s | 0.532 s |
| Consumed four-second Fetch | 4.205 s | About 4.55 s |
| Streamed four-second response | 4.211 s | About 4.55 s |
| Unconsumed four-second Fetch | 10.027 s | About 4.55 s |
| Complete returned browser observations | 21/36 | 33/36 |

Arc missed the 300/800/1500 ms dialogs, delayed request and blinking badge in every repetition. This supports retaining Second Cursor's reaction handling while adding request tracking, rather than adopting Arc's immediate-return timing wholesale. The direct Python API versus MCP/extension transport difference prevents attributing the latency delta solely to settling. Per-case medians, manifest checks and unsupported cases are retained in `build/arc-eval/settling-comparison-audit.json`.

### Real model workflows

The cross-app comparison used fresh Sol 6.1 high conversations in baseline/candidate then candidate/baseline order. Both versions passed both seeds. Each of the four trials passed all 35 independent checks, exercised all eight scheduled disruptions, made 40 model tool calls and encountered exactly the two injected errors. Browser ledger state, exact saved native text and foreground canaries all passed; telemetry was complete and source/runtime fingerprints remained unchanged.

| Seed | Baseline host time | Candidate host time | Baseline artifact | Candidate artifact |
| --- | ---: | ---: | --- | --- |
| 301 | 150.900 s | 133.343 s | `build/arc-eval/baseline/e2e/artifacts/20261006-142112/` | `e2e/artifacts/20261006-142354/` |
| 302 | 125.611 s | 158.589 s | `build/arc-eval/baseline/e2e/artifacts/20261006-142907/` | `e2e/artifacts/20261006-142618/` |

Median host times were 138.255 seconds for the baseline and 145.966 seconds for the candidate. The paired differences point in opposite directions, so this supports preserved recovery, not a consistent end-to-end speed gain. Host time includes orchestration and tool callbacks; it is not pure model inference. The baseline ran its preserved harness and extension, and the candidate ran its frozen bundle and matching extension; all non-runtime harness sources matched. Compact independent audit: `build/arc-eval/cross-app-comparison-audit.json`.

The optional SDK comparison, `e2e/artifacts/20261006-143235/`, also passed all four trials with fresh Sol 6.1 high conversations and reversed arm order. Both arms used the same candidate app and extension; only the SDK mode changed. Every trial made 17 model callbacks and 27 SDK dispatches, with no errors, exactly one write and one commit, correct ledger state and unchanged unrelated records. Source profiles, foreground, Space and pointer remained unchanged. Both constrained trials completed six selected actions with independent verification returning true.

| Seed | Original SDK host time | Constrained SDK host time | Original execution phase | Constrained execution phase |
| --- | ---: | ---: | ---: | ---: |
| 301 | 59.777 s | 55.185 s | 23.630 s | 20.526 s |
| 302 | 55.376 s | 52.297 s | 22.349 s | 23.149 s |

Median host time fell from 57.577 to 53.741 seconds, but the execution phase was faster on one seed and slower on the other. Median SDK dispatch time was essentially unchanged (5.618 versus 5.604 seconds), and neither call count decreased. This is evidence that the bounded mode works with a real model, not proof that it accelerates reasoning. No recovery faults were injected in this form track; the generic judge's conditional recovery flags are not counted as recovery coverage. Compact independent audit: `build/arc-eval/sdk-model-comparison-audit.json`.

## Retained behavior and limits

- Browser actions now track requests that start after input and wait for their completion plus a quiet document. Immediate-call cost stayed essentially unchanged in this cohort, so this is enabled on the existing action path. Metadata distinguishes unavailable coverage and deadline expiry; neither means the business operation succeeded.
- An unread Fetch body can complete through observed byte accounting only when an uncached, uncompressed, non-redirected response supplies a valid Content-Length and exactly that many bytes arrive. Headers alone and partial streams cannot release the wait. Compressed, unknown-length, cached and redirected unread responses can still reach the deadline if Chrome omits its completion event.
- Native visual settling stays opt-in. It caught all tested delayed custom drawing, but even the immediate case rose from a median 359.11 ms to 1723.35 ms. Ordinary AX reads retain their existing behavior. In-flight macOS capture and preceding AX work can extend the probe's own 2.2-second budget.
- Bounded SDK subtasks stay opt-in. The host supplies fresh permitted choices, exact inputs and an independent completion verifier; the connected AI client still owns the model. Action and no-progress bounds survive durable resume, and uncertain writes require reconciliation. These are action-count bounds; hosts still need model/callback cancellation and deadlines.

These fixtures establish behavior on specific delayed effects and seeded workflows, not universal macOS/app compatibility. Browser DOM visibility is not compositor presentation, and synthetic foreground canaries are not physical human input. The two-seed model comparisons are exploratory regression evidence, not a model ranking or a broad reliability estimate.

## Validation

The signed debug app built successfully. Focused validation passed 188 extension tests, 45 SDK tests plus HTTP/MCP recovery smoke and a TypeScript consumer check, and 370 benchmark Python tests. Root Python discovery passed 80 tests with one optional Arc dependency check skipped; all six reference tests, including that optional integration check, passed in the isolated pinned Arc environment. Native policy tests exercise delayed reactions, tiny/color changes, accumulated drift, blinking, continuous animation and deadlines. `git diff --check` passed.

The existing live browser regression suite passed all seven scenarios in `e2e/artifacts/20261006-143154/`: checkbox settling, delayed search, truthful outcomes, REPL uncertainty, freshness guards, live locator sequences and trusted hover.

The full release gate is not claimed here. The existing public catalog size budget was already exceeded by the baseline; that unrelated budget remains outside this adaptation experiment. The installed application has not been replaced by the candidate.

Follow-up: this exact candidate was subsequently pushed to main and installed. Its installation check and the next browser-postcondition comparison are recorded in [the browser-expect verification](verification-browser-expect-2026-10-06.md).
