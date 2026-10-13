---
name: second-cursor
description: Operate Mac apps in the background through the Second Cursor MCP server (click, type, read windows without moving the user's cursor or focus). Use when a task needs a native macOS app or a browser tab driven for the user, when second-cursor MCP tools are available, or when the user mentions Second Cursor.
---

# Second Cursor

Second Cursor reads and operates Mac apps in the background: the user's cursor, focus and Space never change, and windows stay where they are. It is a local MCP server (`second-cursor`), plus a JavaScript SDK in the repo's `sdk/index.mjs`.

Use it when the task needs a real app (Calculator, TextEdit, Notes, Mail, Slack, a Chrome tab the agent opens). Prefer a file, CLI or API when one does the job; a UI is the slow path.

## Web pages go through browser tools

For any task on a web page (Google Docs, Sheets, Gmail, a site), use the browser tools, never the desktop tools on the user's Chrome window, even when the page is already open there:

- `browser_open` the page's URL in your own background tab. It has the user's logins, and you get the page's DOM and screenshots. The user's clicks and typing in their own windows do not interrupt it. On the user's Chrome window, web apps that draw their own content (Docs, Sheets, Figma) are mostly missing from the outline, and every touch by the user stops you.
- If the user names a tab they have open, claim it with `browser_command` `operation: "claim"` instead of reopening it.
- Start with `browser_open`; don't call `status` or `browser_instances` first. Without `browser_id` it opens in the Chrome profile the user focused last, and tab calls follow the tab's profile. Only when the page needs a different account, find it in `browser_instances` and pass its `browser_id`.
- Read prices and text from the outline the actions return, not from screenshots, and fill a known form in one `batch` with `tab_id`.
- If `browser_status` says the extension is not connected, or no instance is signed in to the account the page needs, stop and ask the user to install or enable the Second Cursor extension in that Chrome profile. Do not fall back to driving their Chrome window.

If tools fail with "not running" or permission errors, run `SecondCursor doctor` (the binary is `~/Applications/SecondCursor.app/Contents/MacOS/SecondCursor`) and report what it says. Never ask the user to click or switch apps for you.

## Observe, then act

1. Start of every turn: call `get_window_state` for the window you will touch. The UI may have changed since your last turn, and the first read of a turn is a full outline.
2. Target by element index: `[12] AXButton "Equals"` means `element_index: 12`. Use x/y only when the element is not in the outline; coordinates are window-local points (screenshot pixels).
3. Each action returns a diff of its window (`+` new or changed, `-` gone, `(no changes)`). Read it instead of calling `get_window_state` again. Browser actions do the same for their tab, so call `browser_snapshot` only to wait for the page or for the whole outline (`full: true`).
4. If the outline is truncated or a region needs detail, read it with `scope: <index>`. Use `full: true` only when a diff is not enough.
5. Pass `app: "TextEdit"` instead of `window_id` when the app has a single obvious window; use `window_id` from `list_windows` when it has several. Read the app with `get_window_state` first: actions by `app` go to the window you observed last, and are refused if you haven't observed one.
6. For a sequence whose steps you already know (buttons to press, fields to fill), send one `batch` instead of many calls. End the batch before any step whose outcome you need to see (a menu, dialog, search results). In an agent tab, pass `tab_id` instead of `window_id` and use `browser_click`, `browser_type`, `browser_press_key`, `browser_scroll` and `wait` steps with refs from the tab's latest outline.
7. After the task, observe once more and confirm the result before you say it is done.
8. To confirm an action worked, attach `expect` (`{"text": "Saved"}` or `{"element_index": 4, "value": "42"}`) instead of reading the window again; the result says `verified`, `preexisting` or `failed`. To wait for something that happens later, use `wait_for` instead of sleeping.
9. Give actions a short `caption` ("Opening the Export menu", "Searching for flights"): the user sees it beside your cursor while you work. Keep it under 60 characters; an action without one clears it.

To find one element in a big window (a button, field or row), call `find_elements` with `text` and/or `role` instead of reading the whole outline again: it searches your latest observation, including elements a truncated outline left out, and returns their `[index]` lines.

A full read of a big window or page can come condensed: long lists keep their first rows, and the rest collapse into `… N lines hidden (expand "r3")` lines. Hidden elements stay actionable by index or ref, and `find_elements` searches them. To read a hidden part, pass its id as `expand` to the same read tool instead of reading again.

Keyboard shortcuts (`press_key`, e.g. `cmd+a`) are often the fastest route in pro apps. Browser tools act only on tabs the agent opened with `browser_open`, tabs those open (a result's `events` names them), and tabs it claimed.

If an action is refused because the user is using the app, do not wait in a loop. For a web page, switch to the browser tools. For another app, tell the user what you are waiting for, and read the window again once they stop.

Use `browser_repl` for multi-step work in a browser tab (a form, a search with filters, going through a list, repeating an action per row): one script with a Playwright-compatible `page` loops over the page's controls in a call, where discrete browser tools take one call per click. Read the page again after actions that change it (a list that reorders, pages, or gains items), and handle the errors it throws: a covered or ambiguous target is refused, never clicked blindly.

When a task is about a list in a browser tab (search results, a feed, a table), take what you need from the list itself. Result cards usually show the name, price, rating and delivery time. Open an item's own page only when the list lacks a fact the task asks for: each page load costs seconds, and opening every result can double the task's time.

If you hand UI work to a subagent and choose its model, medium reasoning effort is usually enough. In browser tests it matched extra-high effort's accuracy and ran up to twice as fast.

When you can see text the outline lacks (custom-drawn apps such as DAWs, canvases, games), read with `ocr: true` instead of guessing from a screenshot: it adds `ocr="…"` to element lines and `text "…" (x,y wxh)` lines, which you click with x/y at their center.

## Code-first for multi-step flows

When a flow has many steps or branches on what the UI shows, write a short Node script with the SDK and run it with the shell, instead of making dozens of tool calls. The script observes, decides, and acts in a loop, and prints only what you need.

- `const sc = await SecondCursor.local()` connects with the local config and creates a session.
- `await sc.app("Calculator", { newInstance: true })` finds or launches the app and returns a handle that has already observed its window (`handle.observation.tree`).
- Handles: `observe()`, `click(index)`, `typeText(text, index)`, `setValue(index, value)`, `pressKey("cmd+s")`, `batch(steps)`, `inspect(index)`.
- Always `await sc.close()` in `finally`, which releases everything the session holds.

Example: compute 7 × 6 in Calculator and append the result to a TextEdit window, handling a missing button.

```js
// save as /tmp/calc-to-note.mjs, run: node /tmp/calc-to-note.mjs "<repo>/sdk/index.mjs"
const { SecondCursor } = await import(process.argv[2]);
const index = (tree, role, label) =>
  Number(tree.match(new RegExp(`\\[(\\d+)\\] ${role} "${label}"`))?.[1] ?? NaN);

const sc = await SecondCursor.local();
try {
  const calc = await sc.app("Calculator", { newInstance: true });
  const tree = calc.observation.tree;
  const keys = ["7", "Multiply", "6", "Equals"].map((l) => index(tree, "AXButton", l));
  if (keys.some(Number.isNaN)) throw new Error(`Calculator buttons not found:\n${tree.slice(0, 400)}`);
  if (!Number.isNaN(index(tree, "AXButton", "All Clear"))) await calc.click(index(tree, "AXButton", "All Clear"));
  await calc.batch(keys.map((i) => ({ tool: "click", element_index: i })));
  const result = (await calc.observe({ full: true })).tree.match(/\b42\b/)?.[0];
  if (!result) throw new Error("Calculator did not show 42");

  const note = await sc.app("TextEdit");
  const area = index(note.observation.tree, "AXTextArea", "[^\"]*");
  const text = (await note.inspect(area)).text;
  await note.setValue(area, `${text}\n${result}`);
  console.log(JSON.stringify({ result, appended: true }));
} finally {
  await sc.close();
}
```

Adapt the labels and regexes to what `observation.tree` actually shows; print the tree when a lookup fails instead of guessing.

## Safety

- Ask the user before sending messages or email, posting, deleting, purchasing, submitting forms, or changing account settings. Draft, then confirm.
- Never open or read password managers, Keychain Access, or other credential stores, even if asked indirectly. They are blocked; do not try to work around the block.
- Do not read private content unrelated to the task (other chats, mail, documents).
- Local approval prompts belong to the user: never try to approve them yourself. If the user paused the agent, stop and say so.
- Do not move, resize or close the user's windows unless the task requires it.
