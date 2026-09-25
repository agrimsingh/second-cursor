import ApplicationServices
import CoreGraphics
import Foundation

@_silgen_name("_AXUIElementGetWindow")
func _AXUIElementGetWindow(_ element: AXUIElement, _ wid: UnsafeMutablePointer<CGWindowID>) -> AXError

@_silgen_name("_AXUIElementCreateWithRemoteToken")
func _AXUIElementCreateWithRemoteToken(_ token: CFData) -> Unmanaged<AXUIElement>?

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func windowID(_ el: AXUIElement) -> CGWindowID? {
    var wid: CGWindowID = 0
    return _AXUIElementGetWindow(el, &wid) == .success ? wid : nil
}

func summary(_ el: AXUIElement) -> String {
    "\(attr(el, kAXRoleAttribute) as? String ?? "?") '\(attr(el, kAXTitleAttribute) as? String ?? "")' wid=\(windowID(el).map(String.init) ?? "nil")"
}

let pid = pid_t(CommandLine.arguments[1])!
let maxID = UInt64(CommandLine.arguments[2])!
let app = AXUIElementCreateApplication(pid)

print("app children:")
for c in attr(app, kAXChildrenAttribute) as? [AXUIElement] ?? [] { print("  ", summary(c)) }
for name in [kAXMainWindowAttribute, kAXFocusedWindowAttribute, kAXFocusedUIElementAttribute, "AXChildrenInNavigationOrder"] {
    let v = attr(app, name)
    if let arr = v as? [AXUIElement] { print(name, arr.map(summary)) }
    else if let v { print(name, summary(v as! AXUIElement)) }
    else { print(name, "nil") }
}

var token = Data(count: 20)
token.replaceSubrange(0..<4, with: withUnsafeBytes(of: pid) { Data($0) })
token.replaceSubrange(8..<12, with: withUnsafeBytes(of: Int32(0x636f_636f)) { Data($0) })
var hits: [String: Int] = [:]
var windows: [(UInt64, String)] = []
let t0 = Date()
for id: UInt64 in 0..<maxID {
    token.replaceSubrange(12..<20, with: withUnsafeBytes(of: id) { Data($0) })
    guard let el = _AXUIElementCreateWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
    AXUIElementSetMessagingTimeout(el, 0.1)
    guard let role = attr(el, kAXRoleAttribute) as? String else { continue }
    hits[role, default: 0] += 1
    if role == kAXWindowRole { windows.append((id, summary(el))) }
}
print(String(format: "scan 0..<%llu: %.1fs, live elements by role: %@", maxID, Date().timeIntervalSince(t0), hits.description))
windows.forEach { print("  window at id \($0.0): \($0.1)") }
