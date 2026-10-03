# Model orchestration efficiency acceptance

Failure cases declared before implementation and live experiments:

1. Moving observations outside the stopwatch creates a false speedup. Upfront observations must use the same scoped driver session inside the adapter deadline; every read remains in tool timing and the total call budget.
2. Cached fixture answers or another session's element IDs leak into the prompt. Read only the two authorized windows through normal driver calls, preserve returned content, and keep all freshness and target checks.
3. A profile silently changes model, reasoning, task, runtime, or safety. Pin the observation mode throughout plan, request and result, hold all other settings fixed, alternate execution order, and reject mismatched evidence.
4. A successful intermediate action becomes a false task completion. Report verified completion only when exact final state appears in a tool response and the independent judge later passes; otherwise leave the metric unavailable. Keep full adapter wall time separately.
5. Failures disappear from the speed comparison. Retain every scheduled trial and interruption, report success alongside latency, and distinguish model-requested calls from upfront reads and total tool attempts.
6. Prompt reduction deletes screenshots, uncertainty, or safety context. Preserve raw observation content; a text-only prefetch implementation must explicitly reject unsupported non-text observations rather than silently dropping them.
7. Hiding standalone action definitions breaks nested validation or removes an operation. The compact catalog must retain the complete typed batch schema, all observations, and action safety instructions, prove every hidden action and argument is represented, and leave the engine's original execution allowlist intact. Compare full versus compact catalogs with upfront observations enabled in both, as a separate experiment.

Verify with offline malformed-evidence and budget tests first, then fresh disposable Calculator/TextEdit trials using `gpt-6.1-sol` at `high` reasoning. The fixed task is an integration experiment, not a general computer-use ranking.
