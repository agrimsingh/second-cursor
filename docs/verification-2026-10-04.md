# Second Cursor 0.3.0 — implementation and verification

Raw verification evidence under `build/` and `e2e/artifacts/` is local and Git-ignored; the repository-relative paths below are not published on GitHub.

Implemented on `codex/task-ownership-and-verification`, based on `a6f29e3c20ac183ac11ea9ef814c52999f01c163`; the changes were uncommitted at that verification checkpoint. The final rebuilt candidate passed 40 scenarios with one inconclusive Chrome detector check and no failed scenarios in the selected regression scope. Foreground ownership and rendered Tasks/Setup checks are complete. The strict gate remains INCOMPLETE because detector validity is unproven; this is not a claim that every release scenario or capability was verified.

## Final attended build

The final gate (`e2e/artifacts/best-in-class-20261003T183600.036454Z/report.md`) ran against executable SHA-256 `74350e0f753b8529e8fa63140f3882596e29b4305cf99db5dae77534991ab82c`.

| Coverage on this executable | Result | Evidence |
| --- | --- | --- |
| Core native/browser regression | 23 PASS, 1 INCONCLUSIVE | Core (`e2e/artifacts/20261004-023600/report.md`) |
| Native safety | 5 PASS | Safety (`e2e/artifacts/20261004-023717/report.md`) |
| Foreground preservation and concurrent handoffs | 4 PASS | Focus (`e2e/artifacts/20261004-023727/report.md`) |
| Same-app human key-window ownership | 1 PASS | Ownership (`e2e/artifacts/20261004-023808/report.md`) |
| Rendered Tasks controls, history and Setup | 3 PASS | Panels (`e2e/artifacts/20261004-023814/report.md`) |
| Operation expiry, cancellation and pause | 1 PASS | Execution safety (`e2e/artifacts/20261004-023822/report.md`) |
| Task pause/resume, in-flight takeover and cancellation | 3 PASS | Task control (`e2e/artifacts/20261004-023831/report.md`) |

All endpoint cursor, Space and foreground invariants in these final runs passed. `browser_settled_reads` has passing settling assertions but remains inconclusive because the positive control cannot detect `Runtime.enable` in this Chrome version. No stealth claim follows from that result. The full quiet gate's other suites were tested earlier, not rerun wholesale after the final native focus changes.

This follow-up fixed ordinary keystrokes being refused when switching between two background documents. They now use the same guarded, target-only focus handoff as shortcuts. The regression independently verifies edits and caret motion across first/second/first document, while checking the other document remains unchanged. Shared handoff code now rechecks cancellation and human ownership after acquiring its lock, and skips an unfocus event when macOS cannot identify the foreground process. The rare foreground-lookup failure and cancellation during that exact lock wait were source-reviewed, not fault-injected.

Test repairs preserve production guards: standalone AppKit fixtures declare their main-actor startup, panels are inspected through external accessibility instead of bypassing the self-control restriction, Calculator starts in Basic mode with stable geometry, and pointer actions refresh observations after edits. Cursor uncertainty uses the entire observation interval, including observer subprocess time. Intermediate failed and disturbed runs remain in their artifact directories.

After testing, Codex PID 89320 was restored on original Space 4 with the original single-display layout and no Second Cursor test windows. Restoration evidence (`build/attended-verification-restored.json`) records the checked state. There is no pending desktop-switch permission request.

Final source fingerprint: `0583e2382ab56aabb2a220b951fa4b61225469534ab7848ed0513fd96b915f7c`. The build provenance (`build/verification-20261004-attended.json`) records its exact algorithm, file count, harness fingerprint, binary hash and package checksums.

- Tested app (`build/SecondCursor.app`)
- Current local DMG (`build/release-20261004-verified/SecondCursor-0.3.0-3-local.dmg`)
- Current local ZIP (`build/release-20261004-verified/SecondCursor-0.3.0-3-local.zip`)
- Current release manifest (`build/release-20261004-verified/SecondCursor-0.3.0-3-local.release.json`)

