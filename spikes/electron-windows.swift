import ApplicationServices
import CoreGraphics
import Foundation
@_silgen_name("_AXUIElementGetWindow") func _AXUIElementGetWindow(_ e: AXUIElement, _ w: UnsafeMutablePointer<CGWindowID>) -> AXError
@_silgen_name("_AXUIElementCreateWithRemoteToken") func _AXUIElementCreateWithRemoteToken(_ t: CFData) -> Unmanaged<AXUIElement>?
func attr(_ el: AXUIElement, _ n: String) -> AnyObject? { var v: AnyObject?; return AXUIElementCopyAttributeValue(el, n as CFString, &v) == .success ? v : nil }
let pid = pid_t(CommandLine.arguments[1])!
var token = Data(count: 20)
token.replaceSubrange(0..<4, with: withUnsafeBytes(of: pid) { Data($0) })
token.replaceSubrange(8..<12, with: withUnsafeBytes(of: Int32(0x636f_636f)) { Data($0) })
for id: UInt64 in 0..<5000 {
    token.replaceSubrange(12..<20, with: withUnsafeBytes(of: id) { Data($0) })
    guard let el = _AXUIElementCreateWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
    let role = attr(el, kAXRoleAttribute) as? String
    guard role == "AXWebArea" || role == kAXWindowRole || role == kAXTextAreaRole else { continue }
    var wid: CGWindowID = 0; _ = _AXUIElementGetWindow(el, &wid)
    let title = (attr(el, kAXTitleAttribute) as? String) ?? (attr(el, kAXDescriptionAttribute) as? String) ?? ""
    let win = attr(el, kAXWindowAttribute).map { $0 as! AXUIElement }
    var winWid: CGWindowID = 0; if let win { _ = _AXUIElementGetWindow(win, &winWid) }
    print("id \(id) \(role!) '\(title.prefix(40))' wid=\(wid) windowAttr.wid=\(winWid)")
}
for w in (CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]]) ?? [] where (w[kCGWindowOwnerPID as String] as? Int32) == pid && (w[kCGWindowLayer as String] as? Int) == 0 {
    print("CG", w[kCGWindowNumber as String]!, (w[kCGWindowName as String] as? String) ?? "", w[kCGWindowBounds as String]!, "alpha", w[kCGWindowAlpha as String]!)
}
