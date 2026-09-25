import MCP
import SCShared

/// This policy is applied before any extended operation (including batch children) leaves the app.
enum BrowserPolicy {
    static let sensitive: Set<String> = ["user_tabs", "claim", "history", "clipboard", "cdp"]
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
            return try steps.reduce(into: Set<String>()) { result, step in
                result.formUnion(try approvals(step, inBatch: true))
            }
        }
        if operation == "cdp", !allowedCDP.contains(try args.string("method")) {
            throw SCError.notAllowed("CDP method is outside the tab-scoped allowlist")
        }
        return sensitive.contains(operation) ? [operation] : []
    }
}
