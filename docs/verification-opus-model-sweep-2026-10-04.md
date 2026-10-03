# Opus 5.5 Fast comparison — 2026-10-04

Opus 5.5 Fast medium is the more promising Opus setting from this screen. Both
configurations passed all five scheduled trials. Low was faster on the repeated
native task, but medium completed the browser form with substantially less
recovery. These small fixed fixtures do not establish general reasoning ability
or provider reliability. No client model setting was changed.

## Results through Second Cursor

| Configuration | Strict passes / scheduled | Native median, seconds | Browser form, seconds | Dialog, seconds |
| --- | ---: | ---: | ---: | ---: |
| Opus 5.5 Fast low | 5/5 | 19.092 | 62.050 | 29.134 |
| Opus 5.5 Fast medium | 5/5 | 21.836 | 32.812 | 27.473 |

Native has three repeats per configuration; each browser task has one attempt.
Native trials alternated low/medium, medium/low, low/medium. Browser ran low then
medium. No trial was retried or dropped. Native medians therefore have three
samples; the browser numbers are individual observations, not stable estimates.
The five-task pass count weights the native task three times.

Every native run used two model calls, pressed All Clear → 7 → Multiply → 6 →
Equals in Calculator, observed 42, and preserved the exact original saved file.
Five runs used full-field note replacement; low's second run used selection and
text insertion. These fixture successes do not establish concurrent human editing
safety. The independent browser judge verified exactly one correct form submission
and the required cancel-then-delete dialog sequence in every run.

The preceding cohort's Sol 6.1 medium results were 15.057 seconds native,
39.551 seconds form, and 24.253 seconds dialog. Grok 4.5 Fast low recorded
22.787 / 36.803 / 29.720 seconds. Opus medium was faster than that Grok configuration
in each observed task category, while Sol remained faster for native editing and
dialog handling. These are historical references from a separate cohort, not a
newly interleaved four-model comparison. Keep Sol medium as the provisional
general default; Opus medium is a promising Cursor-hosted alternative.

## Recovery and product issues

Low's form run took 14 model calls. It recovered from malformed browser-command
arguments, an invisible option click, a partially failed batch, and text entering
the wrong field. It repaired the name before the only successful submission, so
the final independent outcome is a true pass despite the intermediate mistakes.

The wrong-field typing is a production issue, not just an incorrect model choice.
`browser_type(ref=e4, text="Priority")` targeted the Route combobox, but the
resulting observation changed Name from `Ada Lovelace` to `Ada LovelacePriority`
and left Route unchanged. In `extension/input.js`, `type` checks that the ref is
live and calls `DOM.focus`, then uses tab-level `Input.insertText`. It does not
verify that the requested element is editable or actually owns keyboard focus
before inserting text. A noneditable target must be rejected rather than letting
text reach another field. This behavior was observed in the model trace; no
separate controlled reproduction or production fix was performed in this run.

The partial batch correctly reported its production outcome as failed: the name
was filled, clicking the invisible option failed, and the checkbox step was
skipped. The benchmark's `_ok` helper and Cursor host only recognized top-level
transport errors, so adapter telemetry labeled that envelope `ok=true`/completed.
The model still saw the failed outcome and recovered, and the independent final
judge remained correct. Transport success and semantic batch failure need
separate metrics; the existing retry counter does not measure this recovery.

The prior dropdown discoverability problem also remains: ordinary `select_option`
uses the option's value `priority`, while the visible label is `Priority` and the
inspector does not expose the label/value mapping. Medium recovered from that
label/value mismatch. Existing browser key/schema restrictions were intentionally
left unchanged for this comparison. These tool and harness issues limit attribution
of recovery latency solely to model intelligence.

Across the four browser trials there were 40 model calls and five top-level
rejections: three harness argument/schema refusals and two production refusals.
Low's form had four of those rejections; medium's form had one. The partial batch
is a separate semantic failure, not a sixth top-level rejection. Both dialog
trials had no rejected calls. All fixture cleanup checks passed.

## Configuration and evidence boundaries

The authenticated live Cursor SDK catalog listed both exact selections:
`claude-opus-5-5`, `context=300k`, `fast=true`, and `effort=low` or `medium`.
The bridge verified the returned model selection during each run. The live catalog
is saved with each cohort; [Cursor's model documentation](https://prod.cursor.com/docs/models/claude-opus-5-5)
also describes Opus 5.5 Fast availability. No model fallback was accepted.

Both used Cursor SDK 1.0.35, Node v22.23.1, and the existing isolated fixture-tool
host. Native used upfront observations; browser used on-demand observations. Both
kept the full catalog, 90-second budget, 40-call budget, ordinary desktop placement,
and the existing independent outcome checks. Wall time includes host startup,
prefetch, model/tool execution, final response, and adapter cleanup. Fixture setup
and later judging are outside that clock. Provider-only inference time and actual
cost were not measured.

The new runner fingerprint is
`d64c9f3d30a2b682944fd4249f2847b3f36df423b6c79191fb834582892de862`.
Only `model_sweep.py` changed among the 15 fingerprinted files: it adds opt-in
profiles and profile-selection validation. All other fingerprinted files match
the earlier comparison at commit `75583cb`, including adapters, tool dispatch,
Cursor bridge, fixtures, and judges. The installed driver remains SHA-256
`6de59480fde5ad6cf670de8d767bf1344d18e46c930380f24fbda8ac3893e76e`.
Task/fixture hashes, Chrome 152.0.7977.75 identity, budgets, and case seeds also
match. Schedule and runner identity changed, so the ordinary strict comparator
must not silently combine the old and new cohorts.

The native focus probe recorded zero active/key/responder changes. The browser
probe recorded four active/key losses and zero responder changes across fixture
launches; all individual trial checks passed. This demonstrates the native quiet
window, not continuous background coexistence across browser setup or simultaneous
human typing. Normal provider/cache/host-load variation remains uncontrolled; the
offline test suite also overlapped the first native trial, which stayed in the
results. No timing adjustment or exclusion was made.

All 258 offline Python benchmark tests passed, including 12 sweep tests. New tests
cover opt-in profile selection, preserved five-profile defaults, exact Opus
parameters, duplicate/unknown profiles, and configuration mismatch rejection.
Both live cohorts completed with all scheduled evidence validated. An independent
trace audit checked task outcomes and tool behavior.

Local ignored artifacts:

- [Native results and exact plan](../e2e/artifacts/model-sweep-20261003T213523.914864Z/results.json)
- [Browser results and exact plan](../e2e/artifacts/model-sweep-20261003T213754.187009Z/results.json)
- [Native focus and cleanup](../e2e/artifacts/workflow-probe-20261003T213522.865285Z/result.json)
- [Browser focus and cleanup](../e2e/artifacts/workflow-probe-20261003T213753.247298Z/result.json)
- [Low form trace, including wrong-field typing](../e2e/artifacts/best-in-class-tasks-20261003T213754.284113Z/trials/browser_work_order-1/model-trace.jsonl)

See [the previous comparison](verification-model-sweep-2026-10-04.md) for the
historical reference results and [Cursor host instructions](../e2e/cursor_host/README.md)
for reproducible invocation commands.
