# Model runner and engine adapter contract

Raw verification evidence under `build/` and `e2e/artifacts/` is local and Git-ignored; the repository-relative paths below are not published on GitHub.

## Shared model runner

By default, `e2e/model_benchmark.py` runs Second Cursor and Cua Driver through the same Codex app-server host implementation, installed host binary, account, model, and reasoning setting. Each trial gets a fresh app-server process, ephemeral conversation, and disposable fixtures. The selected pilot configuration is `gpt-6.1-sol` with `high` reasoning. Cua Driver was updated to 0.33.0 for this comparison; every run records and checks the actual driver version. The optional [Cursor SDK host](../cursor_host/README.md) supports explicit Grok Fast variants with a separate pinned host/model identity; cross-driver comparisons still require identical configurations.

From the repository root, inspect the exact schedule without starting a model or touching the desktop:

```sh
SC_E2E_APP="$HOME/Applications/SecondCursor.app" python3 e2e/model_benchmark.py \
  --engine both --model gpt-6.1-sol --reasoning-effort high \
  --rounds 3 --seed 1 --native-placement desktop --max-steps 40 --timeout 300 --list
```

Run that schedule by removing `--list`:

```sh
SC_E2E_APP="$HOME/Applications/SecondCursor.app" python3 e2e/model_benchmark.py \
  --engine both --model gpt-6.1-sol --reasoning-effort high \
  --rounds 3 --seed 1 --native-placement desktop --max-steps 40 --timeout 300
```

With no `--task` filter, both commands select `native_notes`, `browser_work_order`, `canvas_route`, and `transient_dialog`: two engines × four tasks × three rounds = 24 trials. Engine order alternates by round, Second Cursor/Cua then Cua/Second Cursor, followed by Second Cursor/Cua. Seeds identify repeated fixed cases; they do not randomize fixtures or seed provider sampling. The runner defaults to a 240-second trial budget; the commands above explicitly use 300 seconds for both engines. Adapter-owned setup consumes part of that budget, cleanup is bounded separately, and the 40-step limit counts model-requested fixture tool calls. `--list` reads local versions, binary hashes, and fixture metadata but does not run trials.

The shared runner defaults to native fixtures on the ordinary desktop and headed Chrome for Testing for both engines. Each browser trial gets a fresh harness-owned profile, process, and active `about:blank` tab. The plan pins `browser_visibility` and `browser_identity` (absolute executable path, version, and SHA-256); each adapter verifies the live process executable and stable process identity before connecting. Second Cursor claims exactly the sole blank tab and cannot open another one. Cua binds the supplied PID/window through public `get_browser_state`; a refused binding never substitutes a different browser. The harness owns browser termination.

The matched harness now starts browser fixtures before the adapter timer for both engines. Engine binding, model execution, and adapter cleanup remain inside measured adapter wall time and are separately traced; harness browser startup/termination are outside it. This measures the integration path after fixture readiness, not equal cold-start cost or pure driver latency. Earlier cohorts used a different boundary and browser build/visibility, so their times cannot be combined with this profile.

The implemented track is `host-background` with the `native-tools` interface: each backend retains its own task-scoped tool schemas and observation formats. Scoped text and screenshot content reach the model through the same host. The general comparison contract also names `screenshot-actions`, but this shared adapter does not implement that track. Neither isolated VMs nor Cua Spaces are part of this runner.

Cua's browser adapter permits a native AX fallback on the exact fixture window, restricted to observed controls beneath its `AXWebArea` or attached sheet/dialog. Attached containers must descend from that same `AXWindow` and have role `AXSheet`/`AXDialog` or subrole `AXDialog`/`AXSystemDialog`. Native fallback actions require descendant control tokens and the exact PID/window; container tokens, unattached dialogs, browser chrome, unscoped native keys, and native pixel actions are unavailable. Cua `get_window_state` can include global Apple/app menus, so the adapter projects its AX rows and regenerated text to the fixture window for native tasks, or its WebArea/attached-modal subtrees for browser tasks. `structuredContent.benchmark_scope_projection` labels the scope (`fixture_window` or `fixture_web_area_or_attached_modal`) and returned/omitted counts. Original element handles, metadata, counts, and non-text blocks remain unchanged, including the entire target-window screenshot; images are not cropped to the retained AX subtrees. Other tool responses retain their engine content. This preserves scoped observations without presenting filtered text as an unmodified raw response.

`benchmarks/model_host.py` uses the installed Codex CLI's normal authentication without reading or copying credentials. It starts in an empty temporary directory with `environments: []`, no workspace roots, inherited MCP servers explicitly disabled, and non-fixture features disabled. A private model catalog selects direct tool mode and removes metadata that would otherwise re-enable code mode, agents, search, or plugin tools. The audited host version is currently `codex-cli 0.159.0-alpha.12.1`; any other version refuses until its outgoing tool catalog is audited and allowlisted. Each trial checks the returned model/effort, configuration, and empty inherited MCP inventory, and rejects model fallback. The host still exposes `request_user_input`; every such request is denied. Only the selected fixture tools are executable by the solver. The user's global `$CODEX_HOME/AGENTS.md` writing instructions remain shared context for both engines; exactly that global path is allowed, and repository or other instruction sources are refused. Empty environments do not mean a completely blank prompt.

