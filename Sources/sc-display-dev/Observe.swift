// Observations use their own window-server queries rather than SCDisplay's, so the
// commands grade the module independently.
import AppKit
import SCShared

@_silgen_name("CGSMainConnectionID") func CGSMainConnectionID() -> Int32
@_silgen_name("CGSGetActiveSpace") func CGSGetActiveSpace(_ cid: Int32) -> UInt64
@_silgen_name("CGSCopySpacesForWindows") func CGSCopySpacesForWindows(_ cid: Int32, _ mask: Int32, _ wids: CFArray) -> Unmanaged<CFArray>?
@_silgen_name("CGSCopyManagedDisplaySpaces") func CGSCopyManagedDisplaySpaces(_ cid: Int32) -> Unmanaged<CFArray>?

struct UserState: Equatable {
    var cursor: CGPoint
    var frontmost: String
    var activeSpace: UInt64
}

struct WindowFacts {
    var pid: pid_t
    var frame: CGRect
    var onScreen: Bool
    var spaces: [UInt64]
}

func userState() -> UserState {
    UserState(cursor: CGEvent(source: nil)?.location ?? .zero, frontmost: frontmostASN(), activeSpace: CGSGetActiveSpace(CGSMainConnectionID()))
}

@discardableResult
func run(_ path: String, _ arguments: [String]) -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = arguments
    let pipe = Pipe()
    process.standardOutput = pipe
    guard (try? process.run()) != nil else { return "" }
    let output = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return String(decoding: output, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
}

/// NSWorkspace caches the frontmost app in a process without a running app loop; lsappinfo doesn't.
func frontmostASN() -> String {
    run("/usr/bin/lsappinfo", ["front"])
}

func activeDisplays() -> [CGDirectDisplayID: CGRect] {
    var ids = [CGDirectDisplayID](repeating: 0, count: 32)
    var count: UInt32 = 0
    CGGetActiveDisplayList(32, &ids, &count)
    return Dictionary(uniqueKeysWithValues: ids.prefix(Int(count)).map { ($0, CGDisplayBounds($0)) })
}

func currentSpace(of display: CGDirectDisplayID) -> UInt64? {
    guard let uuid = CGDisplayCreateUUIDFromDisplayID(display)?.takeRetainedValue(),
          let identifier = CFUUIDCreateString(nil, uuid) as String?,
          let managed = CGSCopyManagedDisplaySpaces(CGSMainConnectionID())?.takeRetainedValue() as? [[String: Any]],
          let entry = managed.first(where: { ($0["Display Identifier"] as? String)?.caseInsensitiveCompare(identifier) == .orderedSame })
    else { return nil }
    return ((entry["Current Space"] as? [String: Any])?["ManagedSpaceID"] as? NSNumber)?.uint64Value
}

func windowFacts(_ wid: CGWindowID) -> WindowFacts? {
    guard let info = (CGWindowListCopyWindowInfo([.optionIncludingWindow], wid) as? [[String: Any]])?.first,
          let pid = info[kCGWindowOwnerPID as String] as? pid_t,
          let bounds = info[kCGWindowBounds as String] as? NSDictionary,
          let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary)
    else { return nil }
    let spaces = (CGSCopySpacesForWindows(CGSMainConnectionID(), 7, [wid] as CFArray)?.takeRetainedValue() as? [NSNumber])?.map(\.uint64Value) ?? []
    return WindowFacts(pid: pid, frame: frame, onScreen: info[kCGWindowIsOnscreen as String] as? Bool ?? false, spaces: spaces)
}

func window(pid: pid_t, titled title: String) -> CGWindowID? {
    let all = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
    return all.first { info in
        info[kCGWindowOwnerPID as String] as? pid_t == pid && info[kCGWindowLayer as String] as? Int == 0
            && (info[kCGWindowName as String] as? String)?.contains(title) == true
    }?[kCGWindowNumber as String] as? CGWindowID
}

