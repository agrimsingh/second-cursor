import Foundation
import MCP
import SCShared

extension ToolRouter {
    func taskCall(_ args: Arguments, session: String) async throws -> ToolOutput {
        let action = try args.string("action")
        let taskID = try args.optionalString("task_id")
        let registry = TaskRegistry.shared
        if action == "list" {
            let tasks = registry.list(includeFinished: try args.bool("include_finished", default: false))
                .filter { $0.sessions.contains(session) }
            return ToolOutput(json: ["tasks": Shape.codable(tasks), "persistence_error": registry.persistenceError ?? NSNull()])
        }
        if action == "bind" {
            guard let taskID, let interrupted = registry.snapshot(id: taskID), interrupted.status == .interrupted else {
                throw SCError.notAllowed("Only an interrupted task can be rebound; pass its task_id")
            }
            let task = try registry.bind(session: session, to: taskID)
            return ToolOutput(json: ["task": Shape.codable(task)])
        }
        if action == "create" {
            let task = try registry.create(session: session, title: try args.optionalString("title") ?? "Agent task",
                                           clientName: try args.optionalString("client_name"))
            // Browser control belongs to the runtime session and survives a named task ending. Its generation
            // is monotonic across tasks, so replacement explicitly reactivates the extension's paused session.
            if task.controlGeneration > 0 { try await syncBrowserTaskControl(task: task, action: .resume) }
            return ToolOutput(json: ["task": Shape.codable(task)])
        }
        let own = registry.ensureTask(session: session)
        guard taskID == nil || taskID == own.id else { throw SCError.notAllowed("Task belongs to another session; control only your bound task") }
        let task: SCTaskSnapshot
        if action == "get" {
            task = own
        } else if action == "update" {
            let rawStatus = try args.optionalString("status")
            let status = rawStatus.flatMap(SCTaskStatus.init(rawValue:))
            if rawStatus != nil, status == nil { throw SCError.invalidArgument("Unknown task status") }
            task = try registry.heartbeat(session: session, status: status, title: try args.optionalString("title"),
                                          clientName: try args.optionalString("client_name"))
            if task.status.isTerminal { try await finishTaskSession(session) }
        } else if let control = SCTaskControlAction(rawValue: action) {
            task = try registry.control(id: own.id, action: control)
            // The callback also covers the native panel. Await the browser acknowledgement for an API control
            // so a caller may read/resume without racing an outstanding bridge message.
            try await syncBrowserTaskControl(task: task, action: control)
        } else {
            throw SCError.invalidArgument("Unknown task action")
        }
        return ToolOutput(json: ["task": Shape.codable(task), "persistence_error": registry.persistenceError ?? NSNull()])
    }

    func installTaskControls() {
        TaskRegistry.shared.setControlRequestedHandler { [weak self] event in
            guard let self, let task = TaskRegistry.shared.snapshot(id: event.taskID),
                  task.controlGeneration == event.generation else { return }
            // Stop browser input immediately. Cleanup still waits for the operation boundary below so a
            // suspended drag can send its mandatory release before a personal tab loses its ownership.
            Task { try? await self.syncBrowserTaskControl(task: task, action: .pause, immediate: true) }
        }
        TaskRegistry.shared.setControlHandler { [weak self] event in
            guard let self, let task = TaskRegistry.shared.snapshot(id: event.taskID),
                  task.controlGeneration == event.generation else { return }
            for session in event.sessions {
                self.desktop.closeSession(session)
                self.sessions.forgetObservations(session: session)
                if event.action == .takeover || event.action == .cancel {
                    self.sessions.releaseAllTargets(session: session)
                    self.appApprovals.clear(session: session)
                }
                if event.action != .resume { self.desktop.endAgentActivity(session: session) }
                if event.action == .cancel { self.repl.close(session: session) }
            }
            Task { try? await self.syncBrowserTaskControl(task: task, action: event.action) }
        }
    }

