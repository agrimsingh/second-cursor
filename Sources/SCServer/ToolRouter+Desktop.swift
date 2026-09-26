import Foundation
import MCP
import SCShared

extension ToolRouter {
    /// Codex's argument names, accepted without being advertised.
    private static let aliases: [String: [String: String]] = [
        "click": ["click_count": "count", "mouse_button": "button"],
        "press_key": ["key": "keys"],
    ]

    /// Tools whose element indexes, points or keys refer to what the agent last saw of a window.
    private static let observedWindowTools = ToolCatalog.observingTools.union(["batch", "inspect_element"])

    /// Fills in `window_id` from `app` for window-scoped tools, so a client can address an app by name.
    /// Plain reads and window moves use the app's main window; anything that refers to an observation uses the
    /// app's window this session observed last. Anything else passes through unchanged; while paused nothing is
    /// resolved and the pause gate answers.
    func resolveAppTarget(_ name: String, _ raw: [String: Value]) async throws -> [String: Value] {
        let args = Arguments(raw)
        guard ToolCatalog.windowScopedTools.contains(name), raw["window_id"]?.isNull ?? true, !PauseState.isPaused,
            let app = try args.optionalString("app")?.trimmingCharacters(in: .whitespaces), !app.isEmpty
        else { return raw }
        // A label scope is looked up afresh like a plain read; an index scope refers to what the session saw.
        let indexScoped = try args.scope().index != nil
        var resolved = raw
        if Self.observedWindowTools.contains(name) || (name == "get_window_state" && indexScoped) {
            let session = try args.optionalString("session_id") ?? "legacy"
            resolved["window_id"] = .int(Int(try observedWindow(app, session: session)))
        } else {
            resolved["window_id"] = .int(Int(try await desktop.appWindow(app)))
        }
        return resolved
    }

    /// The app's main window can change between calls, so an index or point the agent holds is only meaningful
    /// in the window it came from.
    private func observedWindow(_ app: String, session: String) throws -> UInt32 {
        try sessions.validate(session)
        let needle = app.lowercased()
        let matching = sessions.observations(session: session).filter {
            $0.appName.lowercased().contains(needle) || $0.bundleID?.lowercased().contains(needle) == true
        }
        let exact = matching.filter { $0.appName.lowercased() == needle || $0.bundleID?.lowercased() == needle }
        guard let pinned = (exact.isEmpty ? matching : exact).first else {
            throw SCError.invalidArgument(
                "This session has not observed a window of \"\(app)\" yet; observe the app first (get_window_state with app)")
        }
        guard AppGuard.owner(of: pinned.windowID)?.pid == pinned.pid else {
            throw SCError.actionFailed(
                "Window \(pinned.windowID) of \(pinned.appName), the one this session last observed, is gone; "
                    + "observe the app again (get_window_state with app)")
        }
        return pinned.windowID
    }

    /// Remembers the window a read, or an action's observation, showed the session.
    func noteObservation(_ name: String, _ args: Arguments, owner: WindowOwner?, output: ToolOutput, session: String) {
        guard let owner, ToolCatalog.windowScopedTools.contains(name),
            name == "get_window_state" || name == "screenshot" || output.json["observation"] is [String: Any],
            let windowID = try? args.windowID()
        else { return }
        noteObserved(windowID, owner: owner, session: session)
    }

    func noteObserved(_ windowID: UInt32, owner: WindowOwner, session: String) {
        let window = Sessions.ObservedWindow(
            windowID: windowID, pid: owner.pid, appName: AppGuard.windowTarget(owner).name, bundleID: owner.bundleID)
        sessions.noteObserved(window, session: session)
    }

