import ApplicationServices
import CoreGraphics
import Foundation
import SCShared

struct SnapshotElement {
    let element: AXUIElement
    let role: String
    let inWebArea: Bool
    let key: String
}

/// One shown element of a window's accessibility tree, in depth-first order.
struct TreeNode {
    let indent: Int
    let role: String
    let label: String
    let value: String?
    let frame: CGRect?
    let indexed: Bool
    let element: AXUIElement
    let inWebArea: Bool
    /// Path of role/label/ordinal from the window root; stable while the UI keeps its structure.
    let key: String
    let actions: [String]
    let states: [String: Bool?]
}

/// Walks an AX subtree once and records every shown element. Rendering limits (max elements,
/// scope, diffs) are applied afterwards, so one walk serves the full view, a scoped read and a diff.
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
    /// Implied by an indexed element or harmless; only the rest are printed as `actions=[...]`.
    static let obviousActions = passiveActions.union([kAXPressAction, kAXConfirmAction])

    /// Custom actions arrive as "Name:<title>\nTarget:…\nSelector:…"; the title is what the outline shows
    /// and what `perform_action` accepts.
    static func actionName(_ action: String) -> String {
        guard action.hasPrefix("Name:") else { return action }
        return String(action.dropFirst(5).prefix { $0 != "\n" })
    }

    private static let attributeNames = [
        kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, "AXLabel", kAXPlaceholderValueAttribute,
        kAXValueAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXChildrenAttribute,
        kAXEnabledAttribute, kAXFocusedAttribute, kAXSelectedAttribute, "AXExpanded",
    ]

    let maxNodes: Int
    let maxDepth: Int
    private let maxVisits: Int
    private(set) var nodes: [TreeNode] = []
    private(set) var truncated = false
    private var visits = 0

    init(maxNodes: Int = 2500, maxDepth: Int = 60) {
        self.maxNodes = max(1, maxNodes)
        self.maxDepth = max(1, maxDepth)
        self.maxVisits = max(4000, self.maxNodes * 8)
    }

    mutating func build(window: AXUIElement, menuBar: AXUIElement?) {
        var siblingOrdinals: [String: Int] = [:]
        for child in window.children { walk(child, depth: 1, indent: 1, parentKey: "", inWebArea: false, ordinals: &siblingOrdinals) }
        if let menuBar, !truncated {
            var menuOrdinals: [String: Int] = [:]
            for item in menuBar.children {
                walk(item, depth: 1, indent: 1, parentKey: "menubar", inWebArea: false, ordinals: &menuOrdinals, recurse: false)
            }
        }
    }

    /// Walks from an element already seen in a full walk, reusing its key so indexes match that walk.
    mutating func build(subtreeOf root: AXUIElement, key: String, inWebArea: Bool) {
        var ordinals: [String: Int] = [:]
        walk(root, depth: 1, indent: 0, parentKey: "", inWebArea: inWebArea, ordinals: &ordinals, knownKey: key)
    }

    private mutating func walk(_ element: AXUIElement, depth: Int, indent: Int, parentKey: String,
                               inWebArea: Bool, ordinals: inout [String: Int], recurse: Bool = true, knownKey: String? = nil) {
        guard nodes.count < maxNodes, visits < maxVisits else {
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

        let states: [String: Bool?] = ["enabled": (values[9] as? NSNumber)?.boolValue,
            "focused": (values[10] as? NSNumber)?.boolValue, "selected": (values[11] as? NSNumber)?.boolValue,
            "expanded": (values[12] as? NSNumber)?.boolValue]
        let key: String
        if let knownKey {
            key = knownKey
        } else {
            let stem = "\(role):\(label.prefix(40))"
            let ordinal = ordinals[stem, default: 0]
            ordinals[stem] = ordinal + 1
            key = "\(parentKey)/\(stem)#\(ordinal)"
        }

        var childIndent = indent
        if shown {
            var frame: CGRect?
            if indexed, let position = AX.point(values[6]), let size = AX.size(values[7]) {
                frame = CGRect(origin: position, size: size)
            }
            nodes.append(TreeNode(indent: indent, role: role, label: label, value: value, frame: frame,
                                  indexed: indexed, element: element, inWebArea: inWebArea, key: key, actions: actions, states: states))
            childIndent += 1
        }

        let children = values[8] as? [AXUIElement] ?? []
        guard recurse, !children.isEmpty else { return }
        guard depth < maxDepth else {
            truncated = true
            return
        }
        let childInWebArea = inWebArea || role == "AXWebArea"
        var childOrdinals: [String: Int] = [:]
        for child in children {
            walk(child, depth: depth + 1, indent: childIndent, parentKey: key, inWebArea: childInWebArea, ordinals: &childOrdinals)
            if truncated { return }
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

/// Assigns each element a number that stays the same across snapshots of the same window, as long as
/// the element keeps its place in the tree. Stable numbers keep diffs small and let an index from an
/// earlier snapshot still resolve.
final class IndexTable {
    private var byKey: [String: Int] = [:]
    private var next = 0

    func index(forKey key: String) -> Int {
        if let existing = byKey[key] { return existing }
        let index = next
        next += 1
        byKey[key] = index
        return index
    }

    /// Drops keys no longer present so the numbering does not grow without bound over a long session.
    func retain(keys: Set<String>) {
        byKey = byKey.filter { keys.contains($0.key) }
    }
}

enum TreeRenderer {
    struct Output {
        var text: String
        var lines: [String]
        var elements: [Int: SnapshotElement]
        var indexedCount: Int
        var truncated: Bool
        var structured: [ElementInfo]

        /// Appends "focused:" and "selected text:" lines for the app's focused element. They join the
        /// line set diffs compare, so a focus or selection change shows up as a -/+ pair. Costs one
        /// multi-attribute AX call; the focused element is matched to an index without further IPC.
        mutating func appendOrientation(focused: AXUIElement?) {
            guard let focused else { return }
            let values = focused.attributes([
                kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, "AXLabel", kAXPlaceholderValueAttribute,
                kAXSelectedTextAttribute,
            ])
            let role = values[0] as? String ?? "AXUnknown"
            let label = values[1...4].lazy.compactMap { $0 as? String }.first { !$0.isEmpty } ?? ""
            let index = elements.first { CFEqual($0.value.element, focused) }?.key
            lines.append("focused: \(index.map { "[\($0)] " } ?? "")\(role) \"\(TreeBuilder.escape(label, limit: 120))\"")
            if let selected = AX.text(values[5]), !selected.isEmpty {
                lines.append("selected text: \"\(TreeBuilder.escape(selected, limit: 200))\"")
            }
            text = lines.joined(separator: "\n")
        }
    }

    /// Renders the window header plus every node, numbering indexed elements from `table`.
    static func full(title: String, size: CGSize, windowOrigin: CGPoint, nodes: [TreeNode], table: IndexTable, maxElements: Int) -> Output {
        let header = "window \"\(TreeBuilder.escape(title, limit: 200))\" (0,0 \(Int(size.width.rounded()))x\(Int(size.height.rounded())))"
        return render(header: header, nodes: nodes, baseIndent: 0, windowOrigin: windowOrigin, table: table, maxElements: maxElements)
    }

    /// Renders a subtree walked by `TreeBuilder.build(subtreeOf:)`; the root line carries index `scope`.
    static func scoped(scope: Int, windowOrigin: CGPoint, nodes: [TreeNode], table: IndexTable, maxElements: Int) -> Output {
        render(header: "scope [\(scope)]", nodes: nodes, baseIndent: 0, windowOrigin: windowOrigin, table: table, maxElements: maxElements)
    }

    /// Frames are printed window-local, so they line up with screenshot pixels.
    private static func render(header: String, nodes: [TreeNode], baseIndent: Int, windowOrigin: CGPoint,
                               table: IndexTable, maxElements: Int) -> Output {
        var lines = [header]
        var elements: [Int: SnapshotElement] = [:]
        var structured: [ElementInfo] = []
        var indexed = 0
        var truncated = false
        for node in nodes {
            if indexed >= maxElements {
                truncated = true
                break
            }
            var line = String(repeating: "  ", count: max(0, node.indent - baseIndent))
            if node.indexed {
                let index = table.index(forKey: node.key)
                elements[index] = SnapshotElement(element: node.element, role: node.role, inWebArea: node.inWebArea, key: node.key)
                let bounds = node.frame.map { Rect(x: $0.minX-windowOrigin.x, y: $0.minY-windowOrigin.y, width: $0.width, height: $0.height) }
                structured.append(ElementInfo(index: index, role: node.role, name: node.label, value: node.value.map { String($0.prefix(300)) }, bounds: bounds, actions: node.actions, states: node.states, valueLength: node.value?.utf16.count))
                indexed += 1
                line += "[\(index)] \(node.role) \"\(TreeBuilder.escape(node.label, limit: 120))\""
            } else {
                line += node.role
                if !node.label.isEmpty { line += " \"\(TreeBuilder.escape(node.label, limit: 120))\"" }
            }
            if let value = node.value, !value.isEmpty || TreeBuilder.editableRoles.contains(node.role) {
                line += " value=\"\(TreeBuilder.escape(value, limit: 300))\""
            }
            if node.indexed, let frame = node.frame {
                line += " (\(Int((frame.minX - windowOrigin.x).rounded())),\(Int((frame.minY - windowOrigin.y).rounded()))"
                    + " \(Int(frame.width.rounded()))x\(Int(frame.height.rounded())))"
            }
            if node.indexed {
                for name in node.states.keys.sorted() {
                    if let value = node.states[name] ?? nil, value == (name != "enabled") {
                        line += " \(name)=\(value)"
                    }
                }
                let secondary = node.actions.filter { !TreeBuilder.obviousActions.contains($0) }
                if !secondary.isEmpty {
                    let names = secondary.map { TreeBuilder.escape(TreeBuilder.actionName($0), limit: 60) }
                    line += " actions=[\(names.joined(separator: ","))]"
                }
            }
            lines.append(line)
        }
        return Output(text: lines.joined(separator: "\n"), lines: lines, elements: elements, indexedCount: indexed, truncated: truncated, structured: structured)
    }

    /// A compact line diff of `current` against `previous`. Because indexes are stable, unchanged
    /// elements render identically and drop out, leaving only added (+) and removed (-) lines.
    /// Returns nil when the diff would not be meaningfully smaller than the full tree.
    static func diff(previous: [String], current: [String]) -> String? {
        var head = 0
        while head < previous.count, head < current.count, previous[head] == current[head] { head += 1 }
        var tail = 0
        while tail < previous.count - head, tail < current.count - head,
              previous[previous.count - 1 - tail] == current[current.count - 1 - tail] { tail += 1 }
        let old = Array(previous[head..<(previous.count - tail)])
        let new = Array(current[head..<(current.count - tail)])
        if old.isEmpty && new.isEmpty { return "(no changes)" }
        // The LCS table is old×new cells; past this the change is a different screen, not an edit.
        guard old.count * new.count <= 250_000 else { return nil }

        var result: [String] = []
        var oi = 0, ni = 0
        for line in longestCommonSubsequence(old, new) {
            while old[oi] != line { result.append("-" + old[oi]); oi += 1 }
            while new[ni] != line { result.append("+" + new[ni]); ni += 1 }
            oi += 1
            ni += 1
        }
        while oi < old.count { result.append("-" + old[oi]); oi += 1 }
        while ni < new.count { result.append("+" + new[ni]); ni += 1 }
        guard Double(result.count) < Double(current.count) * 0.6 else { return nil }
        return result.joined(separator: "\n")
    }

    private static func longestCommonSubsequence(_ a: [String], _ b: [String]) -> [String] {
        if a.isEmpty || b.isEmpty { return [] }
        var table = [[Int]](repeating: [Int](repeating: 0, count: b.count + 1), count: a.count + 1)
        for i in stride(from: a.count - 1, through: 0, by: -1) {
            for j in stride(from: b.count - 1, through: 0, by: -1) {
                table[i][j] = a[i] == b[j] ? table[i + 1][j + 1] + 1 : max(table[i + 1][j], table[i][j + 1])
            }
        }
        var result: [String] = []
        var i = 0, j = 0
        while i < a.count, j < b.count {
            if a[i] == b[j] {
                result.append(a[i]); i += 1; j += 1
            } else if table[i + 1][j] >= table[i][j + 1] {
                i += 1
            } else {
                j += 1
            }
        }
        return result
    }
}
