import AppKit
import ApplicationServices
import Foundation
import SCShared

private enum NativePlan {
    /// The existing explicit batch escape hatch; ordinary coordinate actions always validate their layout.
    @TaskLocal static var acceptsLayoutChange = false
}

public final class Desktop: DesktopControlling, @unchecked Sendable {
    /// What the agent last saw of one window. Only touched on that window's app queue.
    private final class WindowTree {
        let pid: pid_t
        let table = IndexTable()
        var snapshotID: String?
        var observationID: String?
        var inputSequence: UInt64 = 0
        var layoutFingerprint: Int?
        var sheets: [CFHashCode] = []
        var document: String?
        /// The last full rendering and its frame-free keys; the next diff is computed against them.
        var lines: [String]?
        var keys: [String]?
        /// The element limit of the last read, kept so later reads and diffs cut the outline at the same place.
        var maxElements: Int?
        var elements: [Int: SnapshotElement] = [:]
        /// Every element the last full read walked, including those its outline cut off, and the window origin
        /// their frames are relative to. `findElements` searches them.
        var nodes: [TreeNode]?
        var origin: CGPoint = .zero
        /// `lines` never include recognized text, so plain reads diff as before. The last OCR read's text, and the
        /// layout of each full read since, let the next OCR read diff against the previous outline with it merged in.
        var recognized: [RecognizedText]?
        var layout: OutlineLayout?

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
        /// Notifications after this are the app's answer. An accessibility call returns once the app has handled it,
        /// so it is when the call started. Pointer input is handled as it arrives, while the call still runs (a click
        /// holds its focus handoff 100 ms past the last event), so it is when the first press or wheel tick went out.
        /// Other events are handled after the call returns.
        let answers: Date
        var settled = false
        var painted = false
    }
    private var activity: [CGWindowID: Activity] = [:]
    /// When each window's pointer input went out, kept until its action is marked.
    private var delivered: [CGWindowID: Date] = [:]
    private static let synchronousRoutes: Set<String> = [
        "ax_press", "ax_value", "ax_selected_text", "ax_selection", "ax_action", "ax_scroll_page", "ax_scrollbar",
    ]
    private let changes = ChangeObserver()
    /// Condition waits block here between checks, never on an app's queue.
    private let waits = DispatchQueue(label: "dev.secondcursor.desktop.waits", attributes: .concurrent)

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
        let point: Point
        switch event {
        case let .click(at, _), let .scroll(at): point = at
        case let .drag(from, _): point = from
        default: return cursor.present(event)
        }
        Timing.measure("cursor") {
            cursor.present(Timing.measure("cursor_anchor") { Self.target(for: point, in: record) })
            cursor.present(event)
        }
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
        let enqueued = DispatchTime.now().uptimeNanoseconds
        return try await withCheckedThrowingContinuation { continuation in
            queue.async { continuation.resume(with: Result {
                context.timings?.add("queue", ms: Timing.since(enqueued))
                return try Execution.$context.withValue(context) { try context.beforeDispatch(); return try work() }
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

    public func observedApps() -> Int { changes.count }

    public func permissionStatus() -> PermissionStatus {
        PermissionStatus(accessibility: AXIsProcessTrusted(), screenRecording: CGPreflightScreenCaptureAccess())
    }

    public func nativeCapabilities(windowID: UInt32?) async -> NativeCapabilityProfile {
        if let windowID, let record = WindowList.record(windowID) {
            return (try? await run(on: queue(for: record.pid)) { self.capabilities(record) }) ?? capabilities(nil)
        }
        return capabilities(nil)
    }

    private func capabilities(_ record: CGWindowRecord?) -> NativeCapabilityProfile {
        let semanticConflict = record.flatMap { NativeSafety.conflict(pid: $0.pid, wid: $0.id, input: false) }
        let inputConflict = record.flatMap { NativeSafety.conflict(pid: $0.pid, wid: $0.id, input: true) }
        let pointerReason = SkyLight.pointerUnavailableReason ?? inputConflict
        let keyboardReason = SkyLight.keyboardUnavailableReason ?? inputConflict
        let browser = "Use a semantic AX action when exposed, or an agent-owned browser tab for web content. Isolated execution requires a separately configured environment."
        return NativeCapabilityProfile(osVersion: ProcessInfo.processInfo.operatingSystemVersionString,
            inputMonitoringAvailable: HumanInputMonitor.shared.state.monitoring,
            capabilities: [
                "semantic_actions": NativeGestureCapability(supported: semanticConflict == nil, reason: semanticConflict,
                    alternative: semanticConflict == nil ? nil : browser),
                "pointer": NativeGestureCapability(supported: pointerReason == nil, reason: pointerReason, alternative: pointerReason == nil ? nil : browser),
                "drag": NativeGestureCapability(supported: pointerReason == nil, reason: pointerReason, alternative: pointerReason == nil ? nil : browser),
                "keyboard": NativeGestureCapability(supported: keyboardReason == nil, reason: keyboardReason, alternative: keyboardReason == nil ? nil : browser),
                "modifier_click": NativeGestureCapability(supported: false, reason: "Background native modifier clicks are not reliably delivered.", alternative: browser),
                "modifier_drag": NativeGestureCapability(supported: false, reason: "Background native modifier drags are not reliably delivered.", alternative: browser),
                "hover": NativeGestureCapability(supported: false, reason: "Native background hover is not implemented.", alternative: browser),
                "frame_freshness": NativeGestureCapability(supported: false,
                    reason: "ScreenCaptureKit returns pixels without the application's last repaint time. Off-Space custom surfaces may stop repainting.")
            ])
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
        var visualSettle: VisualSettleResult?
        if options.visualSettle {
            if needsSettle(windowID) { await settle(windowID: windowID) }
            visualSettle = try await waitForVisualQuiet(windowID)
        }
        var recognized: [RecognizedText]?
        var ocrError: String?
        var ocrMS: Int?
        if options.ocr {
            if needsSettle(windowID) { await settle(windowID: windowID) }
            await paintWait(windowID)
            let started = Date()
            // Leaves time within the call's deadline for the tree walk.
            let budget = max(0, min(15, (Execution.context.deadline?.timeIntervalSinceNow ?? 60) - 3))
            do {
                recognized = try await TextRecognition.recognize(windowID, budget: budget)
            } catch {
                ocrError = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            }
            ocrMS = Int(Date().timeIntervalSince(started) * 1000)
            Execution.context.timings?.add("ocr", ms: Double(ocrMS ?? 0))
        }
        var snapshot = try await run(windowID: windowID) {
            if self.needsSettle(windowID) { self.settleNow(windowID) }
            return try self.snapshot(windowID, options: options, recognized: recognized)
        }
        snapshot.ocrMS = ocrMS
        snapshot.ocrError = ocrError
        snapshot.visualSettle = visualSettle
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
    /// after the tree settles can show the previous frame. Once per action, a capture waits until the window has had
    /// `repaintTime` since its last change: the action, or the app's latest notification after it.
    private func capture(_ wid: CGWindowID) async throws -> Screenshot {
        await paintWait(wid)
        return try await Capture.window(wid)
    }

    private static let repaintTime: TimeInterval = 0.15

    /// Opt-in because an unchanging first frame does not rule out a delayed reaction. Ordinary AX reads
    /// keep their existing timing; custom surfaces get a declared reaction allowance and a bounded probe.
    /// Capture failures preserve the AX read. Cancellation and dispatch revocation must still propagate.
    private func waitForVisualQuiet(_ wid: CGWindowID) async throws -> VisualSettleResult {
        let started = ProcessInfo.processInfo.systemUptime
        guard let owner = WindowList.record(wid)?.pid else { throw SCError.windowNotFound(wid) }
        func requireTarget() throws -> CGWindowRecord {
            guard let record = WindowList.record(wid), record.pid == owner else {
                // Do not fall through to the AX/OCR read for a replacement process reusing this id.
                throw SCError.actionFailed("The target window disappeared or changed owner during visual settling; observe the app again.")
            }
            return record
        }
        let action = lock.withLock { activity[wid] }
        var policy = VisualSettlingPolicy(
            minimumUntil: started + max(0, 1.6 - (action.map { Date().timeIntervalSince($0.at) } ?? 0)),
            deadline: started + max(0, min(2.2, (Execution.context.deadline?.timeIntervalSinceNow ?? 60) - 0.3)))
        func result(_ status: String, _ reason: String) -> VisualSettleResult {
            VisualSettleResult(status: status, elapsedMS: Int((ProcessInfo.processInfo.systemUptime - started) * 1000),
                samples: policy.samples, changedDuringProbe: policy.changed, reason: reason)
        }
        guard let action, Date().timeIntervalSince(action.at) < 2 else {
            return result("no_recent_action", "No action on this window was recorded in the last two seconds; the visual probe was skipped.")
        }
        return try await Timing.measureAsync("visual_settle") {
            while true {
                try Task.checkCancellation()
                try Execution.context.beforeDispatch()
                guard lock.withLock({ activity[wid]?.at == action.at }) else {
                    return result("superseded", "A newer action arrived during the visual wait; observe that action's result.")
                }
                if policy.decision(at: ProcessInfo.processInfo.systemUptime) == .timedOut {
                    return result("timed_out", "Visual quiet was not established within the probe budget. Inspect before acting; do not repeat an uncertain write.")
                }
                _ = try requireTarget()
                let pixels: [UInt8]
                do { pixels = try await Capture.probe(wid) }
                catch {
                    try Task.checkCancellation()
                    try Execution.context.beforeDispatch()
                    _ = try requireTarget()
                    return result("unavailable", "Visual probe failed: \(error.localizedDescription)")
                }
                try Task.checkCancellation()
                try Execution.context.beforeDispatch()
                guard lock.withLock({ activity[wid]?.at == action.at }) else {
                    return result("superseded", "A newer action arrived during the visual wait; observe that action's result.")
                }
                let record = try requireTarget()
                let identity = "\(record.pid):\(record.bounds):\(changes.state(record.pid)?.count ?? 0)"
                let decision = policy.sample(pixels: pixels, identity: identity, at: ProcessInfo.processInfo.systemUptime)
                if decision == .quiet {
                    return result("quiet", "Sampled target pixels and available AX notifications were quiet for 180 ms after a 1.6 s reaction allowance. This does not verify task success or frame freshness.")
                }
                if decision == .timedOut {
                    return result("timed_out", "Visual quiet was not established within the probe budget. Inspect before acting; do not repeat an uncertain write.")
                }
                let remaining = policy.deadline - ProcessInfo.processInfo.systemUptime
                if remaining > 0 { try await Task.sleep(nanoseconds: UInt64(min(0.08, remaining) * 1_000_000_000)) }
            }
        }
    }

    private func paintWait(_ wid: CGWindowID) async {
        guard var changed = claimPaintWait(wid) else { return }
        if let pid = WindowList.record(wid)?.pid, let last = changes.state(pid)?.last, last > changed { changed = last }
        let remaining = Self.repaintTime - Date().timeIntervalSince(changed)
        guard remaining > 0 else { return }
        await Timing.measureAsync("paint") { try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000)) }
    }

    public func click(windowID: UInt32, target: ClickTarget, button: MouseButton, count: Int, modifiers: [String]) async throws -> ActionResult {
        let flags = try Keyboard.flags(modifiers)
        guard flags.isEmpty else { throw SCError.notAllowed("Native modifier clicks are unsupported because macOS may ignore their modifiers. Use a semantic AX action or the browser backend.") }
        return try await act(windowID) { try self.performClick(windowID, target: target, button: button, count: max(count, 1), flags: flags) }
    }

    public func drag(windowID: UInt32, from: Point, to: Point, modifiers: [String]) async throws -> ActionResult {
        let flags = try Keyboard.flags(modifiers)
        guard flags.isEmpty else { throw SCError.notAllowed("Native modifier drags are unsupported because macOS may ignore their modifiers. Use a semantic AX action or the browser backend.") }
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
                    try self.validateObservation(step.windowID, matchID: step.windowID == windowID)
                    if let record = WindowList.record(step.windowID) {
                        try NativeSafety.check(pid: record.pid, wid: record.id, input: false)
                    }
                    if let caption = step.caption {
                        self.cursor?.present(
                            .activity(session: Execution.context.sessionID, app: nil, near: nil, caption: caption))
                    }
                    self.watchChanges(step.windowID)
                    let started = Date()
                    let result = try NativePlan.$acceptsLayoutChange.withValue(!stopOnLayoutChange) {
                        try Timing.measure("perform") { try self.perform(step) }
                    }
                    self.markAction(on: step.windowID, started: started, route: result.route)
                    results.append(BatchStepResult(tool: step.tool, ok: true, result: result))
                    if stopOnError && result.uncertain == true { halted = true }
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

    public func checkCondition(windowID: UInt32, condition: WaitCondition) async throws -> ConditionResult {
        try await run(windowID: windowID) { try self.evaluate(windowID, condition) }
    }

    public func waitFor(windowID: UInt32, condition: WaitCondition, timeout: TimeInterval) async throws -> ConditionResult {
        guard let record = WindowList.record(windowID) else { throw SCError.windowNotFound(windowID) }
        let queue = queue(for: record.pid)
        let started = Date()
        var deadline = started.addingTimeInterval(max(timeout, 0))
        // Leave the call time to return what it saw rather than run into its own deadline.
        if let callDeadline = Execution.context.deadline { deadline = min(deadline, callDeadline.addingTimeInterval(-1)) }
        var first = true
        while true {
            let seen = changes.state(record.pid)?.count ?? 0
            let checked = Date()
            var result = try await run(on: queue) { [first] in
                if first { self.watchChanges(windowID) }
                return try self.evaluate(windowID, condition)
            }
            let now = Date()
            if result.met || now >= deadline {
                result.waited = result.met && first ? 0 : now.timeIntervalSince(started)
                return result
            }
            first = false
            // Apps that post nothing are re-checked on a timer; apps that never stop posting at most 25 times a second,
            // since each check walks the window on the app's queue.
            let wake = min(deadline, now.addingTimeInterval(0.25))
            await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
                waits.async { [changes] in
                    changes.wait(record.pid, after: seen, until: wake)
                    let pause = min(wake, checked.addingTimeInterval(0.04)).timeIntervalSinceNow
                    if pause > 0 { usleep(UInt32(pause * 1_000_000)) }
                    done.resume()
                }
            }
        }
    }

    /// Runs on the window's app queue. An element that has gone does not fail the check: it no longer has the value.
    private func evaluate(_ wid: CGWindowID, _ condition: WaitCondition) throws -> ConditionResult {
        var holds = true
        var seen: [String] = []
        if let text = condition.text {
            let record = try record(wid)
            let window = try resolver.resolve(pid: record.pid, wid: wid)
            let quoted = "\"\(TreeBuilder.escape(text, limit: 80))\""
            if let found = TreeBuilder.firstContaining(text, in: window, maxNodes: 5000) {
                seen.append("\(quoted) is in the \(found.attribute) of an \(found.role)")
            } else {
                holds = false
                seen.append("no title, value or description in the window contains \(quoted)")
            }
        }
        if let index = condition.elementIndex, let expected = condition.value {
            let record = try record(wid)
            guard let tree = storedTree(for: wid), tree.pid == record.pid, tree.snapshotID != nil else {
                throw SCError.staleSnapshot(wid)
            }
            guard let element = tree.elements[index] else { throw SCError.elementNotFound(index) }
            if element.element.isAlive {
                let value = AX.text(element.element.attribute(kAXValueAttribute))
                holds = holds && value == expected
                seen.append("[\(index)] value is " + (value.map { "\"\(TreeBuilder.escape($0, limit: 200))\"" } ?? "empty"))
            } else {
                holds = false
                seen.append("[\(index)] no longer exists")
            }
        }
        return ConditionResult(met: holds != condition.gone, detail: seen.joined(separator: "; "))
    }

    // MARK: Actions (run on the target app's queue)

    private func act(_ windowID: CGWindowID, _ work: @escaping () throws -> ActionResult) async throws -> ActionResult {
        try await run(windowID: windowID) {
            try self.validateObservation(windowID)
            let record = try self.record(windowID)
            try NativeSafety.check(pid: record.pid, wid: windowID, input: false)
            self.watchChanges(windowID)
            let started = Date()
            let result = try Timing.measure("perform", work)
            self.markAction(on: windowID, started: started, route: result.route)
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
        case let .selectText(elementIndex, text, prefix, suffix, mode):
            return try performSelectText(wid, index: elementIndex, text: text, prefix: prefix, suffix: suffix, mode: mode)
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
        try requirePointerTarget(record, validateLayout: false)
        for point in [from, to] {
            guard point.x >= 0, point.y >= 0, point.x < record.bounds.width, point.y < record.bounds.height else {
                throw SCError.invalidArgument("Point (\(point.x), \(point.y)) is outside window \(wid) (\(Int(record.bounds.width))x\(Int(record.bounds.height)))")
            }
        }
        showCursor(record, .drag(from: Point(x: record.bounds.minX + from.x, y: record.bounds.minY + from.y),
                                 to: Point(x: record.bounds.minX + to.x, y: record.bounds.minY + to.y)))
        let current = try self.record(wid)
        try requirePointerTarget(current)
        noteDelivered(current.id, at: try Pointer.drag(
            pid: current.pid, wid: current.id, window: current.isOnScreen ? try? resolver.resolve(pid: current.pid, wid: current.id) : nil,
            from: Self.location(current, local: CGPoint(x: from.x, y: from.y)),
            to: Self.location(current, local: CGPoint(x: to.x, y: to.y)), flags: flags))
        return ActionResult(route: "skylight_drag")
    }

    private func applyValue(_ wid: CGWindowID, elementIndex: Int, value: String) throws -> ActionResult {
        let (_, element) = try mutationElement(wid, elementIndex)
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
            // A background app also needs its target-only AppKit focus before it can confirm a
            // different document as key. Keep the same guarded handoff used by command shortcuts.
            try SkyLight.requireKeyboardSupport()
            try NativeSafety.check(pid: record.pid, wid: wid, input: true)
            let handoff = try SkyLight.focusWithoutRaise(pid: record.pid, wid: wid)
            guard handoff != nil || SkyLight.isFront(pid: record.pid, wid: wid) else {
                throw SCError.notAllowed("macOS refused a background focus handoff; no key was dispatched. Use a semantic AX action or the browser backend.")
            }
            defer { if let handoff { usleep(50_000); SkyLight.restore(handoff) } }
            try withKeyWindow(record) { try Keyboard.press(chord, pid: record.pid, wid: wid) }
            return ActionResult(route: handoff == nil ? "pid_keyboard" : "pid_keyboard_focus_handoff")
        }
        // The handoff serializes input for every app, so an app that does not answer is found out before it.
        let bar = MenuShortcut.menuBar(pid: record.pid)
        // Menu key equivalents only fire in an AppKit-active app with a key window, so hand the target
        // AppKit focus (no raise, no Space change) for the duration of the chord, then give it back.
        try SkyLight.requireKeyboardSupport()
        try NativeSafety.check(pid: record.pid, wid: wid, input: true)
        let handoff = try SkyLight.focusWithoutRaise(pid: record.pid, wid: wid)
        guard handoff != nil || SkyLight.isFront(pid: record.pid, wid: wid) else {
            throw SCError.notAllowed("macOS refused a background focus handoff; no shortcut was dispatched. Use a semantic AX action or the browser backend.")
        }
        defer { if let handoff { usleep(50_000); SkyLight.restore(handoff) } }
        var route = "pid_keyboard_focus_handoff"
        var uncertain: Bool?
        var note: String?
        try withKeyWindow(record) {
            // The walk has to happen inside the handoff (see MenuShortcut.find), so it gets a hard budget.
            let found = bar.flatMap { MenuShortcut.find(chord, in: $0, deadline: Date().addingTimeInterval(1.5)) }
            try NativeSafety.check(pid: record.pid, wid: wid, input: true)
            let pressed = found?.item.perform(kAXPressAction)
            if pressed == .success || pressed == .cannotComplete {
                route = "ax_menu_shortcut"
                // .cannotComplete only means the app did not answer in time; the item may still run later.
                uncertain = pressed == .cannotComplete ? true : nil
                note = found?.title.map { "pressed menu item \"\($0)\"" }
                if pressed == .cannotComplete {
                    note = (note.map { $0 + "; " } ?? "") + "app did not reply in time; the action may still run"
                }
            } else {
                try Keyboard.press(chord, pid: record.pid, wid: wid)
            }
        }
        return ActionResult(route: route, note: note, uncertain: uncertain)
    }

    private func performNamedAction(_ wid: CGWindowID, elementIndex: Int, action: String) throws -> ActionResult {
        let (record, element) = try mutationElement(wid, elementIndex)
        let available = element.element.actions
        guard let match = available.first(where: { $0 == action || TreeBuilder.actionName($0) == action }) else {
            let names = available.map(TreeBuilder.actionName)
            throw SCError.invalidArgument("Element \(elementIndex) has no action \(action); available: \(names.isEmpty ? "none" : names.joined(separator: ", "))")
        }
        guard match != kAXRaiseAction else {
            throw SCError.notAllowed("AXRaise would change foreground window ordering. Use a background semantic action or hand the task to the user.")
        }
        try NativeSafety.check(pid: record.pid, wid: wid, input: false)
        let error = element.element.perform(match)
        guard error == .success || error == .cannotComplete else {
            throw SCError.actionFailed("\(action) failed (AXError \(error.rawValue))")
        }
        return ActionResult(route: "ax_action", note: error == .cannotComplete ? "app did not reply in time; the action may still have run" : nil,
                            uncertain: error == .cannotComplete ? true : nil)
    }

    // MARK: Settling

    private func markAction(on wid: CGWindowID, started: Date, route: String) {
        let at = Date()
        lock.lock()
        let input = delivered.removeValue(forKey: wid).flatMap { $0 >= started ? $0 : nil }
        activity[wid] = Activity(at: at, answers: input ?? (Self.synchronousRoutes.contains(route) ? started : at))
        lock.unlock()
    }

    private func noteDelivered(_ wid: CGWindowID, at time: Date) {
        lock.lock()
        delivered[wid] = time
        lock.unlock()
    }

    /// Registers for the window's notifications before acting, so the app's answer to the action is not missed.
    private func watchChanges(_ wid: CGWindowID) {
        guard let record = WindowList.record(wid), !changes.isWatching(pid: record.pid, windowID: wid),
              let window = try? resolver.resolve(pid: record.pid, wid: wid) else { return }
        changes.watch(pid: record.pid, windowID: wid, window: window)
    }

    /// Acted on in the last 2 s and not yet seen to settle since.
    private func needsSettle(_ wid: CGWindowID) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard let entry = activity[wid], Date().timeIntervalSince(entry.at) < 2 else { return false }
        return !entry.settled
    }

    /// When the window was acted on, at most once per action, for a window acted on in the last 2 s.
    private func claimPaintWait(_ wid: CGWindowID) -> Date? {
        lock.lock()
        defer { lock.unlock() }
        guard var entry = activity[wid], Date().timeIntervalSince(entry.at) < 2, !entry.painted else { return nil }
        entry.painted = true
        activity[wid] = entry
        return entry.at
    }

    /// Waits until the app has finished reacting to `action` (default: the window's latest action), so an observation
    /// reflects the UI after the change, not mid-update. A window that never holds still (meters, playback) counts as
    /// settled after `cap`, so the next read does not wait again.
    @discardableResult
    private func settleNow(_ wid: CGWindowID, after action: Activity? = nil, cap: TimeInterval = 3) -> Bool {
        guard let record = WindowList.record(wid), let window = try? resolver.resolve(pid: record.pid, wid: wid) else { return false }
        defer {
            lock.lock()
            activity[wid]?.settled = true
            lock.unlock()
        }
        lock.lock()
        let action = action ?? activity[wid]
        lock.unlock()
        // An app that has never posted a notification may not post them at all.
        if let action, changes.watch(pid: record.pid, windowID: wid, window: window),
           let state = changes.state(record.pid), state.count > 0 {
            return Timing.measure("settle") { settle(record.pid, after: action, cap: cap) }
        }
        return Timing.measure("settle_poll") { pollSettle(window, cap: cap) }
    }

    /// Waits for the app's first notification after the action (input events and menus are handled a little later,
    /// so up to 250 ms), then until it has posted nothing for 60 ms, and never less than 30 ms after the action.
    private func settle(_ pid: pid_t, after action: Activity, cap: TimeInterval) -> Bool {
        let answers = action.answers
        let deadline = action.at.addingTimeInterval(cap)
        let started = Date()
        var answered = false
        // The wait for an app that never answers, apart from settling: what a shorter first-reply wait would save.
        defer { if !answered { Execution.context.timings?.add("settle_unanswered", ms: Date().timeIntervalSince(started) * 1000) } }
        while let state = changes.state(pid) {
            let until = if let last = state.last, last > answers {
                max(action.at.addingTimeInterval(0.03), last.addingTimeInterval(0.06))
            } else {
                action.at.addingTimeInterval(0.25)
            }
            answered = state.last.map { $0 > answers } ?? false
            if Date() >= until { return true }
            if Date() >= deadline { return false }
            changes.wait(pid, after: state.count, until: min(until, deadline))
        }
        return false
    }

    /// Polls a fingerprint of the accessibility tree until it holds still for `quiet`.
    private func pollSettle(_ window: AXUIElement, quiet: TimeInterval = 0.2, cap: TimeInterval) -> Bool {
        func fingerprint() -> Int { TreeBuilder.fingerprint(window: window, maxNodes: 1000) }
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

    /// Keys reach whichever window the app treats as key. Only background processes may change their
    /// key window; an active process must already have this target key, so no handoff can steal typing.
    private func withKeyWindow(_ record: CGWindowRecord, _ body: () throws -> Void) throws {
        try SkyLight.requireKeyboardSupport()
        try NativeSafety.check(pid: record.pid, wid: record.id, input: true)
        // AXFocusedWindow remembers a background app's logical window even when AppKit has resigned key.
        // Background key equivalents still need the synthetic make-key record after a focus handoff.
        if NSWorkspace.shared.frontmostApplication?.processIdentifier != record.pid || SkyLight.keyWindow(pid: record.pid) != record.id {
            try NativeSafety.check(pid: record.pid, wid: record.id, input: true)
            guard SkyLight.makeKeyWindow(pid: record.pid, wid: record.id) else {
                throw SCError.notAllowed("macOS refused safe key-window targeting. Use set_value, a semantic AX action or the browser backend.")
            }
            usleep(50_000)
        }
        try NativeSafety.check(pid: record.pid, wid: record.id, input: true)
        guard SkyLight.keyWindow(pid: record.pid) == record.id else {
            throw SCError.notAllowed("The app did not confirm the target as its key window; no keyboard input was sent. Observe again or use a semantic AX action.")
        }
        try body()
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
        try await run(windowID: windowID) {
            let record = try self.record(windowID)
            self.noteObserved(windowID, size: record.bounds.size)
            let tree = self.tree(for: windowID, pid: record.pid)
            tree.inputSequence = HumanInputMonitor.shared.state.inputSequence(for: record.pid)
            tree.layoutFingerprint = (try? self.resolver.resolve(pid: record.pid, wid: windowID)).map { TreeBuilder.layoutFingerprint(window: $0) }
        }
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
        let (all, onScreen) = Timing.measure("window_list") { (WindowList.all(), WindowList.onScreenIDs()) }
        resolver.forget(windowsNotIn: Set(all.map(\.id)))
        var listed: [pid_t: [CGWindowID: AXUIElement]?] = [:]
        var result: [WindowInfo] = []
        for record in all where WindowList.isAppWindow(record) && record.pid != getpid() {
            let app = NSRunningApplication(processIdentifier: record.pid)
            if app?.activationPolicy == .prohibited { continue }
            let appName = app?.localizedName ?? record.ownerName
            if !needle.isEmpty, !appName.lowercased().contains(needle), !(app?.bundleIdentifier?.lowercased().contains(needle) ?? false) {
                continue
            }
            if listed.index(forKey: record.pid) == nil {
                let element = AXUIElementCreateApplication(record.pid)
                AXUIElementSetMessagingTimeout(element, 0.5)
                listed[record.pid] = Timing.measure("ax_windows") { resolver.listedWindows(element) }
            }
            let appWindows = listed[record.pid] ?? nil
            let window = appWindows?[record.id] ?? resolver.cached(pid: record.pid, wid: record.id)
            let onScreenNow = onScreen.contains(record.id)
            // A window its app answered without, shown nowhere and on no Space a display hides, is one the app keeps
            // but no longer shows (closed to the background, as Electron apps do): nothing can read it, and listing it
            // sends an agent back to list_windows for a window that stays unreadable.
            if !onScreenNow, window == nil, appWindows != nil, Timing.measure("spaces", { !Spaces.isHidden(record.id) }) {
                continue
            }
            var info = Timing.measure("window_info") { windowInfo(record, window: window) }
            info.isOnScreen = onScreenNow
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

    private func snapshot(_ wid: CGWindowID, options: SnapshotOptions, recognized: [RecognizedText]?) throws -> WindowSnapshot {
        let record = try record(wid)
        let window = try Timing.measure("ax_resolve") {
            let window = try resolver.resolve(pid: record.pid, wid: wid)
            changes.watch(pid: record.pid, windowID: wid, window: window)
            return window
        }
        let frame = window.frame ?? record.bounds
        let tree = tree(for: wid, pid: record.pid)
        tree.inputSequence = HumanInputMonitor.shared.state.inputSequence(for: record.pid)
        tree.layoutFingerprint = Timing.measure("layout_guard_baseline") { TreeBuilder.layoutFingerprint(window: window) }
        tree.sheets = window.elements("AXSheets").map { CFHash($0) }
        tree.document = window.string(kAXDocumentAttribute)
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
            Timing.measure("walk") { builder.build(subtreeOf: root.element, key: root.key, inWebArea: root.inWebArea) }
            var out = TreeRenderer.scoped(scope: handle, windowOrigin: frame.origin, nodes: builder.nodes,
                                          table: tree.table, maxElements: max(limit, 2000))
            let shown = out.body.count - 1
            if builder.truncated, out.footer == nil { out.footer = TreeRenderer.walkLimitFooter }
            tree.elements.merge(out.elements) { _, scoped in scoped }
            var text = out.text
            var ocrCount: Int?
            if let recognized {
                let layout = OutlineLayout(nodes: builder.nodes, shown: shown, origin: frame.origin, window: nil)
                let merged = TextRecognition.merge(recognized, lines: out.lines, keys: out.keys, layout: layout)
                text = merged.lines.joined(separator: "\n")
                ocrCount = merged.count
            }
            var result = WindowSnapshot(window: windowInfo(record, window: window), snapshotID: id, tree: text,
                                  elementCount: out.indexedCount, truncated: out.truncated || builder.truncated, scope: options.scope)
            result.scopeLabel = options.scope == nil ? options.scopeLabel : nil
            result.elements = out.structured
            result.ocrCount = ocrCount
            result.capabilities = capabilities(record)
            return result
        }

        let app = AXUIElementCreateApplication(record.pid)
        let menuBar = options.includeMenuBar ? app.element(kAXMenuBarAttribute) : nil
        func walk() -> TreeBuilder {
            var builder = TreeBuilder(maxNodes: maxNodes)
            Timing.measure("walk") { builder.build(window: window, menuBar: menuBar) }
            return builder
        }
        var builder = walk()
        let webEngine = resolver.engine(record.pid) != .native
        func hasPage() -> Bool { builder.nodes.contains { $0.role == "AXWebArea" } }
        // Chromium builds a window's page tree about 2 s after the first accessibility client asks for it.
        if webEngine, tree.snapshotID == nil, !hasPage() {
            let deadline = Date().addingTimeInterval(3)
            while !hasPage(), Date() < deadline {
                usleep(250_000)
                builder = walk()
            }
            tree.layoutFingerprint = TreeBuilder.layoutFingerprint(window: window)
        }
        let title = window.string(kAXTitleAttribute) ?? record.title
        var out = Timing.measure("render") {
            TreeRenderer.full(title: title, size: frame.size, windowOrigin: frame.origin, nodes: builder.nodes,
                              table: tree.table, maxElements: limit)
        }
        let layout = OutlineLayout(nodes: builder.nodes, shown: out.body.count - 1, origin: frame.origin,
                                   window: (title, frame.size))
        Timing.measure("orientation") { out.appendOrientation(focused: app.element(kAXFocusedUIElementAttribute)) }
        if builder.truncated, out.footer == nil { out.footer = TreeRenderer.walkLimitFooter }
        tree.table.retain(keys: Set(builder.nodes.map(\.key)))
        let base = tree.snapshotID
        var lines = out.lines
        var keys = out.keys
        var ocrCount: Int?
        if let recognized {
            let merged = TextRecognition.merge(recognized, lines: out.lines, keys: out.keys, layout: layout)
            lines = merged.lines
            keys = merged.keys
            ocrCount = merged.count
        }
        var diff: String?
        if !options.full, var previous = tree.lines, var previousKeys = tree.keys {
            if recognized != nil, let text = tree.recognized, let layout = tree.layout {
                (previous, previousKeys, _) = TextRecognition.merge(text, lines: previous, keys: previousKeys, layout: layout)
            }
            diff = Timing.measure("diff") {
                TreeRenderer.diff(previous: previous, previousKeys: previousKeys, current: lines, currentKeys: keys)
            }
        }
        tree.snapshotID = id
        tree.lines = out.lines
        tree.keys = out.keys
        if let recognized { tree.recognized = recognized }
        if tree.recognized != nil { tree.layout = layout }
        tree.elements = out.elements
        tree.nodes = builder.nodes
        tree.origin = frame.origin
        // findElements hands out indexes for elements past the outline's cut; they keep working while the element exists.
        for node in builder.nodes where node.indexed {
            if let index = tree.table.existingIndex(forKey: node.key), tree.elements[index] == nil {
                tree.elements[index] = SnapshotElement(element: node.element, role: node.role, inWebArea: node.inWebArea, key: node.key, label: node.label)
            }
        }
        var result = WindowSnapshot(
            window: Timing.measure("window_info") { windowInfo(record, window: window) }, snapshotID: id,
            tree: diff ?? lines.joined(separator: "\n"),
            elementCount: out.indexedCount, truncated: out.truncated || builder.truncated,
            isDiff: diff != nil, diffBase: diff != nil ? base : nil
        )
        result.elements = out.structured
        result.ocrCount = ocrCount
        result.capabilities = capabilities(record)
        if webEngine, !hasPage(), Self.isHidden(record) {
            result.hint = "No page content: Chromium-based apps stop rendering a page while its window is hidden "
                + "(covered, minimized or on another Space), which removes the page from this tree. For a web page, use "
                + "the browser tools; for an app, move_to_agent_display renders it again (tell the user, then return_window)."
        }
        return result
    }

    /// The first element in walk order labelled exactly `label`, else case-insensitively; containers (elements with
    /// children in the outline) win over leaves, so a heading that repeats a panel's name does not shadow the panel.
    private func labelledContainer(_ label: String, in window: AXUIElement, windowID: CGWindowID,
                                   maxNodes: Int) throws -> (SnapshotElement, String) {
        var builder = TreeBuilder(maxNodes: maxNodes)
        Timing.measure("walk") { builder.build(window: window, menuBar: nil) }
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
        return (SnapshotElement(element: node.element, role: node.role, inWebArea: node.inWebArea, key: node.key, label: node.label),
                "\"\(TreeBuilder.escape(node.label, limit: 120))\"")
    }

    /// Runs on the app queue because it may index elements the outline left out, which makes them actionable.
    public func findElements(windowID: UInt32, text: String?, role: String?, limit: Int) async throws -> ElementSearch {
        try await run(windowID: windowID) {
            let record = try self.record(windowID)
            var readWindow = false
            if self.storedTree(for: windowID).map({ $0.pid != record.pid || $0.nodes == nil }) ?? true {
                if self.needsSettle(windowID) { self.settleNow(windowID) }
                _ = try self.snapshot(
                    windowID, options: SnapshotOptions(includeScreenshot: false, full: false), recognized: nil)
                readWindow = true
            }
            let tree = self.tree(for: windowID, pid: record.pid)
            guard let nodes = tree.nodes, let snapshotID = tree.snapshotID else { throw SCError.staleSnapshot(windowID) }
            let ranked = Self.rank(nodes, text: text, role: role)
            let matches = ranked.prefix(max(limit, 1)).map { nodes[$0] }
            // Rendering only the matches numbers them from the window's table, exactly as the outline would.
            let out = TreeRenderer.full(title: "", size: .zero, windowOrigin: tree.origin, nodes: matches,
                                        table: tree.table, maxElements: .max)
            tree.elements.merge(out.elements) { current, _ in current }
            let needle = text?.lowercased()
            let lines = zip(matches, out.lines.dropFirst()).map { node, line -> String in
                var line = String(line.drop { $0 == " " })
                if let needle, let description = node.description, description != node.label,
                   description.lowercased().contains(needle) {
                    line += " description=\"\(TreeBuilder.escape(description, limit: 120))\""
                }
                return line
            }
            return ElementSearch(lines: lines, total: ranked.count, snapshotID: snapshotID, readWindow: readWindow)
        }
    }

    /// Positions of the nodes that match, best first: `text` equal to a label, value or description (ignoring case
    /// and surrounding whitespace), then a prefix of one, then inside one; within a tier, indexed elements come
    /// first, then outline order.
    private static func rank(_ nodes: [TreeNode], text: String?, role: String?) -> [Int] {
        let needle = text?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let wanted = role.map { $0.lowercased().hasPrefix("ax") ? $0.lowercased() : "ax" + $0.lowercased() }
        var scored: [(tier: Int, unindexed: Int, position: Int)] = []
        for (position, node) in nodes.enumerated() {
            if let wanted, node.role.lowercased() != wanted { continue }
            var tier = 0
            if let needle, !needle.isEmpty {
                let tiers = [node.label, node.value, node.description].compactMap { field -> Int? in
                    guard let field = field?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() else { return nil }
                    if field == needle { return 0 }
                    if field.hasPrefix(needle) { return 1 }
                    return field.contains(needle) ? 2 : nil
                }
                guard let best = tiers.min() else { continue }
                tier = best
            }
            scored.append((tier, node.indexed ? 0 : 1, position))
        }
        return scored.sorted { ($0.tier, $0.unindexed, $0.position) < ($1.tier, $1.unindexed, $1.position) }.map(\.position)
    }

    private func element(_ wid: CGWindowID, _ index: Int) throws -> (CGWindowRecord, SnapshotElement) {
        let record = try record(wid)
        guard let tree = storedTree(for: wid), tree.pid == record.pid, tree.snapshotID != nil else { throw SCError.staleSnapshot(wid) }
        guard let element = tree.elements[index] else { throw SCError.elementNotFound(index) }
        guard element.element.isAlive else {
            throw SCError.actionFailed("Element \(index) no longer exists; call get_window_state again")
        }
        let identity = TreeBuilder.currentIdentity(element.element)
        guard identity.role == element.role, identity.label == element.label else {
            throw SCError.actionFailed("Element \(index) changed identity since observation; call get_window_state again")
        }
        return (record, element)
    }

    private func mutationElement(_ wid: CGWindowID, _ index: Int) throws -> (CGWindowRecord, SnapshotElement) {
        try validateObservation(wid, matchID: false)
        let (record, element) = try element(wid, index)
        guard element.element.bool(kAXEnabledAttribute) != false else {
            throw SCError.actionFailed("Element \(index) is disabled; read get_window_state before choosing another action")
        }
        try NativeSafety.check(pid: record.pid, wid: wid, input: false)
        return (record, element)
    }

    private func validateObservation(_ wid: CGWindowID, matchID: Bool = true) throws {
        if matchID, let expected = Execution.context.observationID,
           storedTree(for: wid)?.observationID != expected {
            throw SCError.actionFailed("Stale observation; read get_window_state again before acting")
        }
        if let tree = storedTree(for: wid), HumanInputMonitor.shared.state.inputSequence(for: tree.pid) > tree.inputSequence {
            throw SCError.notAllowed("The user interacted with this app since observation. Read get_window_state again after they yield before acting.")
        }
        if let tree = storedTree(for: wid), tree.snapshotID != nil,
           let window = try? resolver.resolve(pid: tree.pid, wid: wid) {
            if window.string(kAXDocumentAttribute) != tree.document {
                throw SCError.actionFailed("The window changed documents since observation; read get_window_state again before acting")
            }
            let sheets = window.elements("AXSheets").map { CFHash($0) }
            if sheets != tree.sheets {
                throw SCError.actionFailed("A sheet opened or closed since observation; read get_window_state again before acting")
            }
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
            try self.performSelectText(windowID, index: index, text: text, prefix: prefix, suffix: suffix, mode: mode)
        }
    }

    /// Shared by standalone selection and a batch while already on the target app's queue.
    private func performSelectText(_ windowID: CGWindowID, index: Int, text: String, prefix: String?, suffix: String?, mode: String) throws -> ActionResult {
        guard !text.isEmpty, ["text", "cursor_before", "cursor_after"].contains(mode) else { throw SCError.invalidArgument("Nonempty text and valid selection mode required") }
        let (record, item) = try mutationElement(windowID, index)
        let value = (AX.text(item.element.attribute(kAXValueAttribute)) ?? "") as NSString
        var matches: [NSRange] = [], start = 0
        while start < value.length {
            let range = value.range(of: text, range: NSRange(location: start, length: value.length-start))
            if range.location == NSNotFound { break }
            let before = value.substring(to: range.location), after = value.substring(from: NSMaxRange(range))
            if prefix.map({ before.hasSuffix($0) }) ?? true, suffix.map({ after.hasPrefix($0) }) ?? true { matches.append(range) }
            // Include overlapping matches ("aa" in "aaa") while keeping the next
            // search boundary outside a UTF-16 surrogate pair.
            let unit = value.character(at: range.location)
            let surrogatePair = (0xD800...0xDBFF).contains(unit) && range.location + 1 < value.length
                && (0xDC00...0xDFFF).contains(value.character(at: range.location + 1))
            start = range.location + (surrogatePair ? 2 : 1)
        }
        guard matches.count == 1 else { throw SCError.invalidArgument("Text matched \(matches.count) times; supply prefix/suffix to select exactly one occurrence") }
        let found = matches[0]
        let range = CFRange(location: mode == "cursor_after" ? NSMaxRange(found) : found.location, length: mode == "text" ? found.length : 0)
        try NativeSafety.check(pid: record.pid, wid: windowID, input: false)
        guard item.element.set(kAXSelectedTextRangeAttribute, AX.rangeValue(range)) == .success else { throw SCError.actionFailed("This element does not support text selection") }
        let actual = AX.range(item.element.attribute(kAXSelectedTextRangeAttribute))
        let verified = actual?.location == range.location && actual?.length == range.length
        return ActionResult(
            route: "ax_selection", verified: verified,
            note: verified ? nil : "Selection readback did not match the requested range; observe before typing or retrying.",
            uncertain: verified ? nil : true)
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
            let record = try self.record(windowID)
            try SkyLight.requireKeyboardSupport()
            try NativeSafety.check(pid: record.pid, wid: windowID, input: true)
            let focused = AXUIElementCreateApplication(record.pid).element(kAXFocusedUIElementAttribute)
            let target = focused.flatMap { element -> AXUIElement? in
                element.element(kAXWindowAttribute)?.windowID == windowID || element.windowID == windowID ? element : nil
            }
            let before = target.flatMap { AX.text($0.attribute(kAXValueAttribute)) }
            let expected = target.flatMap { Self.expectedInsertion(plain, into: before, target: $0) }
            let selectionBefore = target.flatMap { AX.range($0.attribute(kAXSelectedTextRangeAttribute)) }
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
                let reason = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                throw SCError.actionFailed(reason + " Paste was interrupted and may be pending. The paste content was left on the clipboard; the previous clipboard was not restored. Observe before retrying.")
            }
            var note = "format=\(format); Markdown is pasted as source. " + (result.note ?? "")
            // Acknowledging AXPress does not prove that Paste read the clipboard. Wait for the exact insertion
            // before restoring, so a delayed handler cannot insert the user's restored clipboard instead.
            let budget = max(0, min(1, (Execution.context.deadline?.timeIntervalSinceNow ?? 60) - 0.25))
            let deadline = Date().addingTimeInterval(budget)
            var confirmed = false
            if let target, let expected {
                repeat {
                    let actual = AX.text(target.attribute(kAXValueAttribute))
                    let selection = AX.range(target.attribute(kAXSelectedTextRangeAttribute))
                    let collapsed = selectionBefore.map { original in
                        original.length > 0 && selection?.location == original.location + plain.utf16.count && selection?.length == 0
                    } ?? false
                    confirmed = actual == expected && (before != expected || collapsed)
                    if confirmed || Date() >= deadline { break }
                    usleep(20_000)
                } while true
            }
            if confirmed { restore() }
            else {
                note += (note.hasSuffix(" ") ? "" : "; ")
                    + "the exact paste result was not confirmed and the action may still execute, so paste content was left on the clipboard (the previous clipboard was not restored)"
            }
            return ActionResult(route: "clipboard_paste", verified: confirmed ? true : nil, note: note,
                                uncertain: confirmed ? nil : true)
        }
    }

    // MARK: Pointer

    private func performClick(_ wid: CGWindowID, target: ClickTarget, button: MouseButton, count: Int, flags: CGEventFlags) throws -> ActionResult {
        switch target {
        case .point(let x, let y):
            let record = try record(wid)
            return try pixelClick(record, local: CGPoint(x: x, y: y), button: button, count: count, flags: flags)
        case .element(let index):
            let (record, element) = try mutationElement(wid, index)
            let action = button == .left ? kAXPressAction : button == .right ? kAXShowMenuAction : nil
            // Modifier clicks (shift-extend, cmd-toggle) have no accessibility equivalent, so they go through the pointer.
            if flags.isEmpty, let action, element.element.actions.contains(action) {
                if let frame = element.element.frame {
                    showCursor(record, .click(at: Point(x: frame.midX, y: frame.midY), count: button == .left ? count : 1))
                }
                for _ in 0..<(button == .left ? count : 1) {
                    try NativeSafety.check(pid: record.pid, wid: wid, input: false)
                    _ = try self.mutationElement(wid, index)
                    let error = element.element.perform(action)
                    guard error == .success || error == .cannotComplete else {
                        throw SCError.actionFailed("\(action) failed on element \(index) (AXError \(error.rawValue))")
                    }
                    if error == .cannotComplete {
                        return ActionResult(route: button == .left ? "ax_press" : "ax_show_menu",
                            note: "The app did not confirm this press; it may still execute. Remaining requested presses were not sent. Observe before retrying.", uncertain: true)
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
        try requirePointerTarget(record, validateLayout: false)
        guard local.x >= 0, local.y >= 0, local.x < record.bounds.width, local.y < record.bounds.height else {
            throw SCError.invalidArgument("Point (\(local.x), \(local.y)) is outside window \(record.id) (\(Int(record.bounds.width))x\(Int(record.bounds.height)))")
        }
        showCursor(record, .click(at: Point(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y), count: count))
        let current = try self.record(record.id)
        try requirePointerTarget(current)
        noteDelivered(current.id, at: try Pointer.click(
            pid: current.pid, wid: current.id, window: current.isOnScreen ? try? resolver.resolve(pid: current.pid, wid: current.id) : nil,
            at: Self.location(current, local: local), button: button, count: count, flags: flags))
        return ActionResult(route: "skylight_pointer")
    }

    private static func location(_ record: CGWindowRecord, local: CGPoint) -> Pointer.Location {
        Pointer.Location(screen: CGPoint(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y), local: local)
    }

    /// Pointer events are addressed to the window, so they land in place on any Space. A minimized
    /// window has no on-screen frame to hit-test against.
    private func requirePointerTarget(_ record: CGWindowRecord, validateLayout: Bool = true) throws {
        try SkyLight.requirePointerSupport()
        try NativeSafety.check(pid: record.pid, wid: record.id, input: true)
        try validateObservation(record.id, matchID: false)
        if !NativePlan.acceptsLayoutChange, let before=observedSize(record.id),
           abs(before.width-record.bounds.width)>1 || abs(before.height-record.bounds.height)>1 {
            throw SCError.actionFailed("Window resized since observation; observe again before using coordinates")
        }
        let window = try resolver.resolve(pid: record.pid, wid: record.id)
        if validateLayout, !NativePlan.acceptsLayoutChange, let before = storedTree(for: record.id)?.layoutFingerprint,
           Timing.measure("layout_guard", { TreeBuilder.layoutFingerprint(window: window) }) != before {
            throw SCError.actionFailed("Window layout or content changed since observation; get_window_state again before using coordinates. A stable semantic AX element may still be actionable.")
        }
        if window.bool(kAXMinimizedAttribute) == true {
            throw SCError.actionFailed("window \(record.id) is minimized; pixel input needs it restored")
        }
    }

    private func performScroll(_ wid: CGWindowID, target: ClickTarget?, direction: ScrollDirection, amount: Int) throws -> ActionResult {
        if case .element(let index) = target {
            let (record, element) = try mutationElement(wid, index)
            let action = [ScrollDirection.up: "AXScrollUpByPage", .down: "AXScrollDownByPage",
                          .left: "AXScrollLeftByPage", .right: "AXScrollRightByPage"][direction]!
            var candidate: AXUIElement? = element.element
            while let current = candidate {
                if current.actions.contains(action) {
                    for _ in 0..<amount {
                        try NativeSafety.check(pid: record.pid, wid: wid, input: false)
                        let error = current.perform(action)
                        guard error == .success else {
                            throw SCError.actionFailed("AX scrolling was not confirmed (AXError \(error.rawValue)); observe before retrying")
                        }
                    }
                    return ActionResult(route: "ax_scroll_page")
                }
                if current.role == kAXScrollAreaRole, let bar = current.element(
                    direction == .up || direction == .down ? kAXVerticalScrollBarAttribute : kAXHorizontalScrollBarAttribute
                ), let position = (bar.attribute(kAXValueAttribute) as? NSNumber)?.doubleValue {
                    let step = (direction == .up || direction == .left ? -0.1 : 0.1) * Double(amount)
                    try NativeSafety.check(pid: record.pid, wid: wid, input: false)
                    let error = bar.set(kAXValueAttribute, NSNumber(value: min(1, max(0, position + step))))
                    if error == .success { return ActionResult(route: "ax_scrollbar") }
                }
                if current.role == kAXWindowRole { break }
                candidate = current.element(kAXParentAttribute)
            }
            guard let frame = element.element.frame else { throw SCError.actionFailed("Element \(index) is not scrollable") }
            let pointerRecord = try self.record(wid)
            let local = CGPoint(x: frame.midX - pointerRecord.bounds.minX, y: frame.midY - pointerRecord.bounds.minY)
            return try wheel(pointerRecord, local: local, direction: direction, amount: amount)
        }
        let record = try record(wid)
        var local = CGPoint(x: record.bounds.width / 2, y: record.bounds.height / 2)
        if case .point(let x, let y) = target { local = CGPoint(x: x, y: y) }
        return try wheel(record, local: local, direction: direction, amount: amount)
    }

    private func wheel(_ record: CGWindowRecord, local: CGPoint, direction: ScrollDirection, amount: Int) throws -> ActionResult {
        try requirePointerTarget(record, validateLayout: false)
        let (dx, dy): (Int32, Int32) = switch direction {
        case .up: (0, 3)
        case .down: (0, -3)
        case .left: (3, 0)
        case .right: (-3, 0)
        }
        showCursor(record, .scroll(at: Point(x: record.bounds.minX + local.x, y: record.bounds.minY + local.y)))
        let current = try self.record(record.id)
        try requirePointerTarget(current)
        noteDelivered(current.id, at: try Pointer.scroll(pid: current.pid, wid: current.id, at: Self.location(current, local: local),
                                                    dx: dx, dy: dy, ticks: amount))
        return ActionResult(route: "skylight_wheel")
    }

    // MARK: Text

    private func insertText(_ wid: CGWindowID, text: String, elementIndex: Int?, keystrokesOnly: Bool) throws -> ActionResult {
        let record = try record(wid)
        let target: AXUIElement
        var inWebArea: Bool
        if let elementIndex {
            let (_, element) = try mutationElement(wid, elementIndex)
            target = element.element
            inWebArea = element.inWebArea
        } else {
            _ = try resolver.resolve(pid: record.pid, wid: wid)
            let app = AXUIElementCreateApplication(record.pid)
            guard let focused = app.element(kAXFocusedUIElementAttribute),
                  focused.element(kAXWindowAttribute)?.windowID == wid || focused.windowID == wid
            else {
                try withKeyWindow(record) { try Keyboard.type(text, pid: record.pid, wid: wid) }
                return ActionResult(route: "pid_keyboard", verified: nil, note: "no focused element found in this window; typed into it as the app's key window")
            }
            target = focused
            inWebArea = Self.hasWebAreaAncestor(focused)
        }
        if elementIndex == nil { inWebArea = inWebArea || Self.hasWebAreaAncestor(target) }

        if !keystrokesOnly, !inWebArea, target.isSettable(kAXSelectedTextAttribute) {
            let before = AX.text(target.attribute(kAXValueAttribute))
            let expected = Self.expectedInsertion(text, into: before, target: target)
            try NativeSafety.check(pid: record.pid, wid: wid, input: false)
            let error = target.set(kAXSelectedTextAttribute, text as CFString)
            if error == .success {
                let after = AX.text(target.attribute(kAXValueAttribute))
                return ActionResult(route: "ax_selected_text", verified: expected.map { after == $0 },
                    note: expected == nil ? "Text insertion was accepted, but the prior selection was unavailable for exact verification."
                        : after == expected ? nil : "The app accepted AX text insertion but the exact expected text was not observed. Inspect before retrying; use keystrokes_only for apps that do not implement AX text insertion faithfully.")
            }
            if error == .cannotComplete {
                return ActionResult(route: "ax_selected_text", note: "The app did not confirm text insertion; it may still execute. Observe before retrying.", uncertain: true)
            }
        }

        if elementIndex != nil, target.bool(kAXFocusedAttribute) != true {
            try NativeSafety.check(pid: record.pid, wid: wid, input: true)
            guard target.set(kAXFocusedAttribute, kCFBooleanTrue) == .success else {
                throw SCError.notAllowed("The requested element refused focus; no keyboard text was sent. Use set_value or another semantic AX action.")
            }
            usleep(50_000)
            let focused = AXUIElementCreateApplication(record.pid).element(kAXFocusedUIElementAttribute)
            guard focused.map({ CFEqual($0, target) }) ?? (target.bool(kAXFocusedAttribute) == true) else {
                throw SCError.notAllowed("The requested element did not become focused; no keyboard text was sent. Observe again or use set_value.")
            }
        }
        let before = AX.text(target.attribute(kAXValueAttribute))
        let expected = Self.expectedInsertion(text, into: before, target: target)
        try withKeyWindow(record) {
            if elementIndex != nil {
                let focused = AXUIElementCreateApplication(record.pid).element(kAXFocusedUIElementAttribute)
                guard focused.map({ CFEqual($0, target) }) ?? (target.bool(kAXFocusedAttribute) == true) else {
                    throw SCError.notAllowed("Focus changed before text dispatch; no keyboard text was sent. Observe again or use set_value.")
                }
            }
            try Keyboard.type(text, pid: record.pid, wid: wid)
        }
        usleep(50_000)
        let after = AX.text(target.attribute(kAXValueAttribute))
        return ActionResult(route: "pid_keyboard", verified: expected.map { after == $0 },
            note: expected == nil ? "Key events were dispatched, but the prior value and selection did not support exact text verification." : nil)
    }

    private static func expectedInsertion(_ text: String, into value: String?, target: AXUIElement) -> String? {
        guard let value, let range = AX.range(target.attribute(kAXSelectedTextRangeAttribute)) else { return nil }
        let original = value as NSString
        guard range.location >= 0, range.length >= 0, range.location <= original.length,
              range.length <= original.length - range.location else { return nil }
        return original.replacingCharacters(in: NSRange(location: range.location, length: range.length), with: text)
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
