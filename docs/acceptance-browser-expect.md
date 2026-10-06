# Browser postcondition acceptance

An action acknowledgement is not a successful save. This fixture compares a public action followed by public read polling against the same action with an explicit browser expectation. The application state is judged separately from the tool's expectation result.

## Repeated workflows

`e2e/browser_expect.py` uses an owned HTTP server and a disposable visible browser. Each trial starts with an assigned release record, its current approved note, and an adjacent record that must remain untouched. Seeds 401, 402 and 403 use 800, 1,500 and 2,500 ms application timers; the requests deliberately start after the timer, when ordinary DOM/network settling may already have returned.

| Workflow | Required behavior | Independent evidence |
| --- | --- | --- |
| Delayed save | Wait for the saved status without submitting again. | One submission, one commit, exact approved note. |
| Stale form | Observe the revision conflict, reload the current record, then save the corrected note. | Exactly two submissions, one conflict, one commit at the current revision. |
| Delayed modal | Observe the review dialog, confirm it, and wait for the saved state. | Exactly one submission and one commit, after the confirmation. |
| Other-tab decoy | A matching success message in a different tab cannot satisfy the target's pending save. | One submission, zero commits, unchanged records, explicit target timeout. |

The old path uses `browser_click` followed by `browser_command(operation="inspect")` on the rendered status, sleeping up to 100 ms between reads. It never repeats a mutation because the status is missing. The candidate supplies `expect` to the action and receives its result in the same MCP call. Both paths take fresh snapshots between workflow stages that can replace controls. Setup and independent final reads are excluded from action-to-terminal-state timing; all intervening public calls are counted. The timeout starts after the old action call returns, so timeout latencies are characterization rather than an exact deadline race.

The server writes an append-only hash-chained ledger and atomic state files. The judge rereads those files, requires the current complete receipt, validates and replays the ledger, compares the complete final state, checks exact submission and commit counts, and rejects any submission after a successful commit. A success-looking UI with the wrong saved note still fails. Incorrectly accepting the decoy still fails even though the expected application state is unchanged. Stale-form recovery also requires a fresh server read between the conflict and successful save.

## Focused contract checks

Candidate-only cases cover preexisting conditions, missing conditions, hidden text, protected text and values, replacement of the referenced node, long exact values and a mismatching suffix, JavaScript dialogs, same-tab navigation, partial batches, no-observation and screenshot-only responses, and type/key/scroll actions. A partial batch must preserve its first action, stop before its last write, and report a failed expectation. JavaScript dialogs must return a prompt failure and remain available for explicit handling. A newly created control with the same value cannot satisfy a condition tied to a replaced node.

Expected condition outcomes remain separate from action dispatch and persisted task success. One final read-only CDP receipt independently checks focused values, UI events, scroll position and navigation after the timed call; it never drives or repairs an action. `preexisting` never counts as proof that an action created the condition. Protected literal values must not appear in result evidence. Before a protected-ref refusal can pass, an ordinary exact-value expectation must work on the same page, so a globally unavailable condition reader cannot masquerade as privacy protection. The refusal must explicitly state that input was not sent, and the independent receipt must agree. Conditions that cannot be evaluated safely fail or are rejected before dispatch; the fixture never retries them. Dialog cleanup first reads whether a dialog is open, and any cleanup failure is retained alongside the original action failure.

## Actual workspace form and real model

`e2e/browser_expect_workspace.py --takeover` exercises the actual messy invoice form, including its native SELECT and initially empty INPUT/TEXTAREA. Exact typing, selection by label and literal value, and an unselected option's presence and failed disappearance must produce the expected condition status. It then clicks the observed **Review invoice** button with an expectation for **Review invoice update**, checks the visible modal and displayed draft independently, and dismisses it through the observed **Back to editing** button with a `gone` expectation. Separate public DOM reads verify that dismissal preserved every draft value and selected option; **Reload invoice** must then restore the original empty form. This covers the actual form, modal and inactive controls together, including browser-generated accessibility structure absent from the simpler fixture.

Reviewing an invoice sends a read-only `review_invoice` request, so the persisted hash-chained ledger permits record reads plus exactly one matching `review` / `reviewed` result pair for the assigned invoice and revision. It must contain no save attempts, commits or external updates, and every persisted record must equal its baseline. Missing, duplicated or wrong-record reviews fail the judge. The regression records all public calls, failed expectations and source identities, so testing a preserved failing bundle and its repair proves the same complete flow was exercised. This is an integration regression, not a latency benchmark.

`e2e/browser_expect_model.py --takeover` starts four fresh Sol 6.1 high conversations in poll/expect then expect/poll order across seeds 401 and 402. Both arms use the same frozen app and extension; only the optional expectation schema and its instructions differ. The model gathers context and derives invoice values from the visible workspace, handles a stale revision and lost save response, and must read back persisted state before completion. The independent ledger judges exact saved values, unrelated records and duplicate submissions. Reports retain model errors, expectation statuses and actual feature usage; task success through plain-input fallback is not evidence that expectations worked. Neither two seeds nor fresh-host timing establish a broad model speed ranking.

## Commands and evidence

Run the frozen old app and extension, then the candidate against the same fixture and seeds:

```sh
SC_E2E_APP=build/browser-expect-eval/baseline/SecondCursor.app \
SC_E2E_EXTENSION=build/browser-expect-eval/baseline/extension \
python3 e2e/browser_expect.py --takeover --mode poll --label baseline --repetitions 3

SC_E2E_APP=build/SecondCursor.app SC_E2E_EXTENSION=extension \
python3 e2e/browser_expect.py --takeover --mode expect --label candidate --repetitions 3 --focused
```

`--list` prints the schedule and source identities without launching a desktop or browser. `--cases` narrows a live diagnostic while preserving the original failed artifacts. Browser/app binaries, extension content, fixture and harness sources are fingerprinted before and after a cohort. Each trial saves its public call trace, independent checks, ledger and persisted state; the top-level JSON retains every measured row and the harness's focus/cursor/Space results. Changed source identities or incomplete scenario coverage prevent a successful run.

These deterministic trials measure the driver and its public contract. They do not establish model intelligence, physical compositor timing, universal website compatibility, or a statistically reliable latency ranking. The paired model comparison and unchanged Arc reference are separate runners with their own comparison limits.

The [verification record](verification-browser-expect-2026-10-06.md) preserves failed development runs, the resulting regressions, frozen comparison results and installation evidence.
