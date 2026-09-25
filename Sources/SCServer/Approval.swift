import AppKit

/// Model arguments cannot grant approval. Prompts are nonmodal so they never block the main actor.
@MainActor enum Approval {
    private static var current: Prompt?

    static func request(
        tool: String, target: String?, deadline: Date,
        stillAllowed: @escaping @Sendable () -> Bool
    ) async -> Bool {
        while current != nil {
            guard Date() < deadline, stillAllowed() else { return false }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        guard Date() < deadline, stillAllowed() else { return false }
        return await withCheckedContinuation { continuation in
            let prompt = Prompt(tool: tool, target: target, continuation: continuation)
            current = prompt
            prompt.show()
            prompt.watchdog = Task { [weak prompt] in
                while let prompt, !prompt.finished {
                    if Date() >= deadline || !stillAllowed() {
                        prompt.finish(false)
                        break
                    }
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
            }
        }
    }

    @MainActor private final class Prompt: NSObject, NSWindowDelegate {
        let alert = NSAlert()
        let previousApp = NSWorkspace.shared.frontmostApplication
        let continuation: CheckedContinuation<Bool, Never>
        var watchdog: Task<Void, Never>?
        var finished = false

        init(tool: String, target: String?, continuation: CheckedContinuation<Bool, Never>) {
            self.continuation = continuation
            super.init()
            alert.messageText = "Allow Second Cursor to run \(tool)?"
            alert.informativeText =
                "Target: \(target ?? "current session"). This grants access for this call only. The request expires automatically."
            let allow = alert.addButton(withTitle: "Allow once")
            allow.target = self
            allow.action = #selector(approve)
            // Return must not accidentally grant access while the user is typing elsewhere.
            allow.keyEquivalent = ""
            let denyButton = alert.addButton(withTitle: "Deny")
            denyButton.target = self
            denyButton.action = #selector(deny)
            denyButton.keyEquivalent = "\u{1b}"
        }

        func show() {
            alert.layout()
            alert.window.delegate = self
            alert.window.center()
            NSApp.activate(ignoringOtherApps: true)
            alert.window.makeKeyAndOrderFront(nil)
        }

        @objc func approve() { finish(true) }
        @objc func deny() { finish(false) }
        func windowWillClose(_ notification: Notification) { finish(false) }

        func finish(_ allowed: Bool) {
            guard !finished else { return }
            finished = true
            watchdog?.cancel()
            alert.window.orderOut(nil)
            if NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier {
                previousApp?.activate(options: [])
            }
            Approval.current = nil
            continuation.resume(returning: allowed)
        }
    }
}
