import Foundation
import MCP
import SCShared

extension ToolRouter {
    func runDesktop(_ name: String, _ args: Arguments, owner: WindowOwner?) async throws -> ToolOutput {
        switch name {
        case "status":
            let permissions = desktop.permissionStatus()
            return ToolOutput(json: [
                "permissions": ["accessibility": permissions.accessibility, "screen_recording": permissions.screenRecording],
                "paused": PauseState.isPaused,
                "agent_display": Shape.display(await agentDisplay.status()),
                "browser": Shape.browser(await browser.status()),
            ])

        case "list_apps":
            let apps = await desktop.listApps(includeNotRunning: try args.bool("include_not_running", default: false))
            return ToolOutput(json: ["apps": apps.map(Shape.app)])

        case "list_windows":
            let windows = await desktop.listWindows(appQuery: try args.optionalString("app"))
            let bounds = await agentBounds()
            return ToolOutput(json: ["windows": windows.map { Shape.window($0, agentBounds: bounds) }])

        case "launch_app":
            return try await launchApp(args)

        case "batch":
            return try await runBatch(args)

        case "get_window_state":
            let options = SnapshotOptions(
                includeScreenshot: try args.bool("include_screenshot", default: true),
                includeMenuBar: try args.bool("include_menu_bar", default: false),
                full: try args.bool("full", default: false),
                scope: try args.optionalInt("scope")
            )
            let snapshot = try await desktop.windowState(windowID: try args.windowID(), options: options)
            return ToolOutput(json: await snapshotJSON(snapshot), images: snapshot.screenshot.map { [$0] } ?? [])

        case "click":
            guard let target = try args.clickTarget() else { throw SCError.invalidArgument("Pass element_index, or both x and y") }
            let buttonName = try args.optionalString("button")?.lowercased() ?? "left"
            guard let button = MouseButton(rawValue: buttonName) else {
                throw SCError.invalidArgument("button must be one of left, right, middle")
            }
            let count = min(max(try args.int("count", default: 1), 1), 3)
            return action(try await desktop.click(
                windowID: try args.windowID(), target: target, button: button, count: count, modifiers: try args.strings("modifiers")))

        case "drag":
            func coordinate(_ key: String) throws -> Double {
                guard let value = try args.optionalDouble(key) else { throw SCError.invalidArgument("\(key) is required") }
                return value
            }
            return action(try await desktop.drag(
                windowID: try args.windowID(),
                from: Point(x: try coordinate("from_x"), y: try coordinate("from_y")),
                to: Point(x: try coordinate("to_x"), y: try coordinate("to_y")),
                modifiers: try args.strings("modifiers")))

        case "type_text":
            return action(try await desktop.typeText(
                windowID: try args.windowID(), text: try args.string("text"), elementIndex: try args.optionalInt("element_index"),
                keystrokesOnly: try args.bool("keystrokes_only", default: false)))

        case "set_value":
            return action(try await desktop.setValue(
                windowID: try args.windowID(), elementIndex: try args.int("element_index"), value: try args.string("value")))

        case "press_key":
            return action(try await desktop.pressKey(windowID: try args.windowID(), keys: try args.string("keys")))

        case "scroll":
            return action(try await desktop.scroll(
                windowID: try args.windowID(), target: try args.clickTarget(),
                direction: try args.direction(), amount: max(try args.int("amount", default: 3), 1)))

        case "perform_action":
            return action(try await desktop.performAction(
                windowID: try args.windowID(), elementIndex: try args.int("element_index"), action: try args.string("action")))

        case "screenshot":
            let shot = try await desktop.screenshot(windowID: try args.windowID())
            return ToolOutput(json: Shape.screenshot(shot), images: [shot])

        case "agent_display":
            switch try args.string("action").lowercased() {
            case "status":
                return ToolOutput(json: Shape.display(await agentDisplay.status()))
            case "create":
                return ToolOutput(json: Shape.display(try await agentDisplay.ensureDisplay()))
            case "teardown":
                await agentDisplay.teardown()
                return ToolOutput(json: Shape.display(await agentDisplay.status()))
            default:
                throw SCError.invalidArgument("action must be one of status, create, teardown")
            }

        case "move_to_agent_display":
            guard let owner else { throw SCError.windowNotFound(try args.windowID()) }
            let placement = try await agentDisplay.moveToAgentDisplay(windowID: try args.windowID(), pid: owner.pid)
            return ToolOutput(json: Shape.placement(placement))

        case "return_window":
            try await agentDisplay.returnWindow(windowID: try args.windowID())
            return ToolOutput(json: Shape.ok)

        default:
            throw SCError.invalidArgument("Unknown tool \"\(name)\"")
        }
    }

