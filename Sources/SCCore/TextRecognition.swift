import CoreGraphics
import Foundation
import SCShared
import Vision

/// A line of text recognized on a window's pixels; `frame` is in window-local points.
struct RecognizedText: Equatable {
    let text: String
    let frame: CGRect
}

/// What merging recognized text into an outline needs from the read that rendered it. Lines 1...`shown` of that
/// outline render `nodes[0..<shown]` (line 0 is the header); `nodes` runs past `shown` when the outline was cut.
struct OutlineLayout {
    let nodes: [TreeNode]
    let shown: Int
    let origin: CGPoint
    /// Whole-window reads treat the window as the root container: its title covers the title bar, and text inside no
    /// element goes under it. Scoped reads pass nil and drop text outside the scope.
    let window: (title: String, size: CGSize)?
}

/// On-device text recognition (Apple Vision, no network) for windows that draw text accessibility does not expose.
enum TextRecognition {
    /// Vision blocks for hundreds of milliseconds, so it stays off Swift's cooperative pool; serial, because requests
    /// queued behind the first use's model compile would otherwise each hold a blocked thread.
    private static let queue = DispatchQueue(label: "dev.secondcursor.text-recognition")

    /// The first recognition on a Mac waits minutes for the Neural Engine to compile Vision's model, far past any
    /// call deadline. Past `budget` the read goes on without text; the running recognition finishes in the background
    /// and the compiled model is cached for later calls.
    static func recognize(_ wid: CGWindowID, budget: TimeInterval) async throws -> [RecognizedText] {
        let (image, points) = try await Capture.image(wid, maxEdge: nil)
        return try await withCheckedThrowingContinuation { continuation in
            let lock = NSLock()
            var pending: CheckedContinuation<[RecognizedText], Error>? = continuation
            func finish(_ result: Result<[RecognizedText], Error>) {
                lock.lock()
                let waiting = pending
                pending = nil
                lock.unlock()
                waiting?.resume(with: result)
            }
            queue.async {
                lock.lock()
                let abandoned = pending == nil
                lock.unlock()
                if !abandoned { finish(Result { try recognize(image, points: points) }) }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + budget) {
                finish(.failure(SCError.actionFailed(
                    "Text recognition did not finish in \(Int(budget)) s; the first use on a Mac prepares an on-device "
                        + "model, which can take a few minutes. The outline has no recognized text; try ocr again later.")))
            }
        }
    }

