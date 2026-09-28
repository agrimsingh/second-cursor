# Second Cursor client contract

The signed macOS app is the runtime. MCP over HTTP (`/mcp`), JSON over HTTP (`/api`), the native stdio proxy (`SecondCursor mcp`), and the JavaScript SDK all reach the same tool dispatcher and the same policies. No provider credentials are stored in the runtime.

## Connections

Read the localhost port and bearer token from `~/Library/Application Support/SecondCursor/config.json` (or `$SC_HOME/config.json` when `SC_HOME` is set). POST `/api` with `Authorization: Bearer …`, `Content-Type: application/json`, and a body such as:

```json
{"tool":"session","args":{"action":"create"}}
```

The response uses MCP's content envelope: text items contain result JSON and image items contain base64 PNG data. `isError` distinguishes refused or failed operations. `{"tool":"tools/list"}` returns the tool definitions. MCP clients use `/mcp`. Hosts that only launch commands run `~/Applications/SecondCursor.app/Contents/MacOS/SecondCursor mcp`, which proxies stdio to the running app and starts it if needed. `SecondCursor install-mcp --client cursor|claude|codex|gemini` writes that entry for you; `SecondCursor doctor` checks the setup.

## Observations

`get_window_state` returns `snapshot_id` and a compact outline. The first read of a window in a session is the whole outline; later reads return only changed lines (`is_diff: true`, `diff_base`). Diffs compare content, not position: when a scroll or resize only moves elements, they are summarized in one line (`~ N elements moved (scroll or resize)…`) instead of being resent; `full: true` returns every current frame. Outlines stop at `max_elements` indexed elements (default 400, 50–3000; the value sticks for that window until the turn ends), and a truncated outline ends with a line naming the containers it left out. `scope` takes an element index or a container label such as `"Clip Detail"` and reads that part at full depth. Indexed lines show window-local frames, unusual states only (`enabled=false`, `focused=true`, `selected=true`, `expanded=true`), and `actions=[…]` only for actions beyond the obvious press/confirm/menu. Full outlines end with `focused: [i] Role "label"` and, when text is selected, `selected text: "…"`; both take part in diffs. `scope` reads one subtree at full depth. `inspect_element` recovers full values and selection ranges in UTF-16 units. `structured: true` adds the `elements` JSON array to full and scoped reads; diffs never include it.

Screenshots are opt-in (`include_screenshot`) and at most 1024 px on the long edge, because clients shrink larger images and the model would otherwise read positions off a scaled copy. Each screenshot reports `scale` (image pixels per window point) and a `coordinates` hint. Pixel tools take window points by default; pass `coordinate_space: "screenshot"` to use pixels from the latest screenshot of that window instead. A screenshot taken soon after an action waits for the app to repaint. If capture fails, the outline still comes back, with `screenshot_error`.

Every native action (`click`, `drag`, `type_text`, `set_value`, `press_key`, `scroll`, `perform_action`, `select_text`, `paste`) returns an `observation`: the diff against the session's previous read of that window, taken after the UI settles. It advances the diff baseline and becomes the window's current observation. `observe` chooses `tree` (default), `both` (adds a screenshot), `screenshot`, or `none` (no read, no settle wait; use it when chaining blind steps). The settle wait follows the app's accessibility notifications: it waits up to 250 ms for the first notification after the action, then for 60 ms without one, capped at 3 s; apps that post nothing fall back to polling for 200 ms of quiet. `press_key` reports `verified: true` when the app confirmed the menu press was delivered; that doesn't prove the command had an effect, so check the observation. A failed read after a successful action is reported as `observation_error`, not as a failure. `batch` returns one observation after its last step.

Browser actions (`browser_click`, `browser_type`, `browser_press_key`, `browser_scroll`, `browser_navigate`) do the same for their tab: after the page settles they return an `observation` like `browser_snapshot`'s, the removed (`-`) and added (`+`) outline lines since the session's previous read of that tab, or the whole outline when that is not much smaller, and take `observe`. `browser_snapshot` defaults to `full: false`, so only the first read of a tab (or `browser_open`) returns the whole outline; pass `full: true` for the whole outline again.

