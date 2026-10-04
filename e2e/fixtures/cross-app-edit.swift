import AppKit
import ApplicationServices
import Foundation

// Test harness only: exact PID + exact window-title AX observer/external editor.
// Does not activate an app, move focus, type keys, or choose another process.
// cross-app-edit PID TITLE read|append [TEXT]

enum Failure: Error, CustomStringConvertible {
    case message(String)
    var description: String {
        switch self { case let .message(text): return text }
    }
}

func attribute(_ element: AXUIElement, _ name: String) throws -> CFTypeRef? {
    var value: CFTypeRef?
    let status = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    guard status == .success || status == .noValue || status == .attributeUnsupported else {
        throw Failure.message("AX read \(name): \(status.rawValue)")
    }
    return value
}

func textAreas(_ root: AXUIElement, depth: Int = 0) throws -> [AXUIElement] {
    guard depth < 30 else { throw Failure.message("AX tree depth exceeded") }
    if try attribute(root, kAXRoleAttribute) as? String == kAXTextAreaRole { return [root] }
    let children = try attribute(root, kAXChildrenAttribute) as? [AXUIElement] ?? []
    return try children.flatMap { try textAreas($0, depth: depth + 1) }
}

do {
    let args = CommandLine.arguments
    guard args.count >= 4, let pid = Int32(args[1]), pid > 0,
          ["read", "append"].contains(args[3]), args[3] != "append" || args.count == 5 else {
        throw Failure.message("Usage: cross-app-edit PID exact-title read|append [text]")
    }
    guard let app = NSRunningApplication(processIdentifier: pid), app.bundleIdentifier == "com.apple.TextEdit" else {
        throw Failure.message("PID is not the disposable TextEdit process")
    }
    let root = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(root, 5)
    let windows = try attribute(root, kAXWindowsAttribute) as? [AXUIElement] ?? []
    let matching = try windows.filter { try attribute($0, kAXTitleAttribute) as? String == args[2] }
    guard matching.count == 1 else { throw Failure.message("Expected exactly one matching window, found \(matching.count)") }
    let areas = try textAreas(matching[0])
    guard areas.count == 1 else { throw Failure.message("Expected exactly one text area, found \(areas.count)") }
    guard let before = try attribute(areas[0], kAXValueAttribute) as? String else {
        throw Failure.message("Text area has no string value")
    }
    var expected = before
    if args[3] == "append" {
        expected += args[4]
        let result = AXUIElementSetAttributeValue(areas[0], kAXValueAttribute as CFString, expected as CFString)
        guard result == .success else { throw Failure.message("AX append failed: \(result.rawValue)") }
    }
    let after = try attribute(areas[0], kAXValueAttribute) as? String
    guard after == expected else { throw Failure.message("Text after operation did not match the exact expected value") }
    let receipt: [String: Any] = ["pid": pid, "title": args[2], "action": args[3],
        "before": before, "after": after!, "activation_requested": false]
    let data = try JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
} catch {
    fputs("\(error)\n", stderr)
    exit(1)
}
