# Second Cursor model and host comparison — 2026-10-04

Sol 6.1 medium is the provisional default from this screen. It passed all five
scheduled tasks, matched high effort on native latency, and finished both browser
tasks faster. For the Cursor SDK route, Grok 4.5 Fast low is the provisional
choice: five strict passes, with no demonstrated accuracy benefit from medium.
These are operating choices from a small sample, not general intelligence or
provider reliability rankings. No client model setting was changed.

## Measured results

Every action used Second Cursor. Grok used Cursor SDK 1.0.35; Sol used Codex CLI
0.159.0-alpha.12.1. This comparison includes host orchestration and prompt/tool
framing, so cross-host latency is not isolated model inference time.

| Configuration | Strict passes / scheduled | Native median, seconds | Browser form, seconds | Dialog, seconds |
| --- | ---: | ---: | ---: | ---: |
| Grok 4.5 Fast low | 5/5 | 22.787 (3 passes) | 36.803 | 29.720 |
| Grok 4.5 Fast medium | 4/5 | 22.442 (2 passes) | 36.189 | 26.761 |
| Grok 4.7 Fast medium | 4/5 | 23.513 (2 passes) | 76.956 | 36.097 |
| Sol 6.1 medium | 5/5 | 15.057 (3 passes) | 39.551 | 24.253 |
| Sol 6.1 high | 5/5 | 15.012 (3 passes) | 46.259 | 27.523 |

Native has three repeats per configuration; each browser task has one. The 5-task
count therefore weights the native task three times. Do not combine these columns
into an unqualified aggregate speed score. Native medians exclude failed trials;
failures remain in the scheduled denominator. Every browser row passed its
independent event-ledger judge. Wall time includes host startup, prefetch,
model/tool execution, the final response, and adapter cleanup. Harness fixture
setup and later independent judging are outside the measured wall clock.

Sol medium's native median was 33.9% lower than Grok 4.5 low, but Grok 4.5 low
finished the single form trial 2.7 seconds faster. High effort demonstrated no
benefit on this task set. Neither model costs nor provider-only inference time
were measured, so there is no price/performance claim.

## Failures retained in the score

Grok 4.5 medium's third native run ended in the Cursor host before its first model
tool call, after 13.519 seconds, with an empty final response. The fixture remained
unchanged. This is a model/host execution failure; available evidence does not
distinguish its provider or transport cause. It is not an arithmetic failure and
does not enter successful latency statistics.

Grok 4.7 medium's third native run produced the correct Calculator result and
visible note, but failed the requirement to leave the saved scratch file
unchanged. After a rejected batch whose `select_text` step lacked `element_index`, it
recovered with a valid selection, then Return followed by semantic text insertion.
The saved file acquired a newline while the visible note correctly included
`Total: 42`. The trace contains no Save action. Return used the driver's keyboard
focus handoff; text insertion used `ax_selected_text`. This is a strict
file-preservation failure involving the driver/app path, not evidence of an
incorrect calculation or hallucinated visible completion. The follow-up
diagnostic is recorded below without replacing this original failed trial.

All 14 native runs that attempted actions clicked the observed Calculator buttons
7 → Multiply → 6 → Equals and read 42 before editing the note. All 13 strict
native passes preserved both exact note content and the original saved file.
Whole-field replacement is allowed by this fixture, so these results do not prove
safe concurrent editing by a human.

### Saved-file diagnostic

Three fresh, owned TextEdit fixtures reproduced the difference without any model
involved. Semantic `select_text` plus insertion of `\nTotal: 42` preserved the
original saved file. Both the exact Return-plus-text batch and a split version
with an inspection between calls saved the newline. In the split case, the file
already contained the newline immediately after Return, before semantic text
insertion. All three visible notes were correct at the end.

This isolates the persistence side effect to the Return path on these fixtures.
It is consistent with TextEdit autosave during keyboard focus handoff; it does
not independently identify the exact internal autosave trigger. A 10 ms watcher
recorded file changes and post-action reads checked the next two seconds. These
observations do not prove indefinite absence of autosave after semantic edits.
The diagnostic's cursor/Space checks passed and its continuous foreground focus
counters stayed at zero. The production behavior remains unresolved.

- [Diagnostic report](../e2e/artifacts/20261004-053022/report.md)
- [Semantic control](../e2e/artifacts/20261004-053022/semantic_batch.json)
- [Exact Return batch](../e2e/artifacts/20261004-053022/return_batch.json)
- [Split Return diagnostic](../e2e/artifacts/20261004-053022/return_split.json)
- [Diagnostic focus and cleanup](../e2e/artifacts/workflow-probe-20261003T213021.522592Z/result.json)

## Tool friction found in browser traces

The form requires a native select option, confirmation, and exactly one submit.
Several runs recovered from rejected arguments. The following issues confound
any claim that longer runs reflect weaker reasoning alone:

