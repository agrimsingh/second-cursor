# Installation and release acceptance

Failure cases are defined before implementation:

- A missing requested Developer ID must stop a distributable release, without creating or substituting a self-signed identity. A local package must state that Gatekeeper notarization has not been verified.
- A packaged app must include its native browser helper and extension resources, and install the browser bridge without a checkout, Xcode, or administrator access.
- A stale native-host manifest must be detected and repairable. Unknown browser profiles must be left alone; a missing browser is a setup note, not a native-control failure.
- Client install must preserve unrelated settings and secrets, back up changes, remain idempotent, and refuse malformed files without modifying them. Diagnostics must not print bearer tokens or client secrets.
- Connection checks must distinguish server reachability, permissions, pause, client configuration and browser connection. A read-only connection check must not claim that native control has been proven.
- A user-invoked browser control test must create only its own disposable tab, click the visible test button, independently observe the changed result, and close its tab even on failure.
- Ordinary checks must not launch permission dialogs, mutate the user's existing browser tabs, or install client configuration unless the user selects that action.

## Repeatable acceptance

Run the existing `e2e/setup.py` suite against a disposable `SC_HOME` to cover real subprocess client merges, corrupted config refusal, HTTP/stdio connection and doctor status. Run `e2e/release_setup.py` for packaged resource integrity, offline release readiness, browser registration in a temporary home, setup diagnostics, and the explicit disposable browser control test. Browser control requires the isolated test browser and is reported as skipped when it is unavailable.

Use `scripts/package-release.sh --check` before packaging, and run `scripts/package-release.sh --skip-build` only after the canonical build has completed. The package manifest records trust state; a local package is not an Apple-notarized release. Signing and notarization verification are separate from runtime interaction verification.
