import Foundation
import SCShared

/// Cuts a full outline of 80 lines or more down by role alone, with no model and no goal: footers and side panels are
/// hidden, and of a long run of like items (rows, list items, links) only the first chunk is kept. Navigation, toolbars,
/// tabs, menus and dialogs are never cut: they are how an agent reaches everything else. Each run of hidden lines becomes
/// one `… N lines hidden (expand "r3"): <its first line>` line. The rules arm of e2e/jev_prune.py, which benchmarked
/// faster than whole outlines at the same accuracy.
enum OutlineCondenser {
    enum Style { case native, browser }

    struct Condensed {
        var outline: String
        var parts: [(id: Int, lines: [String])]
    }

    static let minimumLines = 80
    private static let chunkLines = 24

    private static let hiddenRoles: [Style: Set<Substring>] = [
        .browser: ["contentinfo", "complementary"],
        .native: [],
    ]
    private static let neverCut: [Style: Set<Substring>] = [
        .browser: ["navigation", "banner", "menubar", "menu", "listbox", "tablist", "toolbar", "dialog", "alertdialog"],
        .native: ["AXToolbar", "AXTabGroup", "AXSheet", "AXMenuBar", "AXMenu", "AXPopover"],
    ]

    /// Parts are numbered from `firstID`. Nil when the outline is too short or nothing would be hidden.
    static func condense(_ outline: String, style: Style, firstID: Int, tool: String) -> Condensed? {
        let page = Page(outline)
        guard page.lines.count >= minimumLines else { return nil }
        let keep = kept(page, style: style)
        let chunkAt = Dictionary(uniqueKeysWithValues: page.chunks.enumerated().map { ($1.start, (n: $0, stop: $1.stop)) })
        enum Entry { case line(String), run(Int) }
        var entries: [Entry] = []
        var runs: [(depth: Int, lines: [String])] = []
        var open = false
        var i = 0
        while i < page.lines.count {
            guard !page.structure.contains(i), let chunk = chunkAt[i] else {
                entries.append(.line(page.lines[i]))
                open = false
                i += 1
                continue
            }
            if keep.contains(chunk.n) {
                entries += page.lines[i..<chunk.stop].map { .line($0) }
                open = false
            } else {
                if !open || runs.last?.depth != page.depth[i] {
                    runs.append((page.depth[i], []))
                    entries.append(.run(runs.count - 1))
                    open = true
                }
                runs[runs.count - 1].lines += page.lines[i..<chunk.stop]
            }
            i = chunk.stop
        }
        var out: [String] = []
        var parts: [(id: Int, lines: [String])] = []
        for entry in entries {
            switch entry {
            case .line(let line): out.append(line)
            // Its "… 1 lines hidden" line would be longer than the line itself.
            case .run(let n) where runs[n].lines.count == 1: out += runs[n].lines
            case .run(let n):
                let run = runs[n], id = firstID + parts.count
                parts.append((id, run.lines))
                out.append(String(repeating: "  ", count: run.depth)
                    + "… \(run.lines.count) lines hidden (expand \"r\(id)\"): \(run.lines[0].drop { $0 == " " }.prefix(70))")
            }
        }
        guard !parts.isEmpty else { return nil }
        let hidden = parts.reduce(0) { $0 + $1.lines.count }
        let header = "(condensed: \(page.lines.count - hidden) of \(page.lines.count) lines shown; to read a hidden part, "
            + "call \(tool) with expand set to its id, or a list of ids)"
        return Condensed(outline: ([header] + out).joined(separator: "\n"), parts: parts)
    }

    private static func kept(_ page: Page, style: Style) -> Set<Int> {
        let hiddenRoles = hiddenRoles[style] ?? [], neverCut = neverCut[style] ?? []
        var listLike: [Int: Bool] = [:]
        var keep = Set<Int>(), cut = Set<Int>()
        for (n, chunk) in page.chunks.enumerated() {
            let roles = Set(chunk.ancestors.map { role(page.lines[$0]) } + [role(page.lines[chunk.start])])
            if !roles.isDisjoint(with: hiddenRoles) { continue }
            if let parent = chunk.ancestors.last, roles.isDisjoint(with: neverCut) {
                let like = listLike[parent] ?? isListLike(page, parent)
                listLike[parent] = like
                if like {
                    if cut.contains(parent) { continue }
                    cut.insert(parent)
                }
            }
            keep.insert(n)
        }
        return keep
    }

    /// Six or more children, at least 80% of them in one role.
    private static func isListLike(_ page: Page, _ i: Int) -> Bool {
        let roles = page.children(i).map { role(page.lines[$0]) }
        guard roles.count >= 6 else { return false }
        let most = Dictionary(roles.map { ($0, 1) }, uniquingKeysWith: +).values.max() ?? 0
        return Double(most) >= 0.8 * Double(roles.count)
    }

    /// The role word of `[12] AXButton "Save" …` or `[e34] button "Save"`.
    private static func role(_ line: String) -> Substring {
        var text = line.drop { $0 == " " }
        if text.hasPrefix("["), let close = text.firstIndex(of: "]") {
            text = text[text.index(after: close)...].drop { $0 == " " }
        }
        return text.prefix { $0 != " " }
    }