    private func syncBrowserTaskControl(task: SCTaskSnapshot, action: SCTaskControlAction, immediate: Bool = false) async throws {
        guard TaskRegistry.shared.snapshot(id: task.id)?.controlGeneration == task.controlGeneration else { return }
        let drained = TaskRegistry.shared.snapshot(id: task.id)?.inFlightCount == 0
        let method = action == .resume ? "session.resume"
            : !immediate && drained && (action == .takeover || action == .cancel) ? "session.takeover" : "session.pause"
        do {
            let instances = try JSONSerialization.jsonObject(with: await browser.instances()) as? [String: Any] ?? [:]
            let connected = Set((instances["browsers"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String })
            let missing = Set(task.targets.compactMap(\.browserID)).subtracting(connected)
            guard missing.isEmpty else {
                throw SCError.browserUnavailable("Task control cannot be acknowledged because a task's browser is disconnected; reconnect it before resuming")
            }
            for session in task.sessions {
                _ = try await browserSessionCommand(method, session: session, generation: task.controlGeneration)
            }
        } catch {
            TaskRegistry.shared.controlFailed(taskID: task.id, generation: task.controlGeneration,
                                               reason: Self.message(for: error))
            throw error
        }
    }

    /// A fresh context prevents a caller's mutation permit, selected browser or default legacy session from
    /// leaking into the control plane. BrowserBridge broadcasts these commands to every connected profile.
    func browserSessionCommand(_ method: String, session: String, generation: Int? = nil) async throws -> [String: Any] {
        var params: [String: Any] = ["session_id": session]
        if let generation { params["generation"] = generation }
        let data = try JSONSerialization.data(withJSONObject: params)
        return try await Execution.$context.withValue(ExecutionContext(sessionID: session)) {
            let reply = try await browser.command(method, params: data)
            return try JSONSerialization.jsonObject(with: reply) as? [String: Any] ?? [:]
        }
    }

    private func finishTaskSession(_ session: String) async throws {
        sessions.releaseAllTargets(session: session)
        sessions.forgetObservations(session: session)
        desktop.closeSession(session)
        desktop.endAgentActivity(session: session)
        appApprovals.clear(session: session)
        _ = try await browserSessionCommand("session.end_turn", session: session)
    }

    func closeSession(session: String) async throws {
        try sessions.close(session)
        TaskRegistry.shared.interrupt(session: session)
        desktop.closeSession(session)
        desktop.endAgentActivity(session: session)
        appApprovals.clear(session: session)
        repl.close(session: session)
        _ = try await browserSessionCommand("session.close", session: session)
    }

    static func targetKey(_ name: String, _ args: Arguments) -> String? {
        // Reclaiming is required before the browser permits a fresh read after takeover. Ownership-only
        // operations still pass the task's status/generation gate, but cannot depend on that later read.
        if name.hasPrefix("browser_") || isTabBatch(name, args) {
            if name == "browser_command", let operation = try? args.optionalString("operation"),
               ["claim", "release", "retain"].contains(operation) { return nil }
            if let id = try? args.optionalInt("tab_id"), let browserID = try? args.optionalString("browser_id") {
                return SCTaskTarget.browserKey(browserID: browserID, tabID: id)
            }
            return nil
        }
        if let id = try? args.optionalInt("window_id") { return "window:\(id)" }
        return nil
    }

    func noteTaskTarget(_ name: String, _ args: Arguments, owner: WindowOwner?, session: String) {
        if name.hasPrefix("browser_") || Self.isTabBatch(name, args) {
            guard let id = try? args.optionalInt("tab_id") else { return }
            guard let browserID = try? args.optionalString("browser_id") else { return }
            TaskRegistry.shared.noteTarget(session: session, target: SCTaskTarget(
                key: SCTaskTarget.browserKey(browserID: browserID, tabID: id), app: "Browser", tabID: id, browserID: browserID))
        } else if let id = try? args.optionalInt("window_id"), let windowID = UInt32(exactly: id) {
            TaskRegistry.shared.noteTarget(session: session, target: SCTaskTarget(
                key: "window:\(id)", app: owner.map { AppGuard.windowTarget($0).name }, bundleID: owner?.bundleID, windowID: windowID))
        }
    }

    func noteTaskObservation(_ name: String, _ args: Arguments, result: ToolOutput, session: String, generation: Int?) {
        guard args.raw["expand"] == nil else { return } // expanding cached outline text is not a new read
        let observation = result.json["observation"] as? [String: Any]
        let browserRead = name.hasPrefix("browser_") || Self.isTabBatch(name, args)
        let freshBrowserTree = ((observation ?? result.json)["freshness"] as? [String: Any])?["status"] as? String == "current"
        let freshBrowserScreenshot = name == "browser_screenshot"
            || (name == "browser_command" && (try? args.string("operation")) == "screenshot")
        let observed = browserRead ? freshBrowserTree || freshBrowserScreenshot
            : observation != nil || ["get_window_state", "screenshot"].contains(name)
        if observed, let key = Self.targetKey(name, args) { TaskRegistry.shared.noteObservation(session: session, targetKey: key, generation: generation) }
        if let tab = result.json["tab"] as? [String: Any], let id = tab["id"] as? Int,
           let browserID = try? args.optionalString("browser_id") {
            let key = SCTaskTarget.browserKey(browserID: browserID, tabID: id)
            TaskRegistry.shared.noteTarget(session: session, target: SCTaskTarget(key: key, app: "Browser", tabID: id, browserID: browserID))
            if freshBrowserTree { TaskRegistry.shared.noteObservation(session: session, targetKey: key, generation: generation) }
        }
    }

    /// Pin the selected instance before authorizing observations. Tab IDs are only unique within one browser,
    /// and choosing the default again later could dispatch to a different profile after a disconnect.
    func resolveBrowserTarget(_ name: String, _ raw: [String: Value]) async throws -> [String: Value] {
        guard (name.hasPrefix("browser_") && !["browser_status", "browser_instances"].contains(name))
            || Self.isTabBatch(name, Arguments(raw)) else { return raw }
        let args = Arguments(raw)
        let chosen: String
        if let requested = try args.optionalString("browser_id") {
            let data = await browser.instances()
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
            let ids = (json["browsers"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String }
            guard ids.contains(requested) else { throw SCError.browserUnavailable("The selected browser is not connected") }
            chosen = requested
        } else {
            chosen = try await browser.selectInstance(tabID: try args.optionalInt("tab_id"))
        }
        var result = raw
        result["browser_id"] = .string(chosen)
        return result
    }
}
