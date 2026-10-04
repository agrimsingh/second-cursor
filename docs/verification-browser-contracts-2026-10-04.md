# Browser target and tool contracts — 2026-10-04

This change addresses the wrong-field typing and avoidable recovery observed in
the [Opus comparison](verification-opus-model-sweep-2026-10-04.md). The scope is
browser target ownership, dropdown discovery/selection, keyboard spelling, and
accurate tool failure reporting. It does not change the configured model.

## Behavior

- `browser_type` checks editability and actual focus ownership before dispatching
  literal CDP text. Native selects, readonly/disabled/inert targets, replaced
  elements, and focus redirects refuse before text is sent. Actual editable refs
  inside shadow roots and same-origin or isolated frames remain supported. If an
  input handler moves focus after insertion, `submit=true` withholds Enter and
  explicitly reports that text was already inserted.
- Isolated-frame commands preserve renderer session identity and refuse when
  their session disappears or changes. They cannot silently fall back to the main
  document. A new isolated-world context can prove a frame migrated back into the
  main renderer; stale pinned operations remain invalid. Snapshot refs retain the
  session and owner ancestry that issued them, and replacement renderers get new
  ordinals. Disabled/inert frame owners also block typing into their documents.
- Native selects expose bounded label/value/selected/disabled metadata in
  `inspect` and `dom_snapshot`. Selection accepts an exact value first, then a
  unique exact label with whitespace normalized. Ambiguous or disabled choices
  fail before any mutation. Multi-select results verify option identity and value
  after page handlers, and preserve redaction even if a handler removes masking.
- Browser shortcut letter casing no longer adds implicit Shift to `Cmd+A` or
  other modified shortcuts. Literal uppercase and explicit Shift retain their
  behavior. Native keys also accept DOM `Arrow*` names.
- Browser benchmark batch schemas omit unsupported native `expect`. Scope
  refusals explain the public contract, and keyboard aliases preserve existing
  fixture boundaries. Tool telemetry records semantic failure separately from
  successful delivery, including partially failed batches; independent final
  task judges remain unchanged.

Native TextEdit Return/autosave behavior is **not fixed** by this change. The tool
descriptions now explain that background key delivery can change the target app's
internal active/key state and trigger autosave. For document line breaks, callers
should include a newline in `type_text`; controls requiring an actual Return still
need `press_key`. Removing the existing native focus handoff without a replacement
would regress multi-window targeting.

## Controlled reproduction

The saved pre-change app and extension reproduced the form bug through public
tools: typing `MUST-NOT-INSERT` at Route changed Name from `Ada Lovelace` to
`Ada LovelaceMUST-NOT-INSERT`. Focus-redirection and replacement cases also typed
into Name. The new four-scenario fixture verifies refusal before input events,
supported editors, conditional submission, and dropdown metadata/selection.

The preserved app binary is SHA-256
`6de59480fde5ad6cf670de8d767bf1344d18e46c930380f24fbda8ac3893e76e`.
The baseline extension is restored from commit
`5b0112b3ab271d46e354a1fc5bff93eceba97ca7` with a per-file hash manifest. Both
`SC_E2E_APP` and `SC_E2E_EXTENSION` must be set to reproduce a condition; selecting
an app alone does not pin the browser's extension code.

Baseline evidence:

- [Wrong-target reproduction](../e2e/artifacts/20261004-233457/report.md).
- [Dropdown reproduction](../e2e/artifacts/20261004-233428/report.md). Its typing
  scenario had an invalid test operation and is not used as evidence.

## Verification

The signed debug bundle built successfully. Its 17 bundled extension files match
the source byte-for-byte, and deep/strict signature verification passed. The app
binary is SHA-256
`976164b3027e09ea1e9068c2a907089812194b1c471ffcf9676d13985e5c47de`.

All 67 JavaScript tests, 265 Python benchmark tests, and 24 native key-parser
checks passed. JavaScript regressions include session replacement before and
during a command, backend-ID reuse, frame migration, exact focus ownership, and
select privacy after handlers. The four new live scenarios first passed against
the working extension, then passed again against the final bundled extension.

All 16 live scenarios passed through the first frozen candidate's public tools:

- [Target and dropdown contracts: 4/4](../e2e/artifacts/20261004-234535/report.md).
- [Browser reliability: 7/7](../e2e/artifacts/20261004-234608/report.md).
- [Browser batches: 4/4](../e2e/artifacts/20261004-234658/report.md).
- [Snapshot, iframe, shadow-root, and redaction regression: 1/1](../e2e/artifacts/20261004-234723/report.md).

These use an isolated headless Chrome for Testing 152.0.7977.75, disposable fixture
pages, and test instances. Per-scenario frontmost app, active Space, and cursor
checks passed. They do not exercise concurrent physical human input. Independent
review found and helped close three additional regression cases before the final
bundle was frozen; no unresolved finding remained in that bounded review.

```sh
node --test e2e/extension_tests/*.test.mjs
python3 -m unittest discover -s e2e/benchmarks -p 'test_*.py'
SC_E2E_APP=build/SecondCursor.app SC_E2E_EXTENSION=build/SecondCursor.app/Contents/Resources/extension python3 e2e/browser_target_contracts.py
```

The focus check and input dispatch are separate browser protocol operations,
so arbitrary page mutation in that final interval is still a limitation. These
tests establish the listed deterministic cases, not flawless behavior on every
website or continuous simultaneous human editing safety.

## Matched form comparison

The experiment uses Opus 5.5 Fast low because its previous form trace exposed the
most tool recovery. It schedules three attempts per frozen runtime, alternating
before/after, after/before, before/after. Both use the same current harness,
90-second/40-call budget, full catalog, on-demand observations, visible pinned
Chrome, and fixed work-order fixture. This isolates the application and extension
changes; it does not measure the separate contribution of the harness fixes.

