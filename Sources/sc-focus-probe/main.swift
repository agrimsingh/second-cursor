// Stand-in for the user's frontmost app in e2e/focus.py: one window whose text field is first
// responder, counting every way it can lose focus.
//   usage: sc-focus-probe --state <path>
// Writes {"resign_active","resign_key","first_responder_changes","active","key","pid"} atomically to
// <path> at launch and on every change. Run it from a .app bundle so LaunchServices activates it.
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
    private var window: ProbeWindow!
    private var resignActive = 0
    private var resignKey = 0
    private var firstResponderChanges = 0

    init(statePath: String?) {
        self.statePath = statePath
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Covers the main screen so background targets under it are fully occluded.
        let frame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
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
let statePath = arguments.firstIndex(of: "--state").flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
let app = NSApplication.shared
let probe = Probe(statePath: statePath)
app.setActivationPolicy(.regular)
app.delegate = probe
app.run()