This arm64 package includes the new app icon and browser-extension icons. Archive extraction, app/helper/runtime signatures, disk-image integrity and package checksums passed verification; the packaged executable hash matches the tested executable. It is locally signed and not notarized. All seven final-run disposable homes and the test app/focus fixture processes were removed. The remaining sections preserve historical checkpoints rather than describing this final executable.

## What changed

- Automatic MCP session ownership and durable operation journaling prevent blind replay after disconnects or crashes. Recovery retains uncertainty; interrupted tasks require an explicit resume and fresh observations.
- Task supervision now has persistent states, named tasks, target ownership, pause, takeover, resume and cancellation, with a native Tasks panel and visible model-thinking state.
- Native input checks human activity, window identity, element identity and coordinate freshness. Unsupported gestures refuse explicitly; uncertain accessibility delivery is preserved through results and recovery.
- Browser input checks DOM/form/viewport freshness, re-resolves live targets, and obeys control generations. Checkbox property settling avoids the full timeout. Ordinary screenshots preserve valid observations; explicit larger captures remain conservatively guarded.
- The SDK supplies typed recovery errors, task APIs, cancellation and observation management. Extended mutations fetch their resulting observation without replaying input.
- Setup covers permissions, client configuration, browser installation and verified connection/control checks. Packaging includes the extension, native helper, Swift compatibility runtime, signatures, checksums and optional notarization.
- A serial release gate and four independently judged workflows are available. External engine adapters can reuse the same fixtures; no comparative Cua or model score has been inferred from these scripted runs.

## Attended desktop checkpoint

After the user authorized hands-off desktop testing, the attended gate passed all 8 selected scenarios (`e2e/artifacts/best-in-class-20261003T182455.557230Z/report.md`): four focus scenarios, one same-app ownership scenario, and three rendered Tasks/Setup scenarios. The fixtures verified background native input, concurrent handoffs, covered Chrome input, keyboard aliases, same-process key-window protection, task controls/history, and the setup connection check. The panel tests restored the previous foreground app; cursor and active-Space checks passed.

This is a clean pass for that selected scope and build, not a full release pass. It does not cover physical simultaneous typing, the monitor-unavailable branch, approval dialogs, or panel resizing and keyboard navigation. Input monitoring was available during the same-app test. The completed focus and panel tests were authorized; no permission request remains for those runs.

## Earlier quiet gate and targeted reruns

The earlier full quiet gate failed (`e2e/artifacts/best-in-class-20261003T181733.901125Z/report.md`). It recorded core and setup failures plus incomplete browser-batch, transport, native-safety, and task-runtime evidence. The targeted reruns (`e2e/artifacts/verification-followup-20261003T182812.092682Z/report.md`) passed those five non-core groups, but the core rerun still failed when the app did not confirm the requested key window. The safety refusal sent no keyboard input. These historical failures remain part of the evidence rather than being replaced by the later attended subset's pass.

`browser_settled_reads` remains **INCONCLUSIVE** for its Chrome Runtime detector check: the positive control failed to notice `Runtime.enable`, so a negative detection during ordinary work proves nothing about stealth. Its earlier per-suite report displayed PASS while carrying that inconclusive check; the strict gate did not count it as clean evidence. Settling assertions and detector validity are separate results.

## Earlier isolated and off-Space evidence

“Functional pass” means all intended action/result assertions passed. It does not turn an inconclusive cursor/focus invariant into a pass.

