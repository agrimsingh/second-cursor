import ApplicationServices
import Foundation

/// Records when each app last posted an accessibility notification, so settling and condition waits can wake on a
/// change instead of polling the tree. One AXObserver per process, all on one run loop thread. Registering talks to
/// the app, so `watch` is called from that app's queue; the lock covers bookkeeping only.
final class ChangeObserver: @unchecked Sendable {
    private static let notifications = [
        kAXValueChangedNotification, kAXTitleChangedNotification, kAXLayoutChangedNotification, kAXCreatedNotification,
        kAXUIElementDestroyedNotification, kAXFocusedUIElementChangedNotification, kAXSelectedTextChangedNotification,
        kAXSelectedChildrenChangedNotification, kAXWindowCreatedNotification, kAXMenuOpenedNotification,
        kAXMenuClosedNotification, kAXSheetCreatedNotification,
    ]
    private static let capacity = 16

    private final class Entry {
        let observer: AXObserver?
        var registered = 0
        var windows: [CGWindowID] = []
        var count: UInt64 = 0
        var last: Date?
        var used = Date()

        init(observer: AXObserver?) { self.observer = observer }
    }

    struct State {
        /// Notifications seen so far; waits compare against it.
        let count: UInt64
        let last: Date?
    }

    /// Guards the entries and is broadcast on every notification.
    private let condition = NSCondition()
    private var entries: [pid_t: Entry] = [:]
    private var runLoop: CFRunLoop!

    init() {
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { [self] in
            runLoop = CFRunLoopGetCurrent()
            // A run loop without sources returns at once; this timer never fires and keeps it running.
            let keepAlive = CFRunLoopTimerCreateWithHandler(nil, CFAbsoluteTimeGetCurrent() + 1e10, 0, 0, 0) { _ in }
            CFRunLoopAddTimer(runLoop, keepAlive, .defaultMode)
            ready.signal()
            CFRunLoopRun()
        }
        thread.name = "dev.secondcursor.ax-notifications"
        thread.qualityOfService = .userInitiated
        thread.start()
        ready.wait()
    }

    func isWatching(pid: pid_t, windowID: CGWindowID) -> Bool {
        condition.lock()
        defer { condition.unlock() }
        return entries[pid]?.windows.contains(windowID) ?? false
    }

    /// Observes `pid` and its window `windowID`. False when nothing could be registered, so the caller polls instead.
    @discardableResult
    func watch(pid: pid_t, windowID: CGWindowID, window: AXUIElement) -> Bool {
        condition.lock()
        sweep()
        let existing = entries[pid]
        existing?.used = Date()
        condition.unlock()
        if let existing, existing.windows.contains(windowID) { return existing.registered > 0 }

        let entry = existing ?? makeEntry(pid)
        if let observer = entry.observer {
            let registered = register(observer, window)
            condition.lock()
            entry.registered += registered
            entry.windows = Array((entry.windows + [windowID]).suffix(32))
            condition.unlock()
        }
        return entry.registered > 0
    }

    /// Processes with an entry, exited ones dropped first.
    var count: Int {
        condition.lock()
        defer { condition.unlock() }
        sweep()
        return entries.count
    }

    func state(_ pid: pid_t) -> State? {
        condition.lock()
        defer { condition.unlock() }
        guard let entry = entries[pid], entry.registered > 0 else { return nil }
        return State(count: entry.count, last: entry.last)
    }

    /// Blocks until `pid` posts a notification beyond `count`, or until `deadline`.
    func wait(_ pid: pid_t, after count: UInt64, until deadline: Date) {
        condition.lock()
        defer { condition.unlock() }
        while (entries[pid]?.count ?? count) == count, condition.wait(until: deadline) {}
    }

    /// An app that cannot be observed still gets an entry, so it is not asked again on every call.
    private func makeEntry(_ pid: pid_t) -> Entry {
        var created: AXObserver?
        let entry: Entry
        if AXObserverCreate(pid, Self.callback, &created) == .success, let observer = created {
            entry = Entry(observer: observer)
            let app = AXUIElementCreateApplication(pid)
            AXUIElementSetMessagingTimeout(app, 1)
            entry.registered = register(observer, app)
            CFRunLoopAddSource(runLoop, AXObserverGetRunLoopSource(observer), .defaultMode)
            CFRunLoopWakeUp(runLoop)
        } else {
            entry = Entry(observer: nil)
        }
        condition.lock()
        defer { condition.unlock() }
        if entries.count >= Self.capacity, let oldest = entries.min(by: { $0.value.used < $1.value.used })?.key {
            drop(oldest)
        }
        entries[pid] = entry
        return entry
    }

    /// Stops at the first call the app does not answer: each one would wait out the messaging timeout.
    private func register(_ observer: AXObserver, _ element: AXUIElement) -> Int {
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        var registered = 0
        for name in Self.notifications {
            let error = AXObserverAddNotification(observer, element, name as CFString, refcon)
            if error == .cannotComplete { break }
            if error == .success { registered += 1 }
        }
        return registered
    }

    /// Called with the lock held.
    private func sweep() {
        for pid in entries.keys where kill(pid, 0) != 0 && errno == ESRCH { drop(pid) }
    }

    /// Called with the lock held.
    private func drop(_ pid: pid_t) {
        if let observer = entries.removeValue(forKey: pid)?.observer {
            CFRunLoopRemoveSource(runLoop, AXObserverGetRunLoopSource(observer), .defaultMode)
        }
    }

    private func noted(_ element: AXUIElement) {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success else { return }
        condition.lock()
        if let entry = entries[pid] {
            entry.count &+= 1
            entry.last = Date()
        }
        condition.broadcast()
        condition.unlock()
    }

    private static let callback: AXObserverCallback = { _, element, _, refcon in
        guard let refcon else { return }
        Unmanaged<ChangeObserver>.fromOpaque(refcon).takeUnretainedValue().noted(element)
    }
}
