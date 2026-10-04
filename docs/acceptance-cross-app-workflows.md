# Cross-app interruption acceptance

This suite combines a seeded browser reconciliation task with an existing native
document and continuous foreground activity. It is a scripted control-path test,
not a model reasoning score or proof of physical keyboard-input detection.

## Required outcomes

1. Derive the invoice amount, route, and memo from rendered policy, purchase-order,
   and latest-message context. A late correction must invalidate the first save;
   the committed save's unavailable confirmation must be reconciled by reading
   persisted state. Preserve unrelated records and reject duplicate submissions.
2. Transfer the verified browser result into an existing disposable TextEdit
   document through public native tools. Preserve its original content and any
   injected concurrent edit. Independently verify the saved file, not just the
   tool's success response.
3. Pause the named task at an edit boundary, modify only the disposable native
   document externally, and resume. Input while paused and input using the old
   observation after resume must fail without changing that document. Observe
   again after resume and rebuild the append from the current content.
4. Continue synthetic foreground text activity in a separate disposable native
   app while the agent works. Record focus, key-window, responder, and text-change
   events throughout the trial. Deliberate foreground-window switches must be
   explicit acknowledged events; no general grace period may hide other losses.
5. A temporary focus theft followed by restoration or wrong-target canary text
   followed by repair must fail the event judge even if the final screen looks
   correct. Duplicate native append dispatches and duplicated persisted summary
   blocks must also fail. Validate these failure cases with negative controls;
   native dispatch counting is not a complete TextEdit mutation history.
6. Retain the seed, disruption schedule, tool transcript, independent browser
   ledger, native file, and canary events for failures as well as successes.
   Missing, truncated, skipped, or inconclusive evidence cannot count as a pass.

## Boundaries

Only disposable profiles, records, apps, and documents may receive test input.
Foreground takeover requires the explicit `--takeover` flag. Restore the previous
foreground app and clean up fixture processes after a run, including failures.
Run GUI suites serially because the browser bridge and foreground desktop are
shared resources.

Native element references identify controls, not immutable text values. The stale
plan in this test is invalidated by task pause/resume; a concurrent text edit by
itself does not provide a compare-and-swap guarantee. Synthetic in-process text
events exercise routing and preservation, not the physical human-input monitor.
The browser's stale revision and lost confirmation are application-level faults;
the separate invoice checkpoint suite covers a real SDK process death after a
committed submission. Report these scopes separately.

The AX snapshot privacy regression must scan both the outline and structured
snapshot output for synthetic sensitive markers, including values hidden behind
overridden native labels and options inside sensitive selects. Ordinary fields
and ordinary choices must remain readable.