- The benchmark's `SAFE_KEYS` accepts `down` but rejects `ArrowDown`, although the
  production browser supports both. Its projected schema does not expose this
  restriction. This is harness-only friction in `e2e/benchmarks/engines.py`.
- The shared production `batch` schema advertises native `expect`, while browser
  `runTabBatch` rejects it. The browser-scoped benchmark schema removes
  `window_id` but retains `expect`, making this mismatch easier to encounter.
  Relevant code is `ToolCatalog.swift`, `ToolRouter+Browser.swift`, and
  `e2e/benchmarks/engines.py`.
- Ordinary browser `select_option` needs the option value `priority`; a visible
  label of `Priority` does not work through that route. The extended browser
  inspector does not expose the options' label/value pairs, even though the
  separate chained-locator path can match labels. Grok 4.7 spent several extra
  calls inspecting, taking a screenshot, and trying keyboard recovery. Relevant
  code is `extension/extended.js` and the browser schema/help.

The audit counted 12 rejected attempts across 96 browser model calls: four
rejections in the harness before driver dispatch and eight production refusals.
Form/dialog rejection counts were Grok 4.5 low 3/1, Grok 4.5 medium 2/0, Grok 4.7
medium 3/0, Sol medium 0/0, and Sol high 3/0. Cursor's host currently replaces
harness exceptions with a generic fixture error, while Codex returns descriptive
scope errors. That error-detail asymmetry also belongs to the measured host path.

The next useful improvement is to make each browser tool's advertised arguments
match what its runtime accepts, and expose select labels/values clearly. Preserve
this baseline and use a new runner fingerprint for any retest after those changes.
The current `retries` metric counts adapter retries, not model attempts that
change strategy; zero retries does not mean there were no rejected calls.

## Controls and limitations

The native cohort used upfront observations and full tool catalogs; the browser
cohort used on-demand observations and full catalogs. Within each cohort, all
profiles used the same fixtures, case identities, driver binary, 90-second model
budget, and 40-call budget. Hosts and fixtures were fresh for each trial. Native
order alternated forward/reverse across three rounds. Browser used one forward
round. Fixed fixtures, odd round counts, provider caching, and sampling variability
limit the conclusions; there was no model sampling seed.

Installed Second Cursor binary SHA-256:
`6de59480fde5ad6cf670de8d767bf1344d18e46c930380f24fbda8ac3893e76e`.
Runner fingerprint:
`0ea2113f4d5f730becfb8968618b82a85806f5b526ca66ca5049b6182a65903d`.
The full pinned plans contain the exact model parameters, host identities, browser
binary identity, source fingerprints, and schedule. The app was not rebuilt or
replaced for this comparison.

The foreground focus fixture recorded zero active/key/responder losses throughout
the full native cohort. Across the complete native/browser run it recorded ten
active/key losses and zero responder changes; browser launches during fixture
setup change foreground. Individual browser trial invariants passed, but this
does not establish background coexistence throughout browser setup. Cursor/Space
checks are sampled; this quiet-window experiment is not a simultaneous human
typing test.

These tasks test basic tool use and outcome preservation. Harder invoice/inbox
reasoning, unseen recovery, adversarial page content, and sustained concurrent
human input need separate fixtures before claiming broad capability.

## Verification and local evidence

- All 254 offline Python benchmark tests passed, including eight new sweep tests.
  The suite verifies exact model/config comparisons, matched schedules, failure
  denominators, successful-only latency, and interrupted/missing-child handling.
- Fifteen native trials completed: 13 strict passes and two failures, with no
  missing or mismatched evidence.
- Ten browser trials completed: ten strict passes, with no missing or mismatched
  evidence. A deterministic browser preflight also passed.
- A separate trace audit found no false passing outcome or calculator shortcut.
- The comparison runner and scoped host leave ordinary cross-driver comparisons
  strict; only this explicit sweep permits its declared model/host differences.

Artifacts remain local under the ignored `e2e/artifacts` directory:

- [Native cohort](../e2e/artifacts/model-sweep-20261003T211718.303300Z/results.json)
- [Browser cohort](../e2e/artifacts/model-sweep-20261003T212325.882550Z/results.json)
- [Native boundary focus](../e2e/artifacts/workflow-probe-20261003T211717.321011Z/native-boundary-focus.json)
- [Whole-cohort focus and cleanup](../e2e/artifacts/workflow-probe-20261003T211717.321011Z/result.json)
- [Browser preflight](../e2e/artifacts/best-in-class-tasks-20261003T211541.738874Z/tasks.json)

Each cohort retains its preflight plan, child logs, per-profile reports, independent
judges, and tool traces. See [the acceptance criteria](acceptance-model-sweep.md)
for the comparison contract and the README for invocation commands.