    private func action(_ result: ActionResult) -> ToolOutput {
        ToolOutput(json: Shape.action(result))
    }

    private func snapshotJSON(_ snapshot: WindowSnapshot) async -> [String: Any] {
        var json: [String: Any] = [
            "window": Shape.window(snapshot.window, agentBounds: await agentBounds()),
            "snapshot_id": snapshot.snapshotID,
            "element_count": snapshot.elementCount,
            "truncated": snapshot.truncated,
            "is_diff": snapshot.isDiff,
            "tree": snapshot.tree,
        ]
        if let base = snapshot.diffBase { json["diff_base"] = base }
        if let scope = snapshot.scope { json["scope"] = scope }
        return json
    }

    private static let batchTools: Set<String> = ["click", "type_text", "press_key", "set_value", "scroll", "drag", "perform_action", "wait"]

    private func runBatch(_ args: Arguments) async throws -> ToolOutput {
        let windowID = try args.windowID()
        let rawSteps = try args.objects("steps")
        guard !rawSteps.isEmpty else { throw SCError.invalidArgument("steps must contain at least one step") }
        guard rawSteps.count <= 50 else { throw SCError.invalidArgument("A batch holds at most 50 steps") }
        let steps = try rawSteps.enumerated().map { try Self.batchStep($0.element, index: $0.offset, defaultWindow: windowID) }
        let observe = try args.optionalString("observe")?.lowercased() ?? "tree"
        guard ["tree", "screenshot", "both", "none"].contains(observe) else {
            throw SCError.invalidArgument("observe must be one of tree, screenshot, both, none")
        }

        // Each step passes the same gates as a standalone call and gets its own log line, refusals included.
        let full = try args.bool("full", default: false)
        let results = try await desktop.runBatch(
            windowID: windowID, steps: steps, stopOnError: try args.bool("stop_on_error", default: true),
            stopOnLayoutChange: try args.bool("stop_on_layout_change", default: true)
        ) { [appGuard, log] step in
            do {
                if PauseState.isPaused { throw SCError.paused }
                _ = try appGuard.checkWindow(step.windowID)
                log.record(ActionLogEntry(tool: step.tool, target: "window_id=\(step.windowID)", detail: step.logDetail))
            } catch {
                log.record(ActionLogEntry(tool: step.tool, target: "window_id=\(step.windowID)", detail: "refused: \(Self.message(for: error))"))
                throw error
            }
        }

        var json: [String: Any] = [
            "steps": results.enumerated().map { index, result -> [String: Any] in
                var step: [String: Any] = ["index": index, "tool": result.tool, "ok": result.ok]
                if result.skipped { step["skipped"] = true }
                if let action = result.result {
                    step["route"] = action.route
                    step["verified"] = action.verified ?? NSNull()
                    if let note = action.note { step["note"] = note }
                }
                if let error = result.error { step["error"] = error }
                return step
            },
            "completed": results.filter(\.ok).count,
            "stopped_early": results.contains(where: \.skipped),
            "observation": NSNull(),
        ]
        var images: [Screenshot] = []
        do {
            switch observe {
            case "tree", "both":
                let snapshot = try await desktop.windowState(
                    windowID: windowID, options: SnapshotOptions(includeScreenshot: observe == "both", full: full))
                json["observation"] = await snapshotJSON(snapshot)
                images = snapshot.screenshot.map { [$0] } ?? []
            case "screenshot":
                await desktop.settle(windowID: windowID)
                let shot = try await desktop.screenshot(windowID: windowID)
                json["observation"] = Shape.screenshot(shot)
                images = [shot]
            default:
                break
            }
        } catch {
            json["observation_error"] = Self.message(for: error)
        }
        return ToolOutput(json: json, images: images)
    }

