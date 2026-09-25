import ApplicationServices
import CoreGraphics
import Foundation

typealias PostEventRecordTo = @convention(c) (UnsafePointer<ProcessSerialNumber>, UnsafePointer<UInt8>) -> Int32
let skylight = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_NOW)
let postEventRecordTo = unsafeBitCast(dlsym(skylight, "SLPSPostEventRecordTo"), to: PostEventRecordTo.self)

@_silgen_name("GetProcessForPID")
func GetProcessForPID(_ pid: pid_t, _ psn: UnsafeMutablePointer<ProcessSerialNumber>) -> OSStatus

// Makes the window key inside its own app without activating the app, so the click isn't swallowed as an activation click.
func makeKeyWindow(pid: pid_t, wid: CGWindowID) {
    var psn = ProcessSerialNumber()
    _ = GetProcessForPID(pid, &psn)
    var bytes = [UInt8](repeating: 0, count: 0xf8)
    bytes[0x04] = 0xf8
    bytes[0x3a] = 0x10
    withUnsafeBytes(of: wid) { for (i, b) in $0.enumerated() { bytes[0x3c + i] = b } }
    for i in 0x20..<0x30 { bytes[i] = 0xff }
    for phase: UInt8 in [0x01, 0x02] {
        bytes[0x08] = phase
        _ = postEventRecordTo(&psn, bytes)
    }
}

let pid = pid_t(CommandLine.arguments[1])!
let wid = CGWindowID(CommandLine.arguments[2])!
let local = CGPoint(x: Double(CommandLine.arguments[3])!, y: Double(CommandLine.arguments[4])!)

let info = (CGWindowListCopyWindowInfo([.optionIncludingWindow], wid) as? [[String: Any]])?.first
let bounds = info?[kCGWindowBounds as String] as? [String: Double] ?? [:]
let screenPoint = CGPoint(x: (bounds["X"] ?? 0) + local.x, y: (bounds["Y"] ?? 0) + local.y)

if CommandLine.arguments.contains("--make-key") { makeKeyWindow(pid: pid, wid: wid); usleep(20_000) }

let source = CGEventSource(stateID: .privateState)
for (type, pressure) in [(CGEventType.leftMouseDown, 1.0), (.leftMouseUp, 0.0)] {
    let e = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: screenPoint, mouseButton: .left)!
    e.setIntegerValueField(.mouseEventClickState, value: 1)
    e.setDoubleValueField(.mouseEventPressure, value: pressure)
    e.setIntegerValueField(.eventTargetUnixProcessID, value: Int64(pid))
    e.setIntegerValueField(.mouseEventWindowUnderMousePointer, value: Int64(wid))
    e.setIntegerValueField(.mouseEventWindowUnderMousePointerThatCanHandleThisEvent, value: Int64(wid))
    e.postToPid(pid)
    usleep(30_000)
}
print("clicked window \(wid) at local \(local) -> screen \(screenPoint)")
