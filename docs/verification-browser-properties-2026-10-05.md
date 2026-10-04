# Browser contracts and generated properties — 2026-10-05

The initial Sol workspace cohort exposed two different problems: the public browser tool left its operation arguments untyped, and flat locators disagreed with Chrome about named sections. This change describes canonical arguments at discovery time and shares role resolution between flat and chained locators. Generated tests subsequently exposed a separate accessible-name precedence bug, which was fixed before the model rerun.

## What the generated tests established

`e2e/browser_role_properties.py` generates 20 HTML variants for each of seeds 104729 and 130363. Names, heading levels, explicit control roles, and ordering vary. Its independent semantic oracle is Chrome's accessibility tree returned by `browser_snapshot(structured=True)`, rather than the DOM helper under test. Flat and chained inspection must resolve the same generated element ID and return the AX role/name. Region counts include unnamed, empty-label, and unresolved-label sections.

Both seeds failed on a button with competing `aria-labelledby` and `aria-label`: Chrome and the chained locator used the resolved labelled-by text, but the flat locator used the other label and found no match. After fixing precedence, the identical manifests and action sequences passed all 136 checks. Raw inspection text and existing case/exact-match behavior remain unchanged.

The suite also generates replacement, removal, and relabeling sequences. Old observation tokens must be refused without changing the page's receipt state; detached refs must also refuse without an explicit token. A fresh observation must deliver exactly one trusted click to the current node generation. These checks passed both before and after the naming fix.

This is bounded, seeded property testing with differential and stateful checks. It retains the seed, generated manifest, AX snapshot, calls, and action sequence, but does not implement automatic shrinking or exhaustive HTML/accessibility coverage. The live tests use a disposable headless browser; they are functional regressions, not a model speed or shared-desktop benchmark.

- [Retained failing run](../e2e/artifacts/20261005-015523/report.md), with [full generated evidence](../e2e/artifacts/20261005-015523/browser-role-properties.json).
- [Identical repaired run](../e2e/artifacts/20261005-015717/report.md), with [full generated evidence](../e2e/artifacts/20261005-015717/browser-role-properties.json).
- [Five fixed public-contract scenarios](../e2e/artifacts/20261005-015746/report.md): 80 checks covering the schema, nested arguments, semantic roles, ambiguity, and select metadata.
- [Four existing target-contract scenarios](../e2e/artifacts/20261005-015754/report.md): 44 checks covering typing targets, frames, focus changes, and exact selection.

All ten live scenarios passed, including their endpoint cursor/focus/Space invariants. These endpoint checks cannot rule out transient interference. The rebuilt candidate also passed all 346 Python harness tests and 100 JavaScript regressions. Build and test logs are under `build/browser-contract-*`.

## Public contract and accounting

The public schema now advertises typed `locator`, `value`, and `text` fields, canonical `inspect` and `select_option` examples, and the distinction between ARIA roles and HTML tags. Existing extended-operation fields remain supported; no new reference-based mutation authority or benchmark permissions were added. The scoped advertisement omits frame access already forbidden by the fixture guard.

The default public catalog grew from 44,302 to 46,292 characters under the existing `len(json.dumps(tools))` measurement. The 42,000-character regression budget was already exceeded before this change and has not been raised. Clearer arguments cost 1,990 characters here; catalog size remains an open optimization, rather than a passing check.

The Codex host now retains dynamic tool lifecycle events and reconciles them with callbacks using exact call identity, tool name, and original JSON arguments. It deduplicates completion replays and reports missing or conflicting evidence as partial coverage. Host-only refusals are not synthesized into driver calls. Host transport failures remain separate from semantic failures in normalized adapter `tool.ok` results. Provider-internal attempts remain unobservable.

The earlier baseline's ten medium failures are confirmed callback failures; upstream pre-callback refusals were not retained and are unknown. The [baseline report](verification-sol-workspace-2026-10-05.md) now states that accounting limit explicitly.

## Model rerun

All six Sol 6.1 medium trials passed, with all 60 independent predicates true, matching baseline case manifests/final states, and verified cleanup. The task/fixture hashes, seeds 41/42, model/effort, browser, host version, prompts, judges, and 100-call/300-second limits match the baseline. Product and runner identities change because of the fixes and expanded telemetry.

| Workflow | Seed | Baseline seconds / calls / failures | Repaired seconds / calls / failures |
| --- | ---: | ---: | ---: |
| Reconcile | 41 | 116.188 / 22 / 1 | 73.028 / 18 / 0 |
| Reconcile | 42 | 73.588 / 17 / 1 | 82.356 / 18 / 0 |
| Handoff | 41 | 126.500 / 28 / 3 | 108.319 / 25 / 0 |
| Handoff | 42 | 131.426 / 25 / 1 | 104.887 / 25 / 0 |
| Recovery | 41 | 159.789 / 30 / 1 | 129.123 / 29 / 0 |
| Recovery | 42 | 148.880 / 31 / 3 | 109.398 / 26 / 0 |

Observed callback failures fell from 10 to zero. All 141 new calls reconcile exactly across requests, raw callbacks, normalized adapter records, and host lifecycle starts/completions. Expanded host coverage is complete in all six trials, with zero host-only refusals, unknown outcomes, or semantic failures. Sixteen successful named-region inspections demonstrate use of the repaired path. The baseline's unavailable upstream coverage remains a limit of the comparison.

