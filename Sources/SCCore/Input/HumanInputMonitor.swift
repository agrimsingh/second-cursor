import AppKit
import CoreGraphics
import Foundation

/// Observes human interaction without consuming or changing it. The event callback only reads event fields and
/// updates a small snapshot; it never calls accessibility APIs or waits for the application's main thread.
final class HumanInputMonitor: @unchecked Sendable {
    static let shared = HumanInputMonitor()

    /// Identifies our own events even if a delivery route strips or changes their source process.
    static let eventTag: Int64 = 0x5343_4855_4D41_4E

    struct State: Sendable {
        let lastInputAt: Date?
        let lastInputPID: pid_t?
        let sequence: UInt64
        /// False when permission is missing, startup has not completed, or the event tap is unavailable.
        let monitoring: Bool
        /// Retaining each app's last sequence prevents later interaction elsewhere from hiding a conflict.
        let recentByPID: [pid_t: UInt64]

        func inputSequence(for pid: pid_t) -> UInt64 { recentByPID[pid] ?? 0 }
    }

    private let lock = NSLock()
    private var lastInputAt: Date?
    private var lastInputPID: pid_t?
    private var sequence: UInt64 = 0
    private var monitoring = false
    private var recentByPID: [pid_t: UInt64] = [:]
    private var frontmostPID: pid_t?

    // These are used only by the event-tap thread, apart from the notification token's retained lifetime.
    private var tap: CFMachPort?
    private var activationObserver: NSObjectProtocol?

    var state: State {
        lock.lock()
        defer { lock.unlock() }
        return State(lastInputAt: lastInputAt, lastInputPID: lastInputPID, sequence: sequence,
                     monitoring: monitoring, recentByPID: recentByPID)
    }

    static func stamp(_ event: CGEvent) {
        event.setIntegerValueField(.eventSourceUserData, value: eventTag)
    }

    private init() {
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { [self] in run(ready: ready) }
        thread.name = "dev.secondcursor.human-input"
        thread.qualityOfService = .userInitiated
        thread.start()
        // NSWorkspace and event-tap setup must not indefinitely delay an agent request or app launch. The snapshot
        // remains explicitly unavailable if setup is slow, and changes when the worker actually becomes ready.
        _ = ready.wait(timeout: .now() + .milliseconds(250))
    }

    private func run(ready: DispatchSemaphore) {
        // Cache activation information outside the tap callback. A session event's target PID is preferred below
        // because a click can be delivered before the resulting application-activation notification arrives.
        let workspace = NSWorkspace.shared
        activationObserver = workspace.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
        ) { [weak self] notification in
            guard let application = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
            self?.setFrontmostPID(application.processIdentifier)
        }
        setFrontmostPID(workspace.frontmostApplication?.processIdentifier)

        let events: [CGEventType] = [
            .leftMouseDown, .rightMouseDown, .otherMouseDown,
            .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
            .scrollWheel, .keyDown, .keyUp, .flagsChanged,
        ]
        let mask = events.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        tap = CGEvent.tapCreate(
            tap: .cgAnnotatedSessionEventTap, place: .tailAppendEventTap, options: .listenOnly,
            eventsOfInterest: mask, callback: Self.callback, userInfo: Unmanaged.passUnretained(self).toOpaque()
        )
        guard let tap, let source = CFMachPortCreateRunLoopSource(nil, tap, 0) else {
            if let tap { CFMachPortInvalidate(tap) }
            self.tap = nil
            stopObservingActivation()
            ready.signal()
            return
        }
        let runLoop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(runLoop, source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        setMonitoring(CGEvent.tapIsEnabled(tap: tap))
        ready.signal()
        CFRunLoopRun()
        setMonitoring(false)
        CGEvent.tapEnable(tap: tap, enable: false)
        CFRunLoopRemoveSource(runLoop, source, .commonModes)
        CFMachPortInvalidate(tap)
        self.tap = nil
        stopObservingActivation()
    }

    private func setFrontmostPID(_ pid: pid_t?) {
        lock.lock()
        frontmostPID = pid.flatMap { $0 > 0 ? $0 : nil }
        lock.unlock()
    }

    private func setMonitoring(_ value: Bool) {
        lock.lock()
        monitoring = value
        lock.unlock()
    }

    private func stopObservingActivation() {
        if let activationObserver { NSWorkspace.shared.notificationCenter.removeObserver(activationObserver) }
        activationObserver = nil
    }

    private func handle(_ type: CGEventType, _ event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            setMonitoring(false)
            if let tap {
                CGEvent.tapEnable(tap: tap, enable: true)
                setMonitoring(CGEvent.tapIsEnabled(tap: tap))
            }
            return
        }
        // System/HID events have no source process. Events from other automation clients must not look like a
        // human taking over, and our explicit marker also covers paths whose source process is rewritten.
        guard event.getIntegerValueField(.eventSourceUserData) != Self.eventTag,
              event.getIntegerValueField(.eventSourceUnixProcessID) == 0 else { return }
        let target = event.getIntegerValueField(.eventTargetUnixProcessID)
        let eventPID: pid_t? = target > 0 && target <= Int64(Int32.max) ? pid_t(target) : nil
        let now = Date()
        lock.lock()
        sequence &+= 1
        lastInputAt = now
        lastInputPID = eventPID ?? frontmostPID
        if let pid = lastInputPID {
            recentByPID[pid] = sequence
            if recentByPID.count > 64, let oldest = recentByPID.min(by: { $0.value < $1.value })?.key {
                recentByPID.removeValue(forKey: oldest)
            }
        }
        lock.unlock()
    }

    private static let callback: CGEventTapCallBack = { _, type, event, info in
        if let info { Unmanaged<HumanInputMonitor>.fromOpaque(info).takeUnretainedValue().handle(type, event) }
        return Unmanaged.passUnretained(event)
    }
}