| Coverage | Functional result | Evidence |
| --- | --- | --- |
| Native safety, explicitly off-Space | 5/5 scenarios | Native evidence (`e2e/artifacts/20261004-014824/report.md`) |
| Real TextEdit rich editing and HTML paste | 1/1 scenario | Rich editing (`e2e/artifacts/20261004-014704/report.md`) |
| Task lifecycle, takeover during input, per-window freshness, cancellation, browser disconnect, heartbeat and restart | 8/8 scenarios, native fixtures explicitly off-Space | Task evidence (`e2e/artifacts/20261004-014838/report.md`) |
| Automatic transport isolation, display lease, crash/retry reconciliation, journal failure and late completion | 4/4 scenarios, explicitly off-Space | Recovery evidence (`e2e/artifacts/20261004-015305/report.md`) |
| Browser settling, outcomes, stale observations, screenshots, locators and hover | 7/7 scenarios | Browser evidence (`e2e/artifacts/20261004-015327/report.md`) |
| Portable package/setup, real control self-test and denied-action cleanup | 6/6 scenarios | Setup evidence (`e2e/artifacts/20261004-015350/report.md`) |
| SDK disconnect recovery, HTTP/MCP task flow and stdio | 3/3 scenarios | SDK evidence (`e2e/artifacts/20261004-015357/report.md`) |
| Password redaction through snapshots, inspection, typing and exports | 1/1 scenario | Privacy evidence (`e2e/artifacts/20261004-015644/report.md`) |
| Native note creation, single work-order submission, canvas dragging and confirmation handling | All 12 workflow outcome judgments passed across 3 rounds; only 8 trials had conclusive endpoint coexistence checks | Workflow evidence (`e2e/artifacts/best-in-class-tasks-20261003T175553.233099Z/e2e/report.md`) |

The earlier browser/setup/SDK gate (`e2e/artifacts/best-in-class-20261003T175327.299405Z/report.md`) was **INCOMPLETE** because cursor movement coincided with user input in several cases. The earlier workflow gate (`e2e/artifacts/best-in-class-tasks-20261003T175553.233099Z/report.md`) was also **INCOMPLETE** for that reason. Its “Verified” column counts fully conclusive trials, not only correct task outcomes. Neither is reported as a clean release pass.

At that checkpoint, additional checks passed: release compilation, deep strict code-signature verification, bundled runtime validation, eight native/Node stdio process scenarios, 24 task-registry concurrency/persistence checks, SDK real-loopback recovery tests, strict TypeScript consumer validation, 20 benchmark-harness checks and `git diff --check`. Existing pointer/scroll and browser batch cases passed in the initial regression run.

Checkbox settling measured 303 ms median in the browser run linked above (five samples); an earlier full call median was about 345 ms. Delayed search still waited for its result. These are historical local scripted measurements, not model completion speed or a competitor comparison.

## Earlier local package

These artifacts and fingerprints belong to the earlier packaging checkpoint. The `build/SecondCursor.app` path is mutable and may now contain a newer build; the archived package manifest and checksums identify the historical package.

- SecondCursor.app (`build/SecondCursor.app`)
- Local DMG (`build/release-20261004/SecondCursor-0.3.0-3-local.dmg`)
- Local ZIP (`build/release-20261004/SecondCursor-0.3.0-3-local.zip`)
- Release manifest (`build/release-20261004/SecondCursor-0.3.0-3-local.release.json`)
- Checksums (`build/release-20261004/SecondCursor-0.3.0-3-local.sha256`)

That package was arm64, version 0.3.0 build 3, locally signed and **not notarized**. Archive extraction, app/helper/library signatures and the DMG were verified at that checkpoint. Public notarization needs a valid Developer ID identity and notary credentials.

Earlier tested executable SHA-256:
`dc6fe590f246ba66a30c789352a06d4040fbdd3d7b7f19e2c9bf2a407e063fcf`

Earlier source fingerprint over sorted file paths and contents under Sources, extension, sdk and Resources:
`b0c7f1690c68acaf84cff80d1a2c53c74301b47af6d1f5b7735227c86b1ea0ec`

## Coverage limits

The initial full-screen-Space runs verified explicitly labeled off-Space behavior after hidden-Space relocation was correctly refused. The later attended run adds foreground ownership and rendered panel evidence; it does not retroactively turn the earlier inconclusive checks into passes.

Physical simultaneous typing, unavailable input monitoring, approval-dialog interaction, and panel resize/keyboard navigation were not covered by the attended gate. Native repaint freshness, canvas/video paint, closed shadow roots, and layout changes without DOM or viewport events remain documented capability limits. The final-build section above identifies the selected regressions rerun after the focus fixes.
