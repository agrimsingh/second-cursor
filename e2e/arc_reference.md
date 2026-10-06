# Optional Arc reference driver

This runner evaluates upstream Arc's browser action and settling behavior on the
same local delayed-effect fixtures as `arc_settling.py`. It makes no model calls
and does not load Arc's macOS input backend. It requires no API keys.

Install into the ignored, isolated environment:

```sh
uv venv e2e/.bin/arc-reference
uv pip install --python e2e/.bin/arc-reference/bin/python -r e2e/arc-reference-requirements.txt
e2e/.bin/arc-reference/bin/python e2e/arc_reference.py
```

The last command checks the pinned install and Chrome for Testing identity
without opening a browser. The upstream revision is
`6ca19d62c95106732fad28f488ecd458c08e02f4`. The runner refuses installations with
a different or missing VCS commit, and records hashes of the loaded backend,
runtime, and settling sources.

Run a visible reference cohort while other desktop fixtures are idle:

```sh
e2e/.bin/arc-reference/bin/python e2e/arc_reference.py --run --repeats 3
```

Select individual fixtures with repeated `--case immediate --case slow_fetch`.
Use `--output e2e/artifacts/<new-directory>` to choose the artifact location.
`--headless` is available for diagnostics, but those measurements must not be
compared with a headed Second Cursor cohort.

## Isolation and comparison boundaries

The runner starts its own Chrome for Testing process group with a new temporary
profile. It takes the same executable and launch flags as Second Cursor's test
launcher, excluding Second Cursor's extension flags. It does not use Arc's
launcher, because that adds background timer overrides that Second Cursor does
not use. The runner creates a background fixture tab alongside the initial blank
tab, matching Second Cursor's agent tab. Both backends use page focus emulation;
the independent observer records effective visibility and viewport dimensions.
No existing browser profile, user tab, native input path, credential, or shared
native-messaging manifest is touched. Cleanup terminates only the process group
started here and removes only its temporary directory.

All navigation is restricted to the harness's loopback fixture server. The
upstream `ChromeBackend` and `DesktopExecutor` are unmodified. A deterministic
policy selects the observed `Run action` control once, then returns. A separate
read-only CDP connection observes the fixture effect and action count; it never
repairs a missed action. Arc's completion status is recorded but is not accepted
as independent proof of success.

`return_ms` starts immediately before the policy returns its click decision and
ends when Arc's executor returns the settled observation. It includes freshness
checks, execution, settling, the final observation, and terminal bookkeeping.
`observe_act_observe_ms` additionally includes the initial target observation.
Browser launch, navigation, and deliberately unrelated longpoll setup are outside
both intervals. The shared observer reports an interval between the last absent
sample and first present sample, rather than claiming its polling timestamp is
the precise render time. This is DOM layout/CSS visibility evidence, not proof
that the compositor displayed the frame.

Arc's default runtime quiet/reaction/timeout values are preserved. Its browser
backend raises the effective settling timeout to 10 seconds; the manifest records
that difference. Second Cursor's public MCP transport and extension are part of
its timing, whereas Arc uses an in-process Python API. Compare readiness and
latency on individual shared cases, not broad computer-use rankings. Native
coexistence and model decision quality require separate tests.

## Artifacts and interpretation

Each run saves `manifest.json`, `results.json`, `arc-settling.json`, and the owned
browser log. The last JSON file uses the same rows and summary shape as Second
Cursor's settling runner, retaining unsupported cases and errors separately in
`results`. Both the manifest and comparison wrapper record fixture hashes. Every
measured result retains the returned observation, all independent observer samples, server
request history, exact action count, and deadline checks. A late effect remains a
miss in `ready_in_result` even if the observer sees it after the call returns.
The process exits nonzero for execution/observer errors, duplicate or absent
actions, deadline violations, and fixture contract violations. A correctly
measured premature return remains a benchmark result rather than a harness
failure. The never-ending-request fixture expects no completed effect and must
never be counted as a successful ready observation.

The screenshot-only case is retained as unsupported: Arc can return DOM plus an
optional screenshot, but does not expose Second Cursor's screenshot-only action
observation contract. This is not scored as a failed Arc task.
