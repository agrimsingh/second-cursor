// Fixture for e2e/expect.py: a window whose text changes a fixed time after a button press, the way an app's UI can
// lag the input that caused it. Built into a minimal .app by expect.py and launched with launch_app.
// It opens on the test instance's agent display when there is one: a window that opens on a desktop Space the user is
// not viewing (they are in a full-screen app) cannot be reached through Accessibility, so launch_app could not move it.
// With SCPlainPlacement in its Info.plist it opens where AppKit puts it instead, like an app with no remembered frame
// (e2e/run.py launch_on_hidden_space).
import AppKit

/// SCDisplayPrivate.m creates the agent display with this vendor and model.
func agentDisplayFrame() -> NSRect? {
    NSScreen.screens.first { screen in
        let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
        return CGDisplayVendorNumber(id) == 0xA6E7 && CGDisplayModelNumber(id) == 0x0001
    }?.frame
}

/// AppKit moves a new window whose rect lies off the main screen onto the user's screen, so the window stays
/// transparent until it has been moved to `target`.
func orderIn(_ window: NSWindow, at target: NSRect) {
    let primaryHeight = NSScreen.screens.first?.frame.maxY ?? 0
    let quartzY = primaryHeight - target.maxY
    window.alphaValue = 0
    window.orderFront(nil)
    var attempts = 0
    func settle() {
        let info = CGWindowListCopyWindowInfo(.optionIncludingWindow, CGWindowID(window.windowNumber)) as? [[String: Any]]
        let bounds = (info?.first?[kCGWindowBounds as String] as? NSDictionary).flatMap {
            CGRect(dictionaryRepresentation: $0 as CFDictionary)
        }
        if let bounds, abs(bounds.minX - target.minX) < 1, abs(bounds.minY - quartzY) < 1 {
            window.alphaValue = 1
            return
        }
        attempts += 1
        guard attempts <= 40 else { return }
        // AppKit skips a setFrame to the frame it believes the window already has.
        window.setFrameOrigin(NSPoint(x: target.minX + 1, y: target.minY))
        window.setFrame(target, display: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05, execute: settle)
    }
    settle()
}

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
        let plain = Bundle.main.object(forInfoDictionaryKey: "SCPlainPlacement") as? Bool == true
        let agent = plain ? nil : agentDisplayFrame()
        let content = agent.map { NSRect(x: $0.minX + 60, y: $0.maxY - 100 - 150, width: 540, height: 150) }
            ?? NSRect(x: 80, y: 80, width: 540, height: 150)
        window = NSWindow(contentRect: content, styleMask: [.titled], backing: .buffered, defer: false)
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
        if agent != nil {
            orderIn(window, at: window.frameRect(forContentRect: content))
        } else {
            window.orderFront(nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let fixture = Fixture()
app.setActivationPolicy(.regular)
app.delegate = fixture
app.run()
