# Computer-use parity acceptance contract

Written before implementation. All checks run against the installed app over HTTP/MCP and real native/browser targets. No unit tests or mocked drivers.

## Failure cases

- Two clients read the same window: one client's diff must never use the other's baseline. A closed/unknown session cannot act. A claimed target cannot be changed by another session.
- A stale observation or removed element must fail before sending input. A resized pixel target must require a fresh observation. Changing enabled/focused/selected/expanded state must appear in observations; unavailable state must remain null.
- Long text and duplicate text must be recoverable by range; an ambiguous selection must fail, disambiguation must select exactly one occurrence, and caret-before/after must use UTF-16 ranges including emoji.
- Native text/HTML/Markdown paste must insert correctly, restore the clipboard if still owned, and must not overwrite a clipboard changed by the human during paste.
- Pause, session close, deadline expiry and cancellation while queued must stop work before dispatch. A dispatched timeout must remain queryable with its eventual outcome. Retrying an operation ID must not duplicate input.
- Batch steps targeting another process must be refused; concurrent operations within one app/tab must be serialized. Global focus handoffs must not overlap.
- Browser instances/profile connections must coexist. Disconnecting one must not fail the other's requests. Claim/release must leave user tabs open; session cleanup must close only unretained agent tabs.
- Browser snapshots must expose state/actions/frames and recover truncated values. DOM locators must reject ambiguity, work in nested frames, and return grounded text/attributes/state.
- Browser fill/check/select/paste/select-text/click variants/drag/targeted scrolling/keys must produce visible expected changes. Navigation, back/forward/reload and waits must have bounded deadlines.
- Dialog inspection/accept/dismiss, chooser file assignment and downloads must work on a local fixture. Upload paths must exist and errors must be actionable.
- Viewport, cropped and full-page screenshots must have correct dimensions and coordinate metadata. Console logs, history, content exports, page assets and optional CDP must yield usable results.
- REST and JavaScript SDK must invoke the same policies, sessions and operations as MCP. Persistent SDK handles must survive multiple calls and return fresh observations.
- Existing E2E scenarios must continue to pass. At completion save results.json, report.md, transcript.jsonl, screenshots, exported files and exact rerun commands. A skip is not a pass.

## Commands

`python3 e2e/run.py` — original native/browser regressions.
`python3 e2e/parity.py` — parity cases against the running signed app and an extension-enabled browser.
`node sdk/smoke.mjs` — SDK/REST live contract and browser workflow.

Human typing coexistence requires a real concurrent human input experiment; automated before/after focus checks alone do not establish it.