/// A freshly launched window resizes and orders in over its first moments; wait until it's stable.
func settledWindow(_ wid: CGWindowID) async -> WindowFacts? {
    var last = windowFacts(wid)
    for _ in 0..<30 {
        try? await Task.sleep(nanoseconds: 200_000_000)
        let now = windowFacts(wid)
        if let now, let previous = last, now.frame == previous.frame { return now }
        last = now
    }
    return last
}

/// Launches a separate, non-activated TextEdit on a scratch file under /tmp/sc-display. Saved
/// state is ignored because killed instances otherwise reopen old documents in new instances.
func launchScratchTextEdit(_ name: String, text: String) async throws -> (pid: pid_t, window: CGWindowID) {
    let directory = URL(fileURLWithPath: "/tmp/sc-display", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let file = directory.appendingPathComponent(name)
    try text.write(to: file, atomically: true, encoding: .utf8)
    let existing = Set(textEditPIDs())
    run("/usr/bin/open", ["-n", "-g", "-a", "TextEdit", file.path, "--args", "-ApplePersistenceIgnoreState", "YES"])
    for _ in 0..<150 {
        if let pid = textEditPIDs().first(where: { !existing.contains($0) }), let window = window(pid: pid, titled: name) {
            return (pid, window)
        }
        try await Task.sleep(nanoseconds: 100_000_000)
    }
    throw SCError.actionFailed("scratch TextEdit window did not appear")
}

func textEditPIDs() -> [pid_t] {
    run("/usr/bin/pgrep", ["-x", "TextEdit"]).split(separator: "\n").compactMap { pid_t($0) }
}

/// Whether this process has an enabled event tap listening for pointer motion.
func clampTapInstalled() -> Bool {
    var count: UInt32 = 0
    guard CGGetEventTapList(0, nil, &count) == .success, count > 0 else { return false }
    var taps = [CGEventTapInformation](repeating: CGEventTapInformation(), count: Int(count))
    guard CGGetEventTapList(count, &taps, &count) == .success else { return false }
    let motion = CGEventMask(1) << CGEventType.mouseMoved.rawValue
    return taps.prefix(Int(count)).contains { $0.tappingProcess == getpid() && $0.enabled && $0.eventsOfInterest & motion != 0 }
}

func secondsSinceUserInput() -> Double {
    let types: [CGEventType] = [.mouseMoved, .leftMouseDown, .rightMouseDown, .leftMouseDragged, .scrollWheel, .keyDown, .flagsChanged]
    return types.map { CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: $0) }.min() ?? .infinity
}

func originInside(_ frame: CGRect, _ bounds: CGRect) -> Bool {
    frame.minX >= bounds.minX && frame.minX < bounds.maxX && frame.minY >= bounds.minY && frame.minY < bounds.maxY
}

func sameOrigin(_ a: CGRect?, _ b: CGRect) -> Bool {
    guard let a else { return false }
    return abs(a.minX - b.minX) <= 2 && abs(a.minY - b.minY) <= 2
}

func describe(_ r: CGRect) -> String {
    String(format: "(%.0f,%.0f %.0fx%.0f)", r.minX, r.minY, r.width, r.height)
}

func describe(_ w: WindowFacts?) -> String {
    guard let w else { return "window gone" }
    return "frame=\(describe(w.frame)) onScreen=\(w.onScreen) spaces=\(w.spaces)"
}

func printStatus(_ s: AgentDisplayStatus) {
    let bounds = s.bounds.map { describe($0.cgRect) } ?? "-"
    print("agent display: active=\(s.isActive) id=\(s.displayID.map(String.init) ?? "-") bounds=\(bounds) hosted=\(s.hostedWindowIDs) clamp=\(s.cursorClampEnabled)")
}

func printDisplays() {
    let state = userState()
    print("cursor=(\(Int(state.cursor.x)),\(Int(state.cursor.y))) activeSpace=\(state.activeSpace) front=\(state.frontmost)")
    for (id, bounds) in activeDisplays().sorted(by: { $0.key < $1.key }) {
        print("display \(id) main=\(CGDisplayIsMain(id) != 0) bounds=\(describe(bounds)) currentSpace=\(currentSpace(of: id).map(String.init) ?? "-")")
    }
}
