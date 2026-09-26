// Click-delivery mode for e2e: a background window with targets that treat the first click into an inactive window
// differently, recording what each one did, so a test can require exactly one action per click or wheel tick.
//   usage: sc-focus-probe --clicks --state <path> [--regular] [--origin x,y]
// Targets: "first_mouse" (a custom view that accepts the first click), "plain" (a custom view that does not, the
// AppKit default, and handles the wheel itself), "button" (a standard push button), "text" (an editable text view) and
// "list" (a standard scroll view). Writes {"pid","window","targets","first_mouse","plain","plain_wheel","button",
// "text_caret","list_wheel","list_offset","active","key"} atomically at launch and after every event; target rects are
// window-local points (top-left origin), text_caret is the caret's character offset, *_wheel count wheel events.
// It never activates and stays out of the Dock; like --canvas it opens at --origin, else behind other windows.
import AppKit

private final class CountingView: NSView {
    let firstMouse: Bool
    var count = 0
    var wheel = 0
    var onChange: (() -> Void)?

    init(firstMouse: Bool) {
        self.firstMouse = firstMouse
        super.init(frame: .zero)
    }

    required init?(coder: NSCoder) { nil }

    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { firstMouse }

    override func draw(_ dirtyRect: NSRect) {
        (firstMouse ? NSColor.systemTeal : NSColor.systemPurple).withAlphaComponent(0.35).setFill()
        bounds.fill()
    }

    override func mouseDown(with event: NSEvent) {
        count += 1
        onChange?()
    }

    override func scrollWheel(with event: NSEvent) {
        wheel += 1
        onChange?()
    }
}

private final class CountingScrollView: NSScrollView {
    var wheel = 0
    var onChange: (() -> Void)?

    override func scrollWheel(with event: NSEvent) {
        wheel += 1
        super.scrollWheel(with: event)
        onChange?()
    }
}

final class Clicks: NSObject, NSApplicationDelegate, NSTextViewDelegate {
    private let statePath: String?
    private var window: NSWindow!
    private let firstMouse = CountingView(firstMouse: true)
    private let plain = CountingView(firstMouse: false)
    private var button: NSButton!
    private var text: NSTextView!
    private let list = CountingScrollView()
    private var presses = 0

    private let origin: CGPoint?

    init(statePath: String?, origin: CGPoint?) {
        self.statePath = statePath
        self.origin = origin
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .aqua)
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 480, height: 320))
        firstMouse.frame = NSRect(x: 20, y: 120, width: 200, height: 80)
        plain.frame = NSRect(x: 260, y: 120, width: 200, height: 80)
        for view in [firstMouse, plain] {
            view.onChange = { [unowned self] in write() }
            content.addSubview(view)
        }
        button = NSButton(title: "Press", target: self, action: #selector(pressed))
        button.frame = NSRect(x: 20, y: 60, width: 120, height: 32)
        content.addSubview(button)
        text = NSTextView(frame: NSRect(x: 180, y: 20, width: 280, height: 72))
        text.string = "alpha beta gamma"
        text.font = NSFont.systemFont(ofSize: 18)
        text.delegate = self
        content.addSubview(text)
        list.frame = NSRect(x: 20, y: 220, width: 440, height: 84)
        list.hasVerticalScroller = true
        let rows = NSTextView(frame: NSRect(x: 0, y: 0, width: 420, height: 84))
        rows.isEditable = false
        rows.isVerticallyResizable = true
        rows.string = (1...200).map { "Row \($0)" }.joined(separator: "\n")
        list.documentView = rows
        list.onChange = { [unowned self] in write() }
        content.addSubview(list)

        let rect = fixtureRect(NSSize(width: 480, height: 320), origin: origin)
        window = NSWindow(contentRect: rect, styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "SC Click Probe"
        window.contentView = content
        orderIn(window, content: rect, front: false)
        for name in [NSApplication.didBecomeActiveNotification, NSApplication.didResignActiveNotification,
                     NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification] {
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [unowned self] _ in write() }
        }
        write()
    }

    @objc private func pressed() {
        presses += 1
        write()
    }

    func textViewDidChangeSelection(_ notification: Notification) { write() }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    private func rect(_ view: NSView) -> [String: Double] {
        let r = view.convert(view.bounds, to: nil)
        return ["x": r.minX, "y": window.frame.height - r.maxY, "width": r.width, "height": r.height]
    }

    private func write() {
        guard let statePath, window != nil else { return }
        let state: [String: Any] = [
            "pid": Int(getpid()),
            "window": window.windowNumber,
            "targets": [
                "first_mouse": rect(firstMouse), "plain": rect(plain), "button": rect(button), "text": rect(text),
                "list": rect(list),
            ],
            "first_mouse": firstMouse.count,
            "plain": plain.count,
            "plain_wheel": plain.wheel,
            "button": presses,
            "text_caret": text.selectedRange().location,
            "list_wheel": list.wheel,
            "list_offset": list.contentView.bounds.origin.y,
            "active": NSApp.isActive,
            "key": window.isKeyWindow,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]) else { return }
        try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
    }
}
