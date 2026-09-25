// Ported from cua-driver's SkyLight bridge (MIT, github.com/trycua/cua); see LICENSES/cua-driver-MIT.txt.
import ApplicationServices
import CoreGraphics
import Foundation
import ObjectiveC

/// Private SkyLight entry points for background input. Every call degrades to the public
/// API (or a no-op) when a symbol is missing, so an OS update weakens delivery instead of crashing.
enum SkyLight {
    private typealias PostToPid = @convention(c) (pid_t, CGEvent) -> Void
    private typealias SetAuthenticationMessage = @convention(c) (CGEvent, AnyObject) -> Void
    private typealias SetWindowLocation = @convention(c) (CGEvent, CGPoint) -> Void
    private typealias SetIntegerField = @convention(c) (CGEvent, UInt32, Int64) -> Void
    private typealias PostEventRecordTo = @convention(c) (UnsafeRawPointer, UnsafeRawPointer) -> Int32
    private typealias GetFrontProcess = @convention(c) (UnsafeMutableRawPointer) -> Int32
    private typealias GetWindowOwner = @convention(c) (Int32, UInt32, UnsafeMutablePointer<Int32>) -> Int32
    private typealias GetConnectionPSN = @convention(c) (Int32, UnsafeMutableRawPointer) -> Int32
    private typealias GetProcessForPID = @convention(c) (pid_t, UnsafeMutableRawPointer) -> Int32
    private typealias GetProcessPID = @convention(c) (UnsafeRawPointer, UnsafeMutablePointer<pid_t>) -> Int32
    private typealias AuthMessageFactory = @convention(c) (AnyClass, Selector, UnsafeMutableRawPointer, Int32, UInt32) -> Unmanaged<AnyObject>?

