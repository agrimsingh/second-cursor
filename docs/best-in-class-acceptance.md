# Release evidence and task benchmark

This contract defines what the release must demonstrate. A successful build, a tool response containing `ok`, or an agent's final answer is not a completed task. A task passes only when its independent result checks and coexistence checks pass. There is no claim of flawless operation across every macOS app.

## Failure cases defined before the harness

- Two ordinary clients accidentally share observations, target ownership, cleanup, or operation history. A client can change another client's window while it is owned.
- Takeover allows queued agent input through, or resume accepts coordinates from before a human edit. An agent steals a key window, focus, cursor, or Space from the human. Human interference during an unattended measurement is inconclusive evidence, not a pass.
- A same-size reflow, scrolled target, changed modal, or old frame preserves an actionable observation. Unsupported rendering or input capabilities are presented as success.
- An accepted input is reported as verified without checking the requested result. A crash erases an unresolved operation and retry duplicates the action. Reusing an operation ID with different arguments is accepted.
- Parallel or long-running tasks disappear from supervision while they are still active. Task takeover/resume/cancel and setup checks only work through a direct API but fail in the rendered app.
- A browser property change waits for the full fallback timeout, or a faster response precedes its promised observation. A double submission creates two work orders.
- A canvas benchmark succeeds through a hidden semantic control or JavaScript assignment without delivering a pointer gesture. A transient confirmation is accepted accidentally, or cancellation changes the result.
- A report treats a skipped task, zero selected scenarios, a crashed suite, a missing result file, or externally disturbed focus check as passing. A timing report calls all time outside tools model time, or substitutes zero for unavailable model usage or cost.

## Running the gate

Run from the repository root after building the bundle under test. Every suite uses the existing disposable `SC_HOME` and browser profile infrastructure. The runner executes suites serially because independent runtimes still share one macOS desktop. It never selects `--installed`.

```sh
python3 e2e/best_in_class.py gate --list
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py gate --profile quiet
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py gate --profile release --takeover --approvals
```

`quiet` covers unattended runtime and task checks. `release` also requires human-attended takeover, consent, and environment-specific cases. Without the corresponding flags those groups are recorded as `NOT_RUN`, never silently dropped from a release report. `--takeover` permits fixtures that take focus; `--approvals` permits real approval dialogs but never simulates the human approving them. Off-Space cases require the actual documented environment and remain incomplete when it is unavailable. `--suite ID` selects a diagnostic subset and the report explicitly records that reduced scope. A release also needs signed/notarized distribution and rendered setup/task-panel evidence; machine credentials and a human grant cannot be fabricated by this harness.

Each gate writes its commands, source revision and dirty paths, bundle digest, OS version, raw suite logs, scenario evidence, and `report.md` / `results.json` under `e2e/artifacts/best-in-class-*`. Existing per-suite artifacts are linked rather than overwritten. Exit codes are `0` for all selected checks passing, `1` for an observed failure, and `2` for incomplete evidence. `SKIP`, `INCONCLUSIVE`, `NOT_RUN`, missing expected scenarios, and absent artifacts all prevent a clean gate.

## Complete tasks and comparison boundaries

```sh
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py tasks --rounds 3
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py tasks --task native_notes --off-space
SC_E2E_APP=build/SecondCursor.app python3 e2e/best_in_class.py tasks --adapter-command '["python3","/absolute/path/adapter.py"]' --engine cua-driver --engine-version COMMIT --model MODEL --rounds 3
python3 e2e/best_in_class.py compare path/to/tasks.json path/to/other/tasks.json
python3 e2e/best_in_class.py self-test
```

The built-in adapter is a deterministic control-path baseline, not a model benchmark. It performs a Calculator-to-TextEdit task and three browser tasks: create one work order, draw a path on a real canvas, and cancel then accept a transient confirmation. The browser judge reads the local fixture's event ledger; native results are independently read after the adapter finishes. Setup and verification calls are excluded from adapter tool metrics. Each trial has fresh fixtures, preserves the original note, and fails on duplicate final actions. Native raw canvas delivery, stale observations, off-Space rendering, same-app human input, and interrupted execution have their own required gate suites; a browser canvas success does not prove native background dragging.

`tasks --off-space` explicitly leaves the TextEdit fixture on its ordinary inactive Space, which can provide native-task coverage while the human is in a full-screen app. It requires the note window to be off screen, outside the agent display, and on an inactive Space both before and after the task. It does not switch Spaces or relax the independent note-content, Calculator-result, or original-file checks. Calculator retains its existing agent-display setup. The default profile is unchanged; artifacts label this placement `textedit-off-space`, and comparisons reject mixed native placements.

Task trials sample the frontmost app, cursor, and Space before and after execution. A transient change restored between those reads is not observable, so task artifacts explicitly mark transient coexistence unverified. The attended focus suites use event counters and are separate evidence. The same-app gate selects either two-window key-window protection or conservative foreground refusal according to real input-monitoring availability, and records which branch ran. Physical human typing and the unavailable branch remain outside that run's evidence.

The external adapter protocol is in [the adapter contract](../e2e/benchmarks/README.md). The same task prompt, geometry, fixtures, and judge apply to every engine. Drivers operating in the host desktop and isolated desktops are separate tracks; an isolated result is never evidence that the driver coexists safely inside the user's current apps. Cross-engine reports require matching task versions, model, track, and fixture fingerprint, and still identify the limited sample count. Unsupported tasks remain in the denominator.

Reports preserve every trial and show verified completion, median and p95 adapter wall time, tool time, tool calls, reported model time, reported cost, and focus/cursor/Space changes. Tool intervals are merged to avoid double-counting parallel calls. Time outside tools is overhead unless a model provider measures it explicitly. Missing usage, model time, or prices stay `null`; the runner does not infer prices. Adapter self-reported telemetry is labelled as such. For model comparisons pin the model, reasoning effort, prompt, execution track, and engine revision, interleave engine runs, and retain the original provider usage and pricing provenance.

## Claim threshold

A passing unattended gate establishes only the scenarios in that report. A release claim additionally requires the attended and off-Space evidence, rendered task/setup verification, and distribution checks for that exact build. A comparative claim requires running the other engine under the same task contract; a source audit, another author's tweet, or this deterministic baseline cannot establish competitive superiority. Report failures and inconclusive trials alongside successful ones.