    private static func recognize(_ image: CGImage, points: CGSize) throws -> [RecognizedText] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        return (request.results ?? []).compactMap { observation in
            guard let text = observation.topCandidates(1).first?.string.trimmingCharacters(in: .whitespacesAndNewlines),
                  !text.isEmpty
            else { return nil }
            // Normalized to the image, origin bottom-left.
            let box = observation.boundingBox
            return RecognizedText(text: text, frame: CGRect(
                x: box.minX * points.width, y: (1 - box.maxY) * points.height,
                width: box.width * points.width, height: box.height * points.height))
        }
    }

    /// Merges `texts` into an outline rendered with `layout`. Text that repeats the label, value or title of an
    /// element it overlaps is dropped. The rest goes on the line of the deepest indexed element containing its
    /// center as `ocr="…"`, or else becomes a `text "…" (x,y wxh)` line, without an index, at the end of the deepest
    /// shown container holding it. Returns the merged lines and keys (frame-free, for diffs) and how many texts were
    /// merged.
    static func merge(_ texts: [RecognizedText], lines: [String], keys: [String],
                      layout: OutlineLayout) -> (lines: [String], keys: [String], count: Int) {
        func local(_ frame: CGRect) -> CGRect { frame.offsetBy(dx: -layout.origin.x, dy: -layout.origin.y) }
        // Every walked element, the ones past a cut too: their text is in the accessibility tree either way.
        var labelled: [(frame: CGRect, texts: [String])] = layout.nodes.compactMap { node in
            guard let frame = node.frame else { return nil }
            let texts = [node.label, node.value ?? ""].map(padded).filter { $0 != "  " }
            return texts.isEmpty ? nil : (local(frame), texts)
        }
        if let window = layout.window {
            labelled.append((CGRect(origin: .zero, size: window.size), [padded(window.title)]))
        }
        /// Whole words only; a text area's value holds each recognized line of it. The texts a recognized text overlaps
        /// may also cover all of it but a stray glyph or two, such as the chevron beside a document's title read as "v".
        func duplicated(_ text: RecognizedText) -> Bool {
            var rest = padded(text.text)
            var matched = false
            for entry in labelled where entry.frame.intersects(text.frame) {
                for known in entry.texts {
                    if known.contains(rest) { return true }
                    let remaining = rest.replacingOccurrences(of: known, with: " ")
                    if remaining != rest {
                        matched = true
                        rest = remaining
                    }
                }
            }
            return matched && rest.unicodeScalars.filter(CharacterSet.alphanumerics.contains).count <= 2
        }

        let shown = Array(layout.nodes.prefix(layout.shown)).map { (node: $0, frame: $0.frame.map(local)) }
        func deepest(containing point: CGPoint, indexed: Bool) -> Int? {
            var best: Int?
            for (i, entry) in shown.enumerated() where entry.node.indexed == indexed {
                guard let frame = entry.frame, frame.contains(point) else { continue }
                if let b = best, let other = shown[b].frame, entry.node.indent < shown[b].node.indent
                    || (entry.node.indent == shown[b].node.indent && frame.width * frame.height > other.width * other.height) {
                    continue
                }
                best = i
            }
            return best
        }

        var attached: [Int: [RecognizedText]] = [:]
        // Keyed by node position; -1 is the window itself.
        var contained: [Int: [RecognizedText]] = [:]
        var count = 0
        for text in texts where !duplicated(text) {
            let center = CGPoint(x: text.frame.midX, y: text.frame.midY)
            if let element = deepest(containing: center, indexed: true) {
                attached[element, default: []].append(text)
            } else if let container = deepest(containing: center, indexed: false) {
                contained[container, default: []].append(text)
            } else if layout.window != nil {
                contained[-1, default: []].append(text)
            } else {
                continue
            }
            count += 1
        }

        func readingOrder(_ texts: [RecognizedText]) -> [RecognizedText] {
            texts.sorted { ($0.frame.minY, $0.frame.minX) < ($1.frame.minY, $1.frame.minX) }
        }
        var inserted: [Int: [(line: String, key: String)]] = [:]
        // Inner containers first, so when several end on the same line their text stays at its own depth.
        for container in contained.keys.sorted(by: >) {
            var last = layout.shown
            var indent = 1
            if container >= 0 {
                last = container
                while last + 1 < layout.shown, layout.nodes[last + 1].indent > layout.nodes[container].indent { last += 1 }
                last += 1
                indent = layout.nodes[container].indent + 1
            }
            for text in readingOrder(contained[container] ?? []) {
                let key = String(repeating: "  ", count: indent) + "text \"\(TreeBuilder.escape(text.text, limit: 200))\""
                let f = text.frame
                let frame = " (\(Int(f.minX.rounded())),\(Int(f.minY.rounded())) \(Int(f.width.rounded()))x\(Int(f.height.rounded())))"
                inserted[last, default: []].append((key + frame, key))
            }
        }

        var mergedLines: [String] = []
        var mergedKeys: [String] = []
        for (i, line) in lines.enumerated() {
            var suffix = ""
            if i >= 1, i <= layout.shown, let texts = attached[i - 1] {
                let joined = readingOrder(texts).map(\.text).joined(separator: " | ")
                suffix = " ocr=\"\(TreeBuilder.escape(joined, limit: 300))\""
            }
            mergedLines.append(line + suffix)
            mergedKeys.append(keys[i] + suffix)
            for entry in inserted[i] ?? [] {
                mergedLines.append(entry.line)
                mergedKeys.append(entry.key)
            }
        }
        return (mergedLines, mergedKeys, count)
    }

    /// Case- and whitespace-folded, padded with spaces so `contains` only matches whole words.
    private static func padded(_ text: String) -> String {
        " " + text.lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ") + " "
    }
}