    private static func batchStep(_ raw: [String: Value], index: Int, defaultWindow: UInt32) throws -> BatchStep {
        let outer = Arguments(raw)
        let tool = try outer.string("tool").lowercased()
        guard batchTools.contains(tool) else {
            throw SCError.invalidArgument("Step \(index): tool must be one of \(batchTools.sorted().joined(separator: ", "))")
        }
        let args = Arguments(raw["args"]?.objectValue ?? [:])
        let windowID = try args.optionalInt("window_id").map { id -> UInt32 in
            guard let wid = UInt32(exactly: id) else { throw SCError.invalidArgument("Step \(index): window_id must be a positive integer") }
            return wid
        } ?? defaultWindow

        func require<T>(_ value: T?, _ what: String) throws -> T {
            guard let value else { throw SCError.invalidArgument("Step \(index) (\(tool)): \(what) is required") }
            return value
        }
        let action: BatchAction
        switch tool {
        case "click":
            let button = MouseButton(rawValue: try args.optionalString("button")?.lowercased() ?? "left")
            action = .click(target: try require(try args.clickTarget(), "element_index or x and y"),
                            button: try require(button, "button left, right or middle"),
                            count: min(max(try args.int("count", default: 1), 1), 3))
        case "type_text":
            action = .typeText(text: try args.string("text"), elementIndex: try args.optionalInt("element_index"),
                               keystrokesOnly: try args.bool("keystrokes_only", default: false))
        case "press_key":
            action = .pressKey(keys: try args.string("keys"))
        case "set_value":
            action = .setValue(elementIndex: try args.int("element_index"), value: try args.string("value"))
        case "scroll":
            action = .scroll(target: try args.clickTarget(), direction: try args.direction(), amount: max(try args.int("amount", default: 3), 1))
        case "drag":
            action = .drag(from: Point(x: try require(try args.optionalDouble("from_x"), "from_x"), y: try require(try args.optionalDouble("from_y"), "from_y")),
                           to: Point(x: try require(try args.optionalDouble("to_x"), "to_x"), y: try require(try args.optionalDouble("to_y"), "to_y")))
        case "perform_action":
            action = .performAction(elementIndex: try args.int("element_index"), action: try args.string("action"))
        default:
            let ms = try args.optionalInt("ms") ?? args.optionalInt("milliseconds")
            action = .wait(milliseconds: try require(ms, "ms"))
        }
        return BatchStep(tool: tool, windowID: windowID, action: action, logDetail: args.logDetail)
    }

    private func launchApp(_ args: Arguments) async throws -> ToolOutput {
        let query = try args.string("app")
        let urls = try args.strings("urls").map(Self.url)
        let newInstance = try args.bool("new_instance", default: false)
        let onAgentDisplay = try args.bool("on_agent_display", default: false)

        // Every window that existed before the launch belongs to the user and must not be moved.
        let before = Set(await desktop.listWindows(appQuery: nil).map(\.id))
        let app = try await desktop.launchApp(query, urls: urls, newInstance: newInstance)
        try appGuard.checkLaunched(app)
        var json: [String: Any] = ["app": Shape.app(app)]
        guard let pid = app.pid else {
            json["windows"] = [Any]()
            return ToolOutput(json: json)
        }

        let appQuery = app.bundleID ?? app.name
        func appWindows() async -> [WindowInfo] {
            await desktop.listWindows(appQuery: appQuery).filter { $0.pid == pid }
        }

        var windows = await appWindows()
        let reusedWithoutNewWindow = urls.isEmpty && windows.contains { before.contains($0.id) }
        if !reusedWithoutNewWindow {
            let deadline = Date().addingTimeInterval(10)
            while !windows.contains(where: { !before.contains($0.id) }), Date() < deadline {
                try await Task.sleep(nanoseconds: 250_000_000)
                windows = await appWindows()
            }
        }

        let fresh = windows.filter { !before.contains($0.id) }
        if onAgentDisplay && !fresh.isEmpty {
            var failures: [String] = []
            for window in fresh {
                do {
                    _ = try await agentDisplay.moveToAgentDisplay(windowID: window.id, pid: pid)
                } catch {
                    failures.append("window \(window.id): \(Self.message(for: error))")
                }
            }
            if !failures.isEmpty {
                json["note"] = "Could not move to the agent display: " + failures.joined(separator: "; ")
            }
            windows = await appWindows()
        }

        let bounds = await agentBounds()
        json["windows"] = windows.map { Shape.window($0, agentBounds: bounds) }
        return ToolOutput(json: json)
    }

    private static func url(_ string: String) -> URL {
        if let url = URL(string: string), let scheme = url.scheme, scheme.count > 1 { return url }
        return URL(fileURLWithPath: (string as NSString).expandingTildeInPath)
    }
}
