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
    let appApprovals: AppApprovals
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
        self.appApprovals = AppApprovals(config: config)
        self.sessions = Sessions(
            claimIdleSeconds: config.claimIdleSeconds ?? 300,
            retentionSeconds: config.operationRetentionSeconds ?? 3600)
    }

    func call(name called: String, arguments: [String: Value]) async -> CallTool.Result {
        // act is batch for clients that see the compact tool surface; one code path keeps their gates, logging and
        // parameters identical.
        let name = called == "act" ? "batch" : called
        let receivedAt = Date()
        // The compact tool schema nests operation-specific fields. Keep flat 0.2 callers compatible.
        var raw = arguments
        var malformedArguments = false
        if name == "browser_command", let nested = raw.removeValue(forKey: "arguments"), !nested.isNull {
            if let object = Arguments.nestedObject(nested) {
                raw.merge(object) { envelope, _ in envelope }
            } else {
                malformedArguments = true
            }
        }
        // A retry is identified by what the client sent, not by the window an app name resolved to.
        let requested = raw
        do {
            guard Self.toolNames.contains(name) else { throw SCError.invalidArgument("Unknown tool: \(name)") }
            if malformedArguments {
                throw SCError.invalidArgument(
                    "arguments must be an object of the operation's fields; operation=capabilities lists them")
            }
            raw = try await resolveAppTarget(name, raw)
            let args = Arguments(raw)
            let session = try args.optionalString("session_id") ?? "legacy"
            if name == "session" {
                switch try args.string("action") {
                case "create":
                    return Self.success(ToolOutput(json: ["session_id": sessions.create()]))
                case "close":
                    sessions.close(session)
                    desktop.closeSession(session)
                    desktop.endAgentActivity(session: session)
                    appApprovals.clear(session: session)
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
                return try await bindTarget(args, session: session, deadline: receivedAt.addingTimeInterval(60))
            }
            let timeout = Double(min(60_000, max(1, try args.int("timeout_ms", default: 60_000)))) / 1000
            let deadline = receivedAt.addingTimeInterval(timeout)
            let suppliedID = try args.optionalString("operation_id")
            let id = suppliedID ?? UUID().uuidString
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let signature = name + String(decoding: try encoder.encode(Value.object(requested)), as: UTF8.self)
            let (operation, fresh) = try sessions.reserve(
                id: id, session: session, signature: signature, deadline: deadline, retain: suppliedID != nil)
            if !fresh {
                if let output = sessions.output(operation) { return Self.success(output) }
                return Self.success(ToolOutput(json: try sessions.info(id, session: session)))
            }
            var admitted = false
            do {
                let owner = try await checkGates(name, args)
                var configuredTools = [name]
                if name == "batch" {
                    configuredTools += try args.objects("steps").compactMap { $0["tool"]?.stringValue?.lowercased() }
                }
                var grants = Set(configuredTools).intersection(appGuard.config.confirmationRequiredTools).sorted().map {
                    ApprovalGrant(
                        title: "Allow Second Cursor to \($0.replacingOccurrences(of: "_", with: " "))?",
                        summary: $0.replacingOccurrences(of: "_", with: " "), detail: "Just this once.")
                }
                let browserApprovals = name == "browser_command" ? try BrowserPolicy.approvals(raw) : []
                if !browserApprovals.isEmpty {
                    let page = appApprovals.mode == .prompt ? await browserPage(args, session: session, deadline: deadline) : nil
                    grants += browserApprovals.sorted().map { BrowserPolicy.grant($0, args, page: page) }
                }
                var app = try await appNeedingApproval(name, args, owner: owner)
                if let target = app, appApprovals.isApproved(target, session: session) { app = nil }
                if app != nil || !grants.isEmpty {
                    try await approve(
                        ApprovalRequest(app: app, grants: grants, session: session),
                        deadline: deadline, stillAllowed: { [sessions] in sessions.mayApprove(operation) })
                }
                // Record admission only after approval. Refusals are recorded once below.
                _ = try await admit(name, args)
                admitted = true
                noteActivity(name, args, owner: owner, session: session)
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
                        if name == "status" {
                            result.json["agent_activity"] = Self.activityJSON(self.desktop.agentActivity())
                        }
                        if suppliedID != nil { result.json["operation_id"] = id }
                        self.noteObservation(name, args, owner: owner, output: result, session: session)
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
                        "Call deadline exceeded; the target app may be busy or hung. Check get_window_state before retrying. [operation_id=\(id); reconcile with operation_status]"
                    ))
            } catch {
                sessions.finish(operation, result: nil, error: Self.message(for: error))
                // Each logged call gets exactly one line: its admission, or this refusal.
                if !admitted && Self.isLogged(name, args) {
                    log.record(
                        ActionLogEntry(tool: name, target: args.logTarget, detail: "refused: \(Self.message(for: error))"))
                }
                return Self.failure(error)
            }
        } catch {
            return Self.failure(error)
        }
    }

    private func bindTarget(_ args: Arguments, session: String, deadline: Date) async throws -> CallTool.Result {
        let target = try args.string("target")
        guard target.hasPrefix("window:"), let windowID = UInt32(target.dropFirst(7)) else {
            throw SCError.invalidArgument("Native target must be window:<id>")
        }
        if try args.optionalString("action") == "release" {
            sessions.release(target, session: session)
        } else {
            var owner = try appGuard.checkWindow(windowID)
            if PauseState.isPaused { throw SCError.paused }
            let app = appApprovals.appApprovalRequired ? AppGuard.windowTarget(owner) : nil
            if let app, !appApprovals.isApproved(app, session: session) {
                try await approve(
                    ApprovalRequest(app: app, grants: [], session: session),
                    deadline: deadline, stillAllowed: { [sessions] in (try? sessions.validate(session)) != nil })
                // The user may have paused, or the window may have closed, while the prompt was up.
                owner = try appGuard.checkWindow(windowID)
                if PauseState.isPaused { throw SCError.paused }
            }
            try sessions.claim(target, session: session)
            let snapshot = try await Self.withTimeout(seconds: 60, tool: "bind_target") {
                try await Execution.$context.withValue(ExecutionContext(sessionID: session)) {
                    try await self.desktop.windowState(
                        windowID: windowID, options: SnapshotOptions(includeScreenshot: false))
                }
            }
            noteObserved(windowID, owner: owner, session: session)
            return Self.success(
                ToolOutput(json: [
                    "target": target, "session_id": session, "observation": await snapshotJSON(snapshot),
                ]))
        }
        return Self.success(ToolOutput(json: ["target": target, "session_id": session]))
    }

    /// Rechecks the pause and blocked-app gates (the user may have acted during approval), then writes
    /// the action log line. `call` logs refusals, so the log accounts for every attempted action.
    private func admit(_ name: String, _ args: Arguments) async throws -> WindowOwner? {
        let owner = try await checkGates(name, args)
        if Self.isLogged(name, args) {
            log.record(ActionLogEntry(tool: name, target: args.logTarget, detail: args.logDetail))
        }
        return owner
    }

    /// The action log covers state-changing calls only.
    private static func isLogged(_ name: String, _ args: Arguments) -> Bool {
        name == "browser_command" || ToolCatalog.actionTools.contains(name)
            || (name == "agent_display" && (try? args.string("action"))?.lowercased() != "status")
    }

    /// Calls that act without moving the pointer; they show the arrow too when it is not up yet.
    private static let keyboardTools: Set<String> = [
        "type_text", "press_key", "set_value", "paste", "select_text", "perform_action", "batch",
    ]

    /// Window, app and browser calls, reads included, keep the session's on-screen presence up until its turn
    /// ends. A native action replaces the arrow's caption with its own, or clears it; reads leave it.
    private func noteActivity(_ name: String, _ args: Arguments, owner: WindowOwner?, session: String) {
        let app: String?
        if let owner, ToolCatalog.windowScopedTools.contains(name) {
            app = AppGuard.windowTarget(owner).name
        } else if name == "launch_app" {
            app = try? args.string("app")
        } else if name.hasPrefix("browser_"), !["browser_status", "browser_instances"].contains(name) {
            // The extension draws the browser cursor and its caption inside the tab.
            desktop.noteAgentActivity(session: session, windowID: nil, app: "Chrome", showArrow: false, caption: nil)
            return
        } else {
            return
        }
        let caption = ToolCatalog.captionedTools.contains(name)
            ? ((try? args.optionalString("caption")).flatMap { $0 }.map(Self.caption) ?? "") : nil
        desktop.noteAgentActivity(
            session: session, windowID: try? args.windowID(), app: app, showArrow: Self.keyboardTools.contains(name),
            caption: caption)
    }

    /// One line, trimmed to what fits beside the cursor.
    static func caption(_ text: String) -> String {
        let line = text.split(whereSeparator: \.isNewline).joined(separator: " ")
            .trimmingCharacters(in: .whitespaces)
        return line.count > 80 ? String(line.prefix(79)) + "…" : line
    }

    private static func activityJSON(_ activity: AgentActivityStatus) -> [String: Any] {
        [
            "active": activity.active,
            "app": activity.app ?? NSNull(),
            "sessions": activity.sessions,
            "arrow": activity.arrow.map { ["x": $0.x, "y": $0.y] } ?? NSNull(),
            "caption": activity.caption ?? NSNull(),
            "idle_seconds": activity.idleSeconds.map { ($0 * 10).rounded() / 10 } ?? NSNull(),
        ]
    }

    /// The app a call would read or operate, when per-app approval applies to it.
    private func appNeedingApproval(_ name: String, _ args: Arguments, owner: WindowOwner?) async throws -> AppTarget? {
        guard appApprovals.appApprovalRequired else { return nil }
        if name == "launch_app" {
            return try await AppGuard.launchTarget(try args.string("app")) { [desktop] in
                await desktop.listApps(includeNotRunning: true)
            }
        }
        guard ToolCatalog.windowScopedTools.contains(name), let owner else { return nil }
        return AppGuard.windowTarget(owner)
    }

    /// Host and title of the tab a browser prompt is about. Best effort: a prompt without them still works.
    private func browserPage(_ args: Arguments, session: String, deadline: Date) async -> BrowserPage? {
        guard let tabID = (try? args.optionalInt("tab_id")) ?? nil,
            let params = try? JSONSerialization.data(withJSONObject: ["tabId": tabID])
        else { return nil }
        let context = ExecutionContext(
            sessionID: session, browserID: (try? args.optionalString("browser_id")) ?? nil, deadline: deadline)
        guard let data = try? await Execution.$context.withValue(context, operation: {
                try await browser.command("tab.info", params: params)
            }),
            let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }
        return BrowserPage(url: info["url"] as? String, title: info["title"] as? String)
    }

    private func approve(
        _ request: ApprovalRequest, deadline: Date, stillAllowed: @escaping @Sendable () -> Bool
    ) async throws {
        guard await appApprovals.authorize(request, deadline: deadline, stillAllowed: stillAllowed), stillAllowed()
        else {
            throw SCError.notAllowed("Local approval denied, cancelled, or expired; nothing was dispatched")
        }
    }

    /// Called after every assistant turn. The session's next read of each window is full again, `app` must be
    /// observed again before acting by name, its "Allow for this session" app approvals lapse (including one
    /// answered after this point on a prompt already showing), and personal tabs it claimed are released (tabs
    /// the agent opened stay open). The session itself stays open, and work already in flight is not cancelled.
    func endTurn(session: String) async -> [String: Any] {
        desktop.closeSession(session)
        desktop.endAgentActivity(session: session)
        sessions.forgetObservations(session: session)
        let cleared = appApprovals.clear(session: session)
        var released = 0
        if let params = try? JSONSerialization.data(withJSONObject: ["session_id": session]),
            let data = try? await browser.command("session.end_turn", params: params),
            let reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        {
            released = (reply["released_tabs"] as? NSNumber)?.intValue ?? 0
        }
        return ["ok": true, "session_id": session, "released_tabs": released, "cleared_app_approvals": cleared]
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
