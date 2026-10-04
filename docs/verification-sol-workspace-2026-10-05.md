# Sol workspace comparison — 2026-10-05

Sol 6.1 medium is the provisional choice from this workspace comparison. Medium and high each passed all six scheduled trials, and medium was faster in every matched task/seed pair. Higher effort demonstrated no outcome or failed-call advantage in this cohort. Two variants per task support that operating choice, not a general reliability estimate or an overall model ranking.

## Matched setup

Both configurations used Second Cursor, the same candidate app and extension, Codex CLI `0.159.0-alpha.12.1`, and headed Chrome for Testing `152.0.7977.75`. Each trial had a fresh host, browser, and synthetic workspace, with on-demand observations, the full scoped tool catalog, a 100-call budget, and a 300-second solver deadline. Only reasoning effort differed between the two configurations.

The three workflows were invoice reconciliation, support handoff, and recovery from a stale invoice followed by a lost save response. Seeds changed business records and display order, not provider sampling. Seed 41 ran medium then high; seed 42 ran high then medium. There were 12 scheduled trials and no replacement runs, exclusions, or missing results. Previous Opus results are outside this matched cohort.

## Outcomes and timings

| Workflow | Seed | Medium wall seconds | High wall seconds | Medium / high calls | Medium / high failed calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| Reconcile | 41 | 116.188 | 211.105 | 22 / 22 | 1 / 1 |
| Reconcile | 42 | 73.588 | 109.437 | 17 / 22 | 1 / 1 |
| Handoff | 41 | 126.500 | 132.444 | 28 / 26 | 3 / 1 |
| Handoff | 42 | 131.426 | 140.645 | 25 / 29 | 1 / 3 |
| Recovery | 41 | 159.789 | 163.512 | 30 / 31 | 1 / 1 |
| Recovery | 42 | 148.880 | 201.421 | 31 / 34 | 3 / 3 |

Every row passed both the independent saved-state judge and all execution checks. Each reconciliation made one correct commit, each handoff made two correct commits to distinct requested tickets, and each recovery encountered a stale refusal before one correct commit and a persisted invoice reread after the lost response. No duplicate submissions or unrelated-record edits occurred. Adapter/supervisor cleanup errors were empty, and every fixture browser's termination was verified.

| Workflow | Medium median seconds | High median seconds |
| --- | ---: | ---: |
| Reconcile | 94.888 | 160.271 |
| Handoff | 128.963 | 136.544 |
| Recovery | 154.335 | 182.466 |

Each median has only two samples; the individual times above preserve the spread. Wall time is harness-measured adapter process time, including binding, model/tool work, final response, and adapter cleanup. Harness browser startup/termination and later independent judging are outside that clock. Workspace-specific verified-completion timestamps, provider-only inference time, and dollar costs are unavailable. These times are not pure driver latency.

Medium made 153 model tool calls and high made 164. All 317 raw requests match their raw results and normalized call records; no request IDs were missing or duplicated. Both recovered from ten failed calls, so successful outcomes do not imply error-free execution. Counts include failed calls, and a batch is one model call rather than its individual input actions. The fixture's intentional stale-save and lost-response events are application outcomes, separate from these failed tool calls. All 120 independent predicates passed, and same-seed manifests and baseline data matched across efforts.

## Product finding retained in the evidence

Medium's first-seed handoff exposed a valid role locator failing: the snapshot identifies `<section aria-label="Record details">` as a `region`, but the flat `browser_command` locator reports no match for that role and name. The flat role map in `extension/extended.js` omits the named-section mapping present in its chained `getByRole` path. This is a product inconsistency, not a model argument error. The model recovered and completed both tickets.

The final audit found four valid region queries rejected by this bug, two per profile. The remaining sixteen errors were nine unsupported `inspect(ref)` calls, four dropdown selections using `label` where `value` was required, and three attempts to use the HTML tag `section` as an ARIA role. Every failure was returned as an error and recovered safely; none was hidden as a successful tool response.

The same role-map split affects other implicit roles, including several landmarks, heading levels, and multiple selects. Sharing role resolution while retaining iframe handling is a focused follow-up; name-precedence differences are a separate issue. The candidate stayed unchanged throughout this comparison, and this role inconsistency is not fixed by the benchmark-default changes. The raw traces retain the exact requests and errors.

## Benchmark defaults and regression coverage

`model_benchmark.py` and `model_sweep.py` now default to the three seeded workspace tasks with 100-call/300-second budgets. The default sweep's five model profiles are unchanged; explicit `--profile` flags select a smaller cohort. The earlier native editing, form, canvas, and dialog cases remain explicit regressions. The lower-level scripted runner and the dedicated native efficiency experiment retain their existing task choices.

All 323 offline Python harness tests passed before the timed cohort, including default selection, explicit legacy selection, budgets, scheduling, and incomplete-result accounting. The log is `build/workspace-sol-python-tests.log`. No source/runtime edits or concurrent test jobs ran during the timed cohort; documentation-only edits did not change runner identity.

## Reproduction and evidence

```sh
SC_E2E_APP=build/SecondCursor.app python3 e2e/model_sweep.py \
  --profile sol-6.1-medium --profile sol-6.1-high \
  --rounds 2 --seed 41 --max-steps 100 --timeout 300
```

Add `--list` to preview the plan without using a model or launching a browser. The recorded plan resolves the default tasks explicitly in each child command.

- [Cohort report](../e2e/artifacts/model-sweep-20261004T164054.819183Z/report.md), [results](../e2e/artifacts/model-sweep-20261004T164054.819183Z/results.json), and [pinned plan](../e2e/artifacts/model-sweep-20261004T164054.819183Z/plan.json).
- [Medium, seed 41](../e2e/artifacts/best-in-class-tasks-20261004T164054.913851Z/tasks.json) and [medium, seed 42](../e2e/artifacts/best-in-class-tasks-20261004T170356.145916Z/tasks.json).
- [High, seed 41](../e2e/artifacts/best-in-class-tasks-20261004T164744.719996Z/tasks.json) and [high, seed 42](../e2e/artifacts/best-in-class-tasks-20261004T165618.307149Z/tasks.json).
- [Runtime hashes before](../e2e/artifacts/model-sweep-20261004T164054.819183Z/runtime-before.json) and [after](../e2e/artifacts/model-sweep-20261004T164054.819183Z/runtime-after.json) are identical. They separately pin the native host helper, which is not covered by the main executable's hash.

Artifacts are retained locally and ignored by Git. The base commit was `5da8dcd` plus the recorded benchmark-default changes. The runner SHA-256 was `fbe286b439011ba293435661f01efd942e30154a4c2bbbafdf82030f034be2e7`; app, extension, fixture, task, browser, host, and case identities remain in the linked evidence. The tested candidate was `build/SecondCursor.app`; this work did not replace the installed app.

This is a small synthetic browser-workflow comparison. It does not establish reliability across arbitrary sites, native-app handoffs, or concurrent human use. Coexistence checks passed at task endpoints and cannot rule out transient interference. The lost response is deliberately simulated by the application; process-crash recovery is a separate test.
