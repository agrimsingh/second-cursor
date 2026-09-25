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

func children(_ el: AXUIElement) -> [AXUIElement] { attr(el, kAXChildrenAttribute) as? [AXUIElement] ?? [] }

// kAXWindowsAttribute omits windows on inactive Spaces; remote tokens reach them.
func findWindow(pid: pid_t, wid: CGWindowID) -> AXUIElement? {
    let app = AXUIElementCreateApplication(pid)
    if let w = (attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []).first(where: { windowID($0) == wid }) { return w }
    var token = Data(count: 20)
    token.replaceSubrange(0..<4, with: withUnsafeBytes(of: pid) { Data($0) })
    token.replaceSubrange(8..<12, with: withUnsafeBytes(of: Int32(0x636f_636f)) { Data($0) })
    for id: UInt64 in 0..<50_000 {
        token.replaceSubrange(12..<20, with: withUnsafeBytes(of: id) { Data($0) })
        guard let el = _AXUIElementCreateWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
        AXUIElementSetMessagingTimeout(el, 0.2)
        if (attr(el, kAXRoleAttribute) as? String) == kAXWindowRole, windowID(el) == wid { return el }
    }
    return nil
}

func resolve(_ root: AXUIElement, _ path: String) -> AXUIElement? {
    var el = root
    for part in path.split(separator: ".") where !part.isEmpty {
        let kids = children(el)
        guard let i = Int(part), i < kids.count else { return nil }
        el = kids[i]
    }
    return el
}

func dump(_ el: AXUIElement, path: String, depth: Int, maxDepth: Int) {
    let role = attr(el, kAXRoleAttribute) as? String ?? "?"
    let label = (attr(el, kAXTitleAttribute) as? String) ?? (attr(el, kAXDescriptionAttribute) as? String) ?? ""
    let value = (attr(el, kAXValueAttribute) as? String).map { " value=\(String($0.prefix(60)).debugDescription)" } ?? ""
    var actions: CFArray?
    AXUIElementCopyActionNames(el, &actions)
    let acts = (actions as? [String] ?? []).filter { $0.hasPrefix("AX") }
    print(String(repeating: "  ", count: depth) + "[\(path)] \(role) \(label.debugDescription)\(value)\(acts.isEmpty ? "" : " \(acts)")")
    guard depth < maxDepth else { return }
    for (i, c) in children(el).enumerated() { dump(c, path: path.isEmpty ? "\(i)" : "\(path).\(i)", depth: depth + 1, maxDepth: maxDepth) }
}

let args = CommandLine.arguments
let pid = pid_t(args[1])!, wid = CGWindowID(args[2])!, cmd = args[3]
let app = AXUIElementCreateApplication(pid)
if args.contains("--enable-web-ax") {
    AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    usleep(500_000)
}
guard let window = findWindow(pid: pid, wid: wid) else { print("window \(wid) not found for pid \(pid)"); exit(1) }

switch cmd {
case "dump":
    dump(window, path: "", depth: 0, maxDepth: Int(args.count > 4 ? args[4] : "4") ?? 4)
case "press":
    guard let el = resolve(window, args[4]) else { print("bad path"); exit(1) }
    print("AXPress ->", AXUIElementPerformAction(el, kAXPressAction as CFString).rawValue)
case "move":
    var point = CGPoint(x: Double(args[4])!, y: Double(args[5])!)
    let value = AXValueCreate(.cgPoint, &point)!
    print("AXPosition set ->", AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, value).rawValue)
case "get":
    guard let el = resolve(window, args[4]) else { print("bad path"); exit(1) }
    print((attr(el, kAXValueAttribute) as? String ?? "nil").debugDescription)
case "setvalue":
    guard let el = resolve(window, args[4]) else { print("bad path"); exit(1) }
    print("AXValue set ->", AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, args[5] as CFString).rawValue)
    print("read back:", (attr(el, kAXValueAttribute) as? String ?? "nil").debugDescription)
default:
    print("unknown command")
}
