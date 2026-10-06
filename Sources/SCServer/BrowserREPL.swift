import Darwin
import Foundation
import JavaScriptCore
import SCShared

/// browser_repl: one persistent JavaScriptCore context per session and browser, whose Playwright-shaped `page` and `context`
/// (BrowserREPL+Prelude.swift) drive the session's Chrome tabs through `BrowserControlling`. A session's scripts run
/// on its own serial queue, one call at a time, and only while a call is running. Failure modes and the decisions
/// covering them are listed in e2e/repl.py.
final class BrowserREPL: @unchecked Sendable {
    static let outputLimit = 40_000
    static let memoryBudget: UInt64 = 1 << 30

    static let actsAs: Set<String> = Set(Session.methods.compactMap { methodTools[$0] })
        .union(Session.operations.compactMap { BrowserPolicy.discreteTool[$0] })
    private static let methodTools = [
        "tabs.open": "browser_open", "tab.navigate": "browser_navigate", "tab.snapshot": "browser_snapshot",
        "tabs.close": "browser_close", "tabs.list": "browser_tabs", "tab.screenshot": "browser_screenshot",
        "extended": "browser_command",
    ]

    private let browser: BrowserControlling
    private let log: ActionLog
    private let lock = NSLock()
    private struct SessionKey: Hashable {
        let sessionID: String
        let browserID: String
    }
    private var sessions: [SessionKey: Session] = [:]

    init(browser: BrowserControlling, log: ActionLog) {
        self.browser = browser
        self.log = log
    }

    func run(code: String, session id: String, deadline: Date) async throws -> ToolOutput {
        guard JSC.setLimit != nil else {
            throw SCError.actionFailed(
                "browser_repl needs JavaScriptCore's execution time limit, which this version of macOS does not export")
        }
        // A Page retains a numeric Chrome tab id. Pin the selected browser before looking up its JS context so
        // switching profiles cannot silently redirect an old Page to a different tab with the same numeric id.
        var context = Execution.context
        if context.browserID == nil {
            let metadata = (try? JSONSerialization.jsonObject(with: await browser.instances())) as? [String: Any]
            let ids = (metadata?["browsers"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String }
            guard ids.count == 1 else {
                if ids.isEmpty { throw SCError.browserUnavailable("the Second Cursor Chrome extension is not connected") }
                throw SCError.invalidArgument("Multiple browsers connected; provide browser_id from browser_instances")
            }
            context.browserID = ids[0]
        }
        let key = SessionKey(sessionID: id, browserID: context.browserID!)
        let session = lock.withLock {
            if let session = sessions[key] { return session }
            let session = Session(id: id, browser: browser, log: log)
            sessions[key] = session
            return session
        }
        // Answer before the router's deadline, so what the code printed still reaches the client.
        let margin = min(1.5, max(0, deadline.timeIntervalSinceNow) * 0.2)
        context.observationID = nil
        let outcome = await session.submit(
            code: code, deadline: deadline.addingTimeInterval(-margin), context: context, client: ClientRequest.current)
        if outcome.failed { throw SCError.actionFailed(outcome.text) }
        return ToolOutput(json: ["output": outcome.text], images: outcome.images, text: outcome.text)
    }

    func close(session id: String) {
        let closed = lock.withLock {
            sessions.keys.filter { $0.sessionID == id }.compactMap { sessions.removeValue(forKey: $0) }
        }
        for session in closed { session.close() }
    }

    /// For the client's `notifications/cancelled`: ends the call that request started.
    func cancel(requestID: String) {
        for session in lock.withLock({ Array(sessions.values) }) { session.cancel(requestID: requestID) }
    }
}

private struct Outcome {
    var text: String
    var images: [Screenshot] = []
    var failed: Bool
}

private final class Call {
    let cancellationID = UUID()
    let code: String
    let deadline: Date
    let context: ExecutionContext
    let requestID: String?
    let continuation: CheckedContinuation<Outcome, Never>
    let started = Date()
    var output = Output()
    var images: [Int: Screenshot] = [:]
    var finished = false