Total calls fell from 153 to 141 (7.8%); total harness wall time fell from 756.372 to 607.111 seconds (19.7%). Five of six pairs were faster, while reconciliation seed 42 was slower. Two seeds per task and sequential before/after execution do not establish a causal 20% speedup or general reliability. Wall time includes adapter setup, model/tool work, final response, and adapter cleanup; fixture setup and subsequent judging are outside that clock.

- [Cohort report](../e2e/artifacts/model-sweep-20261004T175818.343724Z/report.md), [plan](../e2e/artifacts/model-sweep-20261004T175818.343724Z/plan.json), and [complete results](../e2e/artifacts/model-sweep-20261004T175818.343724Z/results.json).
- [Runtime before](../e2e/artifacts/model-sweep-20261004T175818.343724Z/runtime-before.json) and [after](../e2e/artifacts/model-sweep-20261004T175818.343724Z/runtime-after.json) match exactly. The runner was `869d1ded450669d1242427f8432d8562b245db62a57d16877eb9e7525f97046f`; the extension was `35b69734f7764544789971b6139afffc0ea2583ddbb9f15460886a69d62fc83b`.

No production changes, builds, GUI suites, or additional model jobs ran during the timed cohort. Brief read-only source probes and syntax checks ran while new, unexecuted privacy regression files and documentation were prepared outside its fingerprinted inputs. Background host load was not independently controlled. All evidence is retained locally under ignored artifact directories. The candidate is `build/SecondCursor.app`; the installed app has not been replaced.

## Subsequent DOM-read privacy hardening

Review found that sharing the role map newly emitted standalone option and semantic-container rows in `dom_snapshot`. Those rows could expose sensitive option content despite the select's own metadata being masked. Direct option reads and conflicting native-label sensitivity also had pre-existing gaps. The [retained live regression](../e2e/artifacts/20261005-021030/report.md) recorded 89 failed checks out of 344 using synthetic markers: 86 exposed those gaps, while three incorrectly expected native textarea `innerText` to equal its value.

The fix inherits select sensitivity when serializing option names, values, text, attributes, query results, and ambiguity diagnostics. Native and superseded label signals remain conservative sensitivity inputs. Semantic containers no longer acquire names from descendant control text; ordinary paragraph/list summaries and public selected options remain readable. Matching uses unmasked names, and sensitive text is masked before slicing or computing returned length.

The [unchanged first rerun](../e2e/artifacts/20261005-021442/report.md) removed all 86 privacy failures and retained only the same three textarea expectation failures. Correcting those expectations to Chrome's observed empty `innerText` left default/value and `textContent` readability assertions intact, with the same calls and 344 checks. The [corrected final run](../e2e/artifacts/20261005-021633/report.md) passed all 344. Both earlier artifacts remain intact.

The final candidate passed 112 JavaScript tests and all 11 live scenarios, totaling 604 checks: [generated roles/references](../e2e/artifacts/20261005-021542/report.md), [public contracts](../e2e/artifacts/20261005-021557/report.md), [typing/selection targets](../e2e/artifacts/20261005-021605/report.md), and [label/option privacy](../e2e/artifacts/20261005-021633/report.md). Endpoint invariants also passed. The packaged extension matches the tested source byte-for-byte.

This privacy coverage is bounded to DOM command serialization of controls/options and semantic container names. Arbitrary ancestor text reads are outside it. The separate Chrome AX snapshot classifier still uses effective accessible names; native-label overrides in that path were not changed or validated here. This is not a claim of universal sensitive-data masking.

The final extension is `bd2057eb03c456059d5516b9c45368aabeb34efe32458ab8ed7b543850bdb38a`, and its runner is `c07ae75297238a43dc9d9701187ff5fd0c325baa94b7655799067ecdc7934d2a`. These serialization changes follow the six-trial cohort, so its timing numbers apply to the earlier explicitly pinned build.

A separate [held-out Sol medium recovery run on seed 43](../e2e/artifacts/model-sweep-20261004T181702.154964Z/report.md) passed on the final candidate: all ten independent predicates, 29 calls, zero host/callback/semantic failures, and 126.013 seconds of harness wall time. It exercised the stale refusal, committed the requested change exactly once, and reread persisted state after the uncertain save. All call identities reconcile, telemetry coverage is complete, and cleanup passed. Its [runtime before](../e2e/artifacts/model-sweep-20261004T181702.154964Z/runtime-before.json) and [after](../e2e/artifacts/model-sweep-20261004T181702.154964Z/runtime-after.json) match. This validates a new case on the final build without mixing it into the six-case timing comparison.

To reproduce the comparison, run `SC_E2E_APP=build/SecondCursor.app python3 e2e/model_sweep.py --profile sol-6.1-medium --rounds 2 --seed 41 --max-steps 100 --timeout 300`. For the final held-out case, use `--rounds 1 --seed 43 --task workspace_recovery`. The browser command, generated role, and label-privacy scripts listed in the README reproduce the non-model coverage; all use disposable fixtures.
