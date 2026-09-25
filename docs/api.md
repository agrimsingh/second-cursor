# Second Cursor 0.2 client contract

The signed macOS app is the runtime. MCP HTTP (`/mcp`), JSON HTTP (`/api`), the JavaScript SDK, and the stdio adapter all reach the same tool dispatcher. No provider credentials are stored in the runtime.

## Connections

Read the localhost port and bearer token from `~/Library/Application Support/SecondCursor/config.json`. POST `/api` with `Authorization: Bearer …`, `Content-Type: application/json`, and a body such as:

```json
{"tool":"session","args":{"action":"create"}}
```

The response uses MCP's content envelope: text items contain result JSON and image items contain base64 PNG data. `isError` distinguishes refused/failed operations. `{"tool":"tools/list"}` returns the complete tool definitions. MCP clients use `/mcp` normally. For stdio-only hosts, run `node /absolute/path/second-cursor/sdk/mcp-stdio.mjs`.

## Sessions, ownership, observations, and retries

Pass `session_id` on subsequent tools. Omission uses the compatibility session `legacy`; independent hosts should create their own sessions. Native reads are permitted across sessions. Explicit-session mutations hold an idle lease; legacy actions check existing leases but do not acquire one. Native leases expire after five idle minutes (local config `claimIdleSeconds`), and in-flight actions keep their lease until they finish. A claimed browser tab can only be read or changed by its owner. `bind_target` claims/releases `window:<id>`. Closing a session cancels queued work, releases targets, closes ephemeral agent-created tabs, and releases retained/user tabs without closing them.

`get_window_state` returns `snapshot_id`, a compact tree, structured `elements`, and optional screenshot metadata. Text-only is now the default. Each session has its own full/diff baseline. `scope` reads a known subtree; `inspect_element` recovers full values and selection ranges. Offsets and lengths are UTF-16 code units. Unknown state is null. Diff responses omit `elements`; request `full:true` for the structured list. Compact outlines print unusual states only. Settable/editable details are queried by `inspect_element`, not by every tree walk. `browser_snapshot` also returns structured elements and an observation ID; pass `full:false` for session-local diffs.

Pass `observation_id` when acting on a previous native/browser observation. A superseded observation is rejected. Native pixel actions also reject a resized window. Observation IDs are references to what was returned, not a guarantee that an application cannot change afterward; observe again after actions and before branching.

Supply an `operation_id` before submitting an action. Reuse it only for an identical request; the server returns the existing outcome instead of executing again. `operation_status` reconciles timeouts. `cancel_operation` prevents queued work and future batch steps, but cannot undo input already delivered to an app. `timeout_ms` includes queue time and is capped at 60000. A timeout is not cancellation. Terminal result payloads share a 32MB cache; an evicted payload has `result_expired:true` but its operation ID remains reserved for the retention interval, preventing accidental re-execution within that interval. Completed explicit IDs expire after one hour (local config `operationRetentionSeconds`). Retry only within this window; an expired ID is treated as a new request. Successful calls without a supplied ID do not retain history; unresolved timeouts do. Closing a session discards its history. `status.runtime` reports retained operations, result bytes and active HTTP connections.

Native batches cannot cross app processes. Browser batches cannot cross tabs; the Swift runtime is their sole executor. Both execute in order and recheck admission between steps. Configured `confirmationRequiredTools` and browser `user_tabs`, `claim`, `history`, `clipboard`, and `cdp` always show a local Allow once/Deny prompt. Batch children pass the same checks. The prompt activates the app, expires within the call deadline, closes on cancellation, and keeps the main actor responsive; the model cannot approve itself. The host remains responsible for interpreting the meaning of actions such as sending a message or purchasing an item.

## Native editing

- `select_text`: element index, exact text, optional prefix/suffix, and selection_type `text`, `cursor_before`, or `cursor_after`. Ambiguous matches fail.
- `paste`: `text`, `html`, or `md`. Markdown inserts source text. Native paste restores the clipboard only if it still owns it, preserving a concurrent human copy.
- `inspect_element`: `offset` and `length` recover truncated text; response includes `total_length`, `has_more`, selection and available actions.
- `perform_action`: choose an action actually listed in the observation.

## Extended browser interface