    private static let loaded = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY | RTLD_GLOBAL) != nil

    private static func symbol<T>(_ name: String, _ type: T.Type) -> T? {
        _ = loaded
        guard let pointer = dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) else { return nil }
        return unsafeBitCast(pointer, to: type)
    }

    private static let postToPid = symbol("SLEventPostToPid", PostToPid.self)
    private static let setAuthenticationMessage = symbol("SLEventSetAuthenticationMessage", SetAuthenticationMessage.self)
    private static let setWindowLocationFn = symbol("CGEventSetWindowLocation", SetWindowLocation.self)
    private static let setIntegerField = symbol("SLEventSetIntegerValueField", SetIntegerField.self)
    private static let postEventRecordTo = symbol("SLPSPostEventRecordTo", PostEventRecordTo.self)
    private static let getFrontProcess = symbol("_SLPSGetFrontProcess", GetFrontProcess.self)
    private static let getWindowOwner = symbol("SLSGetWindowOwner", GetWindowOwner.self)
    private static let getConnectionPSN = symbol("SLSGetConnectionPSN", GetConnectionPSN.self)
    private static let getProcessForPID = symbol("GetProcessForPID", GetProcessForPID.self)
    private static let getProcessPID = symbol("GetProcessPID", GetProcessPID.self)
    private static let objcMsgSend = symbol("objc_msgSend", AuthMessageFactory.self)

    /// Keyboard events need the authentication envelope for Chromium to accept them as live input.
    /// Mouse events must not carry it: it bypasses the session tap Chromium's window handler reads.
    static func post(_ event: CGEvent, to pid: pid_t, authenticated: Bool) {
        guard let postToPid else {
            event.postToPid(pid)
            return
        }
        if authenticated { attachAuthenticationMessage(event, pid: pid) }
        postToPid(pid, event)
    }

    static func setWindowLocation(_ event: CGEvent, _ point: CGPoint) {
        setWindowLocationFn?(event, point)
    }

    static func setField(_ event: CGEvent, _ field: UInt32, _ value: Int64) {
        if let setIntegerField {
            setIntegerField(event, field, value)
        } else if let known = CGEventField(rawValue: field) {
            event.setIntegerValueField(known, value: value)
        }
    }

    /// `messageWithEventRecord:pid:version:` only exists from macOS 15, hence the responds check.
    private static func attachAuthenticationMessage(_ event: CGEvent, pid: pid_t) {
        guard let setAuthenticationMessage, let objcMsgSend,
              let cls = NSClassFromString("SLSEventAuthenticationMessage")
        else { return }
        let selector = NSSelectorFromString("messageWithEventRecord:pid:version:")
        guard class_respondsToSelector(object_getClass(cls), selector),
              let record = eventRecord(event),
              let message = objcMsgSend(cls, selector, record, pid, 0)?.takeUnretainedValue()
        else { return }
        setAuthenticationMessage(event, message)
    }

    /// `__CGEvent` is `{CFRuntimeBase, uint32_t, SLSEventRecord *}`; the record pointer's offset
    /// has moved between releases, so probe the known slots.
    private static func eventRecord(_ event: CGEvent) -> UnsafeMutableRawPointer? {
        let base = Unmanaged.passUnretained(event).toOpaque()
        for offset in [24, 32, 16] {
            if let record = base.load(fromByteOffset: offset, as: UnsafeMutableRawPointer?.self) { return record }
        }
        return nil
    }

    // MARK: Focus without raise

    private static let focusLock = NSRecursiveLock()

    struct FocusHandoff {
        let previousPSN: ProcessSerialNumber
        let previousWindow: CGWindowID?
        let targetPSN: ProcessSerialNumber
        let targetWindow: CGWindowID
    }

    /// Makes `wid` key inside its app and tells the front app it lost key focus, without reordering
    /// any window, activating the app, or following a Space (yabai's focus-without-raise recipe).
    /// AppKit then treats the next click as a normal click instead of a window-activation click.
    static func focusWithoutRaise(pid: pid_t, wid: CGWindowID) -> FocusHandoff? {
        focusLock.lock()
        guard let front = frontProcess(), let target = processSerialNumber(pid: pid, wid: wid) else { focusLock.unlock(); return nil }
        let previousWindow = processID(front).flatMap(focusedWindow(pid:))
        guard postFocusRecord(to: front, wid: previousWindow ?? 0, focus: false) else { focusLock.unlock(); return nil }
        guard postFocusRecord(to: target, wid: wid, focus: true) else {
            _ = postFocusRecord(to: front, wid: previousWindow ?? 0, focus: true)
            focusLock.unlock(); return nil
        }
        return FocusHandoff(previousPSN: front, previousWindow: previousWindow, targetPSN: target, targetWindow: wid)
    }

    /// Undoes `focusWithoutRaise`; otherwise the user's key window stops taking keystrokes until they click it.
    static func restore(_ handoff: FocusHandoff) {
        defer { focusLock.unlock() }
        _ = postFocusRecord(to: handoff.targetPSN, wid: handoff.targetWindow, focus: false)
        // Respect a human app switch that happened while the background shortcut ran.
        let current = frontProcess() ?? handoff.previousPSN
        let unchanged = current.highLongOfPSN == handoff.previousPSN.highLongOfPSN && current.lowLongOfPSN == handoff.previousPSN.lowLongOfPSN
        let window = unchanged ? handoff.previousWindow : processID(current).flatMap(focusedWindow(pid:))
        _ = postFocusRecord(to: current, wid: window ?? 0, focus: true)
    }

    /// True when WindowServer considers `pid` the front process.
    static func isFront(pid: pid_t, wid: CGWindowID) -> Bool {
        guard let front = frontProcess(), let target = processSerialNumber(pid: pid, wid: wid) else { return false }
        return front.highLongOfPSN == target.highLongOfPSN && front.lowLongOfPSN == target.lowLongOfPSN
    }

    /// The window `pid` currently treats as key, per AX.
    static func keyWindow(pid: pid_t) -> CGWindowID? { focusedWindow(pid: pid) }

    /// cua's make-key record pair, minus the SLPSSetFrontProcessWithOptions call that would front the app.
    /// Makes `wid` AppKit-key inside its (still inactive) app, which menu key equivalents such as cmd+a need.
    @discardableResult
    static func makeKeyWindow(pid: pid_t, wid: CGWindowID) -> Bool {
        guard let postEventRecordTo, var psn = processSerialNumber(pid: pid, wid: wid) else { return false }
        var ok = true
        for kind: UInt8 in [0x01, 0x02] {
            var record = [UInt8](repeating: 0, count: 0xF8)
            record[0x04] = 0xF8
            record[0x08] = kind
            record[0x3A] = 0x10
            withUnsafeBytes(of: wid.littleEndian) { record.replaceSubrange(0x3C..<0x40, with: $0) }
            for i in 0x20..<0x30 { record[i] = 0xFF }
            ok = record.withUnsafeBytes { postEventRecordTo(&psn, $0.baseAddress!) } == 0 && ok
        }
        return ok
    }

    private static func postFocusRecord(to psn: ProcessSerialNumber, wid: CGWindowID, focus: Bool) -> Bool {
        guard let postEventRecordTo else { return false }
        var record = [UInt8](repeating: 0, count: 0xF8)
        record[0x04] = 0xF8
        record[0x08] = 0x0D
        withUnsafeBytes(of: wid.littleEndian) { record.replaceSubrange(0x3C..<0x40, with: $0) }
        record[0x8A] = focus ? 0x01 : 0x02
        var psn = psn
        return record.withUnsafeBytes { bytes in postEventRecordTo(&psn, bytes.baseAddress!) } == 0
    }

    private static func frontProcess() -> ProcessSerialNumber? {
        guard let getFrontProcess else { return nil }
        var psn = ProcessSerialNumber()
        return getFrontProcess(&psn) == 0 ? psn : nil
    }

    private static func processSerialNumber(pid: pid_t, wid: CGWindowID) -> ProcessSerialNumber? {
        var psn = ProcessSerialNumber()
        if let getWindowOwner, let getConnectionPSN {
            var owner: Int32 = 0
            if getWindowOwner(CGSMainConnectionID(), wid, &owner) == 0, owner != 0, getConnectionPSN(owner, &psn) == 0 {
                return psn
            }
        }
        if let getProcessForPID, getProcessForPID(pid, &psn) == 0 { return psn }
        return nil
    }

    private static func processID(_ psn: ProcessSerialNumber) -> pid_t? {
        guard let getProcessPID else { return nil }
        var psn = psn
        var pid: pid_t = 0
        return getProcessPID(&psn, &pid) == 0 && pid > 0 ? pid : nil
    }

    private static func focusedWindow(pid: pid_t) -> CGWindowID? {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        return app.element(kAXFocusedWindowAttribute)?.windowID
    }
}
