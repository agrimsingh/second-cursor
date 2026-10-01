import Foundation
import MCP
import SCShared

struct ToolOutput {
    var json: [String: Any]
    var images: [Screenshot] = []
    /// Sent as the text item instead of `json`, for output a model reads as text.
    var text: String? = nil
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
    let log: ActionLog
    let sessions: Sessions
    let repl: BrowserREPL
    let connectionCount: @Sendable () -> Int
    let hiddenParts: HiddenParts?
    private let timingLog: TimingLog?
    private static let toolNames = Set(ToolCatalog.tools.map(\.name))

    init(
        desktop: DesktopControlling, agentDisplay: AgentDisplayControlling, browser: BrowserControlling,
        config: SCConfig, connectionCount: @escaping @Sendable () -> Int = { 0 }
    ) {
        self.connectionCount = connectionCount
        self.timingLog = config.recordTimings == true ? TimingLog() : nil
        self.hiddenParts = config.condenseOutlines == "rules" ? HiddenParts() : nil
        self.desktop = desktop
        self.agentDisplay = agentDisplay
        self.browser = browser
        self.appGuard = AppGuard(config: config)
        self.appApprovals = AppApprovals(config: config)
        let log = ActionLog()
        self.log = log
        self.repl = BrowserREPL(browser: browser, log: log)
        self.sessions = Sessions(
            claimIdleSeconds: config.claimIdleSeconds ?? 300,
            retentionSeconds: config.operationRetentionSeconds ?? 3600)
    }

    func call(name called: String, arguments: [String: Value]) async -> CallTool.Result {
        guard let timingLog else { return await route(called, arguments, timings: nil) }
        let timings = CallTimings()
        let started = DispatchTime.now().uptimeNanoseconds
        let result = await route(called, arguments, timings: timings)
        timingLog.record(tool: called, ms: Timing.since(started), ok: result.isError != true, phases: timings.phasesMS)
        return result
    }

    private func route(_ called: String, _ arguments: [String: Value], timings: CallTimings?) async -> CallTool.Result {
        // act is batch for clients that see the compact tool surface; one code path keeps their gates, logging and
        // parameters identical.
        let name = called == "act" ? "batch" : called
        let receivedAt = Date()
        var raw = arguments
        var malformedArguments: Error?
        if name == "browser_command" {
            do {
                raw = try Self.flatBrowserCommand(raw)
            } catch {
                malformedArguments = error
            }
        }
        // A retry is identified by what the client sent, not by the window an app name resolved to.
        let requested = raw
        do {
            guard Self.toolNames.contains(name) else { throw SCError.invalidArgument("Unknown tool: \(name)") }
            if let malformedArguments { throw malformedArguments }
            let resolveStarted = DispatchTime.now().uptimeNanoseconds
            raw = try await resolveAppTarget(name, raw)
            timings?.add("resolve", ms: Timing.since(resolveStarted))
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
                    repl.close(session: session)
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
            // wait_for's timeout_ms bounds its wait (at most 10 s), not the call. browser_repl answers before clients'
            // 60 s tool timeouts, so no script outlives a client that gave up.
            let limit = name == "browser_repl" ? 55_000 : 60_000
            let timeout = name == "wait_for" ? 60 : Double(min(limit, max(1, try args.int("timeout_ms", default: limit)))) / 1000
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
            let gatesStarted = DispatchTime.now().uptimeNanoseconds
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
                    let approvalStarted = DispatchTime.now().uptimeNanoseconds
                    try await approve(
                        ApprovalRequest(app: app, grants: grants, session: session),
                        deadline: deadline, stillAllowed: { [sessions] in sessions.mayApprove(operation) })
                    timings?.add("approval", ms: Timing.since(approvalStarted))
                }
                // Record admission only after approval. Refusals are recorded once below.
                _ = try await admit(name, args)
                admitted = true
                noteActivity(name, args, owner: owner, session: session)
                timings?.add("gates", ms: Timing.since(gatesStarted) - (timings?.phasesMS["approval"] ?? 0))
                let mutating = ToolCatalog.actionTools.contains(name)
                let target = mutating ? try args.optionalInt("window_id").map { "window:\($0)" } : nil
                let context = ExecutionContext(
                    sessionID: session, observationID: try args.optionalString("observation_id"),
                    browserID: try args.optionalString("browser_id"), deadline: deadline,
                    browserApprovals: browserApprovals, timings: timings
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
                let encodeStarted = DispatchTime.now().uptimeNanoseconds
                let result = Self.success(output)
                timings?.add("encode", ms: Timing.since(encodeStarted))
                return result
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

    private static let malformedNested =
        "arguments must be an object of the operation's fields; operation=capabilities lists them"

    /// The compact schema nests a browser_command's operation fields under `arguments`, and models write batch steps
    /// the same way. Lifts them beside `operation`, in the call and in each step, before the browser policy reads
    /// them; fields already beside `operation` win, so a nested `operation` or `method` cannot replace the real one.
    private static func flatBrowserCommand(_ raw: [String: Value]) throws -> [String: Value] {
        var flat = try lifted(raw, malformed: malformedNested)
        if flat["operation"]?.stringValue == "batch", let steps = flat["steps"]?.arrayValue {
            flat["steps"] = .array(try steps.enumerated().map { index, step in
                guard let object = step.objectValue else { return step }
                return .object(try lifted(object, malformed: "Batch step \(index + 1): \(malformedNested)"))
            })
        }
        return flat
    }

    private static func lifted(_ object: [String: Value], malformed: String) throws -> [String: Value] {
        var object = object
        guard let nested = object.removeValue(forKey: "arguments"), !nested.isNull else { return object }
        guard let fields = Arguments.nestedObject(nested) else { throw SCError.invalidArgument(malformed) }
        return object.merging(fields) { envelope, _ in envelope }
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
        name == "browser_command" || name == "browser_repl" || ToolCatalog.actionTools.contains(name)
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
        var caption = ToolCatalog.captionedTools.contains(name)
            ? ((try? args.optionalString("caption")).flatMap { $0 }.map(Self.caption) ?? "") : nil
        // A wait is a read: it shows its caption when it has one and otherwise leaves the current one.
        if name == "wait_for" { caption = (try? args.optionalString("caption")).flatMap { $0 }.map(Self.caption) }
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
            "arrow_visible": activity.arrowVisible,
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
        var content: [Tool.Content] = [.text(text: output.text ?? encode(output.json), annotations: nil, _meta: nil)]
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