    func runDesktop(_ name: String, _ given: Arguments, owner: WindowOwner?) async throws -> ToolOutput {
        let args = given.aliasing(Self.aliases[name] ?? [:])
        switch name {
        case "status":
            let permissions = desktop.permissionStatus()
            var runtime = sessions.statistics()
            runtime["http_connections"] = connectionCount()
            return ToolOutput(json: [
                "permissions": [
                    "accessibility": permissions.accessibility, "screen_recording": permissions.screenRecording,
                ],
                "paused": PauseState.isPaused,
                "runtime": runtime,
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
            let structured = try args.bool("structured", default: false)
            let scope = try args.scope()
            let options = SnapshotOptions(
                includeScreenshot: try args.bool("include_screenshot", default: false),
                includeMenuBar: try args.bool("include_menu_bar", default: false),
                maxElements: try args.optionalInt("max_elements").map { min(max($0, 50), 3000) },
                full: try args.bool("full", default: false),
                scope: scope.index,
                scopeLabel: scope.label,
                ocr: try args.bool("ocr", default: false)
            )
            let snapshot = try await desktop.windowState(windowID: try args.windowID(), options: options)
            return ToolOutput(
                json: await snapshotJSON(snapshot, structured: structured),
                images: snapshot.screenshot.map { [$0] } ?? [])

        case "click":
            guard let target = try Self.inWindowPoints(args.clickTarget(), args, windowID: args.windowID()) else {
                throw SCError.invalidArgument("Pass element_index, or both x and y")
            }
            let buttonName = try args.optionalString("button")?.lowercased() ?? "left"
            guard let button = MouseButton(rawValue: buttonName) else {
                throw SCError.invalidArgument("button must be one of left, right, middle")
            }
            let count = min(max(try args.int("count", default: 1), 1), 3)
            let modifiers = try args.strings("modifiers")
            return try await act(args) {
                try await desktop.click(windowID: $0, target: target, button: button, count: count, modifiers: modifiers)
            }

        case "drag":
            func coordinate(_ key: String) throws -> Double {
                guard let value = try args.optionalDouble(key) else {
                    throw SCError.invalidArgument("\(key) is required")
                }
                return value
            }
            let windowID = try args.windowID()
            let from = try Self.windowPoint(try coordinate("from_x"), try coordinate("from_y"), args, windowID: windowID)
            let to = try Self.windowPoint(try coordinate("to_x"), try coordinate("to_y"), args, windowID: windowID)
            let modifiers = try args.strings("modifiers")
            return try await act(args) {
                try await desktop.drag(windowID: $0, from: from, to: to, modifiers: modifiers)
            }

        case "type_text":
            let text = try args.string("text")
            let elementIndex = try args.optionalInt("element_index")
            let keystrokesOnly = try args.bool("keystrokes_only", default: false)
            return try await act(args) {
                try await desktop.typeText(
                    windowID: $0, text: text, elementIndex: elementIndex, keystrokesOnly: keystrokesOnly)
            }

        case "set_value":
            let elementIndex = try args.int("element_index")
            let value = try args.string("value")
            return try await act(args) {
                try await desktop.setValue(windowID: $0, elementIndex: elementIndex, value: value)
            }

        case "press_key":
            let keys = try args.string("keys")
            return try await act(args) { try await desktop.pressKey(windowID: $0, keys: keys) }

        case "scroll":
            let target = try Self.inWindowPoints(args.clickTarget(), args, windowID: args.windowID())
            let direction = try args.direction()
            let amount = try Self.scrollAmount(args, target: target)
            return try await act(args) {
                try await desktop.scroll(windowID: $0, target: target, direction: direction, amount: amount)
            }

        case "perform_action":
            let elementIndex = try args.int("element_index")
            let action = try args.string("action")
            return try await act(args) {
                try await desktop.performAction(windowID: $0, elementIndex: elementIndex, action: action)
            }

        case "inspect_element":
            let data = try await desktop.inspectElement(
                windowID: args.windowID(), index: args.int("element_index"), offset: args.int("offset", default: 0),
                length: args.int("length", default: 4000))
            return ToolOutput(json: try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:])
        case "select_text":
            let index = try args.int("element_index")
            let text = try args.string("text")
            let prefix = try args.optionalString("prefix")
            let suffix = try args.optionalString("suffix")
            let mode = try args.optionalString("selection_type") ?? "text"
            return try await act(args) {
                try await desktop.selectText(
                    windowID: $0, index: index, text: text, prefix: prefix, suffix: suffix, mode: mode)
            }
        case "paste":
            let text = try args.string("text")
            let format = try args.optionalString("format") ?? "text"
            return try await act(args) { try await desktop.paste(windowID: $0, text: text, format: format) }
        case "screenshot":
            let windowID = try args.windowID()
            let shot = try await desktop.screenshot(windowID: windowID)
            var json = screenshotJSON(shot, windowID: windowID)
            json["window_id"] = windowID
            return ToolOutput(json: json, images: [shot])

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

    /// Runs a native action on the target window, then reports what it changed. `observe` is checked
    /// before anything is sent to the app.
    private func act(_ args: Arguments, _ perform: (UInt32) async throws -> ActionResult) async throws -> ToolOutput {
        let windowID = try args.windowID()
        let mode = try Self.observeMode(args)
        var json = Shape.action(try await perform(windowID))
        json["window_id"] = windowID
        let images = await attachObservation(windowID, mode: mode, full: false, to: &json)
        return ToolOutput(json: json, images: images)
    }

    private static func observeMode(_ args: Arguments) throws -> String {
        let mode = try args.optionalString("observe")?.lowercased() ?? "tree"
        guard ["tree", "screenshot", "both", "none"].contains(mode) else {
            throw SCError.invalidArgument("observe must be one of tree, screenshot, both, none")
        }
        return mode
    }

    /// Settles, then adds `observation` to `json` and returns its screenshot, if any. The tree is a diff
    /// against this session's previous read of the window (full when there is none) and becomes the
    /// window's current observation. The action already happened, so a failed read is reported as
    /// `observation_error` beside it rather than thrown; with `both`, a failed capture still returns the tree.
    private func attachObservation(_ windowID: UInt32, mode: String, full: Bool, to json: inout [String: Any]) async
        -> [Screenshot]
    {
        do {
            switch mode {
            case "tree", "both":
                let snapshot = try await desktop.windowState(
                    windowID: windowID, options: SnapshotOptions(includeScreenshot: mode == "both", full: full))
                var observation = await snapshotJSON(snapshot)
                if let error = observation.removeValue(forKey: "screenshot_error") {
                    json["observation_error"] = "Screenshot failed (the tree above is current): \(error)"
                }
                json["observation"] = observation
                return snapshot.screenshot.map { [$0] } ?? []
            case "screenshot":
                await desktop.settle(windowID: windowID)
                let shot = try await desktop.screenshot(windowID: windowID)
                json["observation"] = screenshotJSON(shot, windowID: windowID)
                return [shot]
            default:
                return []
            }
        } catch {
            json["observation_error"] = Self.message(for: error)
            return []
        }
    }

    /// Codex's fractional `pages`: one AX page action per page on an element, else ten 3-line wheel ticks.
    private static func scrollAmount(_ args: Arguments, target: ClickTarget?) throws -> Int {
        if try args.optionalInt("amount") == nil, let pages = try args.optionalDouble("pages") {
            if case .element = target { return max(Int(pages.rounded(.up)), 1) }
            return max(Int((pages * 10).rounded()), 1)
        }
        return max(try args.int("amount", default: 3), 1)
    }

    /// `elements` is opt-in (`structured`) and never part of a diff.
    func snapshotJSON(_ snapshot: WindowSnapshot, structured: Bool = false) async -> [String: Any] {
        var json: [String: Any] = [
            "window": Shape.window(snapshot.window, agentBounds: await agentBounds()),
            "snapshot_id": snapshot.snapshotID,
            "element_count": snapshot.elementCount,
            "truncated": snapshot.truncated,
            "is_diff": snapshot.isDiff,
            "tree": snapshot.tree,
        ]
        if structured, !snapshot.isDiff, let data = try? JSONEncoder().encode(snapshot.elements),
            let elements = try? JSONSerialization.jsonObject(with: data)
        {
            json["elements"] = elements
        }
        if let shot = snapshot.screenshot { json["screenshot"] = screenshotJSON(shot, windowID: snapshot.window.id) }
        if let error = snapshot.screenshotError { json["screenshot_error"] = error }
        if let ms = snapshot.ocrMS { json["ocr_ms"] = ms }
        if let count = snapshot.ocrCount { json["ocr_count"] = count }
        if let error = snapshot.ocrError { json["ocr_error"] = error }
        if let base = snapshot.diffBase { json["diff_base"] = base }
        if let scope = snapshot.scope { json["scope"] = scope }
        if let label = snapshot.scopeLabel { json["scope"] = label }
        return json
    }

    private static let screenshotScales = ScreenshotScales()

    /// Shapes a window screenshot and remembers its scale, so `coordinate_space: "screenshot"` can read pixels off it.
    private func screenshotJSON(_ shot: Screenshot, windowID: UInt32) -> [String: Any] {
        Self.screenshotScales.note(shot.scale, windowID: windowID, session: Execution.context.sessionID)
        var json = Shape.screenshot(shot)
        json["coordinates"] =
            "pass coordinate_space: \"screenshot\" to click at image pixels, or divide by "
            + "\(String(format: "%.4g", shot.scale)) for window points"
        return json
    }

    private static func screenshotCoordinates(_ args: Arguments) throws -> Bool {
        switch try args.optionalString("coordinate_space")?.lowercased() ?? "window" {
        case "window": return false
        case "screenshot": return true
        default: throw SCError.invalidArgument("coordinate_space must be window or screenshot")
        }
    }

    /// x/y in window points. With `coordinate_space: "screenshot"` they are pixels of this session's latest
    /// screenshot of the window, so they are divided by its scale.
    private static func windowPoint(_ x: Double, _ y: Double, _ args: Arguments, windowID: UInt32) throws -> Point {
        guard try screenshotCoordinates(args) else { return Point(x: x, y: y) }
        guard let scale = screenshotScales.scale(windowID: windowID, session: Execution.context.sessionID), scale > 0
        else {
            throw SCError.invalidArgument(
                "coordinate_space \"screenshot\" needs a screenshot of window \(windowID) taken in this session "
                    + "(screenshot, get_window_state with include_screenshot, or observe=both); none was")
        }
        return Point(x: x / scale, y: y / scale)
    }

    private static func inWindowPoints(_ target: ClickTarget?, _ args: Arguments, windowID: UInt32) throws -> ClickTarget? {
        guard case .point(let x, let y)? = target else {
            _ = try screenshotCoordinates(args)
            return target
        }
        let point = try windowPoint(x, y, args, windowID: windowID)
        return .point(x: point.x, y: point.y)
    }

    private static let batchTools: Set<String> = [
        "click", "type_text", "press_key", "set_value", "scroll", "drag", "perform_action", "wait",
    ]

    private func runBatch(_ args: Arguments) async throws -> ToolOutput {
        let windowID = try args.windowID()
        let rawSteps = try args.objects("steps")
        guard !rawSteps.isEmpty else { throw SCError.invalidArgument("steps must contain at least one step") }
        guard rawSteps.count <= 50 else { throw SCError.invalidArgument("A batch holds at most 50 steps") }
        let steps = try rawSteps.enumerated().map {
            try Self.batchStep($0.element, index: $0.offset, defaultWindow: windowID)
        }
        let observe = try Self.observeMode(args)

        // Each step passes the same gates as a standalone call and gets its own log line, refusals included.
        let full = try args.bool("full", default: false)
        let session = Execution.context.sessionID
        let results = try await desktop.runBatch(
            windowID: windowID, steps: steps, stopOnError: try args.bool("stop_on_error", default: true),
            stopOnLayoutChange: try args.bool("stop_on_layout_change", default: true)
        ) { [appGuard, log, sessions] step in
            do {
                if PauseState.isPaused { throw SCError.paused }
                _ = try appGuard.checkWindow(step.windowID)
                try sessions.use("window:\(step.windowID)", session: session)
                log.record(
                    ActionLogEntry(tool: step.tool, target: "window_id=\(step.windowID)", detail: step.logDetail))
            } catch {
                log.record(
                    ActionLogEntry(
                        tool: step.tool, target: "window_id=\(step.windowID)",
                        detail: "refused: \(Self.message(for: error))"))
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
            "window_id": windowID,
            "observation": NSNull(),
        ]
        let images = await attachObservation(windowID, mode: observe, full: full, to: &json)
        return ToolOutput(json: json, images: images)
    }

    private static func batchStep(_ raw: [String: Value], index: Int, defaultWindow: UInt32) throws -> BatchStep {
        let outer = Arguments(raw)
        let tool = try outer.string("tool").lowercased()
        guard batchTools.contains(tool) else {
            throw SCError.invalidArgument(
                "Step \(index): tool must be one of \(batchTools.sorted().joined(separator: ", "))")
        }
        let args = Arguments(try stepArguments(raw, index: index, tool: tool)).aliasing(aliases[tool] ?? [:])
        let windowID =
            try args.optionalInt("window_id").map { id -> UInt32 in
                guard let wid = UInt32(exactly: id) else {
                    throw SCError.invalidArgument("Step \(index): window_id must be a positive integer")
                }
                return wid
            } ?? defaultWindow

        func require<T>(_ value: T?, _ what: String) throws -> T {
            guard let value else {
                let unknown = args.raw.keys.filter {
                    ToolCatalog.batchStepArguments[$0] == nil && aliases[tool]?[$0] == nil && $0 != "milliseconds"
                }.sorted()
                let hint = unknown.isEmpty ? "" : "; unrecognized fields: \(unknown.joined(separator: ", "))"
                throw SCError.invalidArgument("Step \(index) (\(tool)): \(what) is required\(hint)")
            }
            return value
        }
        let action: BatchAction
        switch tool {
        case "click":
            let button = MouseButton(rawValue: try args.optionalString("button")?.lowercased() ?? "left")
            action = .click(
                target: try require(try inWindowPoints(args.clickTarget(), args, windowID: windowID), "element_index or x and y"),
                button: try require(button, "button left, right or middle"),
                count: min(max(try args.int("count", default: 1), 1), 3))
        case "type_text":
            action = .typeText(
                text: try args.string("text"), elementIndex: try args.optionalInt("element_index"),
                keystrokesOnly: try args.bool("keystrokes_only", default: false))
        case "press_key":
            action = .pressKey(keys: try args.string("keys"))
        case "set_value":
            action = .setValue(elementIndex: try args.int("element_index"), value: try args.string("value"))
        case "scroll":
            let target = try inWindowPoints(args.clickTarget(), args, windowID: windowID)
            action = .scroll(target: target, direction: try args.direction(), amount: try scrollAmount(args, target: target))
        case "drag":
            action = .drag(
                from: try windowPoint(
                    try require(try args.optionalDouble("from_x"), "from_x"),
                    try require(try args.optionalDouble("from_y"), "from_y"), args, windowID: windowID),
                to: try windowPoint(
                    try require(try args.optionalDouble("to_x"), "to_x"),
                    try require(try args.optionalDouble("to_y"), "to_y"), args, windowID: windowID))
        case "perform_action":
            action = .performAction(elementIndex: try args.int("element_index"), action: try args.string("action"))
        default:
            let ms = try args.optionalInt("ms") ?? args.optionalInt("milliseconds")
            action = .wait(milliseconds: try require(ms, "ms"))
        }
        return BatchStep(
            tool: tool, windowID: windowID, action: action, logDetail: args.logDetail,
            caption: try args.optionalString("caption").map(ToolRouter.caption))
    }

    /// Accepts args as an object, a JSON string, or fields beside tool. A malformed args value must fail
    /// loudly: silently dropping it makes the step report a missing argument instead.
    private static func stepArguments(_ raw: [String: Value], index: Int, tool: String) throws -> [String: Value] {
        var merged = raw.filter { $0.key != "tool" && $0.key != "args" }
        guard let value = raw["args"], !value.isNull else { return merged }
        guard let object = Arguments.nestedObject(value) else {
            let got = value.stringValue.map { ", not the string \"\($0.prefix(60))\"" } ?? ""
            throw SCError.invalidArgument("Step \(index) (\(tool)): args must be an object such as {\"element_index\": 5}\(got)")
        }
        merged.merge(object) { $1 }
        return merged
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

/// The scale of each session's latest screenshot of each window.
private final class ScreenshotScales: @unchecked Sendable {
    private let lock = NSLock()
    private var scales: [String: (scale: Double, at: Date)] = [:]

    func note(_ scale: Double, windowID: UInt32, session: String) {
        lock.lock()
        defer { lock.unlock() }
        if scales.count >= 512 {
            scales = Dictionary(uniqueKeysWithValues: scales.sorted { $0.value.at > $1.value.at }.prefix(256).map { ($0.key, $0.value) })
        }
        scales["\(session):\(windowID)"] = (scale, Date())
    }

    func scale(windowID: UInt32, session: String) -> Double? {
        lock.lock()
        defer { lock.unlock() }
        return scales["\(session):\(windowID)"]?.scale
    }
}
