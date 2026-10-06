# Arc-inspired adaptation experiment

This experiment compares the committed Second Cursor baseline (`b0a4c22`) with independently implemented ideas from Arc (`6ca19d62c95106732fad28f488ecd458c08e02f4`). The goal is better observed behavior on messy tasks; smaller call-return times alone do not count as an improvement.

## Frozen baseline and execution

The baseline source, extension and signed application are preserved under `build/arc-eval/baseline/`. Its main executable SHA-256 is `a5cbc3dbdc32f44febbbf0edc840ab2ca60dfb6a232cacfd65efd56da5d50e83`. Raw evidence is local and ignored by Git. Candidate native builds and both extensions are pinned separately. Every GUI run uses disposable application state and browser profiles; desktop runs are sequential. The installed application is not the candidate under test.

## Acceptance before measurement

1. Browser settling must observe an action's delayed request/render without waiting for unrelated requests already in flight. Failure, navigation, detach, frame changes and deadlines must remain bounded. A deadline is not proof of successful execution, and waiting must never repeat an uncertain write.
2. Optional native visual settling must detect meaningful custom-drawn changes without repeatedly running OCR. Small meaningful changes and a blinking caret/continuous animation are separate cases. Target-only focus handling, input takeover, and operation reconciliation remain authoritative.
3. Optional host-side constrained decisions must use observation-derived legal choices and caller-supplied literal values, with independent completion verification and explicit action/no-progress bounds. Model assertions alone cannot pass a task. Durable resume must preserve budgets and cannot replay uncertain mutations.
4. New fixtures retain call-return time, first independently observed effect, returned-observation completeness and final outcome separately. A slow correct response can improve reliability; an early incomplete response cannot win on speed.

## Cohorts and interpretation

Run identical settling fixtures against baseline and candidate, with repeated cases and preserved failed trials. If the upstream Arc driver can be bound to the same isolated visible browser and observation contract, report that narrow reference separately, including unsupported cases.

Run a fresh matched cross-app model cohort using Sol 6.1 high, the same goal-only task, public MCP tools, independent browser ledger and saved-file judges, and synthetic foreground canaries. Seed 201 is a development pilot; the final cohort uses unseen seeds 301 and 302 in baseline/candidate then candidate/baseline order after code and fixtures freeze. Freeze code before each candidate cohort; any repair receives a new cohort identity and keeps the earlier failures. These two seed pairs are regression and exploratory latency evidence, not a model ranking or general reliability estimate.

Run the optional bounded host mode through a real model and real runtime, comparing equivalent goals and independent judges where feasible. Any narrower subtask or schema/prompt difference must be explicit. Do not attribute wall time minus tool time to model inference, and do not infer native-client interoperability from a test host alone.

Keep an adaptation only when its focused tests and relevant live checks support the claimed behavior. Experimental features stay opt-in when coverage or latency tradeoffs do not justify a default. Document rejected ideas and unresolved limits with the final evidence.
