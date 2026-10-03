# Task ownership acceptance criteria

These are failure cases to exercise, not claims that the release has passed them. Use a disposable `SC_HOME`, scratch files, and isolated browser fixtures. Record the app revision and artifact directory with every run.

| Failure | Required observation |
| --- | --- |
| Two ordinary clients silently share a task. | Independent initialized transports get different task and runtime-session IDs, and one cannot mutate a target claimed by the other. |
| A long model think is displayed as completed or idle. | Explicit `thinking` and its heartbeat remain visible until the host changes the state, including after 60 seconds without a tool call. |
| A heartbeat or title edit bypasses pause. | Updates cannot transition `paused` or `needs_user` to a mutating state; only explicit resume can. |
| Pausing after admission still permits queued input. | An input check at dispatch rejects the old operation generation. Batch steps and long input recheck the same permit. |
| Takeover releases ownership while input is still running. | New input stops immediately, `in_flight_count` remains nonzero until the worker settles, and resume refuses while it is nonzero. Ownership cleanup runs after the worker drains. |
| Resume reuses a pre-takeover target. | Input fails until a fresh observation of that exact target after resume. Reading another target or reading while paused does not satisfy the requirement. |
| A read begun before resume later clears the freshness gate. | The read's captured control generation differs from the resumed generation, so it is rejected as freshness evidence. |
| A delayed pause callback releases newly resumed ownership. | Cleanup checks its control generation; an obsolete control event cannot release a current claim. |
| Cancelled work starts again. | Input and resume refuse for a cancelled task, including a previously queued operation. A new task requires an explicit create. |
| A tool's successful dispatch is displayed as successful task completion. | The task returns to `thinking`; only host completion ends it, and `last_verified_outcome` comes from an actual postcondition. |
| A transport interruption or restart silently completes or reruns work. | Nonterminal journal entries become `interrupted`; a live session must explicitly bind and resume. Outstanding dispatched operations reconcile before retry. |
| Reusing a cancelled session leaves the browser paused. | New tasks advance the persisted session generation, then a new browser form submission succeeds. |
| A disconnected browser falsely acknowledges resume. | The task remains `needs_user`, exposes `last_control_error`, and refuses new input until control synchronization succeeds. |
| An unreadable journal is silently replaced. | The original bytes remain untouched, `persistence_error` remains visible, and mutations fail closed until the operator repairs the file and restarts. |
| Task persistence exposes page contents or secrets. | The journal contains metadata, opaque IDs, target identifiers, and short verification descriptions, without prompts, screenshots, URLs, or action arguments. |
| Taking over steals focus automatically. | MCP takeover/resume changes ownership and gates only. Revealing an app is a separate explicit UI action. |

Run `SC_E2E_APP=build/SecondCursor.app python3 e2e/tasks.py` for task lifecycle, pause/takeover/resume, cancellation, browser control failure, heartbeat, and restart recovery coverage. The heartbeat case deliberately crosses the former 60-second activity timeout. The restart case terminates only its disposable Second Cursor process and preserves its disposable home across that restart. Browser cases use the harness's isolated browser; the control failure case stops and restarts only that browser.

For deterministic registry races and corrupt-journal handling, compile `swiftc Sources/SCShared/TaskRegistry.swift e2e/task_registry.swift -o /tmp/sc-task-registry-tests`, then run `/tmp/sc-task-registry-tests`. This uses the production registry with Foundation-only host dependency stubs; the MCP/browser/native integration remains covered by `tasks.py`.

Passing deterministic fixtures establishes those tested behaviors on the tested macOS/app versions. It does not establish universal app support or absence of all input races. Record changed Space/focus/cursor checks during external human input as inconclusive and rerun them before making coexistence claims.
