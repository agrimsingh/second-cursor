import AppKit
import ApplicationServices
import Foundation
import SCShared

public final class Desktop: DesktopControlling, @unchecked Sendable {
    /// What the agent last saw of one window. Only touched on that window's app queue.
    private final class WindowTree {
        let pid: pid_t
        let table = IndexTable()
        var snapshotID: String?
        var observationID: String?
        /// The last full rendering and its frame-free keys; the next diff is computed against them.
        var lines: [String]?
        var keys: [String]?
        /// The element limit of the last read, kept so later reads and diffs cut the outline at the same place.
        var maxElements: Int?
        var elements: [Int: SnapshotElement] = [:]

        init(pid: pid_t) { self.pid = pid }
    }

    /// AX and input calls block on the target app, so each app gets its own serial queue: actions on
    /// one app stay ordered, while agents working in different apps run in parallel and a hung app
    /// only stalls its own queue.
    private let lock = NSLock()
    private var queues: [pid_t: DispatchQueue] = [:]
    private let listQueue = DispatchQueue(label: "dev.secondcursor.desktop.list")
    private let resolver = WindowResolver()
    private var trees: [String: WindowTree] = [:]
    /// Window size when the agent last observed it; pixel coordinates it sends are relative to that layout.
    private var observedSizes: [String: CGSize] = [:]
    /// The latest action on each window, and what has happened since: reads settle once and captures wait for a
    /// repaint once per action.
    private struct Activity {
        let at: Date
        var settled = false
        var painted = false
    }
    private var activity: [CGWindowID: Activity] = [:]

    /// Draws the agent's visible cursor. Set once by the app at launch; command-line tools leave it nil.
    public var cursor: AgentCursorPresenting?

    public init() {}

    public func closeSession(_ sessionID: String) {
        lock.lock(); defer { lock.unlock() }
        let prefix=sessionID+":"
        trees=trees.filter { !$0.key.hasPrefix(prefix) }
        observedSizes=observedSizes.filter { !$0.key.hasPrefix(prefix) }
    }

    /// Only for windows on the user's current Space: an arrow aimed at a window they cannot see would mislead.
    private func showCursor(_ record: CGWindowRecord, _ event: AgentCursorEvent) {
        guard record.isOnScreen, let cursor else { return }
        cursor.present(event)
    }

    private func queue(for pid: pid_t) -> DispatchQueue {
        lock.lock()
        defer { lock.unlock() }
        if let queue = queues[pid] { return queue }
        let queue = DispatchQueue(label: "dev.secondcursor.desktop.\(pid)")
        queues[pid] = queue
        return queue
    }

    private func run<T>(on queue: DispatchQueue, _ work: @escaping () throws -> T) async throws -> T {
        let context = Execution.context
        return try await withCheckedThrowingContinuation { continuation in
            queue.async { continuation.resume(with: Result {
                try Execution.$context.withValue(context) { try context.beforeDispatch(); return try work() }
            }) }
        }
    }

    private func run<T>(windowID: CGWindowID, _ work: @escaping () throws -> T) async throws -> T {
        guard let record = WindowList.record(windowID) else { throw SCError.windowNotFound(windowID) }
        return try await run(on: queue(for: record.pid), work)
    }

    /// A window id can be reused by a different process, so a tree for another pid starts over.
    private func tree(for wid: CGWindowID, pid: pid_t) -> WindowTree {
        lock.lock()
        defer { lock.unlock() }
        if let tree = trees["\(Execution.context.sessionID):\(wid)"], tree.pid == pid { return tree }
        let tree = WindowTree(pid: pid)
        trees["\(Execution.context.sessionID):\(wid)"] = tree
        return tree
    }

    private func storedTree(for wid: CGWindowID) -> WindowTree? {
        lock.lock()
        defer { lock.unlock() }
        return trees["\(Execution.context.sessionID):\(wid)"]
    }

    private func forget(_ wid: CGWindowID) {
        lock.lock()
        trees["\(Execution.context.sessionID):\(wid)"] = nil
        observedSizes["\(Execution.context.sessionID):\(wid)"] = nil
        lock.unlock()
    }

    private func noteObserved(_ wid: CGWindowID, size: CGSize) {
        lock.lock()
        observedSizes["\(Execution.context.sessionID):\(wid)"] = size
        lock.unlock()
    }

    private func observedSize(_ wid: CGWindowID) -> CGSize? {
        lock.lock()
        defer { lock.unlock() }
        return observedSizes["\(Execution.context.sessionID):\(wid)"]
    }

    public func permissionStatus() -> PermissionStatus {
        PermissionStatus(accessibility: AXIsProcessTrusted(), screenRecording: CGPreflightScreenCaptureAccess())
    }

    public func listApps(includeNotRunning: Bool) async -> [AppInfo] {
        Apps.list(includeNotRunning: includeNotRunning)
    }

    public func listWindows(appQuery: String?) async -> [WindowInfo] {
        (try? await run(on: listQueue) { self.windows(matching: appQuery) }) ?? []
    }

    public func appWindow(_ query: String) async throws -> UInt32 {
        try await run(on: listQueue) { try self.preferredWindow(query) }
    }

    public func launchApp(_ query: String, urls: [URL], newInstance: Bool) async throws -> AppInfo {
        try Execution.context.beforeDispatch()
        guard let app = Apps.resolve(query) else { throw SCError.appNotFound(query) }
        return try await Apps.launch(app, urls: urls, newInstance: newInstance)
    }

