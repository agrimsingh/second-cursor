# Native model orchestration efficiency

The previous work was clean on `main` at `252c0b1adfac19752e352e89998633681043307e`
before this experiment. Implementation is on `codex/fewer-model-round-trips`.
The installed Second Cursor executable remains
`6de59480fde5ad6cf670de8d767bf1344d18e46c930380f24fbda8ac3893e76e`;
this change is in the model adapter and measurement harness, and requires no app rebuild.

Both treatments use `gpt-6.1-sol` at `high` reasoning through
`codex-cli 0.159.0-alpha.12.1`, fresh isolated model conversations, disposable
Calculator/TextEdit fixtures, and the same 40-call, 120-second budget. The baseline
lets the model request window observations. The upfront treatment reads both
authorized windows in the same scoped session before the model starts. Those
reads stay inside the adapter clock and consume two of the 40 allowed attempts.

The experiment alternates baseline/upfront and upfront/baseline order. Seeds
identify fixed-task pairs; they do not randomize fixtures or provider sampling.
Runner fingerprint:
`963dc52a1d4d41e01723ad967e197d88d8e007d05dc0723832d3d5ab5508110c`.
Task fingerprint:
`8ad3d12d73358e8f0d2d6cfbb7043c5424ba7fd3ab9cd57ae6627dcd5b2ca821`.
New orchestration and completion-analysis modules are included in the fingerprint.

## Smoke and setup interference

The initial baseline passed in 20.210 seconds. A Space change to full-screen Chrome
interrupted the next fixture setup before the optimized adapter ran. The experiment
preserves that pair as incomplete:
`e2e/artifacts/model-efficiency-20261003T204243.985446Z/report.md`.
Its focus probe recorded one activation/key-window loss:
`e2e/artifacts/workflow-probe-20261003T204242.793450Z/result.json`.

The fresh smoke pair passed both profiles, with zero focus-counter changes:

| Metric | Baseline | Upfront |
| --- | ---: | ---: |
| Full measured adapter process time | 21.774 s | 14.835 s |
| Tool-verified final edit, adapter clock | 17.894 s | 11.354 s |
| Post-verification adapter tail | 3.689 s | 3.299 s |
| Total tool time | 1.207 s | 1.150 s |
| Model-requested tool calls | 4 | 2 |
| Total driver attempts, including prefetch | 4 | 4 |
| Cumulative input tokens across requests | 67,406 | 43,910 |

Upfront reads cost 73.5 ms in this sample. These calls were moved before model
startup, not removed from the stopwatch. Token totals sum all requests; they are
not the size of one prompt and are not an invoiced-cost estimate.
Uncached input was effectively unchanged (15,694 versus 15,750 tokens), so the
lower cumulative input mostly reflects avoided cached reprocessing. First-request
input increased from 11,398 to 13,926 tokens because it included the observations.

Smoke evidence:
`e2e/artifacts/model-efficiency-20261003T204344.032928Z/report.md` and
`e2e/artifacts/workflow-probe-20261003T204342.900084Z/result.json`.

The first five-pair repetition is retained at
`e2e/artifacts/model-efficiency-20261003T204434.256409Z/report.md`. All six tasks
that reached a model completed correctly. Only three passed every coexistence
check; two baseline trials and one upfront trial were inconclusive after observed
cursor/Space changes. Four subsequent fixture setups could not run on the active
full-screen Space. The resulting full denominator is ten planned trials with
only one fully passing pair, so this is incomplete evidence, not a clean cohort.

An attempted off-Space pair is also retained at
`e2e/artifacts/model-efficiency-20261003T204754.306616Z/report.md`. Both fixture
preconditions failed before model execution because the required inactive-Space
state was not present. No off-Space performance claim follows from it.

## Compact tool experiment

The additional opt-in `catalog_mode=compact` advertises the complete native batch
tool and five observation tools. Standalone action definitions are represented
by typed batch steps; the projection proves argument coverage, retains their
behavioral instructions, and leaves the engine's full execution allowlist intact
for nested scope validation. Unknown schemas or unsupported coverage fail closed.
Responses, screenshots, freshness checks, and uncertainty handling are unchanged.

The actual installed catalog shrinks from 26,038 to 17,248 canonical UTF-8 bytes
(33.76%) and from fourteen to six advertised tools. These byte measurements are
not provider token counts. Raw schemas and hashes are recorded in
`build/native-tool-surface-measurement.json`.

