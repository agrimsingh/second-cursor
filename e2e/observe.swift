// Independent observer for the E2E suite: reports user-visible state without going through Second Cursor.
import AppKit

@_silgen_name("CGSMainConnectionID") func CGSMainConnectionID() -> Int32
@_silgen_name("CGSGetActiveSpace") func CGSGetActiveSpace(_ cid: Int32) -> UInt64
@_silgen_name("CGSCopyManagedDisplaySpaces") func CGSCopyManagedDisplaySpaces(_ cid: Int32) -> Unmanaged<CFArray>?

/// Whether the main display shows a full-screen app's Space (type 4), which puts other apps' new windows on a desktop
/// Space nobody is viewing.
func mainSpaceIsFullScreen() -> Bool {
    guard let uuid = CGDisplayCreateUUIDFromDisplayID(CGMainDisplayID())?.takeRetainedValue(),
          let identifier = CFUUIDCreateString(nil, uuid) as String?,
          let managed = CGSCopyManagedDisplaySpaces(CGSMainConnectionID())?.takeRetainedValue() as? [[String: Any]],
          let entry = managed.first(where: {
              let name = $0["Display Identifier"] as? String
              return name == "Main" || name?.caseInsensitiveCompare(identifier) == .orderedSame
          }),
          let current = entry["Current Space"] as? [String: Any]
    else { return false }
    return (current["type"] as? NSNumber)?.intValue == 4
}

// Bracket all reads in a clock shared across observer subprocesses. Scenario execution time alone
// excludes these reads and can misclassify human input during either boundary as an agent violation.
let observationStarted = ProcessInfo.processInfo.systemUptime
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
var state: [String: Any] = [
    "observation_started_at_monotonic": observationStarted,
    "second_cursor_windows": approvalWindows,
    "cursor": ["x": cursor.x, "y": cursor.y],
    "frontmost": ["name": front?.localizedName ?? "", "bundle_id": front?.bundleIdentifier ?? "", "pid": front?.processIdentifier ?? 0],
    "active_space": CGSGetActiveSpace(CGSMainConnectionID()),
    "main_space_full_screen": mainSpaceIsFullScreen(),
    // A held button produces dragged events instead of mouseMoved, but still moves the human cursor.
    "seconds_since_user_mouse": [CGEventType.mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged]
        .map { CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: $0) }.min()!,
    "seconds_since_user_key": CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .keyDown),
    // kCGAnyInputEventType (~0) also covers clicks and trackpad gestures such as Space swipes.
    "seconds_since_user_input": CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: unsafeBitCast(UInt32.max, to: CGEventType.self)),
    "displays": displays,
]
state["observation_finished_at_monotonic"] = ProcessInfo.processInfo.systemUptime
let data = try! JSONSerialization.data(withJSONObject: state, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