    init(
        code: String, deadline: Date, context: ExecutionContext, requestID: String?,
        continuation: CheckedContinuation<Outcome, Never>
    ) {
        self.code = code
        self.requestID = requestID
        self.deadline = deadline
        self.context = context
        self.continuation = continuation
    }
}

private struct Output {
    private var lines: [String] = []
    private var size = 0
    private var dropped = 0

    /// `total` is the text's length before the script cut it to the cap.
    mutating func append(_ text: String, total: Int) {
        let room = BrowserREPL.outputLimit - size
        let count = text.count
        let kept = max(0, min(count, room))
        if kept > 0 { lines.append(kept < count ? String(text.prefix(kept)) : text) }
        dropped += max(count, total) - kept
        size += kept + 1
    }

    func text(adding last: String?) -> String {
        var parts = lines
        if dropped > 0 {
            parts.append("… \(dropped) more characters (output is capped at \(BrowserREPL.outputLimit) characters)")
        }
        if let last, !last.isEmpty { parts.append(String(last.prefix(2 * BrowserREPL.outputLimit))) }
        return parts.isEmpty ? "undefined" : parts.joined(separator: "\n")
    }
}

private struct Pending {
    let resolve: JSValue
    let reject: JSValue
    let call: Call
}

private final class Session: @unchecked Sendable {
    fileprivate static let methods: Set<String> = [
        "tabs.open", "tab.navigate", "tab.snapshot", "tabs.close", "tabs.list", "tab.screenshot", "extended",
    ]
    // No operation that needs local approval, reads beyond the page's controls, or runs page JavaScript.
    fileprivate static let operations: Set<String> = [
        "inspect", "query", "click", "hover", "fill", "type", "press_key", "check", "select_option", "select_text",
        "perform_action", "scroll", "wait", "back", "forward", "reload", "dialog", "screenshot",
    ]
    private static let ended = "The browser_repl call that started this has ended"

    let id: String
    private let browser: BrowserControlling
    private let log: ActionLog
    private let queue: DispatchQueue
    private let watchdog = Watchdog()

    // Confined to `queue`.
    private var vm: JSVirtualMachine?
    private var context: JSContext?
    private var runner: JSValue?
    private var current: Call?
    private var waiting: [Call] = []
    private var pending: [Int: Pending] = [:]
    private var nextID = 0
    private var entryDepth = 0
    private var resetNeeded = false
    private var closed = false
    private var notice: String?

    init(id: String, browser: BrowserControlling, log: ActionLog) {
        self.id = id
        self.browser = browser
        self.log = log
        queue = DispatchQueue(label: "dev.secondcursor.repl")
    }

    func submit(code: String, deadline: Date, context: ExecutionContext, client: ClientRequest?) async -> Outcome {
        await withCheckedContinuation { continuation in
            let call = Call(
                code: code, deadline: deadline, context: context, requestID: client?.id, continuation: continuation)
            queue.async { self.enqueue(call) }
            after(deadline.timeIntervalSinceNow) { self.expire(call) }
            let callID = call.cancellationID
            client?.onGone { [weak self] in self?.cancel(callID: callID) }
        }
    }

    /// Callable from any thread: a script running now stops within a watchdog tick.
    func cancel(requestID: String) {
        watchdog.cancel(requestID: requestID)
        queue.async {
            for call in [self.current].compactMap({ $0 }) + self.waiting where call.requestID == requestID {
                self.cancel(call)
            }
        }
    }

    private func cancel(_ call: Call) {
        watchdog.cancel(call: call.cancellationID)
        queue.async {
            self.waiting.removeAll { $0 === call }
            self.finish(call, Watchdog.Reason.cancelled.message, failed: true)
        }
    }

    private func cancel(callID: UUID) {
        watchdog.cancel(call: callID)
        queue.async {
            for call in [self.current].compactMap({ $0 }) + self.waiting where call.cancellationID == callID {
                self.cancel(call)
            }
        }
    }

    func close() {
        watchdog.close()
        queue.async {
            self.closed = true
            for call in [self.current].compactMap({ $0 }) + self.waiting {
                self.finish(call, "The session was closed.", failed: true)
            }
            self.waiting = []
            self.teardown()
        }
    }

