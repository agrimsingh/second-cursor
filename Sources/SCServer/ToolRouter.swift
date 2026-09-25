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
    let sessions: Sessions
    let connectionCount: @Sendable () -> Int
    private static let toolNames = Set(ToolCatalog.tools.map(\.name))

    init(
        desktop: DesktopControlling, agentDisplay: AgentDisplayControlling, browser: BrowserControlling,
        config: SCConfig, connectionCount: @escaping @Sendable () -> Int = { 0 }
    ) {
        self.connectionCount = connectionCount
        self.desktop = desktop
        self.agentDisplay = agentDisplay
        self.browser = browser
        self.appGuard = AppGuard(config: config)
        self.sessions = Sessions(
            claimIdleSeconds: config.claimIdleSeconds ?? 300,
            retentionSeconds: config.operationRetentionSeconds ?? 3600)
    }

    func call(name: String, arguments: [String: Value]) async -> CallTool.Result {
        let receivedAt = Date()
        // The compact tool schema nests operation-specific fields. Keep flat 0.2 callers compatible.
        var raw = arguments
        if name == "browser_command", let nested = raw.removeValue(forKey: "arguments")?.objectValue {
            raw.merge(nested) { envelope, _ in envelope }
        }
        let args = Arguments(raw)
        do {
            guard Self.toolNames.contains(name) else { throw SCError.invalidArgument("Unknown tool: \(name)") }
            let session = try args.optionalString("session_id") ?? "legacy"
            if name == "session" {
                switch try args.string("action") {
                case "create":
                    return Self.success(ToolOutput(json: ["session_id": sessions.create()]))
                case "close":
                    sessions.close(session)
                    desktop.closeSession(session)
                    let payload = try JSONSerialization.data(withJSONObject: ["session_id": session])
                    _ = try? await browser.command("session.close", params: payload)
                    return Self.success(ToolOutput(json: ["closed": true]))
                default:
                    throw SCError.invalidArgument("action must be create or close")
                }
            }
            try sessions.validate(session)
            if name == "operation_status" || name == "cancel_operation" {
                let info = try sessions.info(
                    args.string("operation_id"), session: session, cancel: name == "cancel_operation")
                return Self.success(ToolOutput(json: info))
            }
            if name == "bind_target" {
                return try await bindTarget(args, session: session)
            }
            let timeout = Double(min(60_000, max(1, try args.int("timeout_ms", default: 60_000)))) / 1000
            let deadline = receivedAt.addingTimeInterval(timeout)
            let suppliedID = try args.optionalString("operation_id")
            let id = suppliedID ?? UUID().uuidString
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let signature = name + String(decoding: try encoder.encode(Value.object(raw)), as: UTF8.self)
            let (operation, fresh) = try sessions.reserve(
                id: id, session: session, signature: signature, deadline: deadline, retain: suppliedID != nil)
            if !fresh {
                if let output = sessions.output(operation) { return Self.success(output) }
                return Self.success(ToolOutput(json: try sessions.info(id, session: session)))
            }
            do {
                let owner = try await checkGates(name, args)
                var configuredTools = [name]
                if name == "batch" {
                    configuredTools += try args.objects("steps").compactMap { $0["tool"]?.stringValue?.lowercased() }
                }
                var approvals = Set(configuredTools).intersection(appGuard.config.confirmationRequiredTools)
                let browserApprovals = name == "browser_command" ? try BrowserPolicy.approvals(raw) : []
                approvals.formUnion(browserApprovals.map { "browser.\($0)" })
                if !approvals.isEmpty {
                    let allowed = await Approval.request(
                        tool: approvals.sorted().joined(separator: ", "),
                        target:
                            "session=\(session); browser=\(try args.optionalString("browser_id") ?? "default"); \(args.logTarget ?? "")",
                        deadline: deadline,
                        stillAllowed: { [sessions] in sessions.mayApprove(operation) })
                    guard allowed, sessions.mayApprove(operation) else {
                        throw SCError.notAllowed("Local approval denied, cancelled, or expired; nothing was dispatched")
                    }
                }
                // Record admission only after approval. Denials are recorded separately below.
                _ = try await admit(name, args)
                let mutating = ToolCatalog.actionTools.contains(name)
                let target = mutating ? try args.optionalInt("window_id").map { "window:\($0)" } : nil
                let context = ExecutionContext(
                    sessionID: session, observationID: try args.optionalString("observation_id"),
                    browserID: try args.optionalString("browser_id"), deadline: deadline,
                    browserApprovals: browserApprovals
                ) { [sessions] in
                    if name != "status" { try sessions.dispatch(operation, target: target) }
                }
                let output = try await Self.withTimeout(seconds: max(0.001, deadline.timeIntervalSinceNow), tool: name)
                {
                    do {
                        var result = try await Execution.$context.withValue(context) {
                            if name == "status" { return try await self.runDesktop(name, args, owner: owner) }
                            return name.hasPrefix("browser_")
                                ? try await self.runBrowser(name, args)
                                : try await self.runDesktop(name, args, owner: owner)
                        }
                        if suppliedID != nil { result.json["operation_id"] = id }
                        self.sessions.finish(operation, result: result, error: nil)
                        return result
                    } catch {
                        self.sessions.finish(operation, result: nil, error: Self.message(for: error))
                        throw error
                    }
                }
                return Self.success(output)
            } catch is CallDeadlineExceeded {
                sessions.keepForReconciliation(operation)
                return Self.failure(
                    SCError.actionFailed(
                        "Call deadline exceeded [operation_id=\(id); reconcile with operation_status]"))
            } catch {
                sessions.finish(operation, result: nil, error: Self.message(for: error))
                log.record(
                    ActionLogEntry(
                        tool: name, target: args.logTarget, detail: "refused or failed: \(Self.message(for: error))"))
                return Self.failure(error)
            }
        } catch {
            return Self.failure(error)
        }
    }

    private func bindTarget(_ args: Arguments, session: String) async throws -> CallTool.Result {
        let target = try args.string("target")
        guard target.hasPrefix("window:"), let windowID = UInt32(target.dropFirst(7)) else {
            throw SCError.invalidArgument("Native target must be window:<id>")
        }
        if try args.optionalString("action") == "release" {
            sessions.release(target, session: session)
        } else {
            _ = try appGuard.checkWindow(windowID)
            if PauseState.isPaused { throw SCError.paused }
            try sessions.claim(target, session: session)
            let snapshot = try await Self.withTimeout(seconds: 60, tool: "bind_target") {
                try await Execution.$context.withValue(ExecutionContext(sessionID: session)) {
                    try await self.desktop.windowState(
                        windowID: windowID, options: SnapshotOptions(includeScreenshot: false))
                }
            }
            return Self.success(
                ToolOutput(json: [
                    "target": target, "session_id": session, "observation": await snapshotJSON(snapshot),
                ]))
        }
        return Self.success(ToolOutput(json: ["target": target, "session_id": session]))
    }

    /// Checks the pause and blocked-app gates, then writes the action log line.
    /// Refused calls are logged too, so the log accounts for every attempted action.
    private func admit(_ name: String, _ args: Arguments) async throws -> WindowOwner? {
        let logged =
            name == "browser_command" || ToolCatalog.actionTools.contains(name)
            || (name == "agent_display" && (try? args.string("action"))?.lowercased() != "status")
        do {
            let owner = try await checkGates(name, args)
            if logged { log.record(ActionLogEntry(tool: name, target: args.logTarget, detail: args.logDetail)) }
            return owner
        } catch {
            if logged {
                log.record(
                    ActionLogEntry(tool: name, target: args.logTarget, detail: "refused: \(Self.message(for: error))"))
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
    private static func withTimeout<T>(seconds: Double, tool: String, _ work: @escaping () async throws -> T)
        async throws -> T
    {
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
                    continuation.resume(throwing: CallDeadlineExceeded())
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
            content.append(
                .image(data: image.pngData.base64EncodedString(), mimeType: "image/png", annotations: nil, _meta: nil))
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
        let data =
            (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]))
            ?? Data("{}".utf8)
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

private struct CallDeadlineExceeded: Error {}
