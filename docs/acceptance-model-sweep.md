# Model and host selection experiment

Compare five declared configurations with Second Cursor held constant:
Grok 4.5 Fast low/medium, Grok 4.7 Medium Fast, and Sol 6.1 medium/high.
The Grok runs use Cursor SDK; Sol uses Codex. A cross-family difference includes
the host runtime and prompt, so it cannot be attributed solely to model inference.

All configurations receive the same tasks, fixture builds, driver binary, tool
catalog, budgets, and case identifiers. Use fresh fixtures and a fresh host for
each trial, alternate condition order, and retain every planned case in the
denominator. Ordinary cross-driver comparison must continue rejecting different
model configurations. The separate sweep explicitly permits only its declared
host, model, effort, parameters, and host-version differences.

The primary screen uses `workspace_reconcile`, `workspace_handoff`, and
`workspace_recovery`, with 100 tool calls and 300 seconds per trial. Case seeds
generate matched business records and display order. The default five model
profiles are unchanged; repeat `--profile` to select a smaller cohort. Workspace
trials use on-demand observations consistently across models.

The earlier Calculator editing, work-order form, and dialog tests remain explicit
`--task` regressions. Native-only selections retain upfront observations, and the
separate `model_efficiency.py` experiment keeps its existing native task and budgets.

Independent exact outcome judging is required. Record incomplete, refused,
failed, interrupted, and setup-blocked trials without silently retrying or
dropping them. Report correct completion separately from coexistence checks;
only strict passes contribute to latency summaries. Never declare a model the
winner merely because its successful subset was fastest.

Preflight without a model verifies the disposable browser and fixture. During
native trials, the focus fixture observes activity/key/responder changes.
Browser launch can change foreground during setup; retain that evidence and do
not claim unattended background coexistence from task success.

Model costs and inference time remain unknown unless directly measured. Usage
keeps its provider semantics. Earlier easy-task timings remain regression evidence
and do not establish the primary model recommendation. The workspace cases test
context gathering, record preservation, and recovery; small synthetic samples still
cannot establish general intelligence or a best-in-class ranking.