    /// asyncAfter lets the system coalesce a background app's timers by seconds; deadlines must fire on time.
    private func after(_ seconds: TimeInterval, _ work: @escaping () -> Void) {
        let timer = DispatchSource.makeTimerSource(flags: .strict, queue: queue)
        timer.schedule(deadline: .now() + max(0, seconds), leeway: .milliseconds(10))
        timer.setEventHandler {
            timer.cancel()
            work()
        }
        timer.resume()
    }

    private func enqueue(_ call: Call) {
        if closed { return finish(call, "The session was closed.", failed: true) }
        if current == nil && waiting.isEmpty { start(call) } else { waiting.append(call) }
    }

    private func startNext() {
        if current == nil, !waiting.isEmpty { start(waiting.removeFirst()) }
    }

    private func start(_ call: Call) {
        current = call
        watchdog.begin(deadline: call.deadline, call: call.cancellationID, requestID: call.requestID)
        // Per call and process-wide: memory another session freed but the allocator has not returned yet counts
        // against no one.
        watchdog.memoryLimit = Memory.footprint() + BrowserREPL.memoryBudget
        do {
            try prepare()
        } catch {
            return finish(call, ToolRouter.message(for: error), failed: true)
        }
        if let notice {
            call.output.append("[system] \(notice)", total: 0)
            self.notice = nil
        }
        context?.exception = nil
        enter { _ = runner?.invokeMethod("run", withArguments: [call.code]) }
        if call === current, let exception = context?.exception {
            context?.exception = nil
            finish(call, "Internal error in browser_repl: \(exception)", failed: true)
        }
    }

    private func expire(_ call: Call) {
        guard !call.finished else { return }
        if call === current {
            let seconds = Int(call.deadline.timeIntervalSince(call.started).rounded())
            finish(
                call,
                "TimeoutError: the call's time limit (\(seconds) s) passed while the code was still waiting, on a page "
                    + "action, waitForTimeout, or a promise that never settles. Split longer work across calls; "
                    + "variables set before this point are kept.",
                failed: true)
        } else {
            waiting.removeAll { $0 === call }
            finish(call, "TimeoutError: the previous browser_repl call of this session was still running.", failed: true)
        }
    }

    private func finish(_ call: Call, _ text: String?, failed: Bool, image: Int? = nil) {
        guard !call.finished else { return }
        call.finished = true
        let images = image.flatMap { call.images[$0] }.map { [$0] } ?? []
        call.continuation.resume(returning: Outcome(text: call.output.text(adding: text), images: images, failed: failed))
        guard call === current else { return }
        current = nil
        watchdog.idle()
        // Unwind what the call left behind while no call is current: the handlers of these rejections run before
        // the next call starts, their browser calls fail at once, and nothing they print reaches another call.
        let orphans = pending.values
        pending.removeAll()
        for orphan in orphans { enter { _ = orphan.reject.call(withArguments: [Self.ended]) } }
        queue.async { self.startNext() }
    }

    /// Every entry into JavaScript goes through here, so the watchdog times each outermost entry and a terminated
    /// or over-budget script is handled once it has unwound.
    private func enter(_ body: () -> Void) {
        guard context != nil else { return }
        entryDepth += 1
        if entryDepth == 1 { watchdog.beginSlice() }
        body()
        entryDepth -= 1
        guard entryDepth == 0 else { return }
        if let reason = watchdog.takeFired() {
            // The first entry after a termination inside a microtask is swallowed by it.
            _ = context?.evaluateScript("void 0")
            context?.exception = nil
            if reason == .memory { resetNeeded = true }
            if let call = current { finish(call, reason.message, failed: true) }
        }
        if resetNeeded {
            teardown()
            notice = "The REPL was reset after memory grew by more than 1 GB in one call; variables and pages from "
                + "earlier calls are gone (their tabs stay open)."
        }
    }

