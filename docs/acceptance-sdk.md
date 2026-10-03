# SDK reliability acceptance

The SDK must preserve the information needed to reconcile an action when the connection disappears. A client-side timeout or abort never proves that the server did not act, and the SDK must not retry a mutation automatically.

## Failure cases defined before implementation

1. The host accepts a mutation, records the outcome, and drops the socket before sending its response. The caller receives a typed error with the original operation and session identifiers, reports an uncertain outcome, and can read the recorded result without a second mutation.
2. A client timeout or external AbortSignal interrupts a request after dispatch. Both errors retain reconciliation identifiers; a signal aborted before dispatch makes no HTTP request and reports no uncertain action.
3. A response has invalid JSON, missing MCP content, or malformed JSON inside its text content. Parsing errors retain the operation and session rather than escaping as anonymous SyntaxErrors.
4. HTTP errors and JSON-RPC/tool errors remain distinguishable from network errors. A failed result callback carries the successful result so consumers do not mistake callback failure for a missing action.
5. Concurrent first calls obtain one host-issued session and use it consistently. Ordinary SDK calls never silently share the legacy session. Reconciliation uses the failed operation's session and rejects a mismatched client instead of querying another task.
6. Native actions and browser actions/navigation adopt fresh observations and send their latest token on the next action. Failure invalidates a cached observation, requiring a fresh read before assumptions can be reused.
7. Task creation, list, status updates, pause, takeover, resume, and cancel forward the public task contract. Takeover/resume invalidate local cached observations; the runtime remains authoritative for whether a new observation is required.
8. Closing a client clears its session only after the server confirms closure. A failed close preserves the session for retry/reconciliation. No SDK path automatically repeats a mutation.
9. HTTP and MCP encodings expose the same application results and errors. MCP JSON-RPC errors cannot be mistaken for successful tool output.
10. Concurrent initial callers cancel and time out independently while one host-session negotiation completes. Cancelling the first caller must not fail the other callers, and cancelling a later caller must stop its wait promptly before dispatch.
11. Session/task/target control calls that bypass the operation journal expose a different recovery route. `reconcile(error)` must not pretend those IDs can recover a payload; task-control failures direct the caller to `task().get()`, preserving the same session and task identity.
12. A confirmed close followed by a callback failure still clears the known-closed session. Old handles remain invalidated when the client reconnects, so the caller must read the new session's target state.
13. Extended browser mutations can return verified control evidence without a DOM snapshot. The SDK reads and adopts one fresh snapshot after that response, preserving the original action result if the read fails and never repeating input. Explicit `observe: "none"` opts out and leaves the handle invalidated until a new read.
14. Closing or releasing an explicitly owned tab does not send a DOM observation token, because those operations address ownership rather than a document element or coordinate. The server still verifies ownership, and the SDK invalidates the released target's cached observation.
15. Read-only browser commands omit cached input tokens so the caller can inspect a page that has changed. They do not silently refresh the token used for subsequent input; only an actual document observation does that.

## Verification

`node sdk/recovery-smoke.mjs` runs real loopback HTTP servers with controlled disconnects and verifies dispatch counts, session ownership, aborts/timeouts, error metadata, observations, and task control. It does not control the desktop. `node sdk/smoke.mjs` and `node sdk/stdio-smoke.mjs` remain live-runtime checks and must be run only when the shared desktop verification owner permits them.

Type declarations must cover the SDK's exported classes, request options, typed errors, observations, reconciliation, and task lifecycle. JavaScript syntax checks and a TypeScript consumer check verify package export resolution.
