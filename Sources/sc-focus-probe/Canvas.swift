// Canvas mode for e2e/ocr.py: a custom-drawn window, like a DAW or a game, whose text accessibility cannot see.
//   usage: sc-focus-probe --canvas <word> --state <path>
// The canvas draws <word> and a few more at known places and exposes no accessibility children. Beside it sit an
// accessibility button whose caption is only drawn ("Loop", labelled "Transport") and a real label ("Tempo 128").
// Writes {"pid","window","words","button","label","clicks"} atomically to <path> at launch and after every click,
// with every rect and click in window-local points (top-left origin). A click turns "Stopped" into "Playing"; it does
// not toggle back, because Second Cursor's pixel clicks can reach an AppKit view twice.
// It never activates, stays out of the Dock, and opens small, behind other windows, for the test to move away.
import AppKit

private final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

private final class CanvasView: NSView {
    static let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: 22), .foregroundColor: NSColor.black,
    ]
    var words: [(text: String, origin: CGPoint)] = []
    var onClick: ((NSPoint) -> Void)?

    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.setFill()
        bounds.fill()
        for word in words { (word.text as NSString).draw(at: word.origin, withAttributes: Self.attributes) }
    }

    override func mouseDown(with event: NSEvent) { onClick?(event.locationInWindow) }

    func rect(_ index: Int) -> CGRect {
        CGRect(origin: words[index].origin, size: (words[index].text as NSString).size(withAttributes: Self.attributes))
    }
}

private final class DrawnButton: NSView {
    static let caption = "Loop"
    static let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: 20), .foregroundColor: NSColor.black,
    ]

    override var isFlipped: Bool { true }
    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .button }
    override func accessibilityLabel() -> String? { "Transport" }
    override func accessibilityPerformPress() -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(white: 0.85, alpha: 1).setFill()
        bounds.fill()
        let size = (Self.caption as NSString).size(withAttributes: Self.attributes)
        (Self.caption as NSString).draw(at: captionOrigin(size), withAttributes: Self.attributes)
    }

    func captionOrigin(_ size: CGSize) -> CGPoint {
        CGPoint(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2)
    }
}

final class Canvas: NSObject, NSApplicationDelegate {
    private let word: String
    private let statePath: String?
    private var window: NSWindow!
    private let canvas = CanvasView()
    private let button = DrawnButton()
    private var label: NSTextField!
    private var clicks: [[String: Double]] = []

    init(word: String, statePath: String?) {
        self.word = word
        self.statePath = statePath
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .aqua)
        let content = FlippedView(frame: NSRect(x: 0, y: 0, width: 460, height: 330))
        canvas.frame = content.bounds
        canvas.words = [
            (word, CGPoint(x: 24, y: 16)), ("Render", CGPoint(x: 300, y: 70)),
            ("Marker 42", CGPoint(x: 40, y: 124)), ("Stopped", CGPoint(x: 300, y: 278)),
        ]
        canvas.onClick = { [unowned self] point in
            clicks.append(["x": point.x, "y": window.frame.height - point.y])
            canvas.words[3].text = "Playing"
            canvas.needsDisplay = true
            write()
        }
        content.addSubview(canvas)
        button.frame = NSRect(x: 280, y: 170, width: 130, height: 44)
        content.addSubview(button)
        label = NSTextField(labelWithString: "Tempo 128")
        label.font = NSFont.systemFont(ofSize: 20)
        label.textColor = .black
        label.sizeToFit()
        label.setFrameOrigin(NSPoint(x: 24, y: 230))
        content.addSubview(label)

        // Bottom-left of the main screen, behind every other window, so it covers nothing the user is looking at.
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        window = NSWindow(contentRect: NSRect(x: screen.minX + 20, y: screen.minY + 20, width: 460, height: 330),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "SC Canvas Probe"
        window.contentView = content
        window.orderBack(nil)
        write()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// Window-local points, top-left origin, as Second Cursor reports frames.
    private func entry(_ text: String, _ rect: CGRect, in view: NSView) -> [String: Any] {
        let r = view.convert(rect, to: nil)
        return ["text": text, "x": r.minX, "y": window.frame.height - r.maxY, "width": r.width, "height": r.height]
    }

    private func write() {
        guard let statePath else { return }
        let captionSize = (DrawnButton.caption as NSString).size(withAttributes: DrawnButton.attributes)
        let state: [String: Any] = [
            "pid": Int(getpid()),
            "window": window.windowNumber,
            "words": canvas.words.indices.map { entry(canvas.words[$0].text, canvas.rect($0), in: canvas) },
            "button": entry(DrawnButton.caption, CGRect(origin: button.captionOrigin(captionSize), size: captionSize), in: button),
            "label": entry(label.stringValue, label.bounds, in: label),
            "clicks": clicks,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]) else { return }
        try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
    }
}
