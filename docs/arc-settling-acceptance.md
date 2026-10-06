# Arc-inspired settling comparison

`e2e/arc_settling.py` compares frozen Second Cursor app and extension versions using disposable browser/native fixtures. It is a deterministic driver experiment: no model chooses actions, and its results do not establish model reasoning quality or overall computer-use superiority.

## Run the same fixtures against both versions

```sh
SC_E2E_APP=build/arc-eval/baseline/SecondCursor.app \
SC_E2E_EXTENSION=build/arc-eval/baseline/extension \
python3 e2e/arc_settling.py --takeover --label baseline --repetitions 3

SC_E2E_APP=build/arc-eval/candidate/SecondCursor.app \
SC_E2E_EXTENSION=build/arc-eval/candidate/extension \
python3 e2e/arc_settling.py --takeover --label candidate --repetitions 3 --require-settling
```

Use `--browser-only`, `--native-only`, or `--cases slow_fetch slow_fetch_longpoll` for focused reruns. The runner creates an isolated headed Chrome for Testing instance, a separate Second Cursor home, and native windows on the agent display. Run GUI suites serially during an authorized quiet desktop window. The installed app and personal browser profile are not test targets.

The browser matrix covers an immediate change, four-second requests with consumed and unconsumed Fetch bodies, a four-second request with an immediate loading message, a four-second request alongside a preexisting thirty-second long poll, a streamed response whose final bytes arrive after four seconds, dialogs arriving at 0/300/800/1500 ms, a request that starts after 800 ms, a ten-pixel badge change while a caret blinks, and an unfinished request. The last case also runs with a screenshot-only response to verify that deadline metadata survives that public tool path.

Version 2 adds `slow_fetch_consumed` and `streamed_response`. The original slow-fetch cases intentionally leave their response bodies unconsumed; the new consumed case awaits JSON parsing. The streaming case sends headers and ten bytes of an eighteen-byte identity-encoded JSON body immediately, then sends the rest four seconds later. The application awaits the complete JSON before updating. Request evidence records partial and complete delivery separately, so a network settler cannot pass by treating headers or partial bytes as completion. Keep earlier version-1 artifacts as pilots rather than silently combining different fixture versions.

Browser readiness normally follows request completion events. Chrome can omit completion when the page leaves a Fetch body unread, so the candidate can also recognize the exact declared number of received, uncompressed body bytes. `settling.body_completion_inferred` exposes that narrower evidence. Headers alone are insufficient; compressed, chunked, cached, service-worker, ambiguous, and redirected responses require the ordinary completion path and can reach the deadline. This is a readiness hint, not proof that the application accepted or persisted a mutation.

The native matrix covers a custom drawing surface whose accessibility tree remains unchanged while pixels change at 0/300/800/1500 ms, a ten-pixel badge, and the same badge with a continuously blinking caret. The action uses public `perform_action`, then public `get_window_state`. The candidate opts into `visual_settle` when advertised; the baseline uses its existing screenshot behavior. Each row records whether the option was requested.

## Evidence and acceptance

Each run writes the existing `report.md`, `results.json`, MCP transcript, app logs, and screenshots, plus `arc-settling.json` and per-trial timing files. The manifest hashes the actual app binary, effective extension resources, fixture source, and runner/observer source before and after the cohort. A changed manifest fails the run. Preserve failed and inconclusive artifacts alongside successful repetitions.

- `return_ms` measures the browser action call, or the native action plus the immediately following screenshot read. It excludes fixture setup and the final outcome wait.
- `effect_lower_ms` and `effect_upper_ms` bracket the first independently observed effect. A return inside that sampling interval has unknown ordering; it is not classified as early or complete from timing alone.
- Browser effect sampling uses a separate read-only CDP connection at approximately 25 ms intervals. It reads layout and computed CSS visibility without injecting events or modifying the page. This proves DOM-visible state, not physical compositor presentation, and its polling overhead is present for every driver.
- Native effect timing comes from an atomically written fixture draw receipt. Separately, the returned screenshot is decoded and judged for the expected magenta pixels, including the small badge. A draw receipt alone does not prove the tool returned fresh pixels.
- `ready_in_result` means the browser's returned outline includes the marker or the native returned screenshot includes completion pixels. Screenshots and observations remain available for review.
- Default runs characterize missing returned effects without hiding them behind a failed aggregate. `--require-ready` turns those misses into acceptance failures. A blind delayed timer outside the configured reaction horizon remains a measured limitation, not an implicit readiness guarantee.
- `--require-settling` requires public MCP settling metadata. An unfinished browser request must return `timed_out` with a positive pending count, including the screenshot-only path; returning before an effect in this intentional deadline case is expected and must not be pooled with ordinary early-return failures.
- Every run always checks single dispatch, bounded return time, expected eventual fixture outcome, observer errors, and the existing frontmost-app, active-Space, and pointer invariants. External activity makes an invariant inconclusive; a green timing row cannot override it.

The fixture action counter lives in a JavaScript variable, not a DOM attribute, so measurement instrumentation cannot accidentally wake a mutation-based settler. The explicit loading-message scenario exercises that real application behavior separately. Completion is observed after the actual public-tool action; the evaluator never performs or retries a mutation.

## Local harness verification

```sh
python3 -m unittest discover -s e2e -p 'test_arc_settling.py' -v
xcrun swiftc -typecheck e2e/fixtures/arc-settling.swift
```

These checks cover timing interval classification and fixture server lifecycle without launching a browser, native app, or model. Live artifacts are still required to assess driver behavior.