    private func prepare() throws {
        if runner != nil { return }
        guard let setLimit = JSC.setLimit, let vm = JSVirtualMachine(), let context = JSContext(virtualMachine: vm)
        else { throw SCError.actionFailed("Could not create a JavaScript context") }
        context.name = "Second Cursor browser_repl"
        setLimit(
            JSContextGetGroup(context.jsGlobalContextRef), Watchdog.tick, Watchdog.callback,
            Unmanaged.passUnretained(watchdog).toOpaque())
        self.vm = vm
        self.context = context
        let host = makeHost(in: context)
        enter {
            runner = context.evaluateScript(BrowserREPL.prelude, withSourceURL: URL(string: "second-cursor-repl:prelude"))?
                .call(withArguments: [host])
        }
        guard let runner, context.exception == nil, runner.isObject else {
            let reason = context.exception?.toString() ?? "no runner"
            teardown()
            throw SCError.actionFailed("The REPL failed to start: \(reason)")
        }
        if let set = JSC.setUnhandledRejectionCallback, let callback = runner.forProperty("unhandledRejection"),
            let object = JSValueToObject(context.jsGlobalContextRef, callback.jsValueRef, nil)
        {
            set(context.jsGlobalContextRef, object, nil)
        }
    }

    private func teardown() {
        pending.removeAll()
        runner = nil
        context = nil
        vm = nil
        resetNeeded = false
    }

    private func makeHost(in context: JSContext) -> JSValue {
        let host = JSValue(newObjectIn: context)!
        let call: @convention(block) (String, String, JSValue, JSValue) -> Void = { [weak self] method, params, resolve, reject in
            self?.hostCall(method, params, resolve, reject)
        }
        let sleep: @convention(block) (Double, JSValue, JSValue) -> Void = { [weak self] ms, resolve, reject in
            self?.sleep(ms, resolve, reject)
        }
        let print: @convention(block) (String, Double) -> Void = { [weak self] text, total in
            self?.current?.output.append(text, total: total.isFinite ? Int(total) : 0)
        }
        let remaining: @convention(block) () -> Double = { [weak self] in
            max(0, (self?.current?.deadline.timeIntervalSinceNow ?? 0) * 1000)
        }
        let done: @convention(block) (Bool, String, Double) -> Void = { [weak self] ok, text, image in
            guard let self, let call = self.current else { return }
            self.finish(call, text, failed: !ok, image: image >= 0 ? Int(image) : nil)
        }
        // Returns the compiled value, or the SyntaxError instead of throwing it.
        let compile: @convention(block) (String) -> JSValue = { source in
            let context = JSContext.current()!
            let value = context.evaluateScript(source, withSourceURL: URL(string: "browser_repl"))
            guard let error = context.exception else { return value ?? JSValue(undefinedIn: context) }
            context.exception = nil
            return error
        }
        for (name, block) in [
            ("call", unsafeBitCast(call, to: AnyObject.self)), ("sleep", unsafeBitCast(sleep, to: AnyObject.self)),
            ("print", unsafeBitCast(print, to: AnyObject.self)), ("compile", unsafeBitCast(compile, to: AnyObject.self)),
            ("remaining", unsafeBitCast(remaining, to: AnyObject.self)), ("done", unsafeBitCast(done, to: AnyObject.self)),
        ] {
            host.setObject(block, forKeyedSubscript: name as NSString)
        }
        return host
    }

