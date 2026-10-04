# Workspace workflow acceptance

These synthetic workflows test whether a solver can finish ordinary work across several views, preserve unrelated records, and recover from an uncertain save. They are adapted practical coverage, not a reproduction of an external benchmark or evidence of a leaderboard position. Passing the server's task judge establishes the saved business outcome; the runner separately reports execution, cleanup, and desktop coexistence checks.

## Cases and failure conditions

The case contract defines these failure conditions before model evaluation:

| Task | Required work | Conditions that must fail |
| --- | --- | --- |
| `workspace_reconcile` | Reconcile the named invoice using its order price, accepted quantity from the newest relevant message, capped shipping, latest credit, routing threshold, and exact reconciliation memo. Save once. | Untouched invoice; invoice price used instead of order price; old or unrelated message; wrong amount, route, memo, or record; extra committed edits. |
| `workspace_handoff` | Update both named tickets using each newest conversation, the policy's status-to-owner mapping, and the current Stable release. Preserve the other tickets. | Only one completed ticket; stale conversation; Preview or Retired release; wrong owner, status, or note; edits to similar unrelated records. |
| `workspace_recovery` | Reconcile an invoice, handle a late receiving correction, then inspect the saved invoice after its response is lost. | Skipping the stale conflict, retaining the old amount or memo, missing the persisted-state reread, or submitting again after the successful commit. |

Each seed deterministically changes actual record IDs, names, amounts, owner assignments, release versions, and display order. Similar unrelated records and older messages remain visible. Seeds are case variants, not model sampling seeds. The goal names the records and desired outcome without prescribing the answer or a navigation sequence. The owner selector contains the full roster in alphabetical order, including distractors, so option position does not reveal the policy's assignments.

The recovery fixture publishes its correction on the first otherwise valid target save, increments the invoice revision, and refuses that save with `409 stale_revision`, including when the submitted revision was guessed in advance. A subsequent save of the current record commits durably, then returns a simulated `503 response_lost`. The solver must reread Invoices after that response; reading only Inbox or Activity does not verify the saved invoice. This is an application-level lost-response simulation, not a process crash. The separate [invoice workflow acceptance](acceptance-invoice-workflow.md) covers killing and restarting an SDK process around a checkpointed submission.

## Evidence and independent judgment

`WorkspaceFixture(task, seed, artifact_dir)` serves `/fixture.html` and exposes `.url`, `.prompt`, `.case_metadata()`, `.read()`, and `.judge(snapshot)`. The runner captures one durable snapshot, passes that same snapshot to the judge, and retains both the snapshot and boolean checks. A later read cannot silently change what was judged.

The fixture writes a baseline, final state, safe case manifest, and hash-chained event ledger to the trial directory. State writes use fsync and atomic replacement; every application write attempt, refusal, committed transition, review, and section read is recorded. A retained server receipt detects ledger modification or truncation. Judgment replays transitions from the baseline and compares the complete final state with an independently constructed expected state, including all unrelated records. Missing, malformed, wrong-case, reordered, or corrupted evidence cannot pass. Evidence survives server shutdown; a trial cannot overwrite an existing case directory.

All eight judge predicates must hold: durable evidence validity, ledger replay, exact final state, exact committed effects, unrelated-record preservation, absence of duplicate submissions, and the recovery conflict and persisted-state reread when applicable. Correct final values do not excuse a wrong committed edit that was later repaired. Even a refused write to an unrelated record fails preservation, and any target submission after its first commit fails the duplicate check, including a stale replay that caused no second effect.

Harmless field-validation refusals before a target's first commit are allowed. They remain visible in `write_attempts`, `validation_refusals`, and `stale_refusals` diagnostics; correctness does not require an arbitrary exact number of precommit attempts or an exact reading sequence. The recovery conflict is mandatory and separately checked.

## Browser boundary

The browser sees ordinary business views through `GET /api/view?section=...` and submits flat forms to `POST /api/action`. There is no evaluator endpoint, evidence-file route, expected-answer payload, or hidden solution rendered in the page. Expected values stay inside the independent Python judge. Model and scripted browser solvers must use rendered UI; direct API access in unit tests validates the application and judge, not computer-use capability.

Mutations require the exact loopback Host and Origin. Cross-site API requests, unsupported endpoints, transfer-encoded bodies, non-JSON content, nonfinite numbers, and bodies larger than 16 KiB are refused. The static `/fixture.html` shell may be opened from another site or browser extension only as an exact-Host, top-level document navigation (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`); this exception does not apply to API routes, query variants, iframes, or POST requests. Application fields are explicitly enumerated: integer cents and revisions, listed routes/statuses/owners/releases, and a nonempty note of at most 500 trimmed characters. Review is read-only. Every save checks the current record revision before committing, except for the deliberate first-save recovery conflict described above.

## Offline verification

Run `python3 -m unittest discover -s e2e/benchmarks -p 'test_workspace*.py'` for the fixture, independent falsification, and runner integration tests. These tests cover unchanged baselines, correct seeded outcomes, partial handoff, wrong records and values, safe validation retries, duplicate submissions, repaired bad commits, recovery ordering, stale answers, persistence, evidence tampering, and browser boundary refusals. They start only disposable local fixture servers; they do not establish live model or desktop success. Record live UI and model results separately with their pinned runner, model, browser, seed, and artifact metadata.
