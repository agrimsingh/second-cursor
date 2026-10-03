# Task supervision acceptance cases

The task panel uses the runtime registry as its source of truth. The menu bar remains an accessory app; opening a panel is always a user action, except the existing first-run permission flow. Build and runtime verification are coordinated by the main implementation agent.

## Failures to prevent

1. A model that thinks for more than 60 seconds must not look completed or disappear from the task list. The runtime status remains visible until completion, interruption, cancellation, or an explicit pause. An abandoned host may become interrupted, never silently successful.
2. Multiple named tasks must remain independently visible. Pausing, taking over, resuming, or cancelling one task must leave other tasks unchanged.
3. Takeover must be an enforced ownership transition. A task with dispatched input still in flight must show that it is waiting to stop, and Resume must remain unavailable until the runtime can safely admit fresh work.
4. Resume must announce that a fresh observation is required; it must not show that the task is running with a previously captured target state. A global pause remains visibly distinct from task suspension.
5. Unverified delivery must never be presented as a verified outcome. The panel shows only the registry's last verified outcome under that label; tasks without one say so.
6. An interrupted app relaunch must keep unfinished tasks visible as interrupted. Failed tasks remain readable instead of vanishing when their cursor disappears.
7. A missing or closed app must not be launched by a target button, and no target app may be raised without the user's explicit Show app action. Window/tab identifiers are displayed without claiming the UI can reveal an exact window or tab.
8. A long task name, many targets, an error, or a narrow/resized window must wrap or scroll without clipping controls. Empty, active, paused, interrupted, failed, and completed states need readable native text and accessibility labels.
9. Closing/reopening the panel must preserve registry state and update the selected task. Removing/finishing a selected task must never route a later action to a different task.
10. Per-task operation errors must be visible with enough detail to explain the refusal; an exception must not be swallowed while the row looks successful.
11. The menu must offer Tasks and Setup directly and summarize active, attention-needed, and paused work without inferring completion from cursor visibility. The ambient cursor may hide while paused or obscured without erasing task lifecycle.

## Coordinated verification

Use a disposable SC_HOME and fixture tasks. Verify independent task controls through the runtime, inspect the panel at its minimum and default size, inspect UI accessibility labels, and exercise delayed thinking beyond the old 60-second cutoff. Verify read-only setup checks independently of actual permission prompts. Permission/foreground takeover scenarios requiring human interaction remain explicitly gated by the harness approval mode.
