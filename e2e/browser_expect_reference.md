# Arc reference for browser postconditions

`browser_expect_reference.py` runs the shared persisted-record fixture through
unmodified Arc `ChromeBackend` at commit
`6ca19d62c95106732fad28f488ecd458c08e02f4`. It covers delayed saves and delayed
review dialogs at 1.5 and 2.5 seconds, using the same fixture seeds as Second
Cursor. This is a deterministic driver comparison; no model is involved.

```sh
# Verify the existing optional environment and browser without launching a window.
e2e/.bin/arc-reference/bin/python e2e/browser_expect_reference.py

# Run only when other GUI fixtures are idle.
e2e/.bin/arc-reference/bin/python e2e/browser_expect_reference.py --run --repeats 3

# Unit checks do not launch any GUI.
e2e/.bin/arc-reference/bin/python -m unittest discover -s e2e -p test_browser_expect_reference.py -v
```

See [arc_reference.md](arc_reference.md) for installing the pinned, isolated
optional environment. `--case delayed_save` or `--case delayed_modal` selects a
workflow. Repeated `--seed` arguments select exactly matched fixture inputs; the
default seeds 402 and 403 imply 1,500 and 2,500 milliseconds respectively.
`--headless` is diagnostic only and is not comparable to the visible cohort.

Each stage selects exactly one visible, enabled target from a fresh Arc
observation and uses upstream `materialize_action` to preserve its semantic
guard. After the single click, the adapter polls full Arc observations every
100 ms until the expected visible text appears or the six-second deadline
expires. An uncertain or failed click is never repeated. Opening the delayed
review and confirming it are distinct permitted stages, and confirmation only
runs after the review was observed.

This explicit polling gives Arc the same task objective as Second Cursor's
`expect`: return when the requested condition has been observed. It does not
invent an upstream `expect` API, change Arc's source, or score an early settled
observation as completed work. The adapter calls the backend directly, so it
does not additionally invoke Arc runtime settling before its explicit
confirmation reads.

`verified_task_ms` includes everything from the first click dispatch through the
final confirming observation: guard checking, execution, every poll, and later
stages' target observations. `public_calls` counts the corresponding backend
`execute` and `observe` calls. The initial target observation is excluded to
match the Second Cursor fixture's boundary; `observe_act_confirm_ms` includes
it as well. Launch, fixture navigation, public-state preflight, and independent
judge collection are outside both intervals. These are public driver calls,
not raw CDP messages, model round trips, or interchangeable transport costs:
Second Cursor includes its MCP and extension transport, while Arc uses its
Python backend directly.

The independent server judge replays its persisted hash-chained ledger and
requires one exact commit, one submission, and an unchanged adjacent record. A
separate read-only CDP connection captures the target's visible status before
and after execution, along with its effective viewport and visibility. Those
receipts are outside the timed region; neither driver has concurrent observer
load in this cohort, and these receipts do not establish precise effect timing. Neither judge drives
or repairs an action. DOM/CSS visibility is evidence of layout state, not proof
of compositor presentation. Native coexistence is outside this comparator.

Each run saves `manifest.json`, `browser-expect.json`, the browser log, and each
trial's baseline, final state, and ledger. The manifest records the loaded Arc
package, every installed Arc Python/JavaScript source hash, shared fixture and
harness hashes, actual Chrome binary/version, and launch flags. It verifies
source and browser identities again afterward. Misses, timeouts, and errors
remain in the artifacts and produce a nonzero exit code. An in-flight upstream
observation may overrun the requested deadline; a positive result returned after
that deadline is retained as a failure. Arc's deadline starts at dispatch, while
the SC polling deadline starts after the click and the candidate expectation
deadline starts after input. The fixed successful cases support elapsed-time
comparisons, but timeout success rates are not equivalent across these policies.

The stale-form recovery, second-tab decoy, and focused `expect` metadata cases
are explicitly outside this small reference policy. This is a limitation of
the comparator's coverage, not a claim that Arc cannot perform those tasks.