Window tools take `window_id` or `app` (name or bundle id). For reads and screenshots, `app` means that app's only window, or the main window of its frontmost process; when it can't choose, the error lists candidate window ids. Actions, `inspect_element`, and scoped reads by `app` go to the app window this session observed last, so indexes and coordinates always refer to the window you saw; they are refused if you haven't observed one or it has closed. Results carry the resolved `window_id`. Codex-style argument names are accepted: `click_count`, `mouse_button`, `key` (xdotool names such as `Return`, `Page_Up`, `KP_Enter`, `super+c`), and `pages` for scrolling.

Pass `observation_id` when acting on a previous observation; a superseded one is rejected. Pixel actions also stop when the window changed size since it was last observed. Read `get_window_state` at the start of each turn before acting.

## Postconditions and waiting

Native actions and `batch` take `expect`, so an action can verify its own outcome instead of reading the window again: `{text}` (some element's title, value or description contains it, case-sensitive) or `{element_index, value}` (that element's value equals it exactly), plus `gone` (wait for the condition to stop holding) and `timeout_ms` (default 2000, at most 10000). The condition is checked once before the action and waited for after it (after the last step of a `batch`), waking on the app's accessibility notifications and re-checking every 250 ms for apps that post none. The result adds `expect: {status, waited_ms, detail}`: `verified`, `preexisting` (it already held before the action, so the end state is right but the action is not proven to have caused it), or `failed`. A failed postcondition is not an error: the observation, taken after the wait, still comes back, and `detail` says what was last seen. Malformed `expect` is refused before anything is sent; it may also arrive as a JSON string.

`wait_for` waits for the same condition without acting: `{window_id | app, text?, element_index?, value?, gone?, timeout_ms?}`. It returns the status (a condition that already holds is `verified` with `waited_ms: 0`) and the window's observation diff. It is a read, never holds the app's queue between checks, and keeps presence alive like other reads.

## Text recognition

`get_window_state` with `ocr: true` also reads the text drawn in the window, for apps that draw text accessibility does not expose (DAWs, games, canvases). Recognition runs on device with Apple's Vision framework, on a full-resolution capture of the window rather than the 1024 px screenshot. Recognized text that repeats the label, value or title of an element it overlaps is left out, even with a stray glyph attached, such as the chevron beside a document title. Text inside an indexed element goes on that element's line as `ocr="…"` (several joined with ` | `). Other text becomes a line without an index under the deepest container holding it, such as `text "Export" (412,88 40x14)`, framed in window points; it is not an accessibility element, so click it with x/y at its center. Results add `ocr_ms` (capture and recognition) and `ocr_count` (texts merged); if recognition fails, the outline still comes back, with `ocr_error`. With `scope`, only text inside that element is kept. Recognized text only takes part in diffs between `ocr` reads: a read without `ocr` compares plain outlines, so it never reports recognized lines as removed, and an `ocr` read compares against the previous outline with the last recognized text merged in. On an M2 Pro a read with `ocr` costs about 0.2 s for a small window and 0.3–0.6 s for a 1300×850 pt one. The first recognition on a Mac, and again after an OS update, waits while Vision compiles its model for the Neural Engine, which takes a minute or more; such a read gives up on text after 15 s and returns `ocr_error`, and the compile finishes in the background while the app keeps running.

## Sessions, ownership, and retries

Pass `session_id` on subsequent tools. Omitting it uses the shared compatibility session `legacy`; independent hosts should create their own. Native reads are permitted across sessions. Explicit-session actions hold an idle lease on the window; legacy actions respect existing leases but never take one. Leases expire after five idle minutes (`claimIdleSeconds`), and in-flight actions keep theirs. `bind_target` claims or releases `window:<id>`. Closing a session cancels queued work, releases targets and approvals, closes temporary agent-created tabs, and releases claimed personal tabs without closing them.

Supply an `operation_id` before submitting an action and reuse it only for an identical request; the server returns the existing outcome instead of running it again. `operation_status` reconciles timeouts and `cancel_operation` stops queued work, but neither can undo input already delivered. `timeout_ms` includes approval and queue time and is capped at 60000. Completed explicit IDs are kept for one hour (`operationRetentionSeconds`); calls without an ID keep no history unless they time out. `status.runtime` reports retained operations, result bytes, and open HTTP connections.

