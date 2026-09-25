// Independent observer for the E2E suite: reports user-visible state without going through Second Cursor.
import AppKit

@_silgen_name("CGSMainConnectionID") func CGSMainConnectionID() -> Int32
@_silgen_name("CGSGetActiveSpace") func CGSGetActiveSpace(_ cid: Int32) -> UInt64

let cursor = CGEvent(source: nil)!.location
let front = NSWorkspace.shared.frontmostApplication

var ids = [CGDirectDisplayID](repeating: 0, count: 16)
var count: UInt32 = 0
CGGetActiveDisplayList(16, &ids, &count)
let displays: [[String: Any]] = ids.prefix(Int(count)).map { id in
    let b = CGDisplayBounds(id)
    return ["id": id, "main": CGDisplayIsMain(id) != 0, "x": b.origin.x, "y": b.origin.y, "width": b.width, "height": b.height]
}

// Synthetic per-process events never reach the HID system state, so these only move for real user input.
let approvalWindows = (CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? [])
    .filter { ($0[kCGWindowOwnerName as String] as? String) == "Second Cursor" || ($0[kCGWindowOwnerName as String] as? String) == "SecondCursor" }
    .compactMap { info -> [String: Any]? in
        guard let id = info[kCGWindowNumber as String] as? NSNumber,
              let bounds = info[kCGWindowBounds as String] as? [String: Any],
              (bounds["Height"] as? Double ?? 0) > 100,
              (bounds["Width"] as? Double ?? 0) < 1000 else { return nil }
        return ["id": id, "title": info[kCGWindowName as String] ?? "", "bounds": bounds]
    }
let state: [String: Any] = [
    "second_cursor_windows": approvalWindows,
    "cursor": ["x": cursor.x, "y": cursor.y],
    "frontmost": ["name": front?.localizedName ?? "", "bundle_id": front?.bundleIdentifier ?? "", "pid": front?.processIdentifier ?? 0],
    "active_space": CGSGetActiveSpace(CGSMainConnectionID()),
    "seconds_since_user_mouse": CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .mouseMoved),
    "seconds_since_user_key": CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .keyDown),
    // kCGAnyInputEventType (~0) also covers clicks and trackpad gestures such as Space swipes.
    "seconds_since_user_input": CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: unsafeBitCast(UInt32.max, to: CGEventType.self)),
    "displays": displays,
]
let data = try! JSONSerialization.data(withJSONObject: state, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
