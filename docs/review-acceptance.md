# 0.2 review correction acceptance

Failure cases recorded before the correction code. All behavioral checks use the installed app and real browser/native targets.

- A small native change must return a genuinely smaller diff, without repeating structured elements or common action/state annotations. Full observations retain structured state. HTML paste must still preserve formatting and the clipboard after conversion moves to the main actor.
- A legacy action must not acquire a permanent lease. An explicit active native lease must refuse competing input; after the configured idle interval, another session and legacy clients must recover without restarting. In-flight work must not lose its lease.
- Completed operation retries must deduplicate during the retention interval and expire afterward. Implicit successful calls must not accumulate retry history. Expiration must not evict active work. Completion must not sort all history under a lock.
- Invalid arguments must return a useful error without a reconcile hint. A transport deadline with unresolved work must still return the operation ID and reconciliation guidance.
- Sensitive browser discovery, claim, history, clipboard and allowed raw CDP require a local approval. No tool argument can self-approve; nested batches cannot bypass it. Declined, cancelled and expired prompts must never dispatch and must disappear within the call deadline. Cookie access and arbitrary evaluation via raw CDP must be rejected even before approval.
- Approval windows must visibly activate, remain responsive, and close on timeout/cancel. Action logging must distinguish refusal from admitted execution. A successful human approval cannot be simulated by the agent; document any untested success path.
- Browser diffs must preserve duplicate-line insertions/deletions and order. A full fallback is acceptable for large changes. Only one batch executor may remain.
- Closed HTTP channels must be removed; repeated short connections must not remain in the listener registry. Shutdown must still complete.
- Tool schemas must advertise only relevant control parameters. Extended-operation argument help must be discoverable on demand rather than repeated in every tool list.
- Move the run entrypoint into scripts/, update all live references, and format touched runtime code consistently. Existing regression and parity workflows must keep passing except privacy-sensitive calls, which must now be explicitly tested as approval-gated.

`python3 e2e/review.py` saves reports/transcripts/screenshots using the existing E2E harness. `python3 e2e/review.py --short-retention` temporarily configures short real retention/lease durations, restarts the installed app, runs the lifetime checks, then restores the original configuration and restarts. It does not bypass approval.

Approval scenarios are opt-in via `--approvals` because they deliberately display local UI. Ordinary review and parity runs do not open approval prompts. The consent scenarios must never click Allow themselves. If a person approves during the test, the lifecycle case records that success and verifies cancellation in a separate request.
