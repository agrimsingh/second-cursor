# Measuring Second Cursor against other computer-use systems

Raw verification evidence under `build/` and `e2e/artifacts/` is local and Git-ignored; the repository-relative paths below are not published on GitHub.

Updated 4 October 2026. The shared model runner and both engine adapters are implemented. The selected pilot uses **gpt-6.1-sol with high reasoning** for Second Cursor and Cua Driver on the same Mac. The earlier browser cohort was interrupted because browser visibility and builds differed; the replacement profile pins the same headed Chrome for Testing build and defaults native fixtures to the ordinary desktop. Cua's matched-browser binding currently refuses on consent, so no completed matched cohort supports a comparison yet.

## Current implementation

The earlier Second Cursor installation checkpoint is recorded in the installation record (`build/installed-verification-20261004.json`), with its [exact-build regression evidence](verification-2026-10-04.md). The runner fingerprints the selected `SecondCursor.app` executable for each plan rather than assuming that a mutable installation path still identifies that earlier build. Cua Driver was updated from 0.28.2 to **0.33.0** during this work; the adapter checks the actual CLI version, records its binary hash, and checks daemon-version consistency when the daemon reports a semantic version.

`e2e/model_benchmark.py` schedules repeated, interleaved runs. `benchmarks/model_adapter.py` connects the independent fixture harness to `benchmarks/engines.py` and the shared `benchmarks/model_host.py`. Both adapters use the same installed Codex app-server host and normal account authentication, with no credentials read or copied by the runner. Each trial starts a fresh app-server process and ephemeral conversation; it receives that trial's prompt, permitted target identifiers, and the selected engine's scoped tools.

The host starts with `environments: []`, no workspace roots, configured MCP servers explicitly disabled, and shell, filesystem, browser, connector, memory, skill, and other non-fixture features disabled. A temporary model catalog forces direct tool mode because model metadata can otherwise restore unwanted tools despite feature flags. The solver can execute only fixture tools; `request_user_input` is still exposed by this host version, but every request is denied. The user's global `$CODEX_HOME/AGENTS.md` writing instructions remain shared context for both engines; exactly that path is allowed, while repository and other instruction sources are refused. This is not a completely blank prompt or an OS sandbox.

The outgoing tool catalog has been audited for `codex-cli 0.159.0-alpha.12.1`; other versions refuse until separately audited and allowlisted. Runtime checks also enforce the chosen model/effort, disabled capabilities, empty inherited MCP inventory, and no model fallback. The gpt-6.1-sol/high handshake (`build/model-host-sol-high-handshake.json`) completed one `fixture_probe` call and recorded usage. It is model-host evidence, not a desktop task or competitor score.

## Current evidence

The interrupted cohort (`e2e/artifacts/model-pilot-20261003T192234.227734Z/interrupted.json`) used headless Chrome for Testing for Second Cursor and visible system Chrome for Cua. That mismatch invalidates a fair browser comparison, regardless of individual task results. Its artifacts remain useful integration diagnostics and must not be combined with the replacement cohort.

The matched binding probes pin **Google Chrome for Testing 152.0.7977.75**, executable SHA-256 `201c0aa0fb39c5c3946bf65274ddecbc0669bf7b7bbfc4fedd0bf4ec8da8fe6c`; the exact executable path is recorded in the probe evidence (`build/cua-headed-cft-bind.json`). Each new plan records and verifies its own browser path/version/hash rather than assuming this installation remains unchanged. Cua's public `get_browser_state` refuses this harness-owned PID/window with `browser_consent_required`. Calling public `browser_prepare` with `isolated_new` returned `already_prepared`, but the subsequent binding still refused (`build/cua-headed-cft-prepared-bind.json`). This is an integration/consent barrier before task execution, not a measured task-performance loss or a reason to replace the pinned browser silently. No successful matched cohort is recorded here yet.

A single Second Cursor headed-browser smoke (`e2e/artifacts/model-pilot-20261003T194146.963018Z/report.md`) passed `browser_work_order` with gpt-6.1-sol/high and a 240-second budget: 31.84 seconds of adapter wall time and seven recorded tool calls. Its independent ledger contains exactly one confirmed Ada Lovelace / priority work order. Window metadata confirms an on-screen, non-minimized CFT window outside the agent display. CFT itself was frontmost when the solver started, so this establishes visible task completion, not background human coexistence or a comparison against Cua. Later changes to harness metadata and cleanup require their own evidence.

## Run the 24-trial integration pilot

