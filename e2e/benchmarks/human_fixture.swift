import AppKit
import Foundation

// Build this source into a disposable .app and launch it only for the explicit
// --takeover E2E. Launching deliberately activates the app and its human window.
// Usage: HumanFixture --state /absolute/path/to/state.json

@MainActor
final class FixtureWindow: NSWindow {
    var responderDidChange: (() -> Void)?

    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        let previous = firstResponder
        let accepted = super.makeFirstResponder(responder)
        if accepted && previous !== firstResponder {
            responderDidChange?()
        }
        return accepted
    }
}

@MainActor
final class HumanSafetyFixture: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTextViewDelegate {
    let stateURL: URL
    private var userWindow: FixtureWindow!
    private var agentWindow: FixtureWindow!
    private var userText: NSTextView!
    private var agentText: NSTextView!
    private var agentTextScroll: NSScrollView!
    private var agentButton: NSButton!
    private var buttonStatus: NSTextField!
    private var timer: Timer?
    private var trackingEnabled = false
    private var reportedWriteFailure = false
    private var userResignKey = 0
    private var userResignActive = 0
    private var userFirstResponderChanges = 0
    private var agentButtonPresses = 0

    init(stateURL: URL) {
        self.stateURL = stateURL
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let contentWidth = min(420.0, max(300.0, (screen.width - 100.0) / 2.0))
        let contentHeight = min(310.0, max(240.0, screen.height - 120.0))
        let windowY = screen.maxY - contentHeight - 80.0

        userWindow = makeWindow(
            title: "Human safety fixture — User",
            frame: NSRect(x: screen.minX + 30, y: windowY, width: contentWidth, height: contentHeight)
        )
        agentWindow = makeWindow(
            title: "Human safety fixture — Agent",
            frame: NSRect(x: screen.maxX - contentWidth - 30, y: windowY, width: contentWidth, height: contentHeight)
        )

        let userContent = userWindow.contentView!
        addLabel("Human window", to: userContent, x: 20, y: contentHeight - 45, width: contentWidth - 40, bold: true)
        addLabel("Keep typing here while the agent uses its window.", to: userContent,
                 x: 20, y: contentHeight - 72, width: contentWidth - 40)
        let userEditor = makeEditor(
            label: "Human text editor",
            initialText: "Human baseline. ",
            frame: NSRect(x: 20, y: 24, width: contentWidth - 40, height: contentHeight - 110)
        )
        userText = userEditor.text
        userContent.addSubview(userEditor.scroll)

        let agentContent = agentWindow.contentView!
        addLabel("Agent window", to: agentContent, x: 20, y: contentHeight - 45, width: contentWidth - 40, bold: true)
        let agentEditor = makeEditor(
            label: "Agent text editor",
            initialText: "Agent baseline. ",
            frame: NSRect(x: 20, y: 90, width: contentWidth - 40, height: contentHeight - 155)
        )
        agentText = agentEditor.text
        agentTextScroll = agentEditor.scroll
        agentContent.addSubview(agentEditor.scroll)

        agentButton = NSButton(title: "Agent button", target: self, action: #selector(pressAgentButton))
        agentButton.bezelStyle = .rounded
        agentButton.frame = NSRect(x: 20, y: 34, width: 150, height: 34)
        agentButton.setAccessibilityIdentifier("agent-button")
        agentContent.addSubview(agentButton)
        buttonStatus = addLabel("Presses: 0", to: agentContent, x: 186, y: 38, width: contentWidth - 206)

        userWindow.responderDidChange = { [weak self] in
            guard let self, self.trackingEnabled else { return }
            self.userFirstResponderChanges += 1
            self.writeState()
        }

        // Both windows remain visible in the normal window layer. The human
        // window owns key focus; the agent window is deliberately in the same PID.
        agentWindow.orderFront(nil)
        userWindow.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        userWindow.makeFirstResponder(userText)
        userText.setSelectedRange(NSRange(location: userText.string.utf16.count, length: 0))
        trackingEnabled = true
        writeState()

        timer = Timer(timeInterval: 0.05, target: self, selector: #selector(timerFired), userInfo: nil, repeats: true)
        RunLoop.main.add(timer!, forMode: .common)
    }

    private func makeWindow(title: String, frame: NSRect) -> FixtureWindow {
        let window = FixtureWindow(contentRect: frame, styleMask: [.titled, .closable, .miniaturizable],
                                   backing: .buffered, defer: false)
        window.title = title
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.level = .normal
        window.collectionBehavior = [.moveToActiveSpace]
        return window
    }

    @discardableResult
    private func addLabel(_ text: String, to view: NSView, x: CGFloat, y: CGFloat,
                          width: CGFloat, bold: Bool = false) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.frame = NSRect(x: x, y: y, width: width, height: 24)
        label.font = bold ? .boldSystemFont(ofSize: 17) : .systemFont(ofSize: 12)
        label.lineBreakMode = .byTruncatingTail
        view.addSubview(label)
        return label
    }

    private func makeEditor(label: String, initialText: String, frame: NSRect) -> (scroll: NSScrollView, text: NSTextView) {
        let scroll = NSScrollView(frame: frame)
        scroll.borderType = .bezelBorder
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        let text = NSTextView(frame: NSRect(origin: .zero, size: scroll.contentSize))
        text.isEditable = true
        text.isSelectable = true
        text.isRichText = false
        text.isAutomaticQuoteSubstitutionEnabled = false
        text.isAutomaticDashSubstitutionEnabled = false
        text.isAutomaticTextReplacementEnabled = false
        text.isContinuousSpellCheckingEnabled = false
        text.font = .monospacedSystemFont(ofSize: 15, weight: .regular)
        text.string = initialText
        text.textContainerInset = NSSize(width: 8, height: 8)
        text.minSize = NSSize(width: 0, height: scroll.contentSize.height)
        text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = false
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        text.textContainer?.containerSize = NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude)
        text.setAccessibilityLabel(label)
        text.setAccessibilityIdentifier(label == "Human text editor" ? "user-text" : "agent-text")
        text.delegate = self
        scroll.documentView = text
        return (scroll, text)
    }