This is a scoped engineering harness, not an OS sandbox. `benchmarks/engines.py` restricts targets, URLs, gestures, and public engine tools to the trial fixtures; setup and cleanup are host-owned and separately traced. The model cannot access shell, filesystem, evaluator endpoints, personal connectors, or other MCP tools. Both engines use disposable browser profiles on the host desktop, not a separate GUI session.

Artifacts under `e2e/artifacts/model-pilot-*` include the pinned plan, alternating schedule, child logs, per-engine task artifacts, independent judgments, and model/engine traces. Token usage is captured from app-server usage events when available. `model_ms` and `cost_usd` remain `null`: the host does not expose measured provider-only time or an actual charge. Wall time minus tool time is not model time, and account usage is not a dollar cost.

The comparison validator requires matching execution profile, model configuration (identity, reasoning, tool budget, timeout, and host version), task/fixture and runner fingerprints, interface/execution tracks, native placement, browser visibility and executable identity, task selection, and task/trial/case-seed schedule. Each engine also needs its pinned version; the runner validates child evidence against its original plan and preserves missing or invalid trials in the denominator. Failures, refusals, unsupported tasks, and inconclusive coexistence checks are never silently discarded.

Reports separate the independent task outcome from **all checks passed**. A trial's `independent_task_success` is `true` or `false` only when the native-state or event-ledger judge ran; an absent judgment remains unknown. A correct task can coexist with a failed execution check or inconclusive pointer/focus evidence, so independent success does not change the strict trial status. Aggregates report `independent_verified`, `independent_measured_trials`, and the full trial denominator; timing and cost metrics likewise show their measured sample counts. Task-level coexistence checks sample endpoints, and the attended focus suites provide separate transient-interference evidence. No live pilot result is implied by the presence of this implementation.

The earlier cohort's interruption record (`e2e/artifacts/model-pilot-20261003T192234.227734Z/interrupted.json`) identifies a headless Second Cursor fixture versus a headed Cua fixture; their browser builds also differed. Keep that cohort as integration diagnostics, not a fair driver comparison. The current matched Chrome for Testing binding probe remains refused by Cua with `browser_consent_required`, including after public preparation returned `already_prepared`. One Second Cursor visible-browser task smoke passed with its own browser frontmost; it establishes task completion, not background coexistence. See [the current evidence and pinned browser](../../docs/benchmark-plan-2026-10-04.md#current-evidence); no completed matched cohort is established yet.

## External adapter protocol, version 1

`best_in_class.py tasks` runs a deterministic Second Cursor adapter by default. A different engine or model host can implement the same contract as a process supplied with `--adapter-command '["executable","argument"]'`. The command is an argv JSON array, never a shell command. The harness starts a fresh process per trial and sends exactly one JSON object to stdin:

```json
{
  "protocol": "second-cursor-benchmark/1",
  "task_id": "browser_work_order",
  "trial": 1,
  "track": "host-background",
  "native_placement": "desktop",
  "model": "the explicitly selected model or null",
  "prompt": "The complete task and permitted interaction rules.",
  "targets": {"url": "http://127.0.0.1:PORT/fixture.html?task=form"},
  "timeout_seconds": 180
}
```

Native tasks give `calculator_window_id`, `notes_window_id`, their process IDs, and the original note text instead of a URL. The harness places these fixtures according to `native_placement`, and the adapter must reach those same windows through its own engine. The shared model runner defaults to `desktop`; the lower-level `best_in_class.py tasks` command retains its `agent-display` default unless explicitly changed. Browser tasks give a fresh local URL; the matched headed model profile also supplies `browser_pid` and `browser_window_id`, while the harness owns the disposable profile/process. Lower-level external adapters without this matched profile manage their own tabs/profiles and cannot be mixed into its results. The shared Second Cursor adapter reads only the disposable runtime's connection configuration; credentials stay in the host and never enter the model prompt or trace. A remote or isolated machine needs a deliberately configured route to the fixture server; it cannot be compared in the host-background track. The implementation supports host-background adapters only, and rejects an isolated track until equivalent fixture placement and independent observation exist.

Model-driven requests additionally carry `engine`, `engine_version`, `interface_track`, `case_seed`, `runner_sha256`, `browser_visibility`, `browser_identity`, and a `model_config` object with `model`, `reasoning_effort`, `max_steps`, `timeout_seconds`, and `model_host_version`. Native-only plans keep `browser_identity` null. The shared adapter rejects mismatches between these fields and the pinned plan before a solver starts. The deterministic adapter remains available through `best_in_class.py tasks` without a model configuration.

`observation_mode` is also pinned, defaulting to `on-demand`. The opt-in `upfront` mode supports Second Cursor's `native_notes` task: it reads both windows through the same scoped session before invoking the model, includes the raw observations as untrusted prompt data, and subtracts those two reads from the model's remaining call budget. The stopwatch includes these reads. Unsupported non-text initial observations fail explicitly; normal model tool responses retain image support. The ordinary comparator rejects mixed observation modes; `model_efficiency.py` explicitly compares them in a matched alternating schedule.