Run from the repository root on a regular desktop. The runner executes serially because both engines share the host desktop. `SC_E2E_APP` selects the Second Cursor binary, while the fixture harness creates disposable runtime instances. The shared runner defaults to `--native-placement desktop` and headed Chrome for Testing for both engines. Each browser trial uses the same pinned executable with a fresh harness-owned profile/process and active blank tab. The implemented comparison is **host-background/native-tools**; each engine keeps its own observation and action interface, and the disposable profile is not an isolated GUI session.

Cua can also use native AX controls beneath the exact fixture window's WebArea or an attached sheet/dialog. Its `get_window_state` AX text is explicitly projected to that window for native tasks, or its WebArea/attached-modal subtrees for browser tasks, excluding browser chrome and global Apple/app menus. The projection retains original element handles and metadata; target-window screenshots remain unchanged rather than being cropped to those subtrees. Native browser actions accept only observed descendant control tokens, excluding containers and unattached dialogs, and cannot use native pixel input. The adapter contract records this filtering instead of claiming every model observation is a raw driver response.

```sh
# Inspect versions, fingerprints and schedule; no model or desktop work.
SC_E2E_APP="$HOME/Applications/SecondCursor.app" python3 e2e/model_benchmark.py \
  --engine both --model gpt-6.1-sol --reasoning-effort high \
  --rounds 3 --seed 1 --native-placement desktop --max-steps 40 --timeout 300 --list

# Run the same 24 planned trials.
SC_E2E_APP="$HOME/Applications/SecondCursor.app" python3 e2e/model_benchmark.py \
  --engine both --model gpt-6.1-sol --reasoning-effort high \
  --rounds 3 --seed 1 --native-placement desktop --max-steps 40 --timeout 300
```

The four default tasks are Calculator-to-TextEdit (`native_notes`), exactly one work-order submission (`browser_work_order`), a continuous canvas drag (`canvas_route`), and cancel/accept confirmation (`transient_dialog`). Two engines × four tasks × three rounds gives 24 trials. Order alternates AB/BA by round, with A = Second Cursor and B = Cua Driver. Case seeds 1–3 are fixed trial identifiers; they neither randomize the fixtures nor control provider sampling. The runner's timeout default is 240 seconds; these commands explicitly use 300 seconds for both engines and a maximum of 40 model-requested tool calls. Adapter-owned setup shares the trial budget and cleanup has a separate bounded phase.

The matched harness prepares both engines' browser fixtures before the adapter timer and owns their termination afterward. Adapter wall time includes engine binding, model execution, and adapter cleanup, with separate trace phases. Second Cursor claims the sole initial blank tab and cannot open another; Cua binds the exact supplied PID/window through public `get_browser_state` without launching a substitute browser. Both verify the live process against the pinned executable path, version, hash, and stable process identity. These integration wall times exclude browser cold start and are not pure driver latency. The earlier cohort's different startup boundary is historical evidence only.

Each trial is judged independently after the solver stops, using the native app state or browser fixture event ledger. A model's success statement is insufficient, and duplicate submissions fail. Reports separate **independent task outcome** from **all checks passed**: a verified task result remains visible when execution or coexistence checks fail or remain inconclusive, without changing that strict status. `independent_task_success` is a boolean only when the judge ran; missing judgments remain unknown, and reports show judgment coverage against every scheduled trial.

Artifacts retain the plan, every scheduled trial, rejected or failed calls, engine/model traces, endpoint coexistence checks, and raw token usage. Metric coverage is explicit, so partial timing samples cannot look like measurements of every trial. Actual `cost_usd` and provider-only `model_ms` stay unknown; neither account usage nor wall time minus tool time supplies those measurements.

Comparison requires matching model identity, reasoning effort, tool/wall-clock budgets, host version, runner/task/fixture fingerprints, interface and execution tracks, native placement, browser visibility and executable identity, and complete task/trial/case-seed schedules. Engine identities are pinned separately and rechecked against the plan. Missing child artifacts and mismatches produce explicit incomplete trials rather than reducing the denominator. See [the runner and adapter contract](../e2e/benchmarks/README.md) for the exact boundaries. Task-level focus, pointer, and Space checks are sampled at endpoints, so they do not establish continuous human coexistence or absence of transient interference.

## Expand after the integration pilot

The implemented 24-trial schedule is an adapter pilot with fixed tasks and known answers. After inspecting its failures, expand to the twelve task families below with three randomized cases per family per engine: 72 trials. Actual fixture randomization is future work and must use frozen task definitions and seeds before either engine runs. Neither small pilot is enough evidence for a broad best-in-class claim.

