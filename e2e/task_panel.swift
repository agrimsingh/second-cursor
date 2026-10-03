// External AX inspector for the disposable task-panel E2E suite. It never uses Second Cursor's
// own tools to press its controls, so inspection cannot change the lifecycle being measured.
import AppKit
import ApplicationServices

func read(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}
func text(_ element: AXUIElement, _ name: String) -> String? { read(element, name) as? String }
func label(_ element: AXUIElement) -> String {
    [kAXTitleAttribute, kAXDescriptionAttribute, "AXLabel", kAXValueAttribute]
        .compactMap { text(element, $0) }.first { !$0.isEmpty } ?? ""
}
func actions(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success else { return [] }
    return names as? [String] ?? []
}
func emit(_ value: [String: Any], code: Int32 = 0) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    exit(code)
}

let args = CommandLine.arguments.dropFirst()
guard args.count >= 2, let pid = Int32(args[args.startIndex + 1]) else {
    emit(["error": "usage: task_panel window|inspect|press|select PID window-title [label], or activate PID"], code: 2)
}
let command = args[args.startIndex]
if command == "activate" {
    guard args.count == 2, let running = NSRunningApplication(processIdentifier: pid), !running.isTerminated else {
        emit(["error": "previous foreground process is no longer running"], code: 1)
    }
    let accepted = running.activate(options: [])
    emit(["ok": accepted, "pid": pid], code: accepted ? 0 : 1)
}
guard ["window", "inspect", "press", "select"].contains(command), args.count >= 3 else {
    emit(["error": "a panel command requires its exact window title"], code: 2)
}
guard AXIsProcessTrusted() else { emit(["trusted": false, "error": "runner needs Accessibility"], code: 1) }
let title = args[args.startIndex + 2]
let app = AXUIElementCreateApplication(pid)
guard let windows = read(app, kAXWindowsAttribute) as? [AXUIElement],
      let window = windows.first(where: { text($0, kAXTitleAttribute) == title }) else {
    emit(["trusted": true, "error": "requested test panel not found"], code: 1)
}
if command == "window" {
    let records = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []
    let matches = records.filter {
        ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid
            && $0[kCGWindowName as String] as? String == title
    }
    guard matches.count == 1, let record = matches.first,
          let wid = record[kCGWindowNumber as String] as? NSNumber else {
        emit(["trusted": true, "error": "expected exactly one CG window for the test panel",
              "matches": matches.count, "title": title], code: 1)
    }
    emit(["trusted": true, "pid": pid, "title": title, "window_id": wid.uint32Value])
}
var flattened: [(AXUIElement, [String: Any])] = []
func walk(_ element: AXUIElement, depth: Int = 0) {
    guard flattened.count < 1500, depth < 35 else { return }
    let info: [String: Any] = [
        "role": text(element, kAXRoleAttribute) ?? "", "label": label(element),
        "identifier": text(element, "AXIdentifier") ?? "", "actions": actions(element),
        "enabled": (read(element, kAXEnabledAttribute) as? Bool) ?? true,
        "selected": (read(element, kAXSelectedAttribute) as? Bool) ?? false,
    ]
    flattened.append((element, info))
    for child in read(element, kAXChildrenAttribute) as? [AXUIElement] ?? [] { walk(child, depth: depth + 1) }
}
walk(window)
if command == "inspect" { emit(["trusted": true, "elements": flattened.map(\.1)]) }
guard args.count == 4 else { emit(["error": "an action requires its exact label"], code: 2) }
let wanted = args[args.startIndex + 3]
let matches = flattened.filter { element, info in
    let name = info["label"] as? String ?? ""
    if command == "select" { return name.hasPrefix(wanted + ", ") && actions(element).contains(kAXPressAction) }
    return name == wanted && info["role"] as? String == kAXButtonRole
}
guard matches.count == 1, let match = matches.first else {
    emit(["error": "expected exactly one native control", "matches": matches.count, "label": wanted], code: 1)
}
guard match.1["enabled"] as? Bool == true else { emit(["error": "control is disabled"], code: 1) }
let result = AXUIElementPerformAction(match.0, kAXPressAction as CFString)
emit(["ok": result == .success, "ax_error": result.rawValue], code: result == .success ? 0 : 1)
