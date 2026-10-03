# Model benchmark acceptance

Failure cases declared before implementing the runner:

1. A solver inherits shell, filesystem, browser connectors, repository instructions or previous conversation state and reads the fixture or judge. Each trial needs a fresh isolated model context with only scoped computer-control tools. Unsupported tool isolation must fail closed.
2. A tool call addresses a personal window, tab, app or URL; a nested batch bypasses the restriction; an evaluation or direct HTTP tool fabricates the expected outcome. Validate every call before dispatch and bind allowed targets from the harness, never from the model.
3. Different models, reasoning settings, budgets, observation interfaces or trial schedules get compared as equivalent. Record and validate configuration, host/version and engine versions before comparing artifacts.
4. A mutation times out and the adapter replays it. Preserve uncertainty and never retry mutations automatically. Duplicate submissions and false success claims must fail the independent judge.
5. The model exceeds its call or wall-clock budget, leaves a tool request unanswered, or exits without a completed turn. Stop the process and record failure, retain partial evidence, and clean up only owned targets.
6. Text-only conversion discards screenshots or model tool arguments lose their native schema. Preserve MCP text/image content and native tool definitions through the common host.
7. Tool/model timing and token cost are guessed from total wall time. Record measured tool intervals and provider token usage; leave unavailable inference timing or cost unknown.
8. A comparison averages only successful trials or treats a safe refusal as task completion. Include failed/unsupported trials; score outcomes independently from safety.
9. Benchmark startup or cleanup touches the installed personal runtime or closes personal Chrome. Use disposable Second Cursor homes and explicitly owned Cua browser processes; never infer ownership solely from an app name.

Verification will use malformed-protocol, target-escape, timeout and telemetry tests without UI first, then a no-desktop model-host smoke test, and finally disposable live tasks through each driver. Live outcomes, engine setup failures and disturbed coexistence checks remain distinct evidence. The existing fixed-task pilot is not a randomized task-success benchmark or proof of superiority.
