// Reads a process's menu bar status items through Accessibility, for e2e/cursor.py.
//   usage: status_item <pid>
// Prints {"trusted": Bool, "items": [{"description", "help", "frame": {x, y, width, height}}]}.
import ApplicationServices
import Foundation

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

let pid = Int32(CommandLine.arguments.dropFirst().first ?? "") ?? 0
let app = AXUIElementCreateApplication(pid)
AXUIElementSetMessagingTimeout(app, 2)
var items: [[String: Any]] = []
if let bar = attribute(app, "AXExtrasMenuBar"), CFGetTypeID(bar) == AXUIElementGetTypeID() {
    for item in attribute(bar as! AXUIElement, kAXChildrenAttribute) as? [AXUIElement] ?? [] {
        var origin = CGPoint.zero
        var size = CGSize.zero
        if let position = attribute(item, kAXPositionAttribute) { AXValueGetValue(position as! AXValue, .cgPoint, &origin) }
        if let extent = attribute(item, kAXSizeAttribute) { AXValueGetValue(extent as! AXValue, .cgSize, &size) }
        items.append([
            "description": attribute(item, kAXDescriptionAttribute) as? String ?? "",
            "help": attribute(item, kAXHelpAttribute) as? String ?? "",
            "frame": ["x": origin.x, "y": origin.y, "width": size.width, "height": size.height],
        ])
    }
}
let state: [String: Any] = ["trusted": AXIsProcessTrusted(), "items": items]
print(String(decoding: try! JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]), as: UTF8.self))
