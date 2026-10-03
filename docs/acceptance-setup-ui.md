# Setup experience acceptance cases

These failure cases are defined before implementing the setup panel. Live UI work and the shared build are coordinated by the root agent.

1. Missing Accessibility and Screen Recording permissions must remain separately visible. Each permission action opens the helper on that exact permission, even when the other permission is missing.
2. If the app cannot load its config or the server cannot answer, the panel must show a readable failure and disable checks that require the server. A server failure must not be described as permission denial.
3. An absent browser extension must be described as a browser connection issue. Native setup can still be ready, and the panel must offer an actionable browser connection path.
4. Client configuration installation must be a deliberate button action. It must preserve unrelated client configuration through the shared installer, display its success or refusal, and never show bearer tokens or unrelated config contents.
5. The first-task check must be explicitly described as read-only. It must report what was actually checked and any skipped browser check without claiming to have verified input, clicking, or task completion.
6. Polling must stop when the panel closes, must not overlap requests, and must restart when reopened. A late poll or action result must not reopen the window or clear a newer result.
7. Permission helper completion must not close a newly reopened helper due to a delayed completion from an earlier presentation.
8. Keyboard navigation and standard native controls must reach permission actions, client selection, installation, browser help, and the read-only check. Long failures must wrap and remain readable rather than growing the window beyond its initial size.
