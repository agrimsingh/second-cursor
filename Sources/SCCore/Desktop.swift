import AppKit
import ApplicationServices
import Foundation
import SCShared

public final class Desktop: DesktopControlling, @unchecked Sendable {
    private struct Snapshot {
        let id: String
        let pid: pid_t
        let elements: [SnapshotElement]
    }

    /// AX and input calls block on the target app, so each app gets its own serial queue: actions on
    /// one app stay ordered, while agents working in different apps run in parallel and a hung app
    /// only stalls its own queue.
    private let lock = NSLock()
    private var queues: [pid_t: DispatchQueue] = [:]
    private let listQueue = DispatchQueue(label: "dev.secondcursor.desktop.list")
    private let resolver = WindowResolver()
    private var snapshots: [CGWindowID: Snapshot] = [:]
    private var lastAction: [CGWindowID: Date] = [:]

    public init() {}

    private func queue(for pid: pid_t) -> DispatchQueue {
        lock.lock()
        defer { lock.unlock() }
        if let queue = queues[pid] { return queue }
        let queue = DispatchQueue(label: "dev.secondcursor.desktop.\(pid)")
        queues[pid] = queue
        return queue
    }

    private func run<T>(on queue: DispatchQueue, _ work: @escaping () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async { continuation.resume(with: Result { try work() }) }
        }
    }

    private func run<T>(windowID: CGWindowID, _ work: @escaping () throws -> T) async throws -> T {
        guard let record = WindowList.record(windowID) else { throw SCError.windowNotFound(windowID) }
        return try await run(on: queue(for: record.pid), work)
    }

    private func storeSnapshot(_ snapshot: Snapshot?, for wid: CGWindowID) {
        lock.lock()
        snapshots[wid] = snapshot
        lock.unlock()
    }

    private func storedSnapshot(for wid: CGWindowID) -> Snapshot? {
        lock.lock()
        defer { lock.unlock() }
        return snapshots[wid]
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

    public func launchApp(_ query: String, urls: [URL], newInstance: Bool) async throws -> AppInfo {
        guard let app = Apps.resolve(query) else { throw SCError.appNotFound(query) }
        return try await Apps.launch(app, urls: urls, newInstance: newInstance)
    }

    public func windowState(windowID: UInt32, options: SnapshotOptions) async throws -> WindowSnapshot {
        var snapshot = try await run(windowID: windowID) {
            if self.actedRecently(on: windowID) { self.settleNow(windowID) }
            return try self.snapshot(windowID, options: options)
        }
        if options.includeScreenshot { snapshot.screenshot = try await Capture.window(windowID) }
        return snapshot
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

    public func runBatch(windowID: UInt32, steps: [BatchStep], stopOnError: Bool,
                         admit: @escaping @Sendable (BatchStep) throws -> Void) async throws -> [BatchStepResult] {
        try await run(windowID: windowID) {
            var results: [BatchStepResult] = []
            var halted = false
            for step in steps {
                if halted {
                    results.append(BatchStepResult(tool: step.tool, ok: false, skipped: true, error: "skipped: an earlier step failed"))
                    continue
                }
                do {
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
        // Menu key equivalents only fire in an AppKit-active app with a key window, so hand the target
        // AppKit focus (no raise, no Space change) for the duration of the chord, then give it back.
        let handoff = SkyLight.focusWithoutRaise(pid: record.pid, wid: wid)
        defer { if let handoff { usleep(50_000); SkyLight.restore(handoff) } }
        var route = "pid_keyboard_focus_handoff"
        var note: String?
        withKeyWindow(record) {
            if let item = MenuShortcut.find(chord, pid: record.pid),
               [.success, .cannotComplete].contains(item.perform(kAXPressAction)) {
                route = "ax_menu_shortcut"
                note = item.string(kAXTitleAttribute).map { "pressed menu item \"\($0)\"" }
            } else {
                Keyboard.press(chord, pid: record.pid)
            }
        }
        return ActionResult(route: route, note: note)
    }

    private func performNamedAction(_ wid: CGWindowID, elementIndex: Int, action: String) throws -> ActionResult {
        let (_, element) = try element(wid, elementIndex)
        let available = element.element.actions
        guard available.contains(action) else {
            throw SCError.invalidArgument("Element \(elementIndex) has no action \(action); available: \(available.isEmpty ? "none" : available.joined(separator: ", "))")
        }
        let error = element.element.perform(action)
        guard error == .success || error == .cannotComplete else {
            throw SCError.actionFailed("\(action) failed (AXError \(error.rawValue))")
        }
        return ActionResult(route: "ax_action", note: error == .cannotComplete ? "app did not reply in time; the action may still have run" : nil)
    }

    // MARK: Settling

    private func markAction(on wid: CGWindowID) {
        lock.lock()
        lastAction[wid] = Date()
        lock.unlock()
    }

    private func actedRecently(on wid: CGWindowID) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return lastAction[wid].map { Date().timeIntervalSince($0) < 2 } ?? false
    }

    /// Polls a fingerprint of the accessibility tree until it holds still for `quiet`, so an
    /// observation reflects the UI after the app has finished reacting, not mid-update.
    @discardableResult
    private func settleNow(_ wid: CGWindowID, quiet: TimeInterval = 0.3, cap: TimeInterval = 3) -> Bool {
        guard let record = WindowList.record(wid), let window = try? resolver.resolve(pid: record.pid, wid: wid) else { return false }
        func fingerprint() -> Int {
            let frame = window.frame ?? record.bounds
            var builder = TreeBuilder(origin: frame.origin, maxElements: 400, maxDepth: 40)
            builder.build(window: window, title: "", size: frame.size, menuBar: nil)
            return builder.text.hashValue
        }
        let deadline = Date().addingTimeInterval(cap)
        var last = fingerprint()
        var lastChange = Date()
        while Date() < deadline {
            usleep(80_000)
            let current = fingerprint()
            if current != last {
                last = current
                lastChange = Date()
            } else if Date().timeIntervalSince(lastChange) >= quiet {
                lock.lock()
                lastAction[wid] = nil
                lock.unlock()
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

    public func screenshot(windowID: UInt32) async throws -> Screenshot {
        guard WindowList.record(windowID) != nil else { throw SCError.windowNotFound(windowID) }
        return try await Capture.window(windowID)
    }

    // MARK: Windows

    private func record(_ wid: CGWindowID) throws -> CGWindowRecord {
        guard let record = WindowList.record(wid) else {
            storeSnapshot(nil, for: wid)
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
        let title = window.string(kAXTitleAttribute) ?? record.title
        var builder = TreeBuilder(origin: frame.origin, maxElements: options.maxElements, maxDepth: options.maxDepth)
        let menuBar = options.includeMenuBar ? AXUIElementCreateApplication(record.pid).element(kAXMenuBarAttribute) : nil
        builder.build(window: window, title: title, size: frame.size, menuBar: menuBar)
        let id = String(UUID().uuidString.prefix(8)).lowercased()
        storeSnapshot(Snapshot(id: id, pid: record.pid, elements: builder.elements), for: wid)
        return WindowSnapshot(
            window: windowInfo(record, window: window), snapshotID: id, tree: builder.text,
            elementCount: builder.elements.count, truncated: builder.truncated, screenshot: nil
        )
    }

    private func element(_ wid: CGWindowID, _ index: Int) throws -> (CGWindowRecord, SnapshotElement) {
        let record = try record(wid)
        guard let snapshot = storedSnapshot(for: wid), snapshot.pid == record.pid else { throw SCError.staleSnapshot(wid) }
        guard snapshot.elements.indices.contains(index) else { throw SCError.elementNotFound(index) }
        let element = snapshot.elements[index]
        guard element.element.isAlive else {
            throw SCError.actionFailed("Element \(index) no longer exists; call get_window_state again")
        }
        return (record, element)
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
        Pointer.click(pid: record.pid, wid: record.id, at: Self.location(record, local: local), button: button, count: count, flags: flags)
        return ActionResult(route: "skylight_pointer")
    }

    private static func location(_ record: CGWindowRecord, local: CGPoint) -> Pointer.Location {
        Pointer.Location(screen: CGPoint(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y), local: local)
    }

    /// Pointer events are addressed to the window, so they land in place on any Space. A minimized
    /// window has no on-screen frame to hit-test against.
    private func requirePointerTarget(_ record: CGWindowRecord) throws {
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