    /// An outline split so that every line is covered exactly once: a container too long to be one chunk keeps its own
    /// line as structure, and its children become chunks, consecutive small siblings grouped up to `chunkLines` lines.
    /// `ancestors` are the structure lines above a chunk, outermost first.
    private struct Page {
        let lines: [String]
        let depth: [Int]
        private(set) var end: [Int]
        private(set) var structure = Set<Int>()
        private(set) var chunks: [(start: Int, stop: Int, ancestors: [Int])] = []

        init(_ outline: String) {
            lines = outline.components(separatedBy: "\n")
            depth = lines.map { $0.prefix { $0 == " " }.count / 2 }
            end = Array(repeating: lines.count, count: lines.count)
            var stack: [Int] = []
            for i in lines.indices {
                while let last = stack.last, depth[last] >= depth[i] { end[stack.removeLast()] = i }
                stack.append(i)
            }
            var top = 0
            while top < lines.count {
                visit(top, [])
                top = end[top]
            }
        }

        func children(_ i: Int) -> [Int] {
            var out: [Int] = []
            var j = i + 1
            while j < end[i] {
                out.append(j)
                j = end[j]
            }
            return out
        }

        private mutating func visit(_ i: Int, _ ancestors: [Int]) {
            guard end[i] - i > OutlineCondenser.chunkLines else {
                chunks.append((i, end[i], ancestors))
                return
            }
            structure.insert(i)
            let inner = ancestors + [i]
            var group: (start: Int, stop: Int)?
            for child in children(i) {
                if end[child] - child > OutlineCondenser.chunkLines {
                    if let g = group { chunks.append((g.start, g.stop, inner)) }
                    group = nil
                    visit(child, inner)
                } else if let g = group, end[child] - g.start <= OutlineCondenser.chunkLines {
                    group = (g.start, end[child])
                } else {
                    if let g = group { chunks.append((g.start, g.stop, inner)) }
                    group = (child, end[child])
                }
            }
            if let g = group { chunks.append((g.start, g.stop, inner)) }
        }
    }
}

/// What each session's latest condensed read of a window or tab hid, for `expand`. Ids are unique across the server,
/// so one from an older read or another target can never name a different part.
final class HiddenParts: @unchecked Sendable {
    private struct Read {
        var parts: [Int: [String]]
        /// Ids this target's earlier reads handed out, newest last, so they can be refused as stale.
        var older: [ClosedRange<Int>]
    }

    private let lock = NSLock()
    private var reads: [String: Read] = [:]
    private var order: [String] = []
    private var nextID = 1
    private static let maxTargets = 64
    private static let maxOlder = 16

    /// The condensed outline, remembering what it hid; the outline itself when there is nothing to condense.
    func condense(_ outline: String, style: OutlineCondenser.Style, target: String, tool: String) -> String {
        let key = Self.key(target)
        lock.lock()
        defer { lock.unlock() }
        guard let condensed = OutlineCondenser.condense(outline, style: style, firstID: nextID, tool: tool) else {
            return outline
        }
        nextID += condensed.parts.count
        var older = reads[key]?.older ?? []
        if let previous = reads[key]?.parts.keys, let low = previous.min(), let high = previous.max() {
            older = Array((older + [low...high]).suffix(Self.maxOlder))
        }
        reads[key] = Read(parts: Dictionary(uniqueKeysWithValues: condensed.parts.map { ($0.id, $0.lines) }), older: older)
        order.removeAll { $0 == key }
        order.append(key)
        if order.count > Self.maxTargets { reads[order.removeFirst()] = nil }
        return condensed.outline
    }

    /// The hidden lines of each id, as they were read.
    func expand(_ ids: [String], target: String, noun: String) throws -> String {
        lock.lock()
        defer { lock.unlock() }
        let read = reads[Self.key(target)]
        return try ids.map { name in
            let id = Int(name.trimmingCharacters(in: .whitespaces).drop { $0 == "r" })
            if let id, let lines = read?.parts[id] {
                return "(hidden part r\(id), as it was when read)\n" + lines.joined(separator: "\n")
            }
            if let id, read?.older.contains(where: { $0.contains(id) }) == true {
                throw SCError.invalidArgument(
                    "\(name) is from an older read of this \(noun), whose hidden parts are gone; expand the ids of the "
                        + "latest condensed read, or read the \(noun) again")
            }
            throw SCError.invalidArgument(
                "No hidden part \(name) in the latest condensed read of this \(noun); ids come from its "
                    + "'… N lines hidden (expand \"r3\")' lines")
        }.joined(separator: "\n")
    }

    private static func key(_ target: String) -> String { "\(Execution.context.sessionID) \(target)" }
}

extension ToolRouter {
    /// The hidden parts `expand` names from `target`'s latest condensed read, or nil when the call has no `expand`.
    func expandedParts(_ args: Arguments, target: String, noun: String) throws -> (ids: [String], text: String)? {
        let ids = try args.strings("expand")
        guard !ids.isEmpty else { return nil }
        guard let hiddenParts else { throw SCError.invalidArgument("Nothing is hidden: outline condensing is off") }
        return (ids, try hiddenParts.expand(ids, target: target, noun: noun))
    }
}
