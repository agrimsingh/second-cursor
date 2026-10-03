// Ported from cua-driver's SkyLight bridge (MIT, github.com/trycua/cua); see LICENSES/cua-driver-MIT.txt.
import ApplicationServices
import CoreGraphics
import Foundation
import ObjectiveC
import SCShared

/// Private SkyLight entry points for background input. Public callers check the capability before
/// dispatch, so an OS update cannot silently weaken targeting or report a missing call as delivered.
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
    private static let objcMsgSend = symbol("objc_msgSend", AuthMessageFactory.self)

    static var pointerUnavailableReason: String? {
        if postToPid == nil { return "SLEventPostToPid is unavailable on this macOS version" }
        if setWindowLocationFn == nil { return "CGEventSetWindowLocation is unavailable; window-local targeting cannot be guaranteed" }
        if setIntegerField == nil { return "SLEventSetIntegerValueField is unavailable; window addressing cannot be guaranteed" }
        return focusUnavailableReason
    }

    static var keyboardUnavailableReason: String? {
        if postToPid == nil { return "SLEventPostToPid is unavailable on this macOS version" }
        guard setAuthenticationMessage != nil, objcMsgSend != nil,
              let cls = NSClassFromString("SLSEventAuthenticationMessage"),
              class_respondsToSelector(object_getClass(cls), NSSelectorFromString("messageWithEventRecord:pid:version:"))
        else { return "Authenticated background keyboard events are unavailable on this macOS version" }
        return focusUnavailableReason
    }

    private static var focusUnavailableReason: String? {
        if postEventRecordTo == nil { return "SLPSPostEventRecordTo is unavailable; safe key-window targeting cannot be guaranteed" }
        if getFrontProcess == nil { return "The foreground process cannot be determined safely" }
        if getProcessForPID == nil && (getWindowOwner == nil || getConnectionPSN == nil) {
            return "The target process cannot be resolved safely"
        }
        return nil
    }

    static func requirePointerSupport() throws {
        if let reason = pointerUnavailableReason { throw SCError.notAllowed(reason + ". Use a semantic AX action or the browser backend.") }
    }

    static func requireKeyboardSupport() throws {
        if let reason = keyboardUnavailableReason { throw SCError.notAllowed(reason + ". Use set_value or a semantic AX action, or the browser backend.") }
    }

    /// Keyboard events need the authentication envelope for Chromium to accept them as live input.
    /// Mouse events must not carry it: it bypasses the session tap Chromium's window handler reads.
    @discardableResult
    static func post(_ event: CGEvent, to pid: pid_t, authenticated: Bool) -> Bool {
        HumanInputMonitor.stamp(event)
        guard let postToPid else { return false }
        if authenticated && !attachAuthenticationMessage(event, pid: pid) { return false }
        postToPid(pid, event)
        return true
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
    private static func attachAuthenticationMessage(_ event: CGEvent, pid: pid_t) -> Bool {
        guard let setAuthenticationMessage, let objcMsgSend,
              let cls = NSClassFromString("SLSEventAuthenticationMessage")
        else { return false }
        let selector = NSSelectorFromString("messageWithEventRecord:pid:version:")
        guard class_respondsToSelector(object_getClass(cls), selector),
              let record = eventRecord(event),
              let message = objcMsgSend(cls, selector, record, pid, 0)?.takeUnretainedValue()
        else { return false }
        setAuthenticationMessage(event, message)
        return true
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
        let targetPSN: ProcessSerialNumber
        let targetWindow: CGWindowID
    }

    /// Posts a synthetic `focused` record for `wid` to the target process only, so AppKit treats the
    /// next click as a normal click; no window is reordered and no Space followed. A record must never
    /// reach the real frontmost process (it would resign active mid-typing), so this returns nil and
    /// posts nothing when the target is front or its state is unknown. Serialized until `restore`.
    static func focusWithoutRaise(pid: pid_t, wid: CGWindowID) throws -> FocusHandoff? {
        focusLock.lock()
        var handedOff = false
        defer { if !handedOff { focusLock.unlock() } }
        // Another app's handoff can hold this lock while task control or human ownership changes.
        // Revalidate after waiting, before posting even a synthetic focus record.
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        guard let front = frontProcess(), let target = processSerialNumber(pid: pid, wid: wid),
              !samePSN(front, target), postFocusRecord(to: target, wid: wid, focus: true)
        else {
            return nil
        }
        handedOff = true
        return FocusHandoff(targetPSN: target, targetWindow: wid)
    }

    /// Takes the synthetic focus back from the target only. Skipped when the user switched to the
    /// target meanwhile or foreground identity is unknown: never tell a possible foreground app
    /// that it lost focus. Every path still releases the serialization lock.
    static func restore(_ handoff: FocusHandoff) {
        defer { focusLock.unlock() }
        guard let front = frontProcess(), !samePSN(front, handoff.targetPSN) else { return }
        _ = postFocusRecord(to: handoff.targetPSN, wid: handoff.targetWindow, focus: false)
    }

    /// True when WindowServer considers `pid` the front process.
    static func isFront(pid: pid_t, wid: CGWindowID) -> Bool {
        guard let front = frontProcess(), let target = processSerialNumber(pid: pid, wid: wid) else { return false }
        return samePSN(front, target)
    }

    private static func samePSN(_ a: ProcessSerialNumber, _ b: ProcessSerialNumber) -> Bool {
        a.highLongOfPSN == b.highLongOfPSN && a.lowLongOfPSN == b.lowLongOfPSN
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

    private static func focusedWindow(pid: pid_t) -> CGWindowID? {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        return app.element(kAXFocusedWindowAttribute)?.windowID
    }
}
