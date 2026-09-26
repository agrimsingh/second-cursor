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
    /// Global screen points. The outline prints it only for indexed elements.
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

    private static let fingerprintAttributes = [
        kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute, kAXPositionAttribute,
        kAXSizeAttribute, kAXChildrenAttribute, kAXEnabledAttribute, kAXSelectedAttribute,
    ]

    /// A hash of what an observation would show (roles, labels, values, states and frames of the first `maxNodes`
    /// elements in walk order), for telling when a UI has stopped changing. One multi-attribute call per element
    /// and no action lookups, about half the IPC of a real walk.
    static func fingerprint(window: AXUIElement, maxNodes: Int) -> Int {
        var hasher = Hasher()
        var pending = Array(window.children.reversed())
        var visited = 0
        while visited < maxNodes, let element = pending.popLast() {
            visited += 1
            let values = element.attributes(fingerprintAttributes)
            let role = values[0] as? String
            if let role, prunedRoles.contains(role) { continue }
            hasher.combine(role)
            hasher.combine(values[1] as? String)
            hasher.combine(values[2] as? String)
            hasher.combine(AX.text(values[3]))
            if let position = AX.point(values[4]) {
                hasher.combine(position.x)
                hasher.combine(position.y)
            }
            if let size = AX.size(values[5]) {
                hasher.combine(size.width)
                hasher.combine(size.height)
            }
            hasher.combine((values[7] as? NSNumber)?.boolValue)
            hasher.combine((values[8] as? NSNumber)?.boolValue)
            let children = values[6] as? [AXUIElement] ?? []
            hasher.combine(children.count)
            pending.append(contentsOf: children.reversed())
        }
        return hasher.finalize()
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
            if let position = AX.point(values[6]), let size = AX.size(values[7]) {
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
        /// Outline lines; `keys` holds the same lines without element frames, which is what diffs compare.
        private(set) var body: [String]
        private(set) var bodyKeys: [String]
        var elements: [Int: SnapshotElement]
        var indexedCount: Int
        var truncated: Bool
        var structured: [ElementInfo]
        /// Says what a truncated outline left out; always the outline's last line.
        var footer: String?

        init(lines: [String], keys: [String], elements: [Int: SnapshotElement], indexedCount: Int, truncated: Bool,
             structured: [ElementInfo], footer: String?) {
            body = lines
            bodyKeys = keys
            self.elements = elements
            self.indexedCount = indexedCount
            self.truncated = truncated
            self.structured = structured
            self.footer = footer
        }

        var lines: [String] { footer.map { body + [$0] } ?? body }
        var keys: [String] { footer.map { bodyKeys + [$0] } ?? bodyKeys }
        var text: String { lines.joined(separator: "\n") }

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
            append("focused: \(index.map { "[\($0)] " } ?? "")\(role) \"\(TreeBuilder.escape(label, limit: 120))\"")
            if let selected = AX.text(values[5]), !selected.isEmpty {
                append("selected text: \"\(TreeBuilder.escape(selected, limit: 200))\"")
            }
        }

        private mutating func append(_ line: String) {
            body.append(line)
            bodyKeys.append(line)
        }
    }

    /// The footer for a walk that hit its node or depth limit, where what was left out is unknown.
    static let walkLimitFooter = "… truncated: part of the window was too large or too deeply nested to read in one pass — "
        + "pass scope=<index or label> to read a region"

    /// Renders the window header plus every node, numbering indexed elements from `table`.
    static func full(title: String, size: CGSize, windowOrigin: CGPoint, nodes: [TreeNode], table: IndexTable, maxElements: Int) -> Output {
        let header = "window \"\(TreeBuilder.escape(title, limit: 200))\" (0,0 \(Int(size.width.rounded()))x\(Int(size.height.rounded())))"
        return render(header: header, nodes: nodes, baseIndent: 0, windowOrigin: windowOrigin, table: table, maxElements: maxElements)
    }

    /// Renders a subtree walked by `TreeBuilder.build(subtreeOf:)`. `scope` names the root as the caller
    /// addressed it: `[12]` or `"Clip Detail"`.
    static func scoped(scope: String, windowOrigin: CGPoint, nodes: [TreeNode], table: IndexTable, maxElements: Int) -> Output {
        render(header: "scope \(scope)", nodes: nodes, baseIndent: 0, windowOrigin: windowOrigin, table: table, maxElements: maxElements)
    }

    /// Frames are printed window-local, so they line up with screenshot pixels.
    private static func render(header: String, nodes: [TreeNode], baseIndent: Int, windowOrigin: CGPoint,
                               table: IndexTable, maxElements: Int) -> Output {
        var lines = [header]
        var keys = [header]
        var elements: [Int: SnapshotElement] = [:]
        var structured: [ElementInfo] = []
        var indexed = 0
        var cut: Int?
        for (position, node) in nodes.enumerated() {
            if indexed >= maxElements {
                cut = position
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
            var key = line
            if node.indexed, let frame = node.frame {
                line += " (\(Int((frame.minX - windowOrigin.x).rounded())),\(Int((frame.minY - windowOrigin.y).rounded()))"
                    + " \(Int(frame.width.rounded()))x\(Int(frame.height.rounded())))"
            }
            if node.indexed {
                var tail = ""
                for name in node.states.keys.sorted() {
                    if let value = node.states[name] ?? nil, value == (name != "enabled") {
                        tail += " \(name)=\(value)"
                    }
                }
                let secondary = node.actions.filter { !TreeBuilder.obviousActions.contains($0) }
                if !secondary.isEmpty {
                    let names = secondary.map { TreeBuilder.escape(TreeBuilder.actionName($0), limit: 60) }
                    tail += " actions=[\(names.joined(separator: ","))]"
                }
                line += tail
                key += tail
            }
            lines.append(line)
            keys.append(key)
        }
        return Output(
            lines: lines, keys: keys, elements: elements, indexedCount: indexed, truncated: cut != nil, structured: structured,
            footer: cut.map { truncationFooter(nodes: nodes, cut: $0, baseIndent: baseIndent, table: table, maxElements: maxElements) })
    }

    /// Names the top-level containers (depth 1 and 2) whose content fell after the cut, with how many elements of
    /// each were left out, so the reader can scope to them by label or raise the limit. A depth-1 container that
    /// was cut entirely is named itself; one that is partly shown is broken down into its depth-2 children.
    private static func truncationFooter(nodes: [TreeNode], cut: Int, baseIndent: Int, table: IndexTable,
                                         maxElements: Int) -> String {
        var ancestors: [Int] = []
        var sections: [Int] = []
        var counts: [Int: Int] = [:]
        var others = 0
        var hiddenLines = 0
        for (position, node) in nodes.enumerated() {
            let depth = node.indent - baseIndent
            while let last = ancestors.last, nodes[last].indent - baseIndent >= depth { ancestors.removeLast() }
            ancestors.append(position)
            guard position >= cut else { continue }
            hiddenLines += 1
            let top = ancestors.filter { (1...2).contains(nodes[$0].indent - baseIndent) }
            guard let outer = top.first else { continue }
            let section = outer >= cut || top.count == 1 ? outer : top[1]
            let hasChildren = section + 1 < nodes.count && nodes[section + 1].indent > nodes[section].indent
            guard hasChildren else {
                if node.indexed { others += 1 }
                continue
            }
            if counts[section] == nil {
                sections.append(section)
                counts[section] = 0
            }
            if node.indexed { counts[section, default: 0] += 1 }
        }
        func elements(_ count: Int) -> String { count == 1 ? "1 element" : "\(count) elements" }
        var parts: [String] = []
        for section in sections {
            let count = counts[section] ?? 0
            guard parts.count < 8 else {
                others += count
                continue
            }
            let node = nodes[section]
            var name = node.role
            if !node.label.isEmpty {
                name = "\"\(TreeBuilder.escape(node.label, limit: 60))\""
            } else if node.indexed, section < cut {
                name = "[\(table.index(forKey: node.key))] \(node.role)"
            }
            parts.append(count > 0 ? "\(name) (\(elements(count)))" : name)
        }
        if others > 0 { parts.append("\(elements(others)) elsewhere") }
        if parts.isEmpty { parts.append("\(hiddenLines) more lines") }
        return "… truncated at \(elements(maxElements)); not shown: \(parts.joined(separator: ", "))"
            + " — pass max_elements (up to 3000) or scope=<index or label>"
    }

    /// A compact line diff of the current outline against the previous one. Lines are compared without their
    /// frames: because indexes are stable, unchanged elements render identically and drop out, leaving added (+)
    /// and removed (-) lines, printed with current frames. Elements whose only change is their frame (a scroll or
    /// resize) are not resent but counted on one closing `~` line. Returns nil when the content changes would not
    /// be meaningfully smaller than the full tree.
    static func diff(previous: [String], previousKeys: [String], current: [String], currentKeys: [String]) -> String? {
        var moved = 0
        var head = 0
        while head < previousKeys.count, head < currentKeys.count, previousKeys[head] == currentKeys[head] {
            if previous[head] != current[head] { moved += 1 }
            head += 1
        }
        var tail = 0
        while tail < previousKeys.count - head, tail < currentKeys.count - head,
              previousKeys[previousKeys.count - 1 - tail] == currentKeys[currentKeys.count - 1 - tail] {
            if previous[previous.count - 1 - tail] != current[current.count - 1 - tail] { moved += 1 }
            tail += 1
        }
        let old = head..<(previousKeys.count - tail)
        let new = head..<(currentKeys.count - tail)
        // The LCS table is old×new cells; past this the change is a different screen, not an edit.
        guard old.count * new.count <= 250_000 else { return nil }

        var result: [String] = []
        var oi = old.lowerBound, ni = new.lowerBound
        for (i, j) in commonLines(previousKeys[old], currentKeys[new]) {
            while oi < i { result.append("-" + previous[oi]); oi += 1 }
            while ni < j { result.append("+" + current[ni]); ni += 1 }
            if previous[i] != current[j] { moved += 1 }
            oi += 1
            ni += 1
        }
        while oi < old.upperBound { result.append("-" + previous[oi]); oi += 1 }
        while ni < new.upperBound { result.append("+" + current[ni]); ni += 1 }
        guard Double(result.count) < Double(current.count) * 0.6 else { return nil }
        if moved > 0 {
            result.append("~ \(moved) element\(moved == 1 ? "" : "s") moved (scroll or resize); frames above are current; "
                + "call get_window_state full=true if you need all frames")
        }
        return result.isEmpty ? "(no changes)" : result.joined(separator: "\n")
    }

    /// Index pairs of a longest common subsequence of `a` and `b`, in the slices' own indices.
    private static func commonLines(_ a: ArraySlice<String>, _ b: ArraySlice<String>) -> [(Int, Int)] {
        if a.isEmpty || b.isEmpty { return [] }
        let a = Array(a.enumerated().map { ($0.offset + a.startIndex, $0.element) })
        let b = Array(b.enumerated().map { ($0.offset + b.startIndex, $0.element) })
        var table = [[Int]](repeating: [Int](repeating: 0, count: b.count + 1), count: a.count + 1)
        for i in stride(from: a.count - 1, through: 0, by: -1) {
            for j in stride(from: b.count - 1, through: 0, by: -1) {
                table[i][j] = a[i].1 == b[j].1 ? table[i + 1][j + 1] + 1 : max(table[i + 1][j], table[i][j + 1])
            }
        }
        var result: [(Int, Int)] = []
        var i = 0, j = 0
        while i < a.count, j < b.count {
            if a[i].1 == b[j].1 {
                result.append((a[i].0, b[j].0)); i += 1; j += 1
            } else if table[i + 1][j] >= table[i][j + 1] {
                i += 1
            } else {
                j += 1
            }
        }
        return result
    }
}
