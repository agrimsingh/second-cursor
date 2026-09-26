// Stand-in for the user's frontmost app in e2e/focus.py: one window whose text field is first
// responder, counting every way it can lose focus.
//   usage: sc-focus-probe --state <path> [--avoid x,y,w,h]
// Writes {"resign_active","resign_key","first_responder_changes","active","key","pid"} atomically to
// <path> at launch and on every change. Run it from a .app bundle so LaunchServices activates it.
// By default the window covers the main screen; --avoid (global points, top-left origin) makes it a small window
// in a corner of the screen clear of that rectangle, so a background window stays visible beside it.
// With --canvas <word> it is a custom-drawn background window instead (Canvas.swift, e2e/ocr.py).
import AppKit

final class ProbeWindow: NSWindow {
    var onFirstResponderChange: (() -> Void)?

    @discardableResult
    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        let before = firstResponder
        let changed = super.makeFirstResponder(responder)
        if changed, firstResponder !== before { onFirstResponderChange?() }
        return changed
    }
}

final class Probe: NSObject, NSApplicationDelegate {
    private let statePath: String?
    private let avoid: CGRect?
    private var window: ProbeWindow!
    private var resignActive = 0
    private var resignKey = 0
    private var firstResponderChanges = 0

    init(statePath: String?, avoid: CGRect?) {
        self.statePath = statePath
        self.avoid = avoid
    }

    /// The main screen's visible frame (covering background targets), or with `avoid`, the corner rectangle that
    /// overlaps it least.
    private func placement() -> NSRect {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        guard let avoid else { return screen }
        let primaryHeight = NSScreen.screens.first?.frame.maxY ?? screen.maxY
        let cocoaAvoid = NSRect(x: avoid.minX, y: primaryHeight - avoid.maxY, width: avoid.width, height: avoid.height)
        let size = NSSize(width: 360, height: 240)
        let corners = [
            NSPoint(x: screen.maxX - size.width, y: screen.minY), NSPoint(x: screen.minX, y: screen.minY),
            NSPoint(x: screen.maxX - size.width, y: screen.maxY - size.height), NSPoint(x: screen.minX, y: screen.maxY - size.height),
        ].map { NSRect(origin: $0, size: size) }
        func overlap(_ rect: NSRect) -> CGFloat {
            let shared = rect.intersection(cocoaAvoid)
            return shared.isNull ? 0 : shared.width * shared.height
        }
        return corners.min { overlap($0) < overlap($1) } ?? corners[0]
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let frame = placement()
        window = ProbeWindow(contentRect: frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "SC Focus Probe"
        window.setFrame(frame, display: true)
        let field = NSTextField(frame: NSRect(x: 40, y: window.contentLayoutRect.height - 80, width: 480, height: 28))
        field.placeholderString = "focus probe"
        window.contentView?.addSubview(field)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(field)
        window.onFirstResponderChange = { [unowned self] in
            firstResponderChanges += 1
            write()
        }

        let center = NotificationCenter.default
        center.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { [unowned self] _ in
            resignActive += 1
            write()
        }
        center.addObserver(forName: NSWindow.didResignKeyNotification, object: window, queue: .main) { [unowned self] _ in
            resignKey += 1
            write()
        }
        for name in [NSApplication.didBecomeActiveNotification, NSWindow.didBecomeKeyNotification] {
            center.addObserver(forName: name, object: nil, queue: .main) { [unowned self] _ in write() }
        }
        NSApp.activate(ignoringOtherApps: true)
        write()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    private func write() {
        guard let statePath else { return }
        let state: [String: Any] = [
            "resign_active": resignActive,
            "resign_key": resignKey,
            "first_responder_changes": firstResponderChanges,
            "active": NSApp.isActive,
            "key": window.isKeyWindow,
            "pid": Int(getpid()),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]) else { return }
        try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
    }
}

let arguments = CommandLine.arguments
func value(_ flag: String) -> String? {
    arguments.firstIndex(of: flag).flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
}
let statePath = value("--state")
let avoid = value("--avoid").flatMap { text -> CGRect? in
    let numbers = text.split(separator: ",").compactMap { Double($0) }
    return numbers.count == 4 ? CGRect(x: numbers[0], y: numbers[1], width: numbers[2], height: numbers[3]) : nil
}
let app = NSApplication.shared
let probe: NSApplicationDelegate
if let word = value("--canvas") {
    probe = Canvas(word: word, statePath: statePath)
    app.setActivationPolicy(.accessory)
} else {
    probe = Probe(statePath: statePath, avoid: avoid)
    app.setActivationPolicy(.regular)
}
app.delegate = probe
app.run()