Native batches cannot cross app processes. Browser batches cannot cross tabs. Both run in order and recheck admission between steps. A native step is the tool name plus that tool's arguments, `{"tool": "click", "element_index": 5}`; the older `{"tool": "click", "args": {...}}` still works, with `args` as an object or a JSON string. `browser_command`'s `arguments` may also be a JSON string. Both allowances exist because Gemini through Cursor stringifies free-form nested objects and garbles the keys of declared ones. A malformed `args` or `arguments` is refused with an error naming the field, and a step missing a required argument lists any fields it did not recognize.

## Approvals

The agent needs the user's approval to read or operate an app. The first window tool or `launch_app` call for an app shows a local prompt: **Allow for this session**, **Always allow** (saved to `approvedBundleIDs`), or **Deny**. Concurrent calls for the same app share one prompt. Session approvals last until the session closes; for the `legacy` session, until the turn ends or the app quits. `appApproval: "off"` in the config turns this off. Blocked apps (`blockedBundleIDs`) are never reachable.

Sensitive browser operations always ask, once per call: `user_tabs`, `claim`, `history`, `clipboard`, `cdp`, and `upload` (which hands local files to a web page). A claim lasts until the tab is released, the session or turn ends, or the tab sits idle for five minutes. `confirmationRequiredTools` lists extra tools that always ask. Prompts name the app, site, and files involved, expire with the call deadline, close on cancellation, and are the one place the app takes focus. The model cannot approve itself; the extension checks the grant again on its side.

`approvals` in the config decides how every prompt is answered: `prompt` (default), `deny` (refuse without UI), or `allow` (grant without UI, honored only for disposable test instances started with `SC_HOME`).

Approvals limit what an agent can do through Second Cursor's tools. They are not a sandbox: an agent that can also run shell commands as you can edit the config, or start its own instance with `SC_HOME`, and that instance inherits the app's Accessibility and Screen Recording permissions. If an agent has unrestricted shell access, treat approvals as a guard against mistakes, not against a determined agent.

## Agent presence

While an agent works, its cursor (a black arrowhead with a white outline and an orange glow) stays on screen at its last position, and the menu bar icon turns orange with "Agent active: <app>". The cursor springs to each pointer action, or travels an arc on long moves, and the input is sent once it lands, as in Codex: about 0.2 s for a short move, at most 0.5 s for a long one, never more than 1.5 s. The cursor belongs to its window: it sits directly above the target app's frontmost window under its point (so the app's own menus and sheets stay beneath it), shows wherever you can see that window, and hides while another window covers the point or the window is on another Space. The agent keeps working either way. Only a cursor you can see is waited for, so actions on covered windows, windows on another Space, and background tabs go at once, and a hidden cursor jumps to its new point so it is in place when the window comes back into view. At rest it breathes and wobbles every few seconds so a running task looks alive; with Reduce Motion it stays still. Reads, keyboard actions, pointer actions, and browser calls all keep presence alive, and the menu bar indicator shows it whether or not the cursor is visible. Both clear when the turn ends, the session closes, you pause the agent, or after 60 s without agent calls. `status.agent_activity` reports `{active, app, sessions, arrow, arrow_visible, caption, idle_seconds}`.

Action tools take an optional `caption`: a few words shown in a pill beside the cursor, such as "Opening the Export menu". An action with a caption shows it; an action without one clears it; reads leave it. A `batch` takes one caption, and each step may set its own from that step on. Captions are trimmed to one line of at most 80 characters and end with the turn. Browser actions (`browser_click`, `browser_type`, `browser_press_key`, `browser_scroll`, `browser_navigate`, `browser_command`) take a caption too; the extension draws the same cursor and caption inside the agent's tab, and hides them from the agent's own screenshots, snapshots and DOM reads.

## Turn end

