// A deliberately faulty AX editor: setting a selection returns successfully,
// but its selected range never changes. Used only by e2e/text_batch.py to prove
// that a batch stops before typing when selection readback is unconfirmed.
import AppKit

final class UnconfirmedSelectionEditor: NSTextView {
    override func setAccessibilitySelectedTextRange(_ range: NSRange) {
        // Deliberately acknowledge without applying the requested range.
    }
}

final class SelectionFixture: NSObject, NSApplicationDelegate {
    private var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 480, height: 180),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "SC Unconfirmed Selection"
        let editor = UnconfirmedSelectionEditor(frame: NSRect(x: 0, y: 0, width: 480, height: 180))
        editor.isEditable = true
        editor.isSelectable = true
        editor.isRichText = false
        editor.string = "untouched target"
        editor.setSelectedRange(NSRange(location: 0, length: 0))
        editor.setAccessibilityLabel("Unconfirmed selection editor")
        window.contentView = editor
        window.orderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let fixture = SelectionFixture()
app.setActivationPolicy(.regular)
app.delegate = fixture
app.run()
