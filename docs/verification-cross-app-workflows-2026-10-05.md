# Cross-app interruptions and AX privacy — 2026-10-05

The new cross-app control-path suite passed all 12 trials: seeds 73–75 across
four interruption modes, with 649 assertions and clean endpoint invariants.
Across those trials its foreground event oracle checked 2,196 events and 644
periodic synthetic insertions. Activity occurred during both the browser and
native phases of every trial. No unexpected focus transition, protected-text
corruption, duplicate submission, or duplicate native append was observed.

## What ran

The scripted solver derives an invoice decision from rendered policy, purchase
order, and latest-message content in the seeded operations workspace. It handles
a stale revision, rereads the correction, and reconciles an unavailable save
confirmation from persisted browser state. It then appends that derived result
to an existing disposable TextEdit document and saves the file through public
Second Cursor tools. The independent judge reads the browser's durable ledger,
the native AX value through a separate helper, and the saved file from disk.

The combined mode changes foreground canary windows at seeded tool boundaries
and inserts additional foreground text. It pauses the task, appends an external
correction to the exact scratch TextEdit window, and verifies that paused and
stale resumed writes are refused without changing the document. A fresh read
then rebuilds the append. After its acknowledgement is deliberately discarded,
the solver checks the actual operation journal and document before continuing,
without resubmitting the append.

| Mode | Seeds | Trials | Assertions | Foreground events | Periodic insertions |
| --- | --- | ---: | ---: | ---: | ---: |
| [Combined interruptions](../e2e/artifacts/20261005-023646/report.md) | 73–75 | 3 PASS | 193 | 678 | 175 |
| [Browser recovery and native handoff](../e2e/artifacts/20261005-023736/report.md) | 73–75 | 3 PASS | 124 | 492 | 153 |
| [Pause/resume](../e2e/artifacts/20261005-023820/report.md) | 73–75 | 3 PASS | 163 | 513 | 158 |
| [Pause, concurrent edit, and fresh plan](../e2e/artifacts/20261005-023905/report.md) | 73–75 | 3 PASS | 169 | 513 | 158 |

The canary records text-storage mutations and application/window/responder events
as they happen, rather than relying only on polling or the final screen. Its
independent oracle derives exact text from the harness's command log. Explicit
window switches allow only their exact expected key-window transitions; an
unrelated transition inside that boundary still fails.

Seventeen replay tests reject wrong-target text even after repair, temporary
focus loss followed by restoration, missing/truncated evidence, lost input, and
unacknowledged commands. Review found and fixed a false pass where a focus
command could acknowledge without performing its transition. Nine additional
tests cover disruption schedules, native preservation, duplicate dispatches,
unsaved output, and missing foreground activity. Every live trial retains its
schedule, command/event ledgers, screenshots, independent result, browser identity,
and identical before/after fixture-source hashes. All 32 processes launched for
the 12-case matrix had exited after cleanup.

## Privacy bug fixed

The [baseline live regression](../e2e/artifacts/20261005-023124/report.md) failed
seven of 359 assertions: browser-open observations and plain/structured AX
snapshots exposed synthetic secret values hidden behind benign ARIA label
overrides, along with sensitive option names. Snapshot classification now retains
Chrome's native and superseded naming sources and masks sensitive select
descendants and option content while preserving ordinary values.

An expanded shadow-DOM check caught an over-redaction regression in the initial
fix: Chrome flattens shadow roots in DOMSnapshot, so manually rebuilding native
label lookup conflated duplicate IDs across roots. That implementation was
removed; Chrome's scoped AX naming sources now supply native-label sensitivity.
The [retained failing expansion](../e2e/artifacts/20261005-023357/report.md) has
three failed assertions, and the [final live run](../e2e/artifacts/20261005-023633/report.md)
passes all 363 assertions, including the ordinary shadow value. This covers
structured/textual snapshot serialization, not arbitrary secrets rendered in
screenshots or arbitrary ancestor text.

## Regression evidence and reproduction

- All 125 extension JavaScript tests passed, including 13 new snapshot cases.
- All 346 existing benchmark Python tests and 34 root-level Python tests passed.
  The root tests include the 26 new cross-app/oracle cases. The 44 release-harness
  tests also passed after the manifest change.
- [Generated roles and stale references](../e2e/artifacts/20261005-023958/report.md)
  passed all 136 assertions on the final extension.
- [Real SDK process-kill recovery](../e2e/artifacts/20261005-024013/report.md)
  passed all 22 assertions: the committed invoice survived a real SIGKILL, and
  the resumed process reconciled before mutating without replaying the submission.
- The new [cross-app release-gate entry](../e2e/artifacts/best-in-class-20261004T184022.470906Z/report.md)
  passed through the normal gate runner. This is the scoped entry, not a run of
  the complete release profile.

```bash
scripts/build-app.sh debug
SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_workflows.py --takeover --seed 73 --rounds 3
SC_E2E_APP=build/SecondCursor.app python3 e2e/cross_app_workflows.py --takeover --seed 73 --rounds 3 --mode stale-plan
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py gate --profile release --suite cross-app-interruptions --takeover
SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_label_privacy.py
python3 -m unittest discover -s e2e -p 'test_*.py'
```

The signed candidate is `build/SecondCursor.app`; the installed copy was not
replaced. All 17 packaged extension files matched source, and source stayed
unchanged during the final runs. The tested `snapshot.js` SHA-256 is
`8307baa89d1eabbc9890ab36f88bfa1aef89eb4d584c1e4785f017a84a02aabf`.
Detailed identities, matrix summary, cleanup audit, and test logs are retained
under `build/cross-app-*`; GUI evidence is retained under `e2e/artifacts/`.

## Scope

These are deterministic public-tool runs, not new model reasoning or speed
scores. Foreground insertion goes through the fixture's current key-window
responder inside its own process; it does not emulate physical HID input or type
into personal apps. Window switches occur at recorded tool boundaries while
periodic text activity continues independently. Native stale-plan refusal here
comes from pause/resume invalidation, not a text compare-and-swap guarantee.
Native acknowledgement loss in the cross-app suite is injected after successful
dispatch; the separate process-kill test establishes actual SDK restart recovery.
Native exactly-once evidence combines dispatch count and exact final live/saved
text, not a complete TextEdit mutation history. Seeds reproduce these bounded
sequences; automatic shrinking and arbitrary app/site coverage are not provided.
