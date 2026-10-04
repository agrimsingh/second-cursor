# Second Cursor

Let an AI agent work in your Mac apps while you keep working. Second Cursor reads accessibility trees and browser DOMs, then sends input to supported apps and tabs in the background. It runs as a menu bar app with an MCP server, so your preferred client and model can drive it. App behavior and macOS capabilities set the limits; see [limitations](#limitations) before relying on an unattended workflow.

Local approval prompts deliberately take focus. When an agent first touches an app you have not approved, or asks for a sensitive browser operation (your own tabs, history, clipboard, uploads), Second Cursor brings up a dialog so that you, not the model, decide.

The runtime provides session ownership, structured observations, outcome checks, native text selection and rich paste, DOM browser actions, multi-profile browser connections, and a dependency-free JavaScript SDK. See [the client contract](docs/api.md) and [the E2E acceptance cases](docs/parity-acceptance.md).

## How it works

```mermaid
flowchart LR
    Client["Cursor / any MCP client"] -- "HTTP + bearer token" --> Server["MCP server<br/>(SecondCursor.app)"]
    Server --> Desktop["Desktop control<br/>AX tree, input, capture"]
    Server --> Bridge["Browser bridge<br/>Unix socket"]
    Bridge --> Host["sc-native-host"] --> Ext["Chrome extension<br/>chrome.debugger on agent tabs"]
    Desktop --> Apps["Supported Mac apps, in place"]
```

- **In place.** Clicks, drags, and scrolls are posted to the target app's process and addressed to its window, allowing compatible apps to receive input behind other windows or on another Space. Capability and human-input checks run before dispatch.
- **Accessibility first.** Every window is read as a compact accessibility outline with indexed elements. Clicks and typing go through accessibility actions whenever possible.
- **Diffs, not re-reads.** The first `get_window_state` of a window in a session returns the whole outline. Later reads return only the lines that changed, and an element keeps its index across reads. Pass `full: true` for the whole outline again, or `scope: <index>` to read one region at full depth.
- **Windows on other Spaces.** macOS hides off-Space windows from the normal window list, so Second Cursor finds them through the app's main/focused window or a remote-token lookup (and, for Electron apps, through their web areas).
- **Background keyboard.** Keys are posted to the target app's process. Command shortcuts (cmd+a, cmd+z, ...) briefly hand the app AppKit focus without raising it, press the matching menu item, then give focus back.
- **Human priority.** Recent input to an app stops agent mutations there and invalidates its old observation. Pointer and keyboard work also refuses to change the key window of the foreground app you are using. `capabilities` explains which routes are available for a target; after you yield, the agent must read the target again.
- **Parallel ownership.** MCP connections receive separate runtime sessions automatically. Native actions claim their target window, and each app has a serial queue, so actions in one app stay ordered while independent apps can run concurrently.
- **Tasks and takeover.** **Tasks…** in the menu bar shows each task's client, state, targets, and last verified result. Pause or take over one task while others continue, then resume after the agent observes your changes. Task state stays visible while the model thinks; silence does not mean completion. See [the lifecycle contract](docs/api.md#tasks-and-human-takeover).
- **Visible presence.** While an agent sends calls, its cursor (a black arrowhead with an orange glow) rests on its target window with an optional caption. It hides when covered or on another Space; Chrome draws the same presence inside the agent's tab. Cursor presence clears at turn end, on pause, or after a minute without calls. This visual timeout does not finish the task in the task panel. Visible pointer animation completes before dispatch; covered windows and background tabs do not wait for an invisible cursor.
- **Batching.** `batch` runs predictable steps in order, including native text selection followed by insertion, then checks an optional expected result and returns one observation. This lets a model edit and verify text in one round trip. Steps can address separately observed windows in the same app process; native pixel steps still check the observed size and accessibility layout, and pause, takeover, or human input can stop later steps.
- **Outcome checks and recovery.** Results distinguish dispatched input, an observed UI, verified postconditions, and uncertain effects. A durable operation journal prevents an identical retained request from being replayed after a lost response or restart; uncertain operations require a fresh read before deciding what to do next.
- **Resumable SDK workflows.** The optional agent loop can save a goal, progress, and pending operation before dispatch. After a client crash it reconciles the recorded result and observes its targets before continuing; unresolved outcomes stop for host recovery. [Checkpoints](docs/api.md#javascript-sdk) are opt-in because they contain task content.
- **Browser.** A Chrome extension drives only the tabs it opens itself, in a "Second Cursor" tab group, over Chrome's debugger API. Existing user tabs are available only through local approval for discovery and claim operations; session cleanup leaves them open.
- **Optional agent display.** An invisible virtual monitor is available as a private workspace (`agent_display`, `move_to_agent_display`). It is never used unless asked for. It shares your macOS session, app processes, and clipboard; it is not an isolated desktop or VM. A launch on a hidden desktop can leave the window unreachable. `launch_app` reports that the window was left in place and queues no later move; observe it again and explicitly call `move_to_agent_display` when it is reachable. Teardown refuses while native input is in flight or another session owns a native window.

## Install

Requires macOS 14+ and Xcode (Swift 6).

```bash
scripts/install.sh
```

This creates a local self-signed code-signing identity (so macOS permission grants survive rebuilds), builds `SecondCursor.app`, installs it to `~/Applications`, registers the Chrome native messaging host, copies the extension to `~/Library/Application Support/SecondCursor/extension`, and launches the app.

Then open **Setup…** from the menu bar, or run `SecondCursor --show-onboarding`:

1. Grant **Accessibility** and **Screen & System Audio Recording**. The permission helper opens the relevant System Settings pane. Drag the app icon into the list, or switch Second Cursor on if it is already listed. For Screen Recording, choose **Quit & Reopen** if System Settings asks.
2. Select a client and install its MCP configuration. For browser control, install browser support, then open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select `~/Library/Application Support/SecondCursor/extension`. `SecondCursor install-browser` installs the support files from the app bundle without requiring this source checkout.
3. Check the connection in Setup, or run `SecondCursor doctor`. It reports reachability, version, permissions, pause state, the extension connection, and the config path. `SecondCursor self-test` performs a read-only connection check; add `--browser` to open a disposable agent tab, click its test button, independently read the changed result, and close it. Both commands accept `--json` and return a nonzero exit code when their check fails.

`SecondCursor` in these commands is the app's binary, `~/Applications/SecondCursor.app/Contents/MacOS/SecondCursor`. `SecondCursor --help` lists every command.

The source installer uses a local development signature. Release packaging is available through `scripts/package-release.sh`, with an explicit Developer ID and notarization path; a locally packaged archive is not a notarized public release. See [release distribution](docs/release-distribution.md) for prerequisites and validation.

## Connect a client

One command writes the entry for a client:

```bash
SecondCursor install-mcp --client cursor    # ~/.cursor/mcp.json (HTTP URL + token)
SecondCursor install-mcp --client claude    # ~/.claude.json (Claude Code)
SecondCursor install-mcp --client codex     # ~/.codex/config.toml, [mcp_servers.second_cursor]
SecondCursor install-mcp --client gemini    # ~/.gemini/settings.json
```

It only adds or replaces the `second-cursor` entry and leaves everything else in the file alone. Before it writes, it copies the original to `<file>.bak`, and it creates the file if it is missing. It prints what changed. `--dry-run` shows the entry it would write (token redacted) and which existing entries it would keep, without writing anything. Nothing is installed until you run one of these commands.

Claude Code, Codex and Gemini CLI launch a command rather than connecting over HTTP, so their entry runs `SecondCursor mcp`. That is a stdio MCP server built into the app binary. It forwards each request to the running app and starts the app if it isn't running. `node sdk/mcp-stdio.mjs` does the same for hosts where only Node is available. Any other stdio host can use `SecondCursor mcp` directly.

For Cursor, the menu bar item's **Copy Cursor MCP config** produces the same entry that `install-mcp --client cursor` writes:

```json
{
  "mcpServers": {
    "second-cursor": {
      "url": "http://127.0.0.1:8787/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Custom hosts can use POST `/api` or `sdk/index.mjs`; all interfaces share the same dispatcher and policies.

The port and token live in `~/Library/Application Support/SecondCursor/config.json`. The server only listens on localhost and rejects requests without the token or with a non-local origin.

### Cursor turn-end hook

At the end of an agent turn, `SecondCursor --turn-ended --session <id>` resets that session's observation baseline and releases its turn approvals and claimed personal tabs. Automatic MCP sessions are separate: a host must supply the session ID, or call `/turn-ended` with its `Mcp-Session-Id` header. Omitting the ID affects only the `legacy` compatibility session. The command ignores stdin and always exits 0, so a hook cannot fail loudly.

`install-mcp --client cursor --with-hook` adds the legacy-compatible `stop` hook to `~/.cursor/hooks.json`, preserving other hooks and writing a `.bak` first. `scripts/cursor-hooks.json` is the same sample. This unscoped hook cannot infer an MCP connection's session ID or clean another client's session. Stdio proxies close their own session when the host closes the transport.

### Agent skill

`.cursor/skills/second-cursor/SKILL.md` tells an agent when to use Second Cursor and how to work with it. It covers the observe-then-act loop, and writing a short Node script with `sdk/index.mjs` instead of dozens of tool calls when a flow branches. It also sets the safety rules: ask before sending, deleting or purchasing, and never read password managers. Copy it into another project's `.cursor/skills/`, or into your personal skills, to use it outside this repo.

### Choose a model

Second Cursor runs no model of its own. Whatever client you connect brings one, so pick it in that client. The [initial comparison](docs/verification-model-sweep-2026-10-04.md) and [Opus follow-up](docs/verification-opus-model-sweep-2026-10-04.md) support Sol 6.1 medium as a provisional default, with Opus 5.5 Fast medium a promising Cursor SDK alternative. Both passed all five scheduled trials in their separate cohorts; Sol was faster on native editing and dialog handling, while Opus medium was faster on the single form trial. These results include host overhead and use small fixed fixtures, so they do not establish general model intelligence or price/performance.

## Tools

| Area | Tools |
| --- | --- |
| Status and task control | `status`, `capabilities`, `task` |
| Apps and windows | `list_apps`, `list_windows`, `launch_app`, `get_window_state`, `find_elements`, `screenshot` |
| Sessions (for hosts; callable but not in `tools/list`, like the `session_id`, `operation_id`, `observation_id` and `timeout_ms` arguments) | `session`, `bind_target`, `operation_status`, `cancel_operation` |
| Editing | `inspect_element`, `select_text`, `paste` |
| Extended browser | `browser_instances`, `browser_command` (DOM, dialogs, files, exports, screenshots, history, assets) |
| Browser REPL (opt-in, `toolSurface: "full"`) | `browser_repl`: persistent JavaScript with a Playwright-compatible `page` ([details](docs/api.md#browser-repl)) |
| Input | `batch` (also as `act`), `click`, `drag`, `type_text`, `set_value`, `press_key`, `scroll`, `perform_action` |
| Agent display (optional) | `agent_display`, `move_to_agent_display`, `return_window` |
| Browser | `browser_status`, `browser_tabs`, `browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_press_key`, `browser_scroll`, `browser_screenshot`, `browser_close` |

## Safety

- **App approval:** the first time an agent reads or operates an app, you choose Allow for this session, Always allow, or Deny. Your own tabs, browsing history, clipboard, raw DevTools, and file uploads always ask. See [the client contract](docs/api.md#approvals).
- **Not a sandbox:** approvals limit what an agent can do through Second Cursor's tools. An agent that can also run shell commands as you could edit the config or start its own instance, so treat approvals as a guard against mistakes, not against a determined agent.
- **Pause:** the menu bar item pauses all agent input; **Tasks…** provides controls for one task. `SecondCursor --pause` / `--resume` control the global pause from a shell. Already delivered input cannot be undone.
- **Blocked apps:** `blockedBundleIDs` in `config.json` (password managers, Keychain Access, System Settings by default) can never be launched or touched.
- **Action log:** every state-changing call is appended to `~/Library/Logs/SecondCursor/actions.jsonl` before it runs.
- **Timeouts:** execution deadlines are capped at 60 s. A timeout does not cancel already dispatched input; use operation IDs to reconcile outcomes and cancel queued work.
- **Quitting** returns any window on the agent display to where it came from, then removes the display.

## Testing

Desktop and browser acceptance suites run against a disposable test instance. The harness starts a fresh copy of the installed bundle with its own `SC_HOME` folder, port, token and config, and stops it afterwards. Your running app, config, logs and pause flag are not changed. Ordinary tests do not show approval dialogs or setup panels; rendered UI and consent checks require explicit flags. Transport and SDK regressions also exercise isolated local servers without touching the desktop.

[Text batching and workflow recovery verification](docs/verification-text-workflow-2026-10-04.md) records the native cases, actual model execution, and recovery after a real invoice submission and process kill.

`python3 e2e/best_in_class.py gate --profile quiet` runs the unattended suites serially and treats missing, skipped, or inconclusive evidence as incomplete. `python3 e2e/best_in_class.py tasks --rounds 3` runs independently judged complete tasks with fresh fixtures. Its built-in adapter is a deterministic control baseline; a competitive claim requires running another engine under the same task contract. See [release evidence and benchmarks](docs/best-in-class-acceptance.md) for the attended release gate and comparison boundaries.

`python3 e2e/model_benchmark.py --engine both --model gpt-6.1-sol --reasoning-effort high --rounds 3 --native-placement desktop --max-steps 40 --timeout 300 --list` previews the 24-trial shared model pilot for Second Cursor and Cua Driver; remove `--list` to execute it. The runner uses fresh Codex conversations, fixture-scoped tools, alternating engine order, ordinary desktop native fixtures, and the same pinned headed Chrome for Testing build. This is the `host-background/native-tools` track; seeds identify fixed cases, and cost/model time remain unknown. The earlier browser cohort is invalid for comparison, and Cua's matched-browser binding currently refuses on consent, so no completed matched cohort establishes superiority. See [the benchmark plan](docs/benchmark-plan-2026-10-04.md) and [adapter contract](e2e/benchmarks/README.md).

`python3 e2e/model_efficiency.py --model gpt-6.1-sol --reasoning-effort high --rounds 5 --list` previews a separate Second Cursor experiment comparing observations requested by the model with observations supplied upfront. Remove `--list` to run ten fresh Calculator → TextEdit trials in alternating order. Both modes count every observation in time and call budgets, preserve independent outcome checks, and report the model's final-response delay separately when the tool trace proves the completed edit. See [the acceptance criteria](docs/acceptance-model-efficiency.md).

The optional [Cursor model host](e2e/cursor_host/README.md) runs Grok 4.5/4.7 Fast and Opus 5.5 Fast through the same scoped driver and independent judges. It pins the SDK, complete model selection, and separate host identity. `python3 e2e/model_sweep.py --rounds 3 --timeout 90 --list` previews the five-configuration native comparison; remove `--list` to execute it. For the browser screen, use `--rounds 1 --task browser_work_order --task transient_dialog`. This compares the model together with its host, keeps failed and missing trials in the denominator, and reports successful latency per task. See [the experiment contract](docs/acceptance-model-sweep.md) and [results and limits](docs/verification-model-sweep-2026-10-04.md).

For Opus, add `--profile opus-5.5-fast-low --profile opus-5.5-fast-medium` to select only those configurations, both pinned to 300k context and Fast enabled. The default five-profile selection stays unchanged. See [the Opus results](docs/verification-opus-model-sweep-2026-10-04.md).

The [browser contract verification](docs/verification-browser-contracts-2026-10-04.md) records the resulting typing-target fixes, dropdown selection improvements, semantic failure reporting, and a matched old/new form comparison. It separates reduced tool recovery from noisy model latency and documents the remaining native Return/autosave limitation.

Add `--experiment tool-surface` to `model_efficiency.py` to compare full and compact catalogs with upfront observations enabled in both. The compact catalog represents actions through the complete batch schema and retains all observation tools; the engine still checks each nested action against its original allowlist. Results from the two experiments stay separate.

```bash
python3 e2e/run.py            # or name scenarios: python3 e2e/run.py textedit_background calculator
python3 e2e/parity.py         # sessions, editing, browser
python3 e2e/review.py         # review regressions (--short-retention for lease/retention expiry)
python3 e2e/setup.py          # mcp stdio proxy, doctor, --turn-ended, install-mcp (--ui adds the onboarding panel)
python3 e2e/policy.py         # approvals in deny mode (no UI), turn end, action log
python3 e2e/instance.py       # test-instance isolation
python3 e2e/cursor.py         # arrow and menu-bar presence, drawn on the agent display
python3 e2e/clicks.py         # every pixel click, double-click and drag acts exactly once
python3 e2e/latency.py        # per-call timings, written to latency.json
python3 e2e/find.py           # find_elements, act, the compact tool surface
python3 e2e/repl.py           # browser_repl: persistence, limits, sandbox, Playwright locators
python3 e2e/tasks.py          # task ownership, pause/takeover/resume, restart recovery
python3 e2e/transport_recovery.py  # automatic MCP sessions and durable operation reconciliation
python3 e2e/native_safety.py   # native capabilities and stale-layout/input refusals
python3 e2e/text_batch.py      # native selection, insertion, Unicode and interruption guards
python3 e2e/invoice_workflow.py # kill/restart the SDK after a real invoice submission
node --test sdk/agent-recovery.test.mjs # checkpoint durability and refusal/recovery cases, no GUI
python3 e2e/browser_reliability.py # freshness, hover, verified form state, settling
python3 e2e/browser_target_contracts.py # editable focus, iframe/shadow targets, dropdown contracts
node --test e2e/extension_tests/*.test.mjs # isolated browser logic regressions, no GUI
python3 e2e/release_setup.py   # portable bundle, setup diagnostics and disposable browser check
python3 e2e/task_ui.py --ui   # deliberately opens the disposable Tasks and Setup panels
python3 e2e/focus.py --takeover   # focus safety; takes focus and covers the screen, so run it while you are away
```

Background suites put fixture windows on the agent display. `focus.py --takeover`, `native_safety.py --takeover`, `task_ui.py --ui`, `setup.py --ui`, and `run.py --on-screen` explicitly permit foreground fixture activity. Run those attended checks separately from unattended timing measurements.

`SC_E2E_APP=build/SecondCursor.app` tests another bundle, and `--installed` targets your running app instead (opt-in). `python3 e2e/setup.py` runs `install-mcp` against a temporary `HOME` and checks that your real client configs are unchanged.

`python3 e2e/agent_smoke.py --run` checks that a real model can use the tools. It has the Cursor Agent CLI compute 7 × 6 in Calculator and append the result to a TextEdit window, through a project-level MCP config that points at a test instance, then verifies the result independently. The fixture windows open on the agent display, off your screen. Each report splits the agent loop into model time and tool time. `--models gemini-3.8-flash-medium,composer-2.5-fast --repeat 3` compares models and writes `e2e/artifacts/agent-compare-<ts>.md`. It spends model usage, so without `--run` it only prints what it would do.

The suite talks to the app over MCP like a real client and, around every scenario, independently checks that your cursor, frontmost app, and active Space did not change. Each run writes a report, screenshots, and a full call transcript to `e2e/artifacts/<run-id>/`. Scenarios that depend on your environment (an Electron app open, a window on another Space, the Chrome extension loaded) are skipped with a reason when unavailable.

## Development

| Target | Purpose |
| --- | --- |
| `SCCore` | Desktop control: window discovery, accessibility tree, input, capture |
| `SCDisplay` / `SCDisplayPrivate` | Agent display (CGVirtualDisplay), window hosting, cursor clamp |
| `SCServer` | MCP server over HTTP, tool catalog, gates, action log |
| `SCBrowser` / `sc-native-host` / `extension/` | Browser bridge, native messaging host, Chrome extension |
| `SecondCursor` | Menu bar app that wires everything together |
| `sc-dev`, `sc-display-dev`, `sc-browser-dev` | Command-line tools for exercising each module against real windows |

`spikes/` holds the original research prototypes.

## Limitations

- Relies on private macOS APIs (SkyLight event posting, remote accessibility tokens, CGVirtualDisplay). A macOS update can break any of them, and the app cannot ship through the Mac App Store.
- Native modifier clicks/drags and hover are unsupported and refused before dispatch. Browser hover and supported DOM actions can provide an alternative for web content; the native `capabilities` tool describes current route restrictions.
- Canvas-style apps and games that require hardware input, cross-app drag-and-drop, and HTML5 drag payloads are not guaranteed by process-addressed or browser pointer input. Separate GUI isolation must be configured outside Second Cursor when a workflow requires it.
- Custom-drawn apps (Ableton, games) may stop repainting on a Space you are not viewing. Native captures report unverified frame freshness; an image's capture time is not proof of the app's repaint time. Accessibility layout guards cannot detect changes confined to pixels.
- A window on another Space that neither its app nor any accessibility client has exposed yet cannot be reached until the app is touched once.
- Chrome shows its "started debugging this browser" bar while the extension is attached to an agent tab.
- Human-input arbitration is conservative: recent input requires a new read, another key window in the same foreground app blocks pointer/keyboard work, and unavailable input monitoring blocks foreground mutations. It does not make arbitrary simultaneous editing of one app safe.

## Credits

The background input recipes (SkyLight event posting, focus without raise) build on [cua-driver](https://github.com/trycua/cua) (MIT, see `LICENSES/cua-driver-MIT.txt`) and ideas from [yabai](https://github.com/koekeishiya/yabai).