`POST /turn-ended` (same token and origin checks as `/api`, optional body `{"session_id": "…"}`, default `legacy`) resets that session's diff baselines so the next read is full, clears its session app approvals, and releases personal tabs it claimed. Agent-created tabs stay open and the session stays open. It returns `{ok, session_id, released_tabs, cleared_app_approvals}`. `SecondCursor --turn-ended` calls it, always exits 0, and ignores stdin, so it can run as an agent-stop hook; `scripts/cursor-hooks.json` is a Cursor sample and `install-mcp --client cursor --with-hook` merges it.

## Finding elements

`find_elements` searches the session's latest observation of a window instead of reading the window again: `{window_id | app, text?, role?, limit?}`, with at least one of `text` and `role`. `text` matches each element's label, value and description, ignoring case: exact matches rank first, then prefixes, then substrings; within a rank, indexed elements come before plain text, then outline order. `role` is an AX role with or without the `AX` prefix (`AXButton`, `button`). The search covers every element the latest read walked (up to 2500, or three times `max_elements`), including elements a truncated outline left out; those get indexes like any other and stay actionable while they exist. The result is `{matches, total, observation_id, window_id}`: up to `limit` (default 20, 1–100) outline lines with `[index]` and frames, the number of matches before the limit, and the read that was searched. `hint` asks to refine the query when `total` exceeds the limit, or to read again when nothing matched. An action's observation counts as a read, so a search after an action sees its result; after a change without one (`observe: "none"`, another client, time passing), call `get_window_state` first. A window the session has not observed yet is read once (`read_window: true`), and that read becomes the session's observation and the baseline of its next diff. By `app`, it searches the app window the session observed last, or the app's main window when there is none. It is a read: the same gates as `get_window_state`, and nothing is logged.

## act and the compact tool surface

`act` is `batch` under another name: the same schema, arguments, results, errors and action log lines (logged as `batch`). Its description presents it as the way to act, with one step or many. `toolSurface` in the config chooses what `tools/list` advertises on `/mcp` and `/api`: `"full"` (the default) lists every tool except `act`; `"compact"` lists `act` and `find_elements` and hides `click`, `type_text`, `press_key`, `set_value`, `scroll`, `drag`, `perform_action` and `batch`. Every tool stays callable either way, so existing clients and the SDK keep working. The value is read at startup; anything other than `"compact"` counts as `"full"`. `python3 e2e/agent_smoke.py --run --config toolSurface=compact` measures a model on the compact surface.

## Native editing

- `select_text`: element index, exact text, optional prefix/suffix, and selection_type `text`, `cursor_before`, or `cursor_after`. Ambiguous matches fail.
- `paste`: `text`, `html`, or `md`. Markdown inserts source text. The clipboard is restored only if the paste still owns it, so a copy the user makes meanwhile survives.
- `inspect_element`: `offset` and `length` recover truncated text; the response includes `total_length`, `has_more`, selection, editability, and available actions.
- `perform_action`: choose an action listed in the observation (custom actions by the name shown).

## Extended browser interface

Use `browser_instances` to discover instance IDs and supply `browser_id` when more than one profile is connected. Agent-created tabs work without prompts. `browser_command` takes `operation`, `tab_id`, `browser_id`, session fields, and an `arguments` object for operation-specific fields (flat arguments still work). `operation: capabilities` returns argument help, the CDP allowlist, and which operations need approval.

Locators use one or more of `selector`, `role`/`name`, `label`, `text`, `placeholder`, `test_id`; matching is exact unless `exact:false`. `nth` selects an explicit match. `frame` is a selector or an array for nested frames, including cross-origin frames. Ambiguous matches fail.

| Operations | Arguments / result |
|---|---|
| capabilities | Operations, argument help, CDP allowlist, approval requirements |
| user_tabs, claim, release | Discover personal tabs and claim one (both ask); release without closing |
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
| upload | Absolute regular-file paths; locator or an intercepted chooser; asks first |
| screenshot | full_page or clip; PNG plus dimensions, scale and document-coordinate origin |
| viewport | width/height, or reset:true |
| logs, history | Console entries; history asks first, with query/from/to/limit (Unix milliseconds) |
| export | text/html/md/pdf; Google Workspace xlsx/csv/docx/pptx; youtube_transcript on watch pages |
| assets | List observed resources; action:bundle and asset_ids return selected bodies |
| clipboard | action read/write, text; asks first; the page origin must allow clipboard access |
| cdp | Asks first; only Page.getLayoutMetrics, Page.getNavigationHistory, DOM.getDocument and Accessibility.getFullAXTree |
| batch | Array of extended operation arguments, each flat or nested under `arguments` like the call itself; returns results and a final DOM observation |

