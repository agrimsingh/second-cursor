# Invoice workflow recovery acceptance

This contract is written before the fixture and runner. It exercises the opt-in SDK
workflow checkpoint against a disposable Second Cursor runtime and browser, with a
deterministic adapter. It does not measure model reasoning or concurrent human typing.

The fixture contains 20 synthetic invoices. The task reconciles three named drafts by
calculating their line totals and tax, entering the exact amount and expense account,
and submitting each once. All other invoices must remain byte-for-byte unchanged.
Amounts use integer cents and round half up to the nearest cent. No real payment or
external service is involved.

## Failure cases defined before implementation

1. A successful-looking page conceals incorrect accounting. The server independently
   derives expected net, tax, and gross amounts from its original invoice lines and
   judges the submitted payload; neither the page nor the adapter supplies the truth.
2. Retrying an uncertain submission duplicates an invoice. The server persists every
   submission attempt, including duplicates. The judge requires exactly one attempt
   and one accepted submission for each target invoice; backend deduplication alone
   cannot make this test pass.
3. A checkpoint exists only after the mutation. A local HTTP fault proxy lets the real
   runtime finish the first submit, withholds its response, and signals the harness to
   kill the SDK process. The on-disk checkpoint must already name that pending
   operation and the server ledger must already contain the real submission.
4. Restart silently starts a new session or repeats the pending call. The restarted
   process uses the same checkpoint; the proxy transcript must show operation-status
   reconciliation of the saved identifier, then a full current browser observation,
   before any further mutation. Timestamp assertions require each read to finish
   before the next operation is dispatched; completion order alone is insufficient.
   The saved call must not be dispatched a second time.
5. Recovered work is lost or unrelated invoices are changed. The restarted adapter
   must finish the remaining targets, preserve the first result, and leave all 17
   unrelated records unchanged. The durable ledger is reread from disk for judging.
6. The agent bypasses the UI. Its tool allowlist admits only the fixture tab's browser
   snapshot, click, and batches of typing plus a final submit click. The HTTP proxy
   forwards only the SC API, and the
   fixture records/refuses mutation requests lacking same-origin browser headers.
7. A failed or incomplete run appears green. Missing kill evidence, a child timeout,
   a recovery stop, missing reconciliation/reobservation, changed amounts, duplicate
   attempts, or absent completion all fail the corresponding assertion. Desktop
   interference remains inconclusive under the shared harness rules.
8. The fixture or proxy is broken. Offline tests cover cent rounding, corrupt totals,
   unrelated changes, duplicate attempts, browser-only mutation checks, persistence,
   and response withholding after the upstream operation completes. They launch no
   GUI and make no model calls.

## Execution and evidence

Offline: `python3 -m unittest discover -s e2e -p 'test_invoice_workflow.py'`.

Live (root agent owns GUI execution):
`SC_E2E_APP=build/SecondCursor.app python3 e2e/invoice_workflow.py`.
The default uses a disposable headless Chrome for Testing instance; add
`--headed-browser` to exercise the same controls in a visible fixture window.
The runner stores a durable invoice ledger, both child logs, checkpoint copies,
redacted proxy calls, and explicit recovery assertions under its E2E artifact folder.
It never uses the user's installed runtime configuration or browser profile.

The SDK checkpoint intentionally contains synthetic task/history content and is
opt-in. The fixture artifacts must remain local and ignored. Passing this scenario
proves recovery for a completed operation with an available runtime journal result;
unknown/expired results and runtime restart are separate SDK acceptance cases.
