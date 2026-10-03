import Foundation
import MCP
import SCShared

/// This policy is applied before any extended operation (including batch children) leaves the app.
enum BrowserPolicy {
    static let readOnly: Set<String> = [
        "capabilities", "user_tabs", "dom_snapshot", "inspect", "query", "wait", "screenshot", "history",
        "logs", "downloads", "assets", "export", "cdp",
    ]

    static func mutates(_ raw: [String: Value]) -> Bool {
        guard let operation = raw["operation"]?.stringValue else { return true }
        if operation == "clipboard" { return raw["action"]?.stringValue == "write" }
        if operation == "batch", let steps = raw["steps"]?.arrayValue {
            return steps.contains { $0.objectValue.map(mutates) ?? true }
        }
        return !readOnly.contains(operation)
    }

    static let sensitive: Set<String> = ["user_tabs", "claim", "history", "clipboard", "cdp", "upload"]
    // Deliberately read-only and tab-scoped. No Runtime, Network, Browser or Target escape hatch.
    static let allowedCDP: Set<String> = [
        "Page.getLayoutMetrics", "Page.getNavigationHistory",
        "DOM.getDocument", "Accessibility.getFullAXTree",
    ]

    static func approvals(_ raw: [String: Value], inBatch: Bool = false) throws -> Set<String> {
        let args = Arguments(raw)
        let operation = try args.string("operation")
        if operation == "batch" {
            guard !inBatch else { throw SCError.invalidArgument("Nested batch not allowed") }
            let steps = try args.objects("steps")
            guard !steps.isEmpty, steps.count <= 50 else {
                throw SCError.invalidArgument("Batch requires 1 to 50 steps")
            }
            for (index, step) in steps.enumerated() {
                do {
                    _ = try approvals(step, inBatch: true)
                } catch let SCError.invalidArgument(reason) {
                    throw SCError.invalidArgument("Batch step \(index + 1): \(reason)")
                }
            }
            return []
        }
        if operation == "cdp", !allowedCDP.contains(try args.string("method")) {
            throw SCError.notAllowed("CDP method is outside the tab-scoped allowlist")
        }
        // A prompt for a batch is worded from the batch's own arguments, so it could not show what a step does.
        if inBatch, sensitive.contains(operation) {
            throw SCError.invalidArgument(
                "\(operation) is not allowed in a batch; call \(operation) on its own so the approval can show exactly what it does")
        }
        return sensitive.contains(operation) ? [operation] : []
    }

    /// Prompt wording for one sensitive operation, named by the page's host and title. Only claim outlives the call.
    static func grant(_ operation: String, _ args: Arguments, page: BrowserPage?) -> ApprovalGrant {
        let host = page?.host ?? "this page"
        let tab = page?.title.map { "“\($0)”" } ?? (page?.host ?? "this tab")
        let pageLabel = [page?.host, page?.title.map { "“\($0)”" }].compactMap { $0 }.joined(separator: " — ")
        switch operation {
        case "claim":
            return ApprovalGrant(
                title: "Let Second Cursor control \(pageLabel.isEmpty ? "this tab" : pageLabel)?",
                summary: "control \(tab)",
                detail: "It can read and operate this tab until you release it, the agent's turn ends, or it's idle for 5 minutes.",
                ongoing: true)
        case "user_tabs":
            return ApprovalGrant(
                title: "Let Second Cursor see your open tabs?",
                summary: "see your open tabs",
                detail: "It gets the title and address of every tab in this browser. Just this once.")
        case "history":
            return ApprovalGrant(
                title: "Let Second Cursor read your browsing history?",
                summary: "read your browsing history",
                detail: "It gets matching pages you visited. Just this once.")
        case "clipboard":
            let writing = ((try? args.optionalString("action")) ?? nil) == "write"
            let verb = writing ? "change your clipboard" : "read your clipboard"
            return ApprovalGrant(
                title: "Let Second Cursor \(verb) on \(host)?",
                summary: verb,
                detail: "Just this once.")
        case "cdp":
            return ApprovalGrant(
                title: "Let Second Cursor inspect \(tab) with developer tools?",
                summary: "inspect \(tab)",
                detail: "It runs one read-only inspection. Just this once.")
        case "upload":
            let names = ((try? args.strings("files")) ?? []).map { ($0 as NSString).lastPathComponent }
            let files =
                names.isEmpty
                ? "files"
                : names.prefix(2).joined(separator: ", ") + (names.count > 2 ? " and \(names.count - 2) more" : "")
            return ApprovalGrant(
                title: "Upload \(files) to \(pageLabel.isEmpty ? "this page" : pageLabel)?",
                summary: "upload \(files) to \(host)",
                detail: "The page gets a copy and can send it anywhere. Just this once.")
        default:
            return ApprovalGrant(
                title: "Allow Second Cursor to run \(operation) in the browser?", summary: "run \(operation)",
                detail: "Just this once.")
        }
    }
}

/// Human context for a browser prompt, looked up only when a prompt will actually be shown.
struct BrowserPage: Sendable {
    var host: String?
    var title: String?

    init(url: String?, title: String?) {
        let host = url.flatMap { URL(string: $0)?.host }
        self.host = host?.isEmpty == false ? host : nil
        let title = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.title = title.isEmpty ? nil : String(title.prefix(80))
    }
}