Each condition has explicit app/helper/extension hashes, checked before every
trial and after the schedule. The runner fingerprint is
`7505bc30ec84655d100b028279f76f1c5cca3613b0cebd2a94963cecced9e41f`.
The comparison script and its hash are saved with the artifact. Old and new
engine versions intentionally differ, so ordinary same-version aggregation is
not used to silently merge the cohorts. Every scheduled attempt remains in the
denominator. Model/host/cache variation is uncontrolled; three samples per
condition can only provide preliminary timing evidence.

The first candidate passed all three trials, as did the saved baseline:

| Metric | Before | First candidate |
| --- | ---: | ---: |
| Strict passes / scheduled | 3/3 | 3/3 |
| Completion times, seconds | 39.117, 52.556, 28.817 | 40.905, 29.945, 34.316 |
| Median completion, seconds | 39.117 | 34.316 |
| Median model calls | 12 | 7 |
| Total model calls | 37 | 22 |
| Semantic failures (`ok=false`) | 12 | 4 |
| Host rejections | 11 | 4 |

The median improved 12.3%, but the candidate was faster in only one matched pair.
The paired differences (after minus before) were +1.788, -22.611, and +5.499
seconds. Reduced tool recovery is the stronger result: all three candidate runs
used fewer calls. Tool time medians were 4.604 versus 3.242 seconds; the remaining
wall time includes host/model overhead, final response, and cleanup. Actual cost
and provider-only inference time were not measured.

Each independent event ledger recorded exactly one correct, confirmed Ada
Lovelace/Priority order. All fixture, adapter, and supervisor cleanup checks passed.
Baseline trials 1 and 2 visibly changed the name to `Ada LovelacePriority` and
repaired it before submission; the candidate showed no such corruption. The
controlled regression is the direct proof that unsafe typing now refuses.

The baseline's partially failed batch had successful delivery but `ok=false` in
the corrected telemetry. The four candidate rejections were all `browser_command`
calls supplying `ref` instead of `locator`, followed by recovery through capability
help and semantic locators. Label-based `Priority` selection succeeded in every
candidate trial; the baseline needed the underlying lowercase value.

The continuous focus probe recorded six active/key losses, one per browser fixture
launch, and zero responder changes. Fixtures started foreground, while individual
trial cursor/Space checks passed. These model runs do **not** establish background
coexistence. The desktop was ordinary and idle at preflight; no fresh user quiet-
window acknowledgment was received, so readiness was observed rather than assumed
from an unanswered request.

Evidence (local, ignored artifacts):

- [All six results](../e2e/artifacts/browser-contract-comparison-20261004T154748.247199Z/results.json),
  [plan and runtime hashes](../e2e/artifacts/browser-contract-comparison-20261004T154748.247199Z/plan.json),
  and [unchanged postflight hashes](../e2e/artifacts/browser-contract-comparison-20261004T154748.247199Z/postflight.json).
- [Focus and cleanup](../e2e/artifacts/workflow-probe-20261004T154745.906913Z/result.json).
- The measured candidate is preserved at `build/browser-contract-first-candidate/SecondCursor.app`;
  [archive verification](../e2e/artifacts/browser-contract-comparison-20261004T154748.247199Z/archived-candidate.json)
  confirms its bundled extension matches the measured hashes.

## Final diagnostic refinement

The repeated `ref`/`locator` mismatch justified one final, bounded change after
the comparison: `select_option` and `check` now reject unsupported top-level
`ref` or a missing/malformed locator before targeting, explain that `locator` is
required, and give a semantic locator example. They never guess which element was
intended. Root-level read operations keep their existing behavior. Both the host
path and serialized DOM operation enforce the same contract.

Five additional offline tests brought the JavaScript total to 67. The final
packaged extension matches the source; all four target/selection scenarios and
all seven browser reliability scenarios passed again:

- [Final target and locator diagnostics](../e2e/artifacts/20261004-235536/report.md).
- [Final browser reliability checks](../e2e/artifacts/20261004-235625/report.md).

The other five live scenarios were not rerun for this error-message/argument
guard refinement. Three additional Opus form trials use the final candidate with
the same model, budget, browser, and fixture. They are a separate validation
cohort, not additional samples silently pooled into the interleaved timing result.

All three final-candidate trials passed, with exactly one correct submission each
and verified fixture, adapter, and supervisor cleanup. Completion times were
24.627, 24.031, and 59.757 seconds; tool calls were 7, 6, and 7. Five initial
`ref`/`locator` mistakes remained, but every trial recovered directly from the new
diagnostic with zero capability-help calls, versus a help call in all three first-
candidate trials. No wrong-field edits were observed.

The slow final trial's first observable model activity arrived at 47.54 seconds,
versus 12.53 and 12.65 seconds in the other two. Work after that point took about
12 seconds in each. This is model/host response timing evidence, not isolated
provider inference time. The final median of 24.627 seconds is validation-cohort
data and must not replace the interleaved comparison's result.

Runtime hashes matched before and after all three trials. The focus probe recorded
three active/key losses during fixture launches and zero responder changes; cursor
and Space endpoints were stable. No continuous background coexistence claim is
made. Evidence:

- [Final model results and independent trial evidence](../e2e/artifacts/model-pilot-20261004T155709.068885Z/second-cursor.tasks.json).
- [Final runtime identity and postflight match](../e2e/artifacts/model-pilot-20261004T155709.068885Z/runtime-identity.json).
- [Final focus and cleanup](../e2e/artifacts/workflow-probe-20261004T155707.759392Z/result.json).

The built candidate is `build/SecondCursor.app`. This work verified disposable
instances of that bundle; it did not replace the user's installed app or reload
the user's Chrome extension.