A dialog-triggering action returns the observed dialog instead of waiting for the click to complete. File choosers are intercepted, so files can be assigned without moving the system cursor.

A tab or popup window that an agent tab opens (a `target=_blank` link, `window.open`) becomes an agent tab of the same session. If Chrome made it the active tab of the user's window, the tab the user was viewing is brought back. Chrome still shows the new tab for a few frames first, and nothing can stop a popup window from taking focus. Things the model did not ask about are queued per session and reported on the next result of `browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_press_key`, `browser_scroll`, `browser_close` or `browser_command`, as `events: ["[system] popup opened: tab 12 https://… (from tab 11)", …]`. The events are a popup opened, an agent tab closed by the page or the user, a download started, finished or canceled, and a dialog opened or closed. In a `batch`, each step's result carries its own events. Google Workspace exports need a signed-in document; YouTube transcripts need captions. These service-specific paths are not covered by the local fixture suites.

## JavaScript SDK

```js
import { SecondCursor } from './sdk/index.mjs';
const sc = await SecondCursor.local();
try {
  const notes = await sc.app('TextEdit');
  const area = notes.observation.elements.find((e) => e.role === 'AXTextArea').index;
  await notes.selectText(area, 'draft', { selection_type: 'cursor_after' });
  const { observation } = await notes.typeText(' (reviewed)');
  console.log(observation.tree);
} finally { await sc.close(); }
```

`SecondCursor.local()` reads the config (respecting `SC_HOME`) and creates a session. `App` and `Tab` handles keep the latest observation; actions adopt the observation they return, and `observe()` requests structured elements. `app()` refuses an ambiguous multi-window selection. `call()` exposes every tool. Errors carry `operationId`. `transport: 'mcp'` exercises `/mcp` instead of `/api`. `sdk/agent.mjs` is an optional model-independent loop; its adapter owns provider-specific encoding and its approval hook belongs to the host. For multi-step flows with branching, a short Node script using the SDK is often better than many separate tool calls; see `.cursor/skills/second-cursor/SKILL.md`.

## Validation

Every suite runs against a disposable test instance: the harness starts the installed bundle with its own `SC_HOME`, port, token, and config (`approvals: "allow"`, `appApproval: "off"`), and never reads or writes your config or restarts your app. Pass `--installed` to target your own instance instead, `SC_E2E_APP=<bundle>` to test another build, `--browser` to force the isolated headless browser, and `--approvals` for the interactive prompt scenarios (they show real dialogs and never click them).

```sh
python3 e2e/run.py        # core regressions
python3 e2e/policy.py     # approvals (deny mode, no UI), turn end, action log
python3 e2e/parity.py     # sessions, editing, browser
python3 e2e/review.py     # observation size, leases, bookkeeping (add --short-retention for expiry)
python3 e2e/instance.py   # test-instance isolation
python3 e2e/setup.py      # stdio proxy, doctor, turn end, install-mcp (--ui adds the onboarding panel)
python3 e2e/cursor.py     # arrow and menu-bar presence, drawn on the agent display (cursorOnAgentDisplay)
python3 e2e/clicks.py     # every pixel click, double-click and drag acts exactly once
python3 e2e/focus.py --takeover   # focus safety; takes focus and covers the screen while it runs
python3 e2e/latency.py    # per-call timings, written to latency.json
python3 e2e/expect.py     # expect postconditions, wait_for, notification-driven settling
python3 e2e/find.py       # find_elements, act, the compact tool surface
python3 e2e/ocr.py        # text recognition; a small custom-drawn fixture window on the agent display, timings in ocr-timings.json
python3 e2e/agent_smoke.py --run   # a real Cursor agent drives the tools (costs model usage)
```

Failure cases written before implementation are in `docs/parity-acceptance.md` and `docs/review-acceptance.md`. Reports, transcripts, screenshots, and exported files are written to `e2e/artifacts/<run-id>/`.
