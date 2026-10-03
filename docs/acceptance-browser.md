# Browser reliability acceptance

The acceptance suite drives the shipped MCP surface against a disposable Chrome profile and app instance. A passing run establishes the behaviors below on that app/extension/Chrome build; it does not establish reliability on every website or a competitive benchmark.

Run from the repository after rebuilding the app bundle and extension:

```sh
SC_E2E_APP=build/SecondCursor.app python3 e2e/browser_reliability.py
```

The normal harness records the complete MCP transcript, checks independent focus/cursor/clipboard invariants, and writes `report.md` and `results.json` under `e2e/artifacts/<run-id>/`. Checkbox timing samples and extension phase measurements are also written to `browser-checkbox-settling.json`. Do not run multiple GUI acceptance suites concurrently: they share the macOS foreground and can invalidate one another's invariants. The default disposable run uses no approval dialogs and leaves the installed user's configuration untouched.

| Failure to catch | Scenario | Required evidence |
| --- | --- | --- |
| Native checkbox property changes still wait for the full reaction timeout. | `browser_checkbox_settling` | Five clicks return the expected checked state in their own observations. Median extension settling is below 800 ms, or median client time below 1,100 ms when phase timing is unavailable. |
| Faster input settling misses delayed application results. | `browser_delayed_search` | The typing response itself includes all three results from a 300 ms debounce followed by a 150 ms HTTP response. A later successful read cannot satisfy this assertion. |
| Dispatch or a detached target's state is mistaken for a verified form result. | `browser_truthful_outcomes` | Accepted check/fill/select operations report the expected and actual value as verified. Reverting handlers, an impossible radio uncheck, and a checkbox replaced during its handler report uncertain. A separate public read confirms the live state, and secret fill values remain redacted. |
| REPL methods discard uncertainty and silently allow an agent to continue. | `browser_repl_uncertain_outcomes` | Check, uncheck, fill, and select methods reject with a visible verification error when the handler rejects the requested state or replaces the control. The REPL remains usable, and a fresh locator read confirms the actual live value. |
| Old refs or coordinates can act after same-page reflow or scrolling. | `browser_freshness_guard` | Snapshots carry capture time, age, a revision, and a current status. After a fixture button changes layout or only the viewport, stale ref and coordinate calls fail before clicking the target. A fresh snapshot permits exactly one intended click. |
| Freshness checks break existing safe batch workflows, or aggregate outcomes conceal partial failure. | `browser_live_locator_sequence` | Three stable-ref actions and a live-locator batch with an intervening layout change all reach the intended elements without an observation token. A successful `observe=none` tab batch reports dispatched and unverified. A valid/invalid/valid sequence reports failed and unverified, preserves each step's result, skips the last step, and causes exactly one additional application-side effect. |
| Hover moves only the cursor overlay, synthesizes an untrusted event, or silently ignores unsupported arguments. | `browser_trusted_hover` | Extended hover, REPL locator hover, and grounded REPL mouse movement reach the page with trusted events, including mouse leave and reentry. An unsupported mouse button is refused before mouseenter or click reaches the page. |

The fixture changes through ordinary buttons, input handlers, and timers. The tests never inject page JavaScript to manufacture a passing state. Freshness checks use `snapshot_id` as the next action's `observation_id`; generic input reports `outcome.state=dispatched`, while direct form mutations may report `verified` or `uncertain` with their checked value, text value, or selected values. Every outcome carries a nonempty reason and a boolean `verified` that is true only for the verified state. A verified form mutation establishes its immediate DOM postcondition, not a server-side save or a guarantee against later application changes.

Keep the existing browser batch, covered-target, navigation, redaction, REPL, and focus suites in the release check. This suite covers the new contract and regressions that motivated it; those suites cover the surrounding browser behavior it must preserve. Record the exact artifact directory and pass/fail/inconclusive counts after execution rather than presenting static inspection or a syntax check as a live pass.
