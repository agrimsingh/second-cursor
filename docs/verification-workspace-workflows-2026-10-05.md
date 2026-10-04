# Workspace workflow verification — 2026-10-05

The new suite passed nine scripted UI cases across three data seeds and three model-driven cases on a fresh seed. It tests context gathering, conflicting records, policy application, persistence, and recovery through Second Cursor's browser tools. These are synthetic business workflows, not public benchmark scores or a general reliability estimate.

## Coverage

| Workflow | What completion requires |
| --- | --- |
| Invoice reconciliation | Find the correct invoice among similar records, reconcile the latest receiving message with purchase-order pricing and policy, then save the exact amount, route, and memo. |
| Support handoff | Resolve old and current conversations, find the current Stable release, apply policy-specific owners and statuses, and update both requested tickets without touching other records. |
| Save recovery | Handle a late receiving correction that invalidates the first save, commit the revised result, then verify persisted invoice state after a simulated lost response without submitting again. |

Seeds vary real case data and display order. The model receives a goal, not the solution or a prescribed sequence. Both the scripted solver and the model operate on rendered UI through Second Cursor. A separate judge checks durable state and a replayable event ledger, including unintended writes and duplicate attempts. See the [acceptance contract](acceptance-workspace-workflows.md) for exact failure conditions.

## Verified results

| Check | Result | Local evidence |
| --- | --- | --- |
| Python benchmark and harness regression suite | 321 tests passed; the final blank-tab setup adjustment also passed all 14 integration tests | `build/workspace-python-tests-final.log` |
| JavaScript extension regression suite | 74 tests passed, including seven accessible-label cases | `build/workspace-extension-tests.log` |
| Release app build and signing verification | Passed; packaged extension files match the tested source | `build/SecondCursor.app` |
| Scripted workflows, seed 1 | 3/3 strict passes with independently correct outcomes | [tasks](../e2e/artifacts/best-in-class-tasks-20261004T162738.018981Z/tasks.json) |
| Scripted workflows, seeds 2 and 3 | 6/6 strict passes with independently correct outcomes | [tasks](../e2e/artifacts/best-in-class-tasks-20261004T163207.660661Z/tasks.json) |
| Quiet gate integration, seed 1 rerun | Selected `workspace-workflows` suite passed; this repeats three cases and is not added to the nine distinct cases | [gate report](../e2e/artifacts/best-in-class-20261004T163300.214833Z/report.md) |
| Existing live browser contracts | 4/4 passed: incorrect-target refusal, supported editors, focus-change submission refusal, and exact dropdown selection | [report](../e2e/artifacts/20261005-003328/report.md) |

The model cohort used Opus 5.5 Fast Low through Cursor SDK 1.0.35, with 300k context, a 100-tool-call budget, and a 300-second deadline per task. Seed 41 was separate from the scripted development cases. Every task passed both independent outcome checks and all execution checks.

| Task | Adapter wall time | Tool calls | Durable write evidence |
| --- | ---: | ---: | --- |
| Invoice reconciliation | 55.83 s | 19 | One attempt, one correct commit |
| Support handoff | 68.29 s | 29 | Two attempts, two correct commits |
| Save recovery | 83.73 s | 31 | One stale refusal followed by one correct commit; persisted invoice reread after the lost response |

The model recovered from ten failed tool calls across the three tasks (two, four, and four respectively), including unsupported reference/argument forms and stale or unmatched locators. These attempts are included in the tool-call counts. Correct final outcomes do not imply error-free execution; the traces retain this tool-interface friction for follow-up.

There were no duplicate submissions, unrelated-record changes, or adapter/supervisor cleanup errors. All three owned fixture browsers were confirmed terminated. Timings include adapter binding, model/tool execution, and adapter cleanup; they exclude harness browser startup and termination. Provider inference time and dollar cost remain unavailable. Scripted headless timings and model headed timings are not pooled or ranked against each other.

Model evidence: [cohort report](../e2e/artifacts/model-pilot-20261004T162820.417239Z/report.md), [aggregate](../e2e/artifacts/model-pilot-20261004T162820.417239Z/second-cursor.tasks.json), and [underlying tasks with traces and durable judges](../e2e/artifacts/best-in-class-tasks-20261004T162820.503968Z/tasks.json). Artifacts are retained locally and ignored by Git.

## Bugs exposed during development

The first live workflow exposed a product bug: compact browser snapshots discarded explicit accessibility labels when part of the name overlapped visible text. Several distinct controls appeared as just “Open message.” The extension now retains distinguishing accessible names while preserving useful visible content and existing sensitive-value redaction. Seven focused tests, all three workflows, and the existing live browser contract suite cover the change.

The first model cohort hit a fixture bootstrap bug: navigating its claimed blank tab to the workspace received HTTP 403 before any writes. Those three failed trials remain in [their original cohort](../e2e/artifacts/model-pilot-20261004T162438.998772Z/report.md), and are not treated as model reasoning failures or silently merged into the successful cohort. The fixture now permits exact-host top-level navigation to its static shell while retaining API and mutation restrictions. Two HTTP regression cases cover the boundary, and scripted setup now uses the same blank-tab navigation path. Earlier scripted locator/wait failures and an interrupted development batch also remain in their original artifacts rather than contributing to passing counts.

## Reproduction and limits

```sh
SC_E2E_APP=build/SecondCursor.app python3 e2e/workspace_workflows.py --rounds 3 --seed 1
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py gate --profile quiet --suite workspace-workflows
SC_E2E_APP=build/SecondCursor.app python3 e2e/workspace_workflows.py \
  --model claude-opus-5-5 --model-host cursor-sdk --reasoning-effort low \
  --model-param effort=low --model-param fast=true --model-param context=300k \
  --rounds 1 --seed 41 --max-steps 100 --timeout 300
```

Model execution requires the existing Cursor SDK authentication and an available ordinary desktop. Add `--list` to the workspace command to inspect its invocation without launching a browser or using a model.

The tested candidate was built from base commit `79efea71ca8d220ed273df8e8b02691ea1d6cd71` plus the recorded working changes. The native executable SHA-256 was `d8af135f305171038dcfec1d2b1e03f968da810d23200e481f6d8974ae955c02`; the effective extension SHA-256 was `7851781edfd2e25e9c43f7f5de14d95c402279f162548a0adfe8a7c77e30fabb`. Artifacts also pin source fingerprints, model configuration, and Chrome for Testing 152.0.7977.75. This verification used disposable runtimes and did not replace the installed application.

One model run per task demonstrates this path working, not a statistically reliable success rate or a model ranking. The suite covers multiple views inside one synthetic browser app; it does not yet cover real native-app handoffs, arbitrary websites, downloads/uploads, or extended use while a person works. Coexistence checks sample task endpoints and cannot rule out transient interference. The lost response is an application-level simulation, not an SDK process crash; that separate recovery path is covered in the [earlier workflow verification](verification-text-workflow-2026-10-04.md).
