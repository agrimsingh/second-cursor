import AppKit

struct AppTarget: Sendable {
    /// Bundle id, or the executable path of an app that has none.
    let key: String
    let name: String
    let hasBundleID: Bool
}

/// Copy shown to the user: plain words and human context only (app names, site hosts, page titles),
/// never session, browser or tab ids.
struct ApprovalGrant: Sendable {
    /// The whole question when this is the only thing asked, e.g. "Upload notes.txt to example.com?".
    let title: String
    /// Lowercase verb phrase used when several things are asked at once, e.g. "upload notes.txt".
    let summary: String
    /// One short sentence of consequence and scope, e.g. "The page gets a copy of the file. Just this once."
    let detail: String
    var ongoing = false
}

enum ApprovalDecision: Sendable {
    case deny, once, session, always
}

/// One prompt: ongoing access to an app and/or grants for the current call.
struct ApprovalRequest: Sendable {
    var app: AppTarget?
    var grants: [ApprovalGrant]
    var session: String
}

/// Model arguments cannot grant approval. Prompts are nonmodal so they never block the main actor.
@MainActor enum Approval {
    private static var current: Prompt?

    static func request(
        _ request: ApprovalRequest, deadline: Date,
        stillAllowed: @escaping @Sendable () -> Bool
    ) async -> ApprovalDecision {
        while current != nil {
            guard Date() < deadline, stillAllowed() else { return .deny }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        guard Date() < deadline, stillAllowed() else { return .deny }
        return await withCheckedContinuation { continuation in
            let prompt = Prompt(request, continuation: continuation)
            current = prompt
            prompt.show()
            prompt.watchdog = Task { [weak prompt] in
                while let prompt, !prompt.finished {
                    if Date() >= deadline || !stillAllowed() {
                        prompt.finish(.deny)
                        break
                    }
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
            }
        }
    }

    static func title(_ request: ApprovalRequest) -> String {
        if let app = request.app { return "Allow Second Cursor to use \(app.name)?" }
        if request.grants.count == 1 { return request.grants[0].title }
        return "Allow Second Cursor to \(request.grants.map(\.summary).joined(separator: " and "))?"
    }

    static func message(_ request: ApprovalRequest) -> String {
        guard let app = request.app else {
            return request.grants.count == 1 ? request.grants[0].detail : "Just this once."
        }
        var text = "Second Cursor can read and operate \(app.name) until this session ends."
        if app.hasBundleID { text += " Always allow keeps it until you remove \(app.name) from the approved apps list." }
        if !request.grants.isEmpty {
            text += " It will also \(request.grants.map(\.summary).joined(separator: " and ")), just this once."
        }
        return text
    }

    private static let icon: NSImage? = {
        let accent = NSColor(srgbRed: 0xf5 / 255, green: 0x4e / 255, blue: 0, alpha: 1)
        let configuration = NSImage.SymbolConfiguration(pointSize: 48, weight: .regular)
            .applying(NSImage.SymbolConfiguration(paletteColors: [accent]))
        return NSImage(systemSymbolName: "cursorarrow.rays", accessibilityDescription: "Second Cursor")?
            .withSymbolConfiguration(configuration)
    }()

    @MainActor private final class Prompt: NSObject, NSWindowDelegate {
        let alert = NSAlert()
        let previousApp = NSWorkspace.shared.frontmostApplication
        let continuation: CheckedContinuation<ApprovalDecision, Never>
        var watchdog: Task<Void, Never>?
        var finished = false
        private var choices: [ApprovalDecision] = []

        init(_ request: ApprovalRequest, continuation: CheckedContinuation<ApprovalDecision, Never>) {
            self.continuation = continuation
            super.init()
            alert.messageText = Approval.title(request)
            alert.informativeText = Approval.message(request)
            if let icon = Approval.icon { alert.icon = icon }
            var buttons: [(String, ApprovalDecision)]
            if let app = request.app {
                buttons = [("Allow for this session", .session)]
                if app.hasBundleID { buttons.append(("Always allow", .always)) }
            } else {
                buttons = [(request.grants.contains(where: \.ongoing) ? "Allow" : "Allow once", .once)]
            }
            buttons.append(("Deny", .deny))
            for (index, (title, decision)) in buttons.enumerated() {
                let button = alert.addButton(withTitle: title)
                button.tag = index
                button.target = self
                button.action = #selector(choose(_:))
                // Return must not accidentally grant access while the user is typing elsewhere.
                button.keyEquivalent = decision == .deny ? "\u{1b}" : ""
            }
            choices = buttons.map(\.1)
        }

        func show() {
            alert.layout()
            alert.window.delegate = self
            alert.window.center()
            NSApp.activate(ignoringOtherApps: true)
            alert.window.makeKeyAndOrderFront(nil)
        }

        @objc func choose(_ sender: NSButton) { finish(choices.indices.contains(sender.tag) ? choices[sender.tag] : .deny) }
        func windowWillClose(_ notification: Notification) { finish(.deny) }

        func finish(_ decision: ApprovalDecision) {
            guard !finished else { return }
            finished = true
            watchdog?.cancel()
            alert.window.orderOut(nil)
            if NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier {
                previousApp?.activate(options: [])
            }
            Approval.current = nil
            continuation.resume(returning: decision)
        }
    }
}
