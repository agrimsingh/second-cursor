# Cursor model host acceptance

Cursor is a separate model host, while Second Cursor remains the computer-control
driver. Compare host/model treatments explicitly; never relabel a Cursor run as
the existing Codex treatment or weaken ordinary driver comparison checks.

Before desktop use:

- Authenticate through the official SDK login or `CURSOR_API_KEY`. Never extract
  IDE/CLI session credentials or write keys to benchmark evidence.
- Discover the canonical model and every parameter in the account's SDK catalog.
  Require explicit reasoning, Fast, and context when the catalog exposes it; reject defaults,
  fallback models, missing parameters, and changed resolved selection.
- Use a fresh empty workspace/store, only supplied custom tools, no ambient
  settings/MCP servers/plugins/subagents, and no automatic agent retries.
- Pin the SDK runtime and Node version. Record the actual isolation evidence
  basis. An SDK option acknowledgement is not an observed tool inventory.
- Serialize native callbacks and enforce step/deadline limits outside the model.
  Reject unknown tools, duplicate call IDs, malformed protocol, and model mismatch.
- Preserve MCP text, images, and structured content; retain raw usage accounting
  with its provider identity rather than applying Codex token semantics.
- Exercise a synthetic custom-tool round trip before a native trial, and retain
  failed attempts. Native success requires the existing independent judge.
- Reap owned processes and remove disposable state even after failure.

Failure tests precede implementation. Live tests use no personal documents,
external accounts, model-accessible files, or hidden judge data.
