import ApplicationServices
import CoreGraphics
import Foundation

struct SnapshotElement {
    let element: AXUIElement
    let role: String
    let inWebArea: Bool
}

/// Renders an AX subtree as the indented outline agents read; actionable elements get `[index]`.
struct TreeBuilder {
    static let actionableRoles: Set<String> = [
        "AXButton", "AXTextField", "AXTextArea", "AXLink", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
        "AXMenuButton", "AXSlider", "AXComboBox", "AXIncrementor", "AXDisclosureTriangle", "AXMenuItem",
        "AXMenuBarItem", "AXColorWell", "AXDateField", "AXSearchField",
    ]
    static let rowRoles: Set<String> = ["AXRow", "AXCell"]
    static let editableRoles: Set<String> = ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXSecureTextField"]
    /// Shown only when they carry a label, a value or an action; their children are still walked.
    static let containerRoles: Set<String> = [
        "AXGroup", "AXUnknown", "AXSplitGroup", "AXLayoutArea", "AXLayoutItem", "AXGenericElement",
        "AXStaticText", "AXImage", "AXHeading", "AXSplitter", "AXMatte", "AXGrowArea",
    ]
    /// Chrome that never helps an agent and would crowd out real content.
    static let prunedRoles: Set<String> = ["AXScrollBar", "AXColumn", "AXValueIndicator"]
    static let passiveActions: Set<String> = ["AXShowMenu", "AXScrollToVisible", "AXShowDefaultUI", "AXShowAlternateUI", "AXRaise"]

    private static let attributeNames = [
        kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, "AXLabel", kAXPlaceholderValueAttribute,
        kAXValueAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXChildrenAttribute,
    ]

    let origin: CGPoint
    let maxElements: Int
    let maxDepth: Int
    private let maxVisits: Int
    private(set) var lines: [String] = []
    private(set) var elements: [SnapshotElement] = []
    private(set) var truncated = false
    private var visits = 0

    init(origin: CGPoint, maxElements: Int, maxDepth: Int) {
        self.origin = origin
        self.maxElements = max(1, maxElements)
        self.maxDepth = max(1, maxDepth)
        self.maxVisits = max(3000, self.maxElements * 10)
    }

    var text: String { lines.joined(separator: "\n") }

    mutating func build(window: AXUIElement, title: String, size: CGSize, menuBar: AXUIElement?) {
        lines.append("window \"\(Self.escape(title, limit: 200))\" (0,0 \(Int(size.width.rounded()))x\(Int(size.height.rounded())))")
        for child in window.children { walk(child, depth: 1, indent: 1, inWebArea: false) }
        if let menuBar, !truncated {
            lines.append("menubar")
            for item in menuBar.children { walk(item, depth: 1, indent: 1, inWebArea: false, recurse: false) }
        }
    }

    private mutating func walk(_ element: AXUIElement, depth: Int, indent: Int, inWebArea: Bool, recurse: Bool = true) {
        guard lines.count <= maxElements, visits < maxVisits else {
            truncated = true
            return
        }
        visits += 1
        let values = element.attributes(Self.attributeNames)
        let role = values[0] as? String ?? "AXUnknown"
        guard !Self.prunedRoles.contains(role) else { return }
        let label = values[1...4].lazy.compactMap { $0 as? String }.first { !$0.isEmpty } ?? ""
        let value = AX.text(values[5])
        let actions = element.actions
        let meaningful = actions.contains { $0.hasPrefix("AX") && !Self.passiveActions.contains($0) }
        let indexed = Self.actionableRoles.contains(role) || meaningful || (Self.rowRoles.contains(role) && !actions.isEmpty)
        let shown = indexed || !label.isEmpty || !(value ?? "").isEmpty || !Self.containerRoles.contains(role)

        var childIndent = indent
        if shown {
            var line = String(repeating: "  ", count: indent)
            if indexed {
                line += "[\(elements.count)] \(role) \"\(Self.escape(label, limit: 120))\""
                elements.append(SnapshotElement(element: element, role: role, inWebArea: inWebArea))
            } else {
                line += role
                if !label.isEmpty { line += " \"\(Self.escape(label, limit: 120))\"" }
            }
            // An empty value only carries information on editable fields ("this box is empty").
            if let value, !value.isEmpty || Self.editableRoles.contains(role) { line += " value=\"\(Self.escape(value, limit: 300))\"" }
            if indexed {
                let position = AX.point(values[6]) ?? origin
                let size = AX.size(values[7]) ?? .zero
                line += " (\(Int((position.x - origin.x).rounded())),\(Int((position.y - origin.y).rounded()))"
                    + " \(Int(size.width.rounded()))x\(Int(size.height.rounded())))"
            }
            lines.append(line)
            childIndent += 1
        }

        let children = values[8] as? [AXUIElement] ?? []
        guard recurse, !children.isEmpty else { return }
        guard depth < maxDepth else {
            truncated = true
            return
        }
        let childInWebArea = inWebArea || role == "AXWebArea"
        for child in children {
            walk(child, depth: depth + 1, indent: childIndent, inWebArea: childInWebArea)
            if truncated, lines.count > maxElements || visits >= maxVisits { return }
        }
    }

    /// Escapes like a JSON string body so values stay on one line.
    static func escape(_ text: String, limit: Int) -> String {
        var out = ""
        var count = 0
        for scalar in text.unicodeScalars {
            if count == limit {
                out += "…"
                break
            }
            count += 1
            switch scalar {
            case "\\": out += "\\\\"
            case "\"": out += "\\\""
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case _ where scalar.value < 0x20: out += String(format: "\\u%04x", scalar.value)
            default: out.unicodeScalars.append(scalar)
            }
        }
        return out
    }
}