    /// A failed capture does not fail the read: the tree was taken and is now the diff baseline, so the caller
    /// must see it.
    public func windowState(windowID: UInt32, options: SnapshotOptions) async throws -> WindowSnapshot {
        var snapshot = try await run(windowID: windowID) {
            if self.needsSettle(windowID) { self.settleNow(windowID) }
            return try self.snapshot(windowID, options: options)
        }
        if options.includeScreenshot {
            do {
                snapshot.screenshot = try await capture(windowID)
            } catch {
                snapshot.screenshotError = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            }
        }
        return snapshot
    }

    /// Custom-drawn apps (Ableton, games) update their accessibility tree before they repaint, so a capture right
    /// after the tree settles can show the previous frame. Once per action, give the window a frame's worth of time.
    private func capture(_ wid: CGWindowID) async throws -> Screenshot {
        if claimPaintWait(wid) { try? await Task.sleep(nanoseconds: 150_000_000) }
        return try await Capture.window(wid)
    }

    public func click(windowID: UInt32, target: ClickTarget, button: MouseButton, count: Int, modifiers: [String]) async throws -> ActionResult {
        let flags = try Keyboard.flags(modifiers)
        return try await act(windowID) { try self.performClick(windowID, target: target, button: button, count: max(count, 1), flags: flags) }
    }

    public func drag(windowID: UInt32, from: Point, to: Point, modifiers: [String]) async throws -> ActionResult {
        let flags = try Keyboard.flags(modifiers)
        return try await act(windowID) { try self.performDrag(windowID, from: from, to: to, flags: flags) }
    }

    public func typeText(windowID: UInt32, text: String, elementIndex: Int?, keystrokesOnly: Bool) async throws -> ActionResult {
        try await act(windowID) { try self.insertText(windowID, text: text, elementIndex: elementIndex, keystrokesOnly: keystrokesOnly) }
    }

    public func setValue(windowID: UInt32, elementIndex: Int, value: String) async throws -> ActionResult {
        try await act(windowID) { try self.applyValue(windowID, elementIndex: elementIndex, value: value) }
    }

    public func pressKey(windowID: UInt32, keys: String) async throws -> ActionResult {
        let chord = try Keyboard.parse(keys)
        return try await act(windowID) { try self.performKey(windowID, chord: chord) }
    }

    public func runBatch(windowID: UInt32, steps: [BatchStep], stopOnError: Bool, stopOnLayoutChange: Bool,
                         admit: @escaping @Sendable (BatchStep) throws -> Void) async throws -> [BatchStepResult] {
        try await run(windowID: windowID) {
            let initial = try self.record(windowID)
            guard steps.allSatisfy({ WindowList.record($0.windowID)?.pid == initial.pid }) else {
                throw SCError.invalidArgument("A batch cannot cross app processes; submit a separate batch for each app")
            }
            try self.validateObservation(windowID)
            // Pixel steps were planned against the layout the agent last saw, or failing that, the layout at batch start.
            var expected: [CGWindowID: CGSize] = [:]
            for wid in Set(steps.map(\.windowID)) {
                expected[wid] = self.observedSize(wid) ?? WindowList.record(wid)?.bounds.size
            }
            var results: [BatchStepResult] = []
            var halted = false
            for step in steps {
                if halted {
                    results.append(BatchStepResult(tool: step.tool, ok: false, skipped: true, error: "skipped: an earlier step failed"))
                    continue
                }
                if stopOnLayoutChange, step.action.usesPixels, let before = expected[step.windowID],
                   let now = WindowList.record(step.windowID)?.bounds.size,
                   abs(now.width - before.width) > 1 || abs(now.height - before.height) > 1 {
                    results.append(BatchStepResult(tool: step.tool, ok: false, error:
                        "Stopped before this step: the window layout changed (\(Int(before.width))x\(Int(before.height)) → "
                        + "\(Int(now.width))x\(Int(now.height))) since it was last observed, so its pixel coordinates may be stale. "
                        + "Observe again, or pass stop_on_layout_change=false."))
                    halted = true
                    continue
                }
                do {
                    try Execution.context.beforeDispatch()
                    try admit(step)
                    let result = try self.perform(step)
                    self.markAction(on: step.windowID)
                    results.append(BatchStepResult(tool: step.tool, ok: true, result: result))
                } catch {
                    let message = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                    results.append(BatchStepResult(tool: step.tool, ok: false, error: message))
                    if stopOnError || (error as? SCError) == .paused { halted = true }
                }
            }
            return results
        }
    }

    public func settle(windowID: UInt32) async {
        _ = try? await run(windowID: windowID) { self.settleNow(windowID) }
    }

    // MARK: Actions (run on the target app's queue)

    private func act(_ windowID: CGWindowID, _ work: @escaping () throws -> ActionResult) async throws -> ActionResult {
        try await run(windowID: windowID) {
            try self.validateObservation(windowID)
            let result = try work()
            self.markAction(on: windowID)
            return result
        }
    }