    /// Runs `method` against the browser for the running call, pinned to its session, deadline and (empty)
    /// approvals. Never throws into JavaScript: every failure rejects.
    private func hostCall(_ method: String, _ json: String, _ resolve: JSValue, _ reject: JSValue) {
        func refuse(_ message: String) { enter { _ = reject.call(withArguments: [message]) } }
        guard let call = current else { return refuse(Self.ended) }
        if Memory.footprint() > watchdog.memoryLimit {
            resetNeeded = true
            refuse(Watchdog.Reason.memory.message)
            return finish(call, Watchdog.Reason.memory.message, failed: true)
        }
        guard Self.methods.contains(method),
            var params = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any]
        else { return refuse("Unknown browser method \(method)") }
        let operation = params["operation"] as? String
        if method == "extended" {
            guard let operation, Self.operations.contains(operation) else {
                return refuse("browser_repl does not offer the \(operation ?? "unnamed") operation")
            }
        }
        for key in ["local_approvals", "session_id", "deadline", "observation_id", "files"] { params[key] = nil }
        if let wait = params["timeout_ms"] as? Double {
            params["timeout_ms"] = Int(max(0, min(wait, call.deadline.timeIntervalSinceNow * 1000)))
        }
        guard let body = try? JSONSerialization.data(withJSONObject: params) else {
            return refuse("Could not encode the browser call")
        }
        if method == "tabs.open" || method == "tab.navigate", let url = params["url"] as? String {
            log.record(ActionLogEntry(tool: "browser_repl", target: "url=\(url)", detail: method))
        }
        let id = nextID
        nextID += 1
        pending[id] = Pending(resolve: resolve, reject: reject, call: call)
        let browser = self.browser
        let context = call.context
        let image = method == "tab.screenshot" || operation == "screenshot"
        let tabID = params["tabId"] as? Int
        let targetKey = tabID.map { tab in
            context.browserID.map { SCTaskTarget.browserKey(browserID: $0, tabID: tab) } ?? "tab:\(tab)"
        }
        let mutates = ["tabs.open", "tabs.close", "tab.navigate"].contains(method)
            || (method == "extended" && !["inspect", "query", "wait", "screenshot"].contains(operation ?? "")
                && !(operation == "dialog" && params["accept"] == nil))
        Task {
            let result: Result<Data, Error>
            do {
                result = .success(try await Execution.$context.withValue(context) {
                    if let tabID, let targetKey {
                        TaskRegistry.shared.noteTarget(session: context.sessionID,
                            target: SCTaskTarget(key: targetKey, app: "Browser", tabID: tabID, browserID: context.browserID))
                    }
                    if mutates {
                        try TaskRegistry.shared.checkMutation(session: context.sessionID,
                            targetKey: targetKey)
                    }
                    let data = try await browser.command(method, params: body)
                    if let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                        let openedTab = (json["tab"] as? [String: Any])?["id"] as? Int
                        let observedTab = tabID ?? openedTab
                        let observation = method == "tab.snapshot" ? json : json["observation"] as? [String: Any]
                        if let observedTab, let observation,
                           (observation["freshness"] as? [String: Any])?["status"] as? String == "current" {
                            let key = context.browserID.map { SCTaskTarget.browserKey(browserID: $0, tabID: observedTab) }
                                ?? "tab:\(observedTab)"
                            TaskRegistry.shared.noteTarget(session: context.sessionID,
                                target: SCTaskTarget(key: key, app: "Browser", tabID: observedTab, browserID: context.browserID))
                            TaskRegistry.shared.noteObservation(session: context.sessionID, targetKey: key)
                        }
                    }
                    return data
                })
            } catch {
                result = .failure(error)
            }
            self.queue.async { self.complete(id, result, image: image) }
        }
    }

    private func complete(_ id: Int, _ result: Result<Data, Error>, image: Bool) {
        guard let entry = pending.removeValue(forKey: id), entry.call === current else { return }
        switch result {
        case .success(var data):
            // The PNG stays on this side; the script gets a handle it can return as the cell's value.
            if image, var json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                let encoded = json.removeValue(forKey: "data") as? String, let png = Data(base64Encoded: encoded)
            {
                let handle = entry.call.images.count
                entry.call.images[handle] = Screenshot(
                    pngData: png, width: json["width"] as? Int ?? 0, height: json["height"] as? Int ?? 0,
                    scale: json["scale"] as? Double ?? 1)
                json["image"] = handle
                data = (try? JSONSerialization.data(withJSONObject: json)) ?? Data("{}".utf8)
            }
            enter { _ = entry.resolve.call(withArguments: [String(decoding: data, as: UTF8.self)]) }
        case .failure(let error):
            enter { _ = entry.reject.call(withArguments: [ToolRouter.message(for: error)]) }
        }
    }

    private func sleep(_ ms: Double, _ resolve: JSValue, _ reject: JSValue) {
        guard let call = current else { return enter { _ = reject.call(withArguments: [Self.ended]) } }
        let id = nextID
        nextID += 1
        pending[id] = Pending(resolve: resolve, reject: reject, call: call)
        let delay = max(0, min(ms.isFinite ? ms / 1000 : 0, call.deadline.timeIntervalSinceNow))
        after(delay) {
            guard let entry = self.pending.removeValue(forKey: id), entry.call === self.current else { return }
            self.enter { _ = entry.resolve.call(withArguments: []) }
        }
    }
}

