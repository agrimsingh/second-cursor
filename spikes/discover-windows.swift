import ApplicationServices
import CoreGraphics
import Foundation

@_silgen_name("_AXUIElementGetWindow")
func _AXUIElementGetWindow(_ element: AXUIElement, _ wid: UnsafeMutablePointer<CGWindowID>) -> AXError

@_silgen_name("_AXUIElementCreateWithRemoteToken")
func _AXUIElementCreateWithRemoteToken(_ token: CFData) -> Unmanaged<AXUIElement>?

@_silgen_name("CGSMainConnectionID")
func CGSMainConnectionID() -> Int32

@_silgen_name("CGSCopySpacesForWindows")
func CGSCopySpacesForWindows(_ cid: Int32, _ mask: Int32, _ wids: CFArray) -> Unmanaged<CFArray>?

@_silgen_name("CGSGetActiveSpace")
func CGSGetActiveSpace(_ cid: Int32) -> UInt64

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func windowID(_ el: AXUIElement) -> CGWindowID? {
    var wid: CGWindowID = 0
    return _AXUIElementGetWindow(el, &wid) == .success ? wid : nil
}

let pid = pid_t(CommandLine.arguments[1])!
let maxID = UInt64(CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "20000")!
let cid = CGSMainConnectionID()
print("active space:", CGSGetActiveSpace(cid))

let onScreen = Set(((CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]]) ?? []).compactMap { $0[kCGWindowNumber as String] as? Int })
let cgWindows = ((CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]) ?? []).filter {
    ($0[kCGWindowOwnerPID as String] as? Int32) == pid && ($0[kCGWindowLayer as String] as? Int) == 0
        && ((($0[kCGWindowBounds as String] as? [String: Double])?["Width"]) ?? 0) > 100
}

let app = AXUIElementCreateApplication(pid)
AXUIElementSetMessagingTimeout(app, 2)
var axWindows: [CGWindowID: AXUIElement] = [:]
for w in attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? [] { if let id = windowID(w) { axWindows[id] = w } }

var bruteWindows: [CGWindowID: AXUIElement] = [:]
var token = Data(count: 20)
token.replaceSubrange(0..<4, with: withUnsafeBytes(of: pid) { Data($0) })
token.replaceSubrange(8..<12, with: withUnsafeBytes(of: Int32(0x636f_636f)) { Data($0) })
let t0 = Date()
for id: UInt64 in 0..<maxID {
    token.replaceSubrange(12..<20, with: withUnsafeBytes(of: id) { Data($0) })
    guard let el = _AXUIElementCreateWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
    AXUIElementSetMessagingTimeout(el, 0.2)
    if (attr(el, kAXRoleAttribute) as? String) == kAXWindowRole, let wid = windowID(el) { bruteWindows[wid] = el }
}
print(String(format: "remote-token scan 0..<%llu took %.2fs", maxID, Date().timeIntervalSince(t0)))

for w in cgWindows {
    let wid = CGWindowID(w[kCGWindowNumber as String] as! Int)
    let spaces = (CGSCopySpacesForWindows(cid, 7, [wid] as CFArray)?.takeRetainedValue() as? [UInt64]) ?? []
    let el = axWindows[wid] ?? bruteWindows[wid]
    let minimized = el.flatMap { attr($0, kAXMinimizedAttribute) as? Bool }
    print("wid \(wid) '\((w[kCGWindowName as String] as? String ?? "").prefix(30))' spaces=\(spaces) onscreen=\(onScreen.contains(Int(wid))) minimized=\(minimized.map(String.init) ?? "?") inAXWindows=\(axWindows[wid] != nil) inRemoteToken=\(bruteWindows[wid] != nil)")
}