`catalog_mode` defaults to `full`. The opt-in `compact` native catalog advertises `batch`, `get_window_state`, `inspect_element`, `find_elements`, `screenshot`, and `wait_for`. It retains the complete batch schema and standalone action instructions after checking typed argument coverage. The engine keeps its full allowlist for nested validation; only the model's advertised definitions change. Adapter results retain catalog hashes, advertised names, and canonical byte counts. `model_efficiency.py --experiment tool-surface` compares this profile against the full catalog with upfront observations held constant; the ordinary comparator rejects mixed catalog modes.

`model_sweep.py` separately compares Grok 4.5 Fast low/medium, Grok 4.7 Fast medium, and Sol 6.1 medium/high through Second Cursor. Grok uses the pinned Cursor SDK host; Sol uses the audited Codex host. Install and authenticate the [Cursor host](../cursor_host/README.md) first. Run `python3 e2e/model_sweep.py --rounds 3 --timeout 90 --list` from the repository root to inspect the native schedule; remove `--list` to execute it. Browser coverage uses `--rounds 1 --task browser_work_order --task transient_dialog`. Native-only plans supply upfront observations; browser or mixed plans use on-demand observations. Every profile in a cohort receives the same observation mode, full tool catalog, budgets, fixtures, and driver binary.

The sweep alternates profile order across rounds and explicitly permits only its declared model, host, effort, model parameters, and host-version differences. Each child still passes the ordinary runner's exact-plan validation. `model-sweep-*` artifacts retain all scheduled cases, partial evidence, per-profile reports, and per-task summaries. Failed, inconclusive, and missing cases stay in the denominator; only strict passes with independently verified task success contribute to latency. Cross-host results measure the complete integration, not isolated inference speed. See [the acceptance criteria](../../docs/acceptance-model-sweep.md) and [the initial comparison](../../docs/verification-model-sweep-2026-10-04.md).

Normalized tool events carry `origin` (`model` or `host-prefetch`), `call_id`, and `arguments`. Terminal evidence reports `model_tool_calls`, `prefetch_tool_calls`, and `driver_tool_calls`; these are attempted calls, including rejected attempts, not a count of successful low-level input events. Host milestones measure local observations of startup and model activity, not provider inference time. Verified-completion timing is available only when the final tool response proves the exact expected note, the Calculator result was observed, and the later independent judge and coexistence checks pass. Its clock begins at adapter entry; full harness-measured process time remains a separate metric.

With explicit `--off-space`, `native_placement` is `textedit-off-space`: TextEdit stays on its ordinary inactive Space, while Calculator retains the agent-display setup. The harness verifies TextEdit remains off screen and outside the agent display before and after execution. Default placement and task-result checks are unchanged, and comparison requires matching native placements.

Use only the engine's normal computer-control tools to complete the task. Do not call fixture endpoints directly, run JavaScript to set application state, read or edit fixture files, or use the shell to complete a task. For canvas drawing, deliver a real pointer gesture; a DOM assignment, hidden button, or fake `dispatchEvent` does not meet the contract. The harness judges the final app state and a fixture event ledger independently of the adapter's reported success. This is an engineering benchmark, not a tamper-resistant untrusted-code sandbox; preserve the engine trace for audit.

Stdout contains newline-delimited JSON. Emit one terminal result and any number of tool interval events. Diagnostics go to stderr. `start_ms` / `end_ms` are elapsed milliseconds from adapter process start; include every tool call, including failures and retries. The harness merges intervals before reporting tool time. Report a retry only when the adapter actually retries a previous operation.

```json
{"type":"tool","name":"click","start_ms":12,"end_ms":182,"ok":true,"retry":false}
{"type":"result","status":"completed","model":"provider/model","model_ms":1020,"cost_usd":0.002,"usage":{"input_tokens":50,"output_tokens":20},"pricing_source":"provider invoice or versioned pricing URL","engine_trace":"/path/to/trace"}
```

Only `completed`, `unsupported`, and `failed` are valid terminal statuses. The process must exit zero as well as report `completed`, and independent checks must still pass. Missing terminal results, malformed event JSON, invalid timing intervals, timeouts, nonzero exits, or unsupported tasks cannot pass. `model_ms`, `cost_usd`, and `usage` are optional and remain `null` when unavailable. A reported cost must include `pricing_source`; a reported model must match `--model`. Model time must come from actual model request timing, not wall time minus tools. Tool time and provider metrics from an external adapter are explicitly self-reported. The process runs in its own process group, which is terminated on completion or timeout so helpers do not leak.

The harness writes the request, redacted adapter events, stderr, independent result evidence, timings, source/bundle identity, and coexistence invariants for every trial. Never log credentials in adapter telemetry. Model comparisons enforce the full configuration and schedule checks described above; a matching model name alone is insufficient. A deterministic adapter has no model usage and its cost is unknown, not zero.
