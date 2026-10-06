# Browser postconditions — 2026-10-06–07

This record separates fixture development, frozen comparisons, model trials and installed verification. Baseline Second Cursor is `a6b6990`; upstream Arc is unchanged at [`6ca19d62c95106732fad28f488ecd458c08e02f4`](https://github.com/shhivv/arc-cua/tree/6ca19d62c95106732fad28f488ecd458c08e02f4). Raw evidence under `build/` and `e2e/artifacts/` is local and ignored by Git.

## Contract

Browser `expect` verifies a public UI condition after one action or a tab batch. It distinguishes `verified`, `preexisting` and `failed`; a failed confirmation never repeats input. Text checks stay in the target tab, while exact-value checks remain bound to the original node and document. Missing privacy or frame coverage cannot establish that text is absent. Protected values are unavailable, and partial batches retain their step results without claiming a verified expectation. The independent fixture judge separately checks persisted business state, exact submissions and commits, and unchanged adjacent records.

The selected browser must advertise `browser_expect_v1` before dispatch. An older extension is refused before captions or input, even if another connected profile supports the feature. `observe: none` still performs the condition check. Observation work and an in-flight protocol read have separate bounds, so `expect.timeout_ms` is not the total tool-call deadline.

## Development failures retained

- Baseline pilot `e2e/artifacts/20261006-231931/` passed all three scenarios but failed during final manifest reporting after cleanup. The harness now saves the bundle identity before cleanup; a regression test and fresh confirmation `20261006-232459` passed. The original pilot is not counted as a clean cohort.
- First candidate `e2e/artifacts/20261006-232224/` failed 19 of 20 scenarios, usually refusing input because ordinary text-control accessibility nodes were absent from DOMSnapshot. The protected-ref case also revealed a weak test: a globally broken reader could satisfy its refusal criterion. It now requires a successful public-value check on the same page first.
- Actual Chrome protocol capture `e2e/artifacts/20261006-232356/` established that native text fields expose duplicate user-agent shadow nodes through AX. The repair excludes only plain internal content already represented by a known host value, preserves DOM-backed `aria-owns` descendants, and rejects distinct uncovered content. Explicit sparse DOM value facts support empty fields without treating a missing value as empty. Added regressions cover public suggestion lists, false absence, unknown descendants and privacy masking.
- Repaired candidate pilot `e2e/artifacts/20261006-233040/` passed 19 of 20 scenarios. The remaining HTML-dialog case opened the correct review UI but returned an unavailable expectation instead of confirming it. Its failed result and unchanged saved record remain in the artifact.
- Modal protocol capture `e2e/artifacts/20261006-233200/` identified empty, ignored AX placeholders with Chrome's `activeModalDialog` reason. The repair accepts only childless, nameless/value-less `role:none` leaves explicitly tied to a DOM-covered active dialog. Visible, content-bearing, descendant-bearing and unbound missing nodes remain failures. Actual-protocol replay now includes the review heading, with negative regressions protecting the exception's bounds.
- First model cohort `e2e/artifacts/20261006-233548/` exposed another coverage gap on the actual invoice form. Poll seed 401 passed in 159.642 seconds with 31 calls; expect seed 401 finished the business task in 141.252 seconds with 35 calls, but used fallback plain input after the feature became unavailable. Its six expectations produced zero verified, one preexisting, three failed and two pre-input refusals. Both ledgers independently proved the stale conflict, lost response, single commit and persisted readback. Root stopped the cohort after these two trials to fix the issue; expect seed 402 was interrupted and poll seed 402 did not run. This is development recovery evidence, not a valid performance comparison or successful feature demonstration.
- Actual messy-workspace protocol capture `e2e/artifacts/20261006-234144/` identified an uncovered native `MenuListPopup` wrapper below the DOM-backed Decision `SELECT`; its four options were already DOM-backed. The empty amount and textarea values were covered. The repair admits only the empty structural wrapper and continues checking every option. Independent review confirmed public options remain visible and a protected SELECT still masks its value and descendants.
- The dedicated actual-workspace regression correctly failed against the preserved pre-repair bundle in `e2e/artifacts/20261006-234705/`, at the same invoice-opening expectation. The earlier 28-case cohort `20261006-233410` remains a valid narrow test of that older bundle, but it did not cover this native SELECT shape.
- Second model cohort `e2e/artifacts/20261006-234800/` demonstrated six verified expectations on the repaired form, then exposed a separate unavailable condition when that form opened its review modal. Poll seed 401 passed in 132.933 seconds with 31 calls; expect seed 401 passed the task in 136.211 seconds with 35 calls, using six verified, one preexisting, two failed expectations and one pre-input refusal. Both raw ledgers passed. Root stopped this development cohort after the two completed trials; its partial third trial is interrupted and its fourth is unrun.
- Actual full-workspace modal capture `e2e/artifacts/20261006-235606/` showed that Chrome reparents an empty ignored SELECT wrapper under HTML while retaining four covered, empty, ignored option children. The existing inactive-modal exception admitted only leaves. The repair validates each child's exact parent, DOM coverage and shared visible-modal binding before accepting this container; twelve malformed variants remain rejected. Workspace regression v2 now covers form → review → dismissal → reload. It failed at the modal expectation on the previous bundle in `20261006-235908`, after a separate read proved the modal was visible, and passed on the repaired bundle in `20261006-235927`. Neither run saved any changes.

## Baseline and reference

The baseline cohort `e2e/artifacts/20261006-232726/` passed all 12 workflow scenarios across seeds 401–403 (800, 1,500 and 2,500 ms timers). All before/after source fingerprints matched. This is one trial at each delay for each workflow, not three repeats at a fixed delay.

The pinned Arc reference `e2e/artifacts/browser-expect-arc-20261006T152842.411160Z/` passed all 12 delayed-save and delayed-dialog trials: three repetitions at seeds 402 and 403. Arc executes each observed action once and explicitly polls its public observations; no upstream source is changed. Arc uses its direct Python backend while Second Cursor includes MCP and extension transport, so public call counts do not have identical costs. Stale-form, second-tab and strict `expect` contract cases are outside this reference policy. Timeout boundaries differ and must not be used as a matched timeout-success comparison.

## Final candidate and model results

The final signed debug candidate's binary SHA-256 is `8155086b71787184bdb9969cc18f2c53fbc45e26e6f9757da0b77828c28ab2ad`; the effective extension is an exact copy of that bundle's resources and the working-tree extension. `e2e/artifacts/20261007-000758/` passed all 28 scenarios: 12 workflows and 16 focused contract cases. All source fingerprints remained unchanged. See [the acceptance contract](acceptance-browser-expect.md) for the fixture and comparison boundaries.

An independent audit replayed every persisted ledger, recomputed state transitions and verified exact submissions, commits and adjacent-record preservation. Baseline passed 159 scenario checks and 36 coexistence checks; candidate passed 389 and 84 respectively. There were no skipped or inconclusive checks. These coexistence checks compare scenario endpoints; they do not establish the absence of transient foreground interference. Arc's 180 checks all passed, without a coexistence measurement.

The first actual-workspace form regression passed in `e2e/artifacts/20261006-234722/` on the preceding `eeeceeeb…43d28` bundle, but did not include the subsequent review modal. The expanded v2 regression passed in `e2e/artifacts/20261006-235927/` on the final candidate and extension `95d2df2dcf14169b04e245faa3dd819983fa0f614e01257496ea2a6e196523b1`: 27 checks plus three endpoint invariants. Its nine expectations returned six verified, two preexisting and one intentional timeout for the still-present unselected Hold option, with no unavailable checks or tool errors. Independent DOM reads confirmed exact drafts, selected option labels/values, the visible review modal, hidden modal after dismissal and preserved drafts; reload restored empty fields. The hash-validated persisted ledger contained three reads plus one paired read-only review/result, zero write attempts and zero commits, with every saved record equal to its baseline.

The timing medians below aggregate the same three seeds/delays in each SC arm. Call lists follow seeds 401, 402, 403; setup and independent final judging are excluded.

| Workflow | Poll calls | Expect calls | Poll median | Expect median |
| --- | --- | --- | ---: | ---: |
| Delayed save | 7, 12, 20 | 1, 1, 1 | 1.812 s | 1.815 s |
| Stale form and recovery | 16, 27, 43 | 5, 5, 5 | 4.397 s | 4.333 s |
| Review dialog and save | 13, 25, 41 | 3, 3, 3 | 3.798 s | 3.823 s |
| Other-tab decoy, expected timeout | 32, 31, 30 | 1, 1, 1 | 4.070 s | 3.735 s |

Public calls fell from 297 to 30 across all 12 workflows (89.9% fewer), or 204 to 27 across the nine successful-save workflows (86.8% fewer). Timing differences were small and mixed. Timeout boundaries differ, so the decoy delta is not evidence of faster successful task completion.

| Shared case | Arc calls over three repeats | Arc median | SC poll, one trial | SC expect, one trial |
| --- | --- | ---: | ---: | ---: |
| Save, 1.5 s timer | 16, 17, 17 | 1.665 s | 1.812 s | 1.815 s |
| Save, 2.5 s timer | 26, 26, 26 | 2.719 s | 2.772 s | 2.749 s |
| Modal, 1.5 s timers | 35, 34, 33 | 3.352 s | 3.798 s | 3.823 s |
| Modal, 2.5 s timers | 53, 52, 51 | 5.365 s | 5.897 s | 5.814 s |

Arc completed every supported shared case. The smaller SC interface-call count does not mean fewer low-level protocol reads or less CPU work; `expect` moves bounded polling into the extension. These small deterministic cohorts do not establish a broad performance ranking.

The fresh final Sol 6.1 high comparison passed all four trials in `e2e/artifacts/20261006-235954/` on the full-workspace repair (`8155086b…28ab2ad`). Both arms use the same repaired, frozen candidate app and extension; the candidate arm exposes optional `expect`. Fresh conversations alternate poll/expect then expect/poll across seeds 401 and 402. The model reads messy supporting records, derives the required invoice values and recovers from stale revisions and a lost save response; an independent durable ledger judges the result.

| Model arm | Seed | Verified task time | Public calls | Expect results |
| --- | ---: | ---: | ---: | --- |
| Poll | 401 | 116.146 s | 31 | Not exposed |
| Expect | 401 | 125.763 s | 31 | 8 verified, 3 preexisting, 1 timeout |
| Expect | 402 | 118.413 s | 31 | 7 verified, 3 preexisting, 2 timeouts |
| Poll | 402 | 101.806 s | 32 | Not exposed |

Every trial made two save attempts: the first encountered the scheduled stale revision, and the second committed exactly once despite the lost response. The model re-read persisted state instead of resubmitting after that lost response. Independent replay confirmed exact final values and untouched unrelated records, with complete tool telemetry and unchanged source/runtime identities. There were no MCP errors, unavailable checks or missing expectation results. All 76 scenario checks and 12 endpoint coexistence checks passed.

The three failed conditions were correct timeouts for model-guessed text absent from the page: “Message details” instead of “Record details,” “received” instead of “Receiving confirmed…,” and “Purchase orders” instead of “Orders.” The returned observations let the model recover. The expect arm was slower on both seeds and saved only one public call across the two trials; this cohort demonstrates working checks and recovery, not a model speed improvement. Verified task time includes isolated host startup, model reasoning, public dispatches and final durable judging. Four fresh conversations are too few for a general model or driver ranking.

## Automated regression checks

All 280 extension tests passed, including 92 browser-expect tests. The focused Python command ran 71 tests: 65 passed and six optional Arc cases were skipped in the ordinary Python environment. All 11 Arc-reference tests, including those six, passed in the pinned Arc environment. All nine workspace-regression unit tests and all 371 benchmark unit tests passed. Production Swift router and socket probes verify malformed-input refusal, partial-batch accounting and extension capability selection without GUI input. The signed app built and its code signature verified successfully.

The existing browser suites ran serially on the final candidate and all passed: 22 scenarios, 722 scenario checks and 66 endpoint coexistence checks, with no skipped or inconclusive cases.

| Existing suite | Scenarios | Checks | Local artifact |
| --- | ---: | ---: | --- |
| Browser reliability | 7 | 74 | `20261007-000935` |
| Browser batches | 4 | 25 | `20261007-001020` |
| Command contracts | 5 | 80 | `20261007-001042` |
| Label and option privacy | 1 | 363 | `20261007-001049` |
| Input target contracts | 4 | 44 | `20261007-001053` |
| Seeded roles and reference lifetimes | 1 | 136 | `20261007-001114` |

The property suite covered 40 generated HTML variants across two seeds, including replaced, removed and relabeled targets. These are targeted local checks; the full release gate and CI are not claimed.

## Installation

The previously tested Arc-adaptation bundle was installed before this phase and its authenticated browser self-test passed. Its binary SHA-256 is `3eae5c470f201e180d026638514336cec590252312bdb9d79c3310d2f3e1dd77`.

The final candidate was installed without rebuilding using `scripts/install-built.sh`. The installed binary at `~/Applications/SecondCursor.app` matches the tested `8155086b…28ab2ad` hash; its bundled extension and the installed extension files match the tested source. The previous app is preserved at `build/install-backups/install-Q3N6RlNr/previous.app`.

Installed `doctor` passed with both required permissions, an authenticated MCP connection and a connected browser. The installed browser self-test opened an owned disposable tab, clicked its button, independently checked the resulting page and closed the tab. Evidence is saved in `build/browser-expect-eval/candidate-installed-doctor.json` and `candidate-installed-self-test.json`.

The complete v2 workspace form/modal regression was also rerun from the installed application path with the installed extension files in a fresh disposable browser profile. It passed all 27 scenario checks and three endpoint invariants in `e2e/artifacts/20261007-001240/`. This confirms the installed bundle's feature path independently of the user's older loaded extension; it does not reload or modify that existing browser profile.

The user's running Chrome profile still advertises the older capability set without `browser_expect_v1`. The installed feature check therefore stopped before opening a test tab or sending input, as intended; `installed-expect-check.json` records that boundary. Reloading Second Cursor in Chrome's extensions settings is still required before the new feature can be verified in that existing profile. The user has been asked to reload; automated access to those settings is blocked by the browser tool's security policy.
