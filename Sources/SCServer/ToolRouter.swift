import Foundation
import MCP
import SCShared

struct ToolOutput {
    var json: [String: Any]
    var images: [Screenshot] = []
}

typealias WindowOwner = (pid: Int32, bundleID: String?)

/// Applies the gates every tool call goes through, then dispatches to the desktop,
/// agent display, or browser implementation.
final class ToolRouter: Sendable {
    let desktop: DesktopControlling
    let agentDisplay: AgentDisplayControlling
    let browser: BrowserControlling
    let appGuard: AppGuard
    let log = ActionLog()
    private static let toolNames = Set(ToolCatalog.tools.map(\.name))

    init(desktop: DesktopControlling, agentDisplay: AgentDisplayControlling, browser: BrowserControlling, config: SCConfig) {
        self.desktop = desktop
        self.agentDisplay = agentDisplay
        self.browser = browser
        self.appGuard = AppGuard(config: config)
    }

    func call(name: String, arguments: [String: Value]) async -> CallTool.Result {
        let args = Arguments(arguments)
        do {
            guard Self.toolNames.contains(name) else { throw SCError.invalidArgument("Unknown tool \"\(name)\"") }
            let owner = try await admit(name, args)
            let output = try await Self.withTimeout(seconds: 60, tool: name) {
                name.hasPrefix("browser_")
                    ? try await self.runBrowser(name, args)
                    : try await self.runDesktop(name, args, owner: owner)
            }
            return Self.success(output)
        } catch {
            return Self.failure(error)
        }
    }

    /// Checks the pause and blocked-app gates, then writes the action log line.
    /// Refused calls are logged too, so the log accounts for every attempted action.
    private func admit(_ name: String, _ args: Arguments) async throws -> WindowOwner? {
        let logged = ToolCatalog.actionTools.contains(name)
            || (name == "agent_display" && (try? args.string("action"))?.lowercased() != "status")
        do {
            let owner = try await checkGates(name, args)
            if logged { log.record(ActionLogEntry(tool: name, target: args.logTarget, detail: args.logDetail)) }
            return owner
        } catch {
            if logged {
                log.record(ActionLogEntry(tool: name, target: args.logTarget, detail: "refused: \(Self.message(for: error))"))
            }
            throw error
        }
    }

    private func checkGates(_ name: String, _ args: Arguments) async throws -> WindowOwner? {
        if name != "status" && PauseState.isPaused { throw SCError.paused }
        if name == "launch_app" {
            try await appGuard.checkLaunch(try args.string("app")) { [desktop] in
                await desktop.listApps(includeNotRunning: true)
            }
        }
        if ToolCatalog.windowScopedTools.contains(name) {
            return try appGuard.checkWindow(try args.windowID())
        }
        return nil
    }

    /// Answers the client when the work finishes or the deadline passes, whichever is first. A task
    /// group would wait for the work anyway, and AX calls blocked on a hung app cannot be cancelled;
    /// the abandoned work only holds up that app's own queue.
    private static func withTimeout<T>(seconds: Double, tool: String, _ work: @escaping () async throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<T, Error>) in
            let once = ResumeOnce()
            Task {
                do {
                    let value = try await work()
                    if once.claim() { continuation.resume(returning: value) }
                } catch {
                    if once.claim() { continuation.resume(throwing: error) }
                }
            }
            Task {
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                if once.claim() {
                    continuation.resume(throwing: SCError.actionFailed(
                        "\(tool) timed out after \(Int(seconds))s; the target app may be busy or hung. Check get_window_state before retrying."))
                }
            }
        }
    }

    func agentBounds() async -> Rect? {
        let status = await agentDisplay.status()
        return status.isActive ? status.bounds : nil
    }

    static func message(for error: Error) -> String {
        if let error = error as? LocalizedError, let description = error.errorDescription { return description }
        return "\(error)"
    }

    private static func success(_ output: ToolOutput) -> CallTool.Result {
        var content: [Tool.Content] = [.text(text: encode(output.json), annotations: nil, _meta: nil)]
        for image in output.images {
            content.append(.image(data: image.pngData.base64EncodedString(), mimeType: "image/png", annotations: nil, _meta: nil))
        }
        return CallTool.Result(content: content, isError: false)
    }

    private static func failure(_ error: Error) -> CallTool.Result {
        CallTool.Result(content: [.text(text: message(for: error), annotations: nil, _meta: nil)], isError: true)
    }

    /// JSONSerialization raises an Objective-C exception (a crash) on NaN or infinity,
    /// which can come back from AX frames, so non-finite numbers become null.
    private static func encode(_ json: [String: Any]) -> String {
        let object = JSONSerialization.isValidJSONObject(json) ? json : (finite(json) as? [String: Any] ?? [:])
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])) ?? Data("{}".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    private final class ResumeOnce: @unchecked Sendable {
        private let lock = NSLock()
        private var claimed = false

        func claim() -> Bool {
            lock.lock()
            defer { lock.unlock() }
            if claimed { return false }
            claimed = true
            return true
        }
    }

    private static func finite(_ value: Any) -> Any {
        switch value {
        case let dictionary as [String: Any]: return dictionary.mapValues(finite)
        case let array as [Any]: return array.map(finite)
        case let double as Double where !double.isFinite: return NSNull()
        default: return value
        }
    }
}