| Task family | Independent success check | Fixture work needed |
| --- | --- | --- |
| Calculate and update a native report | Correct computed total; original text preserved | Existing task; randomize values and remove the answer from the prompt |
| Edit two native documents with similar titles | Only the requested text and selection change in the intended window | Extend existing window-pinning regression into a goal-driven task |
| Read several invoices and produce a reconciliation | Exact totals and source references; unrelated records untouched | New synthetic documents and output evaluator |
| Complete a conditional web work order | Correct fields and exactly one submission | Extend existing form with random records and branches |
| Reconcile a paginated work queue | Exact changed-record set, export content and no duplicates | New local web fixture and state ledger |
| Find and act on a virtualized row | Correct row changes after scrolling/reflow | New dynamic-list fixture |
| Draw through a canvas route | Trusted pointer events traverse randomized gates | Existing gesture judge; randomize geometry |
| Cancel one confirmation and accept another | Correct final state and confirmation sequence | Existing task; randomize target details |
| Recover from a stale observation | Requested goal completed after a fresh read; no wrong action | Reuse stale-layout fixtures with goal-driven recovery |
| Recover after disconnect immediately following submission | Exactly one effect; agent reconciles uncertainty | Reuse transport recovery machinery and independent event ledger |
| Yield to takeover and resume | No post-takeover mutation; fresh observation before continuation | Reuse task-control fixtures with external supervisor |
| Work while a person types elsewhere | Task result plus exact human text; no lost keys, pointer or focus interference | Controlled scratch document, continuous observation and a scheduled human typing interval |

Include AppKit, a dedicated SwiftUI fixture and a canvas/custom-editor surface. Do not use personal emails, account records or documents as test data. Only the last family requires the person's physical participation; synthetic events do not establish physical typing coexistence.

### Remaining benchmark work

1. Add randomized fixture data and the additional independent judges above. Freeze geometry and initial data for each paired case; provider sampling is not currently seeded.
2. Implement a separate common screenshot/pointer/keyboard adapter before running the `screenshot-actions` track. The current `native-tools` comparison measures each engine's scoped native interface; the scores must remain separate.
3. Keep native placement identical within each comparison. The shared pilot now defaults to ordinary desktop windows; run `agent-display` or `textedit-off-space` as explicitly matched alternative profiles and record unsupported tasks rather than changing placement for one engine.
4. Add continuous event monitoring for foreground, key-window and Space changes alongside app-owned event ledgers. Current task endpoints can miss a transient disturbance; retain attended event-counter focus tests as separate evidence.
5. Expand reporting of unintended mutations and human interventions for the new task families. Preserve failed and unsupported trials, distinguish safe refusal from completed work, and retain unknown cost/model time unless actual measurements become available.

For a larger comparison, use many more independent task instances and report paired uncertainty intervals by task family. Three cases per family are useful for finding defects, but insufficient for stable tail-latency or superiority claims. Publish the task set and failure traces with any performance claim.

## External benchmarks after the local pilot

**Start with BrowserGym's MiniWoB integration.** Its setup supports locally hosted task HTML and pins a MiniWoB revision. Start with text entry, checkboxes, list selection, scrolling, dragging, changing layouts, tree navigation and inbox tasks. Run both drivers through the same actuator adapter, using the benchmark's own outcomes for scoring. BrowserGym must supply task setup and evaluation; it must not secretly perform the agent's actions through Playwright. Package installation and the adapter are still required. [Setup and pinned revision](https://github.com/ServiceNow/BrowserGym/blob/main/browsergym/miniwob/README.md).

**Then use a predeclared WebArena-Verified subset for longer work.** It supplies versioned tasks and deterministic evaluation based on responses and recorded network traces. Start with one site's workflows and a reproducible reset process. Hosting the sites and recording evaluator-compatible traces are additional work. Do not report a subset score as the full benchmark score. [WebArena-Verified](https://github.com/ServiceNow/webarena-verified).

**Keep OSWorld and Spaces isolation as separate later tracks.** Standard OSWorld uses guest applications that differ from this macOS-native product. A port to macOS is a custom derivative, not an official comparable score. Spaces additionally needs equivalent guest fixtures and separate measurements of boot/readiness, host handoff, resource overhead and concurrent agents. Our current harness binds fixtures to localhost and only accepts `host-background`; it cannot currently compare an isolated VM run. [OSWorld setup](https://github.com/xlang-ai/OSWorld), [Cua benchmark adapters](https://cua.ai/docs/cua-bench/guides/adapter-benchmarks).

The next evidence is the matched 24-trial pilot from the implemented shared runner. Report its independently verified outcomes and failures before drawing any conclusion about which driver lets this model finish the same work more reliably or efficiently.