Use `browser_instances` to discover persistent instance IDs. Agent-created tabs work without prompts; personal-tab discovery and claim require local approval. Supply `browser_id` when more than one extension profile is connected. Existing browser tools remain available. `browser_command` takes `operation`, `tab_id`, `browser_id`, session fields, and an `arguments` object for operation-specific fields. Flat arguments remain compatible. `operation:capabilities` supplies detailed argument help on demand.

Locators use one or more of `selector`, `role`/`name`, `label`, `text`, `placeholder`, `test_id`; matching is exact unless `exact:false`. `nth` selects an explicit match. `frame` is a selector or an array for nested frame traversal, including debugger-attached cross-origin frame contexts. Ambiguous matches fail.

| Operations | Arguments / result |
|---|---|
| capabilities | Advertised operations and locator fields |
| user_tabs, claim, release | Discover user tabs; explicitly claim one; release without closing |
| retain | mode: ephemeral, deliverable, handoff; retained tabs survive session cleanup |
| dom_snapshot, inspect | Structured DOM state; inspect supports offset/length and returns attributes and bounds |
| click | locator or x/y; button, count, modifiers |
| fill, type, paste | text; paste format text/md/html; HTML requires a locator |
| select_text | text, prefix/suffix, selection_type |
| check, select_option | checked boolean; option value |
| perform_action | locator and an advertised action: focus, scroll_into_view, expand/collapse, increment/decrement |
| press_key, scroll, drag | keys; direction/amount and optional locator; from_x/from_y/to_x/to_y |
| back, forward, reload | Browser navigation |
| wait | url substring, locator/state, or event download/filechooser/navigation; bounded timeout_ms |
| dialog | Omit accept to inspect; accept boolean and optional prompt_text to answer |
| upload | Absolute regular-file paths; locator or an intercepted chooser |
| screenshot | full_page or clip; PNG plus dimensions, scale and document-coordinate origin |
| viewport | width/height, or reset:true |
| logs, history | Console entries; query/from/to/limit for history (Unix milliseconds) |
| export | text/html/md/pdf; Google Workspace xlsx/csv/docx/pptx; youtube_transcript on watch pages |
| assets | List observed resources; action:bundle and asset_ids return selected bodies |
| clipboard | action read/write, text; requires the page origin to allow clipboard access |
| cdp | Locally approved method/params; only Page.getLayoutMetrics, Page.getNavigationHistory, DOM.getDocument and Accessibility.getFullAXTree are allowed |
| batch | Array of extended operation arguments; returns results and final DOM observation |

A dialog-triggering action returns the observed dialog instead of waiting for the click to complete. File choosers are intercepted, allowing explicit assignment without moving the system cursor. Google Workspace exports need a signed-in document and can return a download in progress; YouTube transcripts require available captions. These service-specific paths depend on the service and are not covered by the local fixture suite.

## JavaScript SDK

```js
import { SecondCursor } from './sdk/index.mjs';
const sc = await SecondCursor.local();
try {
  const { browsers } = await sc.state();
  const tab = await sc.browser(browsers[0].id).open('https://example.com');
  const state = await tab.domSnapshot();
  // Choose locators from observed UI, then act and observe again.
  await tab.markDeliverable();
} finally { await sc.close(); }
```

App and Tab handles persist in the caller's JavaScript environment. `app()` refuses an ambiguous multi-window selection. Opening/binding a target includes an initial observation. `call()` exposes every tool for capabilities not covered by convenience methods. Errors carry `operationId`. Use `transport:'mcp'` to exercise the MCP endpoint instead of JSON HTTP. `sdk/agent.mjs` supplies an optional model-independent loop; its adapter owns provider-specific tool/image encoding and its approval hook belongs to the host.

## Validation

`docs/parity-acceptance.md` records failure cases written before implementation. Run `scripts/build_and_run.sh --verify`, `python3 e2e/start-browser.py`, `python3 e2e/parity.py`, `python3 e2e/run.py`, then `node sdk/smoke.mjs`. Review regressions: `python3 e2e/review.py --short-retention` (temporarily changes local lease/retention settings and restores them), plus `node sdk/stdio-smoke.mjs`. Browser startup prints its isolated PID/profile for cleanup. Do not run pause scenarios concurrently with another suite. Reports, transcripts, screenshots, PDF/HTML/text exports and selected asset bodies are stored under `e2e/artifacts/`.

Review approval scenarios are excluded by default. Add `--approvals` only for a deliberate interactive test; ordinary parity/review runs do not produce approval dialogs.