    private func perform(_ step: BatchStep) throws -> ActionResult {
        let wid = step.windowID
        switch step.action {
        case let .click(target, button, count):
            return try performClick(wid, target: target, button: button, count: max(count, 1), flags: [])
        case let .typeText(text, elementIndex, keystrokesOnly):
            return try insertText(wid, text: text, elementIndex: elementIndex, keystrokesOnly: keystrokesOnly)
        case let .pressKey(keys):
            return try performKey(wid, chord: try Keyboard.parse(keys))
        case let .setValue(elementIndex, value):
            return try applyValue(wid, elementIndex: elementIndex, value: value)
        case let .scroll(target, direction, amount):
            return try performScroll(wid, target: target, direction: direction, amount: max(amount, 1))
        case let .drag(from, to):
            return try performDrag(wid, from: from, to: to, flags: [])
        case let .performAction(elementIndex, action):
            return try performNamedAction(wid, elementIndex: elementIndex, action: action)
        case let .wait(milliseconds):
            usleep(UInt32(min(max(milliseconds, 0), 10_000)) * 1000)
            return ActionResult(route: "wait")
        }
    }

    private func performDrag(_ wid: CGWindowID, from: Point, to: Point, flags: CGEventFlags) throws -> ActionResult {
        let record = try record(wid)
        try requirePointerTarget(record)
        for point in [from, to] {
            guard point.x >= 0, point.y >= 0, point.x < record.bounds.width, point.y < record.bounds.height else {
                throw SCError.invalidArgument("Point (\(point.x), \(point.y)) is outside window \(wid) (\(Int(record.bounds.width))x\(Int(record.bounds.height)))")
            }
        }
        showCursor(record, .drag(from: Point(x: record.bounds.minX + from.x, y: record.bounds.minY + from.y),
                                 to: Point(x: record.bounds.minX + to.x, y: record.bounds.minY + to.y)))
        Pointer.drag(pid: record.pid, wid: record.id,
                     from: Self.location(record, local: CGPoint(x: from.x, y: from.y)),
                     to: Self.location(record, local: CGPoint(x: to.x, y: to.y)), flags: flags)
        return ActionResult(route: "skylight_drag")
    }

