// Fixture for e2e/expect.py: a window whose text changes a fixed time after a button press, the way an app's UI can
// lag the input that caused it. Built into a minimal .app by expect.py and launched with launch_app.
import AppKit

/// NSButton holds its highlight for about 100 ms inside the accessibility press, so the delay would start before
/// the press returns; this button answers the press at once.
final class DelayedButton: NSTextField {
    var delay = 0
    var onChange: (() -> Void)?

    override func accessibilityRole() -> NSAccessibility.Role? { .button }
    override func accessibilityLabel() -> String? { stringValue }
    override func accessibilityPerformPress() -> Bool {
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(delay)) { [self] in onChange?() }
        return true
    }
}

/// Reports a new value without posting a notification, like apps whose accessibility layer never announces changes.
final class SilentText: NSView {
    var text = "silent: idle"

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .staticText }
    override func accessibilityValue() -> Any? { text }
}

final class Fixture: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let label = NSTextField(labelWithString: "state: idle")
    private let silent = SilentText(frame: NSRect(x: 20, y: 76, width: 320, height: 20))
    private var changes = 0
    private var silentChanges = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 540, height: 150), styleMask: [.titled],
                          backing: .buffered, defer: false)
        window.title = "SC Delayed"
        label.frame = NSRect(x: 20, y: 104, width: 320, height: 24)
        window.contentView?.addSubview(label)
        window.contentView?.addSubview(silent)
        let buttons: [(String, Int, () -> Void)] = [
            ("Change in 150 ms", 150, { [unowned self] in changes += 1; label.stringValue = "state: changed \(changes)" }),
            ("Change in 400 ms", 400, { [unowned self] in changes += 1; label.stringValue = "state: changed \(changes)" }),
            ("Silent change", 150, { [unowned self] in silentChanges += 1; silent.text = "silent: changed \(silentChanges)" }),
        ]
        for (i, (title, delay, change)) in buttons.enumerated() {
            let button = DelayedButton(labelWithString: title)
            button.delay = delay
            button.onChange = change
            button.frame = NSRect(x: 20 + i * 170, y: 30, width: 160, height: 32)
            window.contentView?.addSubview(button)
        }
        window.orderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let fixture = Fixture()
app.setActivationPolicy(.regular)
app.delegate = fixture
app.run()