The final two experiments hold the finished implementation fixed at runner
fingerprint `86d3b80f5519b41ec2f0d6d60f2c1517e8c009d4ea9d209c49ecdaa7ff47c35a`.
The observation experiment keeps the full catalog in both profiles. The tool
surface experiment keeps upfront observations in both profiles. This separates
the effects instead of combining both changes into one comparison.

## Final quiet-window results

All twelve trials in the two final, separately scheduled experiments passed both
the independent task judge and strict coexistence checks. Each comparison has
three alternating pairs, with every planned trial retained.

| Comparison / profile | Passed | Wall median / p95 | Verified edit median / p95 | Model calls / total attempts |
| --- | ---: | ---: | ---: | ---: |
| Observation baseline | 3/3 | 23.927 / 25.338 s | 20.009 / 20.993 s | 4 / 4 |
| Upfront observations | 3/3 | 16.452 / 16.609 s | 12.170 / 12.329 s | 2 / 4 |
| Tool surface: upfront + full | 3/3 | 16.576 / 17.252 s | 11.745 / 12.829 s | 2 / 4 |
| Tool surface: upfront + compact | 3/3 | 15.718 / 17.507 s | 12.003 / 13.233 s | 2 / 4 |

Upfront observations lowered the cohort median wall time by 31.24%. All three
matched pairs were faster: 5.633, 8.887, and 8.078 seconds saved. The median paired
saving is 8.078 seconds; it differs from subtracting the two profile medians.
Median upfront observation cost was 73.5 ms and remains included in every metric.
The upfront cohort still spends a median 4.108 seconds after the tool-verified edit
in final model response, host shutdown, and adapter cleanup.

Compact catalogs saved 2.091 and 1.534 seconds in two pairs, but lost 1.371 seconds
in the third. The median verified edit did not improve, and the slower pair still
used exactly two model calls with no retries. Its extra wait appeared before the
first action and between batches; actual tool time was slightly lower. Compact
remains an explicit opt-in experiment rather than a new default.

The scoped catalogs used in these trials were 26,094 versus 17,272 bytes (33.81%
smaller). Median cumulative input tokens fell from 43,914 to 39,771 (9.43%), and
median first-request input fell from 13,928 to 12,547. Cache hits varied: compact's
cohort had 55,329 uncached input tokens versus 47,262 for full. These results do not
establish realized cost savings.

Three samples make p95 the observed maximum, not a dependable tail estimate.
The inherited engine also iterates a set when ordering tool definitions, and
fixture window IDs are embedded in their schemas. Ordering varies across fresh
processes and may add prompt/cache noise; a follow-up should stabilize ordering
while preserving target constraints before making finer latency claims. The
current source was kept frozen throughout both experiments.

Final task fingerprint:
`b2c44b15bd23c60f45a491d080c9e2dd1e228526570108ec7614762a2fa72d53`.
Observation evidence:
`e2e/artifacts/model-efficiency-20261003T205045.384550Z/report.md`.
Tool-surface evidence:
`e2e/artifacts/model-efficiency-20261003T205311.298231Z/report.md`.
Both ran under one focus probe:
`e2e/artifacts/workflow-probe-20261003T205044.162709Z/result.json`.
It recorded zero activation, key-window, or first-responder losses and verified
its own process cleanup.

All 204 offline benchmark tests passed on the final source. Cleanup independently
confirmed all 81 recorded test PIDs absent and all 28 disposable runtime homes
removed, including interrupted/setup-failed attempts. Evidence:
`build/efficiency-final-offline-tests.log` and
`build/efficiency-cleanup-verification.json`. `git diff --check` also passed.

## Interpretation

Full wall time begins at harness process launch. Verified-edit timing begins at
adapter entry, so adding that timestamp and its finalization tail gives adapter
duration, slightly less than full process wall time. The early timestamp is
reported retrospectively only when the final tool response proves the exact note,
the Calculator result was observed, and the later independent judge and strict
coexistence checks pass. It does not measure the first instant the goal became true
or claim that independent judging happened before the model finished.

Host startup and model-activity markers are local observations. Time outside tools
includes host, transport, and model work; it is not pure provider inference time.
The fixed task cannot establish best-in-class performance across applications.
Continuous focus counters complement endpoint cursor/Space checks, but do not
prove simultaneous physical human typing or continuous cursor/Space stability.

Raw artifacts are local and Git-ignored. The tests, acceptance criteria, runner,
and this summary are tracked; local evidence links are not GitHub-hosted files.