    @objc private func pressAgentButton() {
        agentButtonPresses += 1
        buttonStatus.stringValue = "Presses: \(agentButtonPresses)"
        writeState()
    }

    @objc private func timerFired() {
        // Read the live text views each tick so AX value changes are recorded
        // even when AppKit does not issue NSTextViewDelegate notifications.
        writeState()
    }

    func textDidChange(_ notification: Notification) {
        writeState()
    }

    func windowDidResignKey(_ notification: Notification) {
        guard trackingEnabled, let window = notification.object as? NSWindow, window === userWindow else { return }
        userResignKey += 1
        writeState()
    }

    func applicationDidResignActive(_ notification: Notification) {
        guard trackingEnabled else { return }
        userResignActive += 1
        writeState()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApplication.shared.terminate(nil)
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        writeState()
    }

    private func windowLocalFrame(of view: NSView, in window: NSWindow) -> [String: Double] {
        let screenFrame = window.convertToScreen(view.convert(view.bounds, to: nil))
        return [
            "x": Double(screenFrame.minX - window.frame.minX),
            "y": Double(window.frame.maxY - screenFrame.maxY),
            "width": Double(screenFrame.width),
            "height": Double(screenFrame.height),
        ]
    }

    private func writeState() {
        guard let userWindow, let agentWindow, let userText, let agentText,
              let agentTextScroll, let agentButton else { return }
        let state: [String: Any] = [
            "pid": ProcessInfo.processInfo.processIdentifier,
            "ready": trackingEnabled,
            "active": NSApplication.shared.isActive,
            "user_window": userWindow.windowNumber,
            "agent_window": agentWindow.windowNumber,
            "user_key": userWindow.isKeyWindow,
            "agent_key": agentWindow.isKeyWindow,
            "user_text": userText.string,
            "agent_text": agentText.string,
            "user_resign_key": userResignKey,
            "user_resign_active": userResignActive,
            "user_first_responder_changes": userFirstResponderChanges,
            "agent_button_presses": agentButtonPresses,
            // Frames include the title bar in the top-left window origin.
            // The text frame is the visible editor viewport, not its unbounded document.
            "agent_text_frame": windowLocalFrame(of: agentTextScroll.contentView, in: agentWindow),
            "agent_button_frame": windowLocalFrame(of: agentButton, in: agentWindow),
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: state, options: [.sortedKeys])
            try data.write(to: stateURL, options: .atomic)
        } catch {
            if !reportedWriteFailure {
                fputs("Human fixture cannot write state: \(error)\n", stderr)
                reportedWriteFailure = true
            }
        }
    }
}

let arguments = CommandLine.arguments
guard let stateFlag = arguments.firstIndex(of: "--state"), stateFlag + 1 < arguments.count,
      arguments[stateFlag + 1].hasPrefix("/") else {
    fputs("Usage: HumanFixture --state /absolute/path/to/state.json\n", stderr)
    exit(2)
}
// Standalone swiftc builds do not infer actor isolation for this top-level entry point.
// AppKit starts on the main thread, and its run loop keeps the fixture on that actor.
MainActor.assumeIsolated {
    let application = NSApplication.shared
    let fixture = HumanSafetyFixture(stateURL: URL(fileURLWithPath: arguments[stateFlag + 1]))
    application.setActivationPolicy(.regular)
    application.delegate = fixture
    application.run()
}