    private func applyValue(_ wid: CGWindowID, elementIndex: Int, value: String) throws -> ActionResult {
        let (_, element) = try element(wid, elementIndex)
        let error = element.element.set(kAXValueAttribute, value as CFString)
        guard error == .success else { throw SCError.actionFailed("Setting AXValue failed (AXError \(error.rawValue))") }
        let readBack = AX.text(element.element.attribute(kAXValueAttribute))
        return ActionResult(route: "ax_value", verified: readBack == value,
                            note: readBack == value ? nil : "read back \(readBack.map { "\"\($0.prefix(80))\"" } ?? "nothing")")
    }

    private func performKey(_ wid: CGWindowID, chord: KeyChord) throws -> ActionResult {
        let record = try record(wid)
        _ = try resolver.resolve(pid: record.pid, wid: wid)
        guard chord.flags.contains(.maskCommand) else {
            withKeyWindow(record) { Keyboard.press(chord, pid: record.pid) }
            return ActionResult(route: "pid_keyboard")
        }
        // The handoff serializes input for every app, so an app that does not answer is found out before it.
        let bar = MenuShortcut.menuBar(pid: record.pid)
        // Menu key equivalents only fire in an AppKit-active app with a key window, so hand the target
        // AppKit focus (no raise, no Space change) for the duration of the chord, then give it back.
        let handoff = SkyLight.focusWithoutRaise(pid: record.pid, wid: wid)
        defer { if let handoff { usleep(50_000); SkyLight.restore(handoff) } }
        var route = "pid_keyboard_focus_handoff"
        var verified: Bool?
        var note: String?
        withKeyWindow(record) {
            // The walk has to happen inside the handoff (see MenuShortcut.find), so it gets a hard budget.
            let found = bar.flatMap { MenuShortcut.find(chord, in: $0, deadline: Date().addingTimeInterval(1.5)) }
            let pressed = found?.item.perform(kAXPressAction)
            if pressed == .success || pressed == .cannotComplete {
                route = "ax_menu_shortcut"
                // .cannotComplete only means the app did not answer in time; the item may still run later.
                verified = pressed == .success ? true : nil
                note = found?.title.map { "pressed menu item \"\($0)\"" }
                if pressed == .cannotComplete {
                    note = (note.map { $0 + "; " } ?? "") + "app did not reply in time; the action may still run"
                }
            } else {
                Keyboard.press(chord, pid: record.pid)
            }
        }
        return ActionResult(route: route, verified: verified, note: note)
    }

    private func performNamedAction(_ wid: CGWindowID, elementIndex: Int, action: String) throws -> ActionResult {
        let (_, element) = try element(wid, elementIndex)
        let available = element.element.actions
        guard let match = available.first(where: { $0 == action || TreeBuilder.actionName($0) == action }) else {
            let names = available.map(TreeBuilder.actionName)
            throw SCError.invalidArgument("Element \(elementIndex) has no action \(action); available: \(names.isEmpty ? "none" : names.joined(separator: ", "))")
        }
        let error = element.element.perform(match)
        guard error == .success || error == .cannotComplete else {
            throw SCError.actionFailed("\(action) failed (AXError \(error.rawValue))")
        }
        return ActionResult(route: "ax_action", note: error == .cannotComplete ? "app did not reply in time; the action may still have run" : nil)
    }

    // MARK: Settling

    private func markAction(on wid: CGWindowID) {
        lock.lock()
        activity[wid] = Activity(at: Date())
        lock.unlock()
    }

    /// Acted on in the last 2 s and not yet seen to settle since.
    private func needsSettle(_ wid: CGWindowID) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let entry = activity[wid], Date().timeIntervalSince(entry.at) < 2 else { return false }
        return !entry.settled
    }

    /// True at most once per action, for a window acted on in the last 2 s.
    private func claimPaintWait(_ wid: CGWindowID) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard var entry = activity[wid], Date().timeIntervalSince(entry.at) < 2, !entry.painted else { return false }
        entry.painted = true
        activity[wid] = entry
        return true
    }

    /// Polls a fingerprint of the accessibility tree until it holds still for `quiet`, so an
    /// observation reflects the UI after the app has finished reacting, not mid-update. A window that
    /// never holds still (meters, playback) counts as settled after `cap`, so the next read does not wait again.
    @discardableResult
    private func settleNow(_ wid: CGWindowID, quiet: TimeInterval = 0.2, cap: TimeInterval = 3) -> Bool {
        guard let record = WindowList.record(wid), let window = try? resolver.resolve(pid: record.pid, wid: wid) else { return false }
        func fingerprint() -> Int { TreeBuilder.fingerprint(window: window, maxNodes: 1000) }
        defer {
            lock.lock()
            activity[wid]?.settled = true
            lock.unlock()
        }
        let deadline = Date().addingTimeInterval(cap)
        var last = fingerprint()
        var lastChange = Date()
        while Date() < deadline {
            usleep(60_000)
            let current = fingerprint()
            if current != last {
                last = current
                lastChange = Date()
            } else if Date().timeIntervalSince(lastChange) >= quiet {
                return true
            }
        }
        return false
    }

    /// Keys reach whichever window the app treats as key, so make the target key first (without
    /// activating the app). When the user is working in that same app, hand key back afterwards.
    private func withKeyWindow(_ record: CGWindowRecord, _ body: () -> Void) {
        let previous = SkyLight.isFront(pid: record.pid, wid: record.id) ? SkyLight.keyWindow(pid: record.pid) : nil
        SkyLight.makeKeyWindow(pid: record.pid, wid: record.id)
        usleep(50_000)
        body()
        if let previous, previous != record.id {
            usleep(50_000)
            SkyLight.makeKeyWindow(pid: record.pid, wid: previous)
        }
    }

    public func scroll(windowID: UInt32, target: ClickTarget?, direction: ScrollDirection, amount: Int) async throws -> ActionResult {
        try await act(windowID) { try self.performScroll(windowID, target: target, direction: direction, amount: max(amount, 1)) }
    }

    public func performAction(windowID: UInt32, elementIndex: Int, action: String) async throws -> ActionResult {
        try await act(windowID) { try self.performNamedAction(windowID, elementIndex: elementIndex, action: action) }
    }

    /// Only a window acted on moments ago goes through its app's queue (to settle first), so an idle
    /// screenshot never waits behind a busy or hung app.
    public func screenshot(windowID: UInt32) async throws -> Screenshot {
        if needsSettle(windowID) { await settle(windowID: windowID) }
        guard let record = WindowList.record(windowID) else { throw SCError.windowNotFound(windowID) }
        noteObserved(windowID, size: record.bounds.size)
        return try await capture(windowID)
    }

    // MARK: Windows

    private func record(_ wid: CGWindowID) throws -> CGWindowRecord {
        guard let record = WindowList.record(wid) else {
            forget(wid)
            throw SCError.windowNotFound(wid)
        }
        return record
    }

    private func windows(matching query: String?) -> [WindowInfo] {
        let needle = query?.trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        let all = WindowList.all()
        resolver.forget(windowsNotIn: Set(all.map(\.id)))
        let onScreen = WindowList.onScreenIDs()
        var listed: [pid_t: [CGWindowID: AXUIElement]] = [:]
        var result: [WindowInfo] = []
        for record in all where WindowList.isAppWindow(record) && record.pid != getpid() {
            let app = NSRunningApplication(processIdentifier: record.pid)
            if app?.activationPolicy == .prohibited { continue }
            let appName = app?.localizedName ?? record.ownerName
            if !needle.isEmpty, !appName.lowercased().contains(needle), !(app?.bundleIdentifier?.lowercased().contains(needle) ?? false) {
                continue
            }
            if listed[record.pid] == nil {
                let element = AXUIElementCreateApplication(record.pid)
                AXUIElementSetMessagingTimeout(element, 0.5)
                listed[record.pid] = resolver.listedWindows(element)
            }
            let window = listed[record.pid]?[record.id] ?? resolver.cached(pid: record.pid, wid: record.id)
            var info = windowInfo(record, window: window)
            info.isOnScreen = onScreen.contains(record.id)
            result.append(info)
        }
        return result
    }

    /// Several windows: the matching process that is frontmost, else the most recently launched, and
    /// then its only window or its AX main (else focused) window.
    private func preferredWindow(_ query: String) throws -> UInt32 {
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        let matches = windows(matching: needle)
        let exact = matches.filter { $0.appName.lowercased() == needle || $0.bundleID?.lowercased() == needle }
        let candidates = exact.isEmpty ? matches : exact
        guard let only = candidates.first else {
            throw SCError.invalidArgument("No window for app \"\(query)\"; call launch_app first")
        }
        if candidates.count == 1 { return only.id }

        func rank(_ pid: pid_t) -> (Int, Date) {
            let app = NSRunningApplication(processIdentifier: pid)
            return (app?.isActive == true ? 1 : 0, app?.launchDate ?? .distantPast)
        }
        let pid = Set(candidates.map(\.pid)).max { rank($0) < rank($1) } ?? only.pid
        let own = candidates.filter { $0.pid == pid }
        if own.count == 1 { return own[0].id }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 1)
        if let main = [kAXMainWindowAttribute, kAXFocusedWindowAttribute].lazy
            .compactMap({ app.element($0)?.windowID }).first(where: { id in own.contains { $0.id == id } }) {
            return main
        }
        let listed = candidates.prefix(20).map { "\($0.id) \"\($0.title)\" (\($0.appName), pid \($0.pid))" }
        throw SCError.invalidArgument(
            "\(candidates.count) windows match app \"\(query)\" and none is clearly its main window; pass window_id, one of: "
                + listed.joined(separator: "; ") + (candidates.count > 20 ? "; …" : ""))
    }

    private func windowInfo(_ record: CGWindowRecord, window: AXUIElement?) -> WindowInfo {
        let app = NSRunningApplication(processIdentifier: record.pid)
        var title = record.title
        if title.isEmpty, let window { title = window.string(kAXTitleAttribute) ?? "" }
        return WindowInfo(
            id: record.id, pid: record.pid, appName: app?.localizedName ?? record.ownerName, bundleID: app?.bundleIdentifier,
            title: title, frame: Rect(record.bounds), spaceIDs: Spaces.ids(for: record.id), isOnScreen: record.isOnScreen,
            isMinimized: window.flatMap { $0.bool(kAXMinimizedAttribute) }
        )
    }

    // MARK: Snapshots

    private func snapshot(_ wid: CGWindowID, options: SnapshotOptions) throws -> WindowSnapshot {
        let record = try record(wid)
        let window = try resolver.resolve(pid: record.pid, wid: wid)
        let frame = window.frame ?? record.bounds
        let tree = tree(for: wid, pid: record.pid)
        let id = String(UUID().uuidString.prefix(8)).lowercased()
        noteObserved(wid, size: record.bounds.size)
        tree.observationID = id
        let limit = max(1, options.maxElements ?? tree.maxElements ?? 400)
        tree.maxElements = limit
        let maxNodes = max(2500, limit * 3)

        if options.scope != nil || options.scopeLabel != nil {
            let root: SnapshotElement
            let handle: String
            if let scope = options.scope {
                guard tree.snapshotID != nil else { throw SCError.staleSnapshot(wid) }
                guard let element = tree.elements[scope] else { throw SCError.elementNotFound(scope) }
                guard element.element.isAlive else {
                    throw SCError.actionFailed("Element \(scope) no longer exists; call get_window_state again")
                }
                root = element
                handle = "[\(scope)]"
            } else {
                let label = options.scopeLabel ?? ""
                (root, handle) = try labelledContainer(label, in: window, windowID: wid, maxNodes: maxNodes)
                // A label needs no earlier read, and the indexes it returns must be actionable.
                if tree.snapshotID == nil { tree.snapshotID = id }
            }
            var builder = TreeBuilder(maxNodes: maxNodes)
            builder.build(subtreeOf: root.element, key: root.key, inWebArea: root.inWebArea)
            var out = TreeRenderer.scoped(scope: handle, windowOrigin: frame.origin, nodes: builder.nodes,
                                          table: tree.table, maxElements: max(limit, 2000))
            if builder.truncated, out.footer == nil { out.footer = TreeRenderer.walkLimitFooter }
            tree.elements.merge(out.elements) { _, scoped in scoped }
            var result = WindowSnapshot(window: windowInfo(record, window: window), snapshotID: id, tree: out.text,
                                  elementCount: out.indexedCount, truncated: out.truncated || builder.truncated, scope: options.scope)
            result.scopeLabel = options.scope == nil ? options.scopeLabel : nil
            result.elements = out.structured
            return result
        }

        var builder = TreeBuilder(maxNodes: maxNodes)
        let app = AXUIElementCreateApplication(record.pid)
        let menuBar = options.includeMenuBar ? app.element(kAXMenuBarAttribute) : nil
        builder.build(window: window, menuBar: menuBar)
        let title = window.string(kAXTitleAttribute) ?? record.title
        var out = TreeRenderer.full(title: title, size: frame.size, windowOrigin: frame.origin, nodes: builder.nodes,
                                    table: tree.table, maxElements: limit)
        out.appendOrientation(focused: app.element(kAXFocusedUIElementAttribute))
        if builder.truncated, out.footer == nil { out.footer = TreeRenderer.walkLimitFooter }
        tree.table.retain(keys: Set(builder.nodes.map(\.key)))
        let base = tree.snapshotID
        var diff: String?
        if !options.full, let lines = tree.lines, let keys = tree.keys {
            diff = TreeRenderer.diff(previous: lines, previousKeys: keys, current: out.lines, currentKeys: out.keys)
        }
        tree.snapshotID = id
        tree.lines = out.lines
        tree.keys = out.keys
        tree.elements = out.elements
        var result = WindowSnapshot(
            window: windowInfo(record, window: window), snapshotID: id, tree: diff ?? out.text,
            elementCount: out.indexedCount, truncated: out.truncated || builder.truncated,
            isDiff: diff != nil, diffBase: diff != nil ? base : nil
        )
        result.elements = out.structured
        return result
    }

    /// The first element in walk order labelled exactly `label`, else case-insensitively; containers (elements with
    /// children in the outline) win over leaves, so a heading that repeats a panel's name does not shadow the panel.
    private func labelledContainer(_ label: String, in window: AXUIElement, windowID: CGWindowID,
                                   maxNodes: Int) throws -> (SnapshotElement, String) {
        var builder = TreeBuilder(maxNodes: maxNodes)
        builder.build(window: window, menuBar: nil)
        let nodes = builder.nodes
        func hasChildren(_ i: Int) -> Bool { i + 1 < nodes.count && nodes[i + 1].indent > nodes[i].indent }
        let exact = nodes.indices.filter { nodes[$0].label == label }
        let folded = nodes.indices.filter { nodes[$0].label.caseInsensitiveCompare(label) == .orderedSame }
        guard let match = exact.first(where: hasChildren) ?? exact.first ?? folded.first(where: hasChildren) ?? folded.first
        else {
            var candidates: [String] = []
            for i in nodes.indices where hasChildren(i) && !nodes[i].label.isEmpty {
                let quoted = "\"\(TreeBuilder.escape(nodes[i].label, limit: 40))\""
                if !candidates.contains(quoted) { candidates.append(quoted) }
                if candidates.count == 12 { break }
            }
            throw SCError.invalidArgument(
                "No element labelled \"\(TreeBuilder.escape(label, limit: 80))\" in window \(windowID); "
                    + (candidates.isEmpty ? "it has no labelled containers" : "labelled containers: " + candidates.joined(separator: ", "))
                    + ". Pass one of these, or an element index")
        }
        let node = nodes[match]
        return (SnapshotElement(element: node.element, role: node.role, inWebArea: node.inWebArea, key: node.key),
                "\"\(TreeBuilder.escape(node.label, limit: 120))\"")
    }

    private func element(_ wid: CGWindowID, _ index: Int) throws -> (CGWindowRecord, SnapshotElement) {
        let record = try record(wid)
        guard let tree = storedTree(for: wid), tree.pid == record.pid, tree.snapshotID != nil else { throw SCError.staleSnapshot(wid) }
        guard let element = tree.elements[index] else { throw SCError.elementNotFound(index) }
        guard element.element.isAlive else {
            throw SCError.actionFailed("Element \(index) no longer exists; call get_window_state again")
        }
        return (record, element)
    }

    private func validateObservation(_ wid: CGWindowID) throws {
        if let expected = Execution.context.observationID,
           storedTree(for: wid)?.observationID != expected {
            throw SCError.actionFailed("Stale observation; read get_window_state again before acting")
        }
    }

    public func inspectElement(windowID: UInt32, index: Int, offset: Int, length: Int) async throws -> Data {
        try await run(windowID: windowID) {
            let (_, item) = try self.element(windowID, index)
            let text = AX.text(item.element.attribute(kAXValueAttribute)) ?? item.element.string(kAXTitleAttribute) ?? ""
            let ns = text as NSString
            guard offset >= 0, offset <= ns.length, length >= 0, length <= 100_000 else {
                throw SCError.invalidArgument("offset/length must be in range; length is limited to 100000 UTF-16 units")
            }
            let count = min(length, ns.length-offset)
            let selection = AX.range(item.element.attribute(kAXSelectedTextRangeAttribute))
            return try JSONSerialization.data(withJSONObject: [
                "text": ns.substring(with: NSRange(location: offset, length: count)), "offset": offset,
                "length": count, "total_length": ns.length, "has_more": offset+count < ns.length,
                "actions": item.element.actions,
                "editable": item.element.isSettable(kAXValueAttribute),
                "selection": selection.map { ["offset": $0.location, "length": $0.length] } as Any? ?? NSNull()
            ])
        }
    }

    public func selectText(windowID: UInt32, index: Int, text: String, prefix: String?, suffix: String?, mode: String) async throws -> ActionResult {
        try await act(windowID) {
            guard !text.isEmpty, ["text", "cursor_before", "cursor_after"].contains(mode) else { throw SCError.invalidArgument("Nonempty text and valid selection mode required") }
            let (_, item) = try self.element(windowID, index)
            let value = (AX.text(item.element.attribute(kAXValueAttribute)) ?? "") as NSString
            var matches: [NSRange] = [], start = 0
            while start < value.length {
                let range = value.range(of: text, range: NSRange(location: start, length: value.length-start))
                if range.location == NSNotFound { break }
                let before = value.substring(to: range.location), after = value.substring(from: NSMaxRange(range))
                if prefix.map({ before.hasSuffix($0) }) ?? true, suffix.map({ after.hasPrefix($0) }) ?? true { matches.append(range) }
                start = NSMaxRange(range)
            }
            guard matches.count == 1 else { throw SCError.invalidArgument("Text matched \(matches.count) times; supply prefix/suffix to select exactly one occurrence") }
            let found = matches[0]
            let range = CFRange(location: mode == "cursor_after" ? NSMaxRange(found) : found.location, length: mode == "text" ? found.length : 0)
            guard item.element.set(kAXSelectedTextRangeAttribute, AX.rangeValue(range)) == .success else { throw SCError.actionFailed("This element does not support text selection") }
            let actual = AX.range(item.element.attribute(kAXSelectedTextRangeAttribute))
            return ActionResult(route: "ax_selection", verified: actual?.location == range.location && actual?.length == range.length)
        }
    }

    private static let clipboardLock = NSLock()
    public func paste(windowID: UInt32, text: String, format: String) async throws -> ActionResult {
        guard ["text", "html", "md"].contains(format) else { throw SCError.invalidArgument("format must be text, html or md") }
        // AppKit HTML import must run on the main thread, before taking the app queue or clipboard lock.
        let plain = format == "html" ? await MainActor.run {
            (try? NSAttributedString(data: Data(text.utf8),
                options: [.documentType: NSAttributedString.DocumentType.html],
                documentAttributes: nil))?.string ?? text
        } : text
        return try await act(windowID) {
            Self.clipboardLock.lock(); defer { Self.clipboardLock.unlock() }
            let pb = NSPasteboard.general
            let saved = (pb.pasteboardItems ?? []).map { item in item.types.compactMap { type in item.data(forType: type).map { (type, $0) } } }
            pb.clearContents()
            let item = NSPasteboardItem()
            if format == "html" {
                item.setString(text, forType: .html)
                item.setString(plain, forType: .string)
            } else { item.setString(text, forType: .string) }
            pb.writeObjects([item]); let owned = pb.changeCount
            func restore() {
                guard pb.changeCount == owned else { return }
                pb.clearContents()
                let items = saved.map { entries -> NSPasteboardItem in let item = NSPasteboardItem(); for (type, data) in entries { item.setData(data, forType: type) }; return item }
                if !items.isEmpty { pb.writeObjects(items) }
            }
            let result: ActionResult
            do {
                result = try self.performKey(windowID, chord: Keyboard.parse("cmd+v"))
            } catch {
                restore()
                throw error
            }
            var note = "format=\(format); Markdown is pasted as source. " + (result.note ?? "")
            // An unconfirmed press or queued keystrokes may read the clipboard later; restoring now could paste
            // the user's own clipboard into the app instead.
            if result.verified == true {
                self.settleNow(windowID)
                restore()
            } else {
                note += (note.hasSuffix(" ") ? "" : "; ")
                    + "the app did not confirm the paste, so the pasted content was left on the clipboard (the previous clipboard was not restored)"
            }
            return ActionResult(route: "clipboard_paste", verified: result.verified, note: note)
        }
    }

    // MARK: Pointer

    private func performClick(_ wid: CGWindowID, target: ClickTarget, button: MouseButton, count: Int, flags: CGEventFlags) throws -> ActionResult {
        switch target {
        case .point(let x, let y):
            let record = try record(wid)
            return try pixelClick(record, local: CGPoint(x: x, y: y), button: button, count: count, flags: flags)
        case .element(let index):
            let (record, element) = try element(wid, index)
            let action = button == .left ? kAXPressAction : button == .right ? kAXShowMenuAction : nil
            // Modifier clicks (shift-extend, cmd-toggle) have no accessibility equivalent, so they go through the pointer.
            if flags.isEmpty, let action, element.element.actions.contains(action) {
                if let frame = element.element.frame {
                    showCursor(record, .click(at: Point(x: frame.midX, y: frame.midY), count: button == .left ? count : 1))
                }
                for _ in 0..<(button == .left ? count : 1) {
                    let error = element.element.perform(action)
                    guard error == .success || error == .cannotComplete else {
                        throw SCError.actionFailed("\(action) failed on element \(index) (AXError \(error.rawValue))")
                    }
                }
                return ActionResult(route: button == .left ? "ax_press" : "ax_show_menu")
            }
            guard let frame = element.element.frame else {
                throw SCError.actionFailed("Element \(index) has no \(action ?? "AX") action and no frame to click")
            }
            let windowOrigin = try resolver.resolve(pid: record.pid, wid: wid).frame?.origin ?? record.bounds.origin
            let local = CGPoint(x: frame.midX - windowOrigin.x, y: frame.midY - windowOrigin.y)
            var result = try pixelClick(record, local: local, button: button, count: count, flags: flags)
            if !flags.isEmpty { result.note = "clicked element \(index)'s center with modifiers" }
            else { result.note = "element \(index) has no \(action ?? "matching") action; clicked its center" }
            return result
        }
    }

    private func pixelClick(_ record: CGWindowRecord, local: CGPoint, button: MouseButton, count: Int, flags: CGEventFlags) throws -> ActionResult {
        try requirePointerTarget(record)
        guard local.x >= 0, local.y >= 0, local.x < record.bounds.width, local.y < record.bounds.height else {
            throw SCError.invalidArgument("Point (\(local.x), \(local.y)) is outside window \(record.id) (\(Int(record.bounds.width))x\(Int(record.bounds.height)))")
        }
        showCursor(record, .click(at: Point(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y), count: count))
        Pointer.click(pid: record.pid, wid: record.id, at: Self.location(record, local: local), button: button, count: count, flags: flags)
        return ActionResult(route: "skylight_pointer")
    }

    private static func location(_ record: CGWindowRecord, local: CGPoint) -> Pointer.Location {
        Pointer.Location(screen: CGPoint(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y), local: local)
    }

    /// Pointer events are addressed to the window, so they land in place on any Space. A minimized
    /// window has no on-screen frame to hit-test against.
    private func requirePointerTarget(_ record: CGWindowRecord) throws {
        if Execution.context.observationID != nil, let before=observedSize(record.id),
           abs(before.width-record.bounds.width)>1 || abs(before.height-record.bounds.height)>1 {
            throw SCError.actionFailed("Window resized since observation; observe again before using coordinates")
        }
        guard !record.isOnScreen else { return }
        let window = try resolver.resolve(pid: record.pid, wid: record.id)
        if window.bool(kAXMinimizedAttribute) == true {
            throw SCError.actionFailed("window \(record.id) is minimized; pixel input needs it restored")
        }
    }

    private func performScroll(_ wid: CGWindowID, target: ClickTarget?, direction: ScrollDirection, amount: Int) throws -> ActionResult {
        if case .element(let index) = target {
            let (_, element) = try element(wid, index)
            let action = [ScrollDirection.up: "AXScrollUpByPage", .down: "AXScrollDownByPage",
                          .left: "AXScrollLeftByPage", .right: "AXScrollRightByPage"][direction]!
            var candidate: AXUIElement? = element.element
            while let current = candidate {
                if current.actions.contains(action) {
                    for _ in 0..<amount { _ = current.perform(action) }
                    return ActionResult(route: "ax_scroll_page")
                }
                if current.role == kAXScrollAreaRole, let bar = current.element(
                    direction == .up || direction == .down ? kAXVerticalScrollBarAttribute : kAXHorizontalScrollBarAttribute
                ), let position = (bar.attribute(kAXValueAttribute) as? NSNumber)?.doubleValue {
                    let step = (direction == .up || direction == .left ? -0.1 : 0.1) * Double(amount)
                    let error = bar.set(kAXValueAttribute, NSNumber(value: min(1, max(0, position + step))))
                    if error == .success { return ActionResult(route: "ax_scrollbar") }
                }
                if current.role == kAXWindowRole { break }
                candidate = current.element(kAXParentAttribute)
            }
            guard let frame = element.element.frame else { throw SCError.actionFailed("Element \(index) is not scrollable") }
            let record = try record(wid)
            let local = CGPoint(x: frame.midX - record.bounds.minX, y: frame.midY - record.bounds.minY)
            return try wheel(record, local: local, direction: direction, amount: amount)
        }
        let record = try record(wid)
        var local = CGPoint(x: record.bounds.width / 2, y: record.bounds.height / 2)
        if case .point(let x, let y) = target { local = CGPoint(x: x, y: y) }
        return try wheel(record, local: local, direction: direction, amount: amount)
    }

    private func wheel(_ record: CGWindowRecord, local: CGPoint, direction: ScrollDirection, amount: Int) throws -> ActionResult {
        try requirePointerTarget(record)
        let (dx, dy): (Int32, Int32) = switch direction {
        case .up: (0, 3)
        case .down: (0, -3)
        case .left: (3, 0)
        case .right: (-3, 0)
        }
        showCursor(record, .scroll(at: Point(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y)))
        Pointer.scroll(pid: record.pid, wid: record.id, at: Self.location(record, local: local), dx: dx, dy: dy, ticks: amount)
        return ActionResult(route: "skylight_wheel")
    }

    // MARK: Text

    private func insertText(_ wid: CGWindowID, text: String, elementIndex: Int?, keystrokesOnly: Bool) throws -> ActionResult {
        let record = try record(wid)
        let target: AXUIElement
        var inWebArea: Bool
        if let elementIndex {
            let (_, element) = try element(wid, elementIndex)
            target = element.element
            inWebArea = element.inWebArea
        } else {
            _ = try resolver.resolve(pid: record.pid, wid: wid)
            let app = AXUIElementCreateApplication(record.pid)
            guard let focused = app.element(kAXFocusedUIElementAttribute),
                  focused.element(kAXWindowAttribute)?.windowID == wid || focused.windowID == wid
            else {
                withKeyWindow(record) { Keyboard.type(text, pid: record.pid) }
                return ActionResult(route: "pid_keyboard", verified: nil, note: "no focused element found in this window; typed into it as the app's key window")
            }
            target = focused
            inWebArea = Self.hasWebAreaAncestor(focused)
        }
        if elementIndex == nil { inWebArea = inWebArea || Self.hasWebAreaAncestor(target) }

        if !keystrokesOnly, !inWebArea, target.isSettable(kAXSelectedTextAttribute) {
            let before = AX.text(target.attribute(kAXValueAttribute))
            if target.set(kAXSelectedTextAttribute, text as CFString) == .success {
                let after = AX.text(target.attribute(kAXValueAttribute))
                if after != before || text.isEmpty { return ActionResult(route: "ax_selected_text", verified: after?.contains(text) ?? false) }
            }
        }

        if elementIndex != nil, target.bool(kAXFocusedAttribute) != true {
            target.set(kAXFocusedAttribute, kCFBooleanTrue)
            usleep(50_000)
        }
        let before = AX.text(target.attribute(kAXValueAttribute))
        withKeyWindow(record) { Keyboard.type(text, pid: record.pid) }
        usleep(50_000)
        let after = AX.text(target.attribute(kAXValueAttribute))
        return ActionResult(route: "pid_keyboard", verified: before == nil && after == nil ? nil : after != before)
    }

    private static func hasWebAreaAncestor(_ element: AXUIElement) -> Bool {
        var current: AXUIElement? = element
        for _ in 0..<60 {
            guard let node = current else { return false }
            let role = node.role
            if role == "AXWebArea" { return true }
            if role == kAXWindowRole || role == kAXApplicationRole { return false }
            current = node.element(kAXParentAttribute)
        }
        return false
    }
}