/// JavaScriptCore's execution time limit, re-armed from its own callback so it can check the wall clock, the call's
/// deadline, memory and session state every `tick` of script CPU time. A callback that declines without re-arming
/// is never called again during that entry.
private final class Watchdog: @unchecked Sendable {
    static let tick = 0.05
    static let sliceLimit: TimeInterval = 5

    enum Reason {
        case slice, deadline, memory, closed, cancelled

        var message: String {
            switch self {
            case .slice:
                return "Stopped: the code ran for \(Int(Watchdog.sliceLimit)) s without yielding (an infinite loop?). "
                    + "Variables set before that are kept."
            case .deadline:
                return "TimeoutError: stopped at the call's time limit while running synchronous code."
            case .memory:
                return "Stopped: memory grew by more than 1 GB during this call. The REPL was reset; variables and pages "
                    + "from earlier calls are gone (their tabs stay open)."
            case .closed:
                return "The session was closed."
            case .cancelled:
                return "Cancelled: the client stopped waiting for this call. Nothing more of it runs; browser input "
                    + "already sent is not undone."
            }
        }
    }

    static let callback: JSC.ShouldTerminate = { context, raw in
        guard let raw else { return true }
        return Unmanaged<Watchdog>.fromOpaque(raw).takeUnretainedValue().check(context, raw)
    }

    private let lock = NSLock()
    private var sliceStart = Date()
    private var deadline = Date.distantFuture
    private var closed = false
    private var running: (call: UUID, requestID: String?)?
    private var cancelled = false
    private var fired: Reason?
    var memoryLimit = UInt64.max

    private func check(_ context: JSContextRef?, _ raw: UnsafeMutableRawPointer) -> Bool {
        let reason: Reason? = lock.withLock {
            let now = Date()
            if closed {
                fired = .closed
            } else if cancelled {
                fired = .cancelled
            } else if Memory.footprint() > memoryLimit {
                fired = .memory
            } else if now >= deadline {
                fired = .deadline
            } else if now.timeIntervalSince(sliceStart) > Self.sliceLimit {
                fired = .slice
            } else {
                return nil
            }
            return fired
        }
        if reason == nil { JSC.setLimit?(JSContextGetGroup(context), Self.tick, Self.callback, raw) }
        return reason != nil
    }

    func begin(deadline: Date, call: UUID, requestID: String?) {
        lock.withLock {
            self.deadline = deadline
            running = (call, requestID)
            cancelled = false
        }
    }
    /// Between calls nothing should run; whatever the last call left runs one tick at most.
    func idle() {
        lock.withLock {
            deadline = Date()
            running = nil
        }
    }
    func cancel(call: UUID) { lock.withLock { if running?.call == call { cancelled = true } } }
    func cancel(requestID: String) { lock.withLock { if running?.requestID == requestID { cancelled = true } } }
    func beginSlice() { lock.withLock { sliceStart = Date() } }
    func close() { lock.withLock { closed = true } }
    func takeFired() -> Reason? {
        lock.withLock {
            defer { fired = nil }
            return fired
        }
    }
}

private enum Memory {
    static func footprint() -> UInt64 {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        return result == KERN_SUCCESS ? info.phys_footprint : 0
    }
}

/// Exported by JavaScriptCore but missing from the SDK headers (JSContextRefPrivate.h).
private enum JSC {
    typealias ShouldTerminate = @convention(c) (JSContextRef?, UnsafeMutableRawPointer?) -> Bool
    typealias SetLimit = @convention(c) (JSContextGroupRef?, Double, ShouldTerminate?, UnsafeMutableRawPointer?) -> Void
    typealias SetRejectionCallback = @convention(c) (JSGlobalContextRef?, JSObjectRef?, UnsafeMutablePointer<JSValueRef?>?)
        -> Void

    static let setLimit: SetLimit? = symbol("JSContextGroupSetExecutionTimeLimit")
    static let setUnhandledRejectionCallback: SetRejectionCallback? = symbol("JSGlobalContextSetUnhandledRejectionCallback")

    private static func symbol<T>(_ name: String) -> T? {
        dlsym(UnsafeMutableRawPointer(bitPattern: -2), name).map { unsafeBitCast($0, to: T.self) }
    }
}
