# Task runtime integration acceptance

These failure cases are defined before the integration changes. Every desktop check runs against a disposable `SC_HOME`; interactive approval tests remain opt-in.

- Two ordinary initialized MCP connections must receive distinct runtime sessions. One client's target lease, observation baseline, cancellation, or turn cleanup must not affect the other.
- A task pause or takeover arriving while a call waits in a native/browser queue must prevent that queued input. A running input may finish, so takeover must report outstanding work and resume must wait for it to drain.
- After resume, the first mutation of each previously bound target must require a fresh observation. A read while paused must not count as a post-resume observation.
- A batch and a browser REPL must use the same task gates as individual actions. Read-only status, inspection, and control must remain available while a task is paused.
- Only an explicit checked postcondition may produce a verified outcome. A returned observation proves what was seen, not that a click achieved its intent. Transport timeout or crash must remain uncertain until reconciled.
- Restart must recover task metadata as interrupted and retain mutation reconciliation records without automatically repeating an action.
- Native unsupported gestures and stale layouts must refuse before input. Browser capability metadata and tool schemas must agree about supported gestures.
- The task panel must show host-reported thinking and needs-user states without treating a quiet minute as completion. Per-task control must not pause unrelated work.
- Existing SDK, MCP, native editing, browser, cursor, policy, and isolated-instance suites must remain valid; changed compatibility requirements must be explicit in the client contract.

Verification records must distinguish a failure, an environmental skip, and an inconclusive run affected by external input. No passing compile or successful dispatch alone establishes task completion.
