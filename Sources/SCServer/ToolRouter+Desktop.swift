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
        let waitsOnIndex = try name == "wait_for" && args.optionalInt("element_index") != nil
        var resolved = raw
        if Self.observedWindowTools.contains(name) || (name == "get_window_state" && indexScoped) || waitsOnIndex {
            let session = try args.optionalString("session_id") ?? "legacy"
            resolved["window_id"] = .int(Int(try observedWindow(app, session: session)))
        } else if name == "find_elements",
            let pinned = try? observedWindow(app, session: try args.optionalString("session_id") ?? "legacy")
        {
            // A search runs on what the session saw; an app it has not seen is read afresh like get_window_state.
            resolved["window_id"] = .int(Int(pinned))
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
            name == "get_window_state" || name == "screenshot" || name == "find_elements"
                || output.json["observation"] is [String: Any],
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
            runtime["observed_apps"] = desktop.observedApps()
            return ToolOutput(json: [
                "permissions": [
                    "accessibility": permissions.accessibility, "screen_recording": permissions.screenRecording,
                ],
                "paused": PauseState.isPaused,
                "runtime": runtime,
                "agent_display": Shape.display(await agentDisplay.status()),
                "browser": Shape.browser(await browser.status()),
                "native_capabilities": Shape.codable(await desktop.nativeCapabilities(windowID: nil)),
            ])
        case "capabilities":
            let requestedWindow = try args.optionalInt("window_id")
            let windowID = requestedWindow.flatMap(UInt32.init(exactly:))
            if requestedWindow != nil && windowID == nil { throw SCError.invalidArgument("window_id must be an unsigned 32-bit integer") }
            if let windowID { _ = try appGuard.checkWindow(windowID) }
            return ToolOutput(json: [
                "native": Shape.codable(await desktop.nativeCapabilities(windowID: windowID)),
                "agent_display_is_isolated": false,
                "isolated_backend_available": false,
                "alternatives": ["browser DOM/CDP for web controls", "a separate app instance where supported", "an externally provisioned isolated desktop"],
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
            return Self.isTabBatch(name, args) ? try await runTabBatch(args) : try await runBatch(args)

        case "get_window_state":
            if let expanded = try expandedParts(args, target: "window:\(try args.windowID())", noun: "window") {
                return ToolOutput(json: ["tree": expanded.text, "expanded": expanded.ids, "is_diff": false])
            }
            let structured = try args.bool("structured", default: false)
            let scope = try args.scope()
            let options = SnapshotOptions(
                includeScreenshot: try args.bool("include_screenshot", default: false),
                includeMenuBar: try args.bool("include_menu_bar", default: false),
                maxElements: try args.optionalInt("max_elements").map { min(max($0, 50), 3000) },
                full: try args.bool("full", default: false),
                scope: scope.index,
                scopeLabel: scope.label,
                ocr: try args.bool("ocr", default: false),
                visualSettle: try args.bool("visual_settle", default: false)
            )
            let snapshot = try await desktop.windowState(windowID: try args.windowID(), options: options)
            var json = await snapshotJSON(snapshot, structured: structured)
            if let hiddenParts, !snapshot.isDiff, snapshot.scope == nil, snapshot.scopeLabel == nil {
                json["tree"] = hiddenParts.condense(
                    snapshot.tree, style: .native, target: "window:\(snapshot.window.id)", tool: "get_window_state")
            }
            return ToolOutput(json: json, images: snapshot.screenshot.map { [$0] } ?? [])

        case "find_elements":
            let text = try args.optionalString("text")?.trimmingCharacters(in: .whitespacesAndNewlines)
            let role = try args.optionalString("role")?.trimmingCharacters(in: .whitespaces)
            guard !(text ?? "").isEmpty || !(role ?? "").isEmpty else {
                throw SCError.invalidArgument("Pass text, role, or both")
            }
            let limit = min(max(try args.int("limit", default: 20), 1), 100)
            let windowID = try args.windowID()
            let found = try await desktop.findElements(
                windowID: windowID, text: text?.isEmpty == false ? text : nil,
                role: role?.isEmpty == false ? role : nil, limit: limit)
            var json: [String: Any] = [
                "matches": found.lines, "total": found.total, "observation_id": found.snapshotID, "window_id": windowID,
            ]
            if found.readWindow { json["read_window"] = true }
            if found.total > limit {
                json["hint"] = "Showing \(limit) of \(found.total) matches; refine the query (longer text, or a role)"
            } else if found.total == 0 {
                json["hint"] = "No element of observation \(found.snapshotID) matches; if the UI changed since, call get_window_state and search again"
            }
            return ToolOutput(json: json)

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
        case "wait_for":
            let windowID = try args.windowID()
            let (condition, timeout) = try Self.condition(args, field: nil)
            let outcome = try await desktop.waitFor(windowID: windowID, condition: condition, timeout: timeout)
            var json: [String: Any] = [
                "status": outcome.met ? "verified" : "failed", "waited_ms": Self.milliseconds(outcome.waited),
                "detail": outcome.detail, "window_id": windowID,
            ]
            _ = await attachObservation(windowID, mode: "tree", full: false, to: &json)
            return ToolOutput(json: json)
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
                let lease = try sessions.beginDisplayMutation(session: Execution.context.sessionID)
                defer { sessions.endDisplayMutation(lease) }
                try Execution.context.beforeDispatch()
                await agentDisplay.teardown()
                let state = await agentDisplay.status()
                guard !state.isActive else {
                    throw SCError.actionFailed("Agent display teardown was interrupted; inspect its status before retrying")
                }
                return ToolOutput(json: Shape.display(state))
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

    /// Runs a native action on the target window, then reports what it changed. `observe` and `expect` are checked
    /// before anything is sent to the app.
    private func act(_ args: Arguments, _ perform: (UInt32) async throws -> ActionResult) async throws -> ToolOutput {
        let windowID = try args.windowID()
        let mode = try Self.observeMode(args)
        let expectation = try Self.expectation(args)
        let before = try await checkBefore(windowID, expectation)
        var json = Shape.action(try await perform(windowID))
        json["window_id"] = windowID
        if let expectation {
            json["expect"] = await expectResult(windowID, expectation, before: before, timeout: expectation.timeout)
        }
        let images = await attachObservation(windowID, mode: mode, full: false, to: &json)
        return ToolOutput(json: json, images: images)
    }

    private struct Expectation {
        let condition: WaitCondition
        let timeout: TimeInterval
    }

    /// `expect` may arrive as a JSON string (Gemini through Cursor stringifies nested objects). Unknown fields are
    /// refused by name, since a garbled key would otherwise leave the check silently weaker.
    private static func expectation(_ args: Arguments) throws -> Expectation? {
        guard let value = args.raw["expect"], !value.isNull else { return nil }
        guard let object = Arguments.nestedObject(value) else {
            let got = value.stringValue.map { ", not the string \"\($0.prefix(60))\"" } ?? ""
            throw SCError.invalidArgument("expect must be an object such as {\"text\": \"Saved\"}\(got)")
        }
        let unknown = object.keys.filter { !["text", "element_index", "value", "gone", "timeout_ms"].contains($0) }.sorted()
        guard unknown.isEmpty else {
            throw SCError.invalidArgument(
                "expect: unrecognized fields \(unknown.joined(separator: ", ")); it takes text, element_index with value, gone and timeout_ms")
        }
        let (condition, timeout) = try condition(Arguments(object), field: "expect")
        return Expectation(condition: condition, timeout: timeout)
    }

    /// The condition fields shared by `expect` and `wait_for`; errors name `field` when given.
    private static func condition(_ args: Arguments, field: String?) throws -> (WaitCondition, TimeInterval) {
        do {
            let text = try args.optionalString("text")
            let index = try args.optionalInt("element_index")
            let value = try args.optionalString("value")
            guard text != nil || index != nil || value != nil else {
                throw SCError.invalidArgument("pass text, or element_index with value, e.g. {\"text\": \"Saved\"}")
            }
            if text?.isEmpty == true { throw SCError.invalidArgument("text must not be empty") }
            guard (index == nil) == (value == nil) else {
                throw SCError.invalidArgument("element_index and value go together: the element must have exactly that value")
            }
            let condition = WaitCondition(text: text, elementIndex: index, value: value, gone: try args.bool("gone", default: false))
            return (condition, Double(min(max(try args.int("timeout_ms", default: 2000), 0), 10_000)) / 1000)
        } catch SCError.invalidArgument(let reason) {
            throw SCError.invalidArgument(field.map { "\($0): \(reason)" } ?? reason)
        }
    }

    /// A bad element_index is refused here, before anything is sent to the app.
    private func checkBefore(_ windowID: UInt32, _ expectation: Expectation?) async throws -> ConditionResult? {
        guard let expectation else { return nil }
        do {
            return try await desktop.checkCondition(windowID: windowID, condition: expectation.condition)
        } catch let error as SCError {
            switch error {
            case .elementNotFound, .staleSnapshot: throw SCError.invalidArgument("expect: \(Self.message(for: error))")
            default: throw error
            }
        }
    }

    /// Waits for the expectation after the action and reports it. The action already happened, so a failed wait is
    /// reported in the result, never thrown.
    private func expectResult(_ windowID: UInt32, _ expectation: Expectation, before: ConditionResult?,
                              timeout: TimeInterval) async -> [String: Any] {
        let started = Date()
        do {
            let after = try await desktop.waitFor(windowID: windowID, condition: expectation.condition, timeout: timeout)
            let status = !after.met ? "failed" : before?.met == true ? "preexisting" : "verified"
            return ["status": status, "waited_ms": Self.milliseconds(after.waited), "detail": after.detail]
        } catch {
            return ["status": "failed", "waited_ms": Self.milliseconds(Date().timeIntervalSince(started)),
                    "detail": Self.message(for: error)]
        }
    }

    private static func milliseconds(_ seconds: TimeInterval) -> Int { Int((seconds * 1000).rounded()) }

    static func observeMode(_ args: Arguments) throws -> String {
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
        if let visualSettle = snapshot.visualSettle { json["visual_settle"] = Shape.codable(visualSettle) }
        if let capabilities = snapshot.capabilities { json["capabilities"] = Shape.codable(capabilities) }
        if let base = snapshot.diffBase { json["diff_base"] = base }
        if let scope = snapshot.scope { json["scope"] = scope }
        if let label = snapshot.scopeLabel { json["scope"] = label }
        let browserHint = snapshot.isDiff ? nil : await browserWindowHint(snapshot.window)
        let hints = [snapshot.hint, browserHint].compactMap { $0 }
        if !hints.isEmpty { json["hint"] = hints.joined(separator: " ") }
        return json
    }

    /// Driving the user's own browser window is the slow, contended path for a web page, and an agent that is not
    /// told so takes it: the page's custom-drawn content is missing from this tree, and every touch by the user stops it.
    private func browserWindowHint(_ window: WindowInfo) async -> String? {
        guard BrowserStatus.hostsExtension(bundleID: window.bundleID) else { return nil }
        if await browser.status().connected {
            return "This is the user's own browser window. For work on a web page, open the page in your own tab with "
                + "browser_open (browser_instances names each Chrome profile's account; pass browser_id for the one "
                + "signed in to the page's account): you get its DOM and screenshots, and the user's input does not interrupt you."
        }
        return "This is the user's own browser window, and the Second Cursor extension is not connected in any Chrome "
            + "profile. For work on a web page, ask the user to install or enable the extension in the profile this "
            + "window uses, instead of driving this window: its page content is incomplete here and the user's input stops you."
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
        "click", "type_text", "select_text", "press_key", "set_value", "scroll", "drag", "perform_action", "wait",
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
        let expectation = try Self.expectation(args)
        let before = try await checkBefore(windowID, expectation)

        // Each step passes the same gates as a standalone call and gets its own log line, refusals included.
        let full = try args.bool("full", default: false)
        let session = Execution.context.sessionID
        let results = try await desktop.runBatch(
            windowID: windowID, steps: steps, stopOnError: try args.bool("stop_on_error", default: true),
            stopOnLayoutChange: try args.bool("stop_on_layout_change", default: true)
        ) { [appGuard, log, sessions] step in
            do {
                if PauseState.isPaused { throw SCError.paused }
                let owner = try appGuard.checkWindow(step.windowID)
                let taskTarget = "window:\(step.windowID)"
                TaskRegistry.shared.noteTarget(session: session, target: SCTaskTarget(
                    key: taskTarget, app: AppGuard.windowTarget(owner).name,
                    bundleID: owner.bundleID, windowID: step.windowID))
                try TaskRegistry.shared.checkMutation(session: session, targetKey: taskTarget)
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
                    step["uncertain"] = action.uncertain ?? false
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
        if let expectation {
            // A batch that stopped early did not do what the expectation describes, so it is checked once, not waited for.
            json["expect"] = await expectResult(
                windowID, expectation, before: before,
                timeout: results.contains(where: \.skipped) ? 0 : expectation.timeout)
        }
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
        case "select_text":
            let text = try args.string("text")
            let mode = try args.optionalString("selection_type") ?? "text"
            guard !text.isEmpty, ["text", "cursor_before", "cursor_after"].contains(mode) else {
                throw SCError.invalidArgument("Step \(index) (select_text): nonempty text and valid selection_type required")
            }
            action = .selectText(
                elementIndex: try args.int("element_index"), text: text,
                prefix: try args.optionalString("prefix"), suffix: try args.optionalString("suffix"), mode: mode)
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
    static func stepArguments(_ raw: [String: Value], index: Int, tool: String) throws -> [String: Value] {
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
                    _ = try await agentDisplay.hostLaunched(windowID: window.id, pid: pid)
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
