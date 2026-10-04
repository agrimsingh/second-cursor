import AppKit
import Foundation

// A disposable foreground canary, not a physical keyboard or HID simulator.
// Commands and acknowledgements cross atomic JSON files; every focus and text
// transition is appended to a ledger which is never reset during a run.

@MainActor
final class CanaryWindow: NSWindow {
    var responderChanged: (() -> Void)?
    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        let previous = firstResponder
        let accepted = super.makeFirstResponder(responder)
        if accepted && previous !== firstResponder { responderChanged?() }
        return accepted
    }
}

@MainActor
final class CanaryFixture: NSObject, NSApplicationDelegate, NSWindowDelegate, @preconcurrency NSTextStorageDelegate {
    let stateURL: URL
    let commandURL: URL
    let ledger: FileHandle
    private var windows: [String: CanaryWindow] = [:]
    private var editors: [String: NSTextView] = [:]
    private var seenText: [String: String] = [:]
    private var timer: Timer?
    private var ready = false
    private var sequence = 0
    private var counters: [String: Int] = [:]
    private var lastCommand = 0
    private var lastAck: [String: Any] = [:]
    private var expectedWindow = "A"
    private var pendingFocus: (id: Int, window: String, deadline: Double)?
    private var settledSince: Double?
    private var activityInterval: Double?
    private var nextActivity = 0.0
    private var activityCount = 0
    private var inputSource: [String: Any]?
    private var writeError: String?

    init(stateURL: URL, commandURL: URL, ledgerURL: URL) throws {
        self.stateURL = stateURL
        self.commandURL = commandURL
        FileManager.default.createFile(atPath: ledgerURL.path, contents: Data())
        self.ledger = try FileHandle(forWritingTo: ledgerURL)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let width = min(420.0, max(280.0, (screen.width - 100) / 2))
        for (index, name) in ["A", "B"].enumerated() {
            let window = CanaryWindow(contentRect: NSRect(x: screen.minX + 30 + CGFloat(index) * (width + 20),
                y: screen.maxY - 350, width: width, height: 260),
                styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            window.title = "Second Cursor protected canary \(name)"
            window.isReleasedWhenClosed = false
            window.collectionBehavior = [.moveToActiveSpace]
            window.delegate = self
            let scroll = NSScrollView(frame: NSRect(x: 16, y: 16, width: width - 32, height: 228))
            scroll.hasVerticalScroller = true
            scroll.autoresizingMask = [.width, .height]
            scroll.borderType = .bezelBorder
            let editor = NSTextView(frame: NSRect(origin: .zero, size: scroll.contentSize))
            editor.isRichText = false
            editor.isEditable = true
            editor.isAutomaticQuoteSubstitutionEnabled = false
            editor.isAutomaticDashSubstitutionEnabled = false
            editor.isAutomaticTextReplacementEnabled = false
            editor.isContinuousSpellCheckingEnabled = false
            editor.font = .monospacedSystemFont(ofSize: 14, weight: .regular)
            editor.textContainerInset = NSSize(width: 8, height: 8)
            editor.isVerticallyResizable = true
            editor.isHorizontallyResizable = false
            editor.autoresizingMask = [.width]
            editor.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
            editor.textContainer?.widthTracksTextView = true
            editor.textContainer?.containerSize = NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude)
            editor.string = "Protected canary \(name).\n"
            editor.setAccessibilityIdentifier("protected-canary-\(name)")
            editor.setAccessibilityLabel("Protected canary \(name)")
            editor.textStorage?.delegate = self
            scroll.documentView = editor
            window.contentView?.addSubview(scroll)
            windows[name] = window
            editors[name] = editor
            seenText[name] = editor.string
            window.responderChanged = { [weak self] in
                self?.transition("responder_changed", detail: ["window": name])
            }
            _ = window.makeFirstResponder(editor)
            editor.setSelectedRange(NSRange(location: editor.string.utf16.count, length: 0))
            window.orderFront(nil)
        }
        windows["A"]?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // Startup activation is asynchronous. The first ready event is emitted
        // only after stable foreground ownership, so no startup event is hidden
        // inside the measured interval.
        pendingFocus = (id: 0, window: "A", deadline: ProcessInfo.processInfo.systemUptime + 8)
        timer = Timer(timeInterval: 0.05, target: self, selector: #selector(tick), userInfo: nil, repeats: true)
        RunLoop.main.add(timer!, forMode: .common)
        writeState()
    }

    private func windowName(_ window: NSWindow?) -> String? {
        windows.first(where: { $0.value === window })?.key
    }

    private func responderName(_ name: String) -> String {
        guard let responder = windows[name]?.firstResponder else { return "none" }
        return responder === editors[name] ? "\(name).editor" : String(describing: type(of: responder))
    }

    private func snapshot() -> [String: Any] {
        ["pid": ProcessInfo.processInfo.processIdentifier, "ready": ready,
         "active": NSApp.isActive, "key_window": windowName(NSApp.keyWindow) ?? "none",
         "documents": editors.mapValues { $0.string },
         "responders": windows.mapValues { window in responderName(windowName(window)!) },
         "windows": windows.mapValues { $0.windowNumber }, "expected_window": expectedWindow,
         "sequence": sequence, "counters": counters, "last_ack": lastAck,
         "activity_count": activityCount, "activity_running": activityInterval != nil,
         "timestamp": Date().timeIntervalSince1970, "uptime": ProcessInfo.processInfo.systemUptime,
         "pending_focus": pendingFocus?.id ?? -1, "write_error": writeError ?? ""]
    }

    private func record(_ kind: String, detail: [String: Any] = [:]) {
        sequence += 1
        var event = snapshot()
        event["kind"] = kind
        event["detail"] = detail
        event["boundary_id"] = pendingFocus?.id ?? -1
        do {
            var data = try JSONSerialization.data(withJSONObject: event, options: [.sortedKeys])
            data.append(10)
            try ledger.write(contentsOf: data)
        } catch { writeError = "Cannot append event ledger: \(error)" }
    }

    private func transition(_ kind: String, detail: [String: Any] = [:]) {
        guard ready else { return }
        counters[kind, default: 0] += 1
        record(kind, detail: detail)
        writeState()
    }

    private func collectTextChanges() {
        guard ready else { return }
        for name in ["A", "B"] {
            let text = editors[name]!.string
            if seenText[name] != text {
                seenText[name] = text
                var detail: [String: Any] = ["window": name, "text": text]
                if let inputSource { detail["input"] = inputSource }
                transition("text_changed", detail: detail)
            }
        }
    }

    func textStorage(_ textStorage: NSTextStorage, didProcessEditing editedMask: NSTextStorageEditActions,
                     range editedRange: NSRange, changeInLength delta: Int) {
        if editedMask.contains(.editedCharacters) { collectTextChanges() }
    }

    private func acknowledge(_ id: Int, ok: Bool = true, error: String = "") {
        lastAck = ["id": id, "ok": ok, "error": error, "sequence": sequence + 1]
        record("command_ack", detail: lastAck)
        writeState()
    }

    private func deliver(_ text: String, source: [String: Any]) {
        inputSource = source
        var detail = source
        detail["target"] = expectedWindow
        detail["text"] = text
        record("input_intent", detail: detail)
        // Deliberately route through the CURRENT key window and first responder,
        // never a reference to the expected editor. This cannot dispatch into any
        // other process, and inactive ownership is recorded as an undelivered tick.
        var delivered = false
        if NSApp.isActive, let responder = NSApp.keyWindow?.firstResponder as? NSTextInputClient {
            responder.insertText(text, replacementRange: NSRange(location: NSNotFound, length: 0))
            delivered = true
        }
        collectTextChanges()
        detail["delivered"] = delivered
        record("input_result", detail: detail)
        inputSource = nil
    }

    private func readCommand() {
        guard pendingFocus == nil,
              let data = try? Data(contentsOf: commandURL),
              let command = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let id = command["id"] as? Int, id > lastCommand else { return }
        lastCommand = id
        record("command_received", detail: command)
        switch command["action"] as? String {
        case "focus":
            guard let name = command["window"] as? String, let window = windows[name] else {
                acknowledge(id, ok: false, error: "focus requires window A or B"); return
            }
            pendingFocus = (id, name, ProcessInfo.processInfo.systemUptime + 5)
            settledSince = nil
            record("focus_begin", detail: ["id": id, "window": name])
            window.makeKeyAndOrderFront(nil)
            _ = window.makeFirstResponder(editors[name])
            NSApp.activate(ignoringOtherApps: true)
        case "type":
            guard let text = command["text"] as? String, text.count <= 4096 else {
                acknowledge(id, ok: false, error: "type requires at most 4096 characters"); return
            }
            deliver(text, source: ["command_id": id])
            acknowledge(id)
        case "start_activity":
            let milliseconds = (command["interval_ms"] as? Double) ?? 150
            guard milliseconds >= 50, milliseconds <= 10000 else {
                acknowledge(id, ok: false, error: "interval_ms must be between 50 and 10000"); return
            }
            activityInterval = milliseconds / 1000
            nextActivity = ProcessInfo.processInfo.systemUptime + activityInterval!
            acknowledge(id)
        case "stop_activity":
            activityInterval = nil
            acknowledge(id)
        case "mark":
            acknowledge(id)
        case "quit":
            activityInterval = nil
            acknowledge(id)
            NSApp.terminate(nil)
        default:
            acknowledge(id, ok: false, error: "Unknown action")
        }
    }

    @objc private func tick() {
        collectTextChanges()
        let now = ProcessInfo.processInfo.systemUptime
        if let pending = pendingFocus {
            if NSApp.isActive && windowName(NSApp.keyWindow) == pending.window
                && responderName(pending.window) == "\(pending.window).editor" {
                if settledSince == nil { settledSince = now }
                if now - settledSince! >= 0.1 {
                    expectedWindow = pending.window
                    pendingFocus = nil
                    settledSince = nil
                    if !ready {
                        ready = true
                        record("ready")
                    } else {
                        record("focus_settled", detail: ["id": pending.id, "window": pending.window])
                        acknowledge(pending.id)
                    }
                }
            } else { settledSince = nil }
            if now > pending.deadline && pendingFocus != nil {
                pendingFocus = nil
                if pending.id == 0 { writeError = "Startup failed to acquire foreground ownership" }
                else { acknowledge(pending.id, ok: false, error: "Foreground window did not settle") }
            }
        }
        if ready {
            readCommand()
            if pendingFocus == nil, let interval = activityInterval, now >= nextActivity {
                activityCount += 1
                let token = String(format: "<%@:%06d>", expectedWindow, activityCount)
                deliver(token, source: ["activity_tick": activityCount])
                nextActivity = now + interval
            }
        }
        writeState()
    }

    func applicationDidResignActive(_ notification: Notification) { transition("app_resign_active") }
    func applicationDidBecomeActive(_ notification: Notification) { transition("app_become_active") }
    func windowDidResignKey(_ notification: Notification) {
        transition("window_resign_key", detail: ["window": windowName(notification.object as? NSWindow) ?? "unknown"])
    }
    func windowDidBecomeKey(_ notification: Notification) {
        transition("window_become_key", detail: ["window": windowName(notification.object as? NSWindow) ?? "unknown"])
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        collectTextChanges()
        record("terminated")
        writeState()
        try? ledger.synchronize()
    }
    private func writeState() {
        do {
            let data = try JSONSerialization.data(withJSONObject: snapshot(), options: [.sortedKeys])
            try data.write(to: stateURL, options: .atomic)
        } catch { writeError = "Cannot write state: \(error)" }
    }
}

let arguments = CommandLine.arguments
func pathArgument(_ flag: String) -> URL? {
    guard let index = arguments.firstIndex(of: flag), index + 1 < arguments.count,
          arguments[index + 1].hasPrefix("/") else { return nil }
    return URL(fileURLWithPath: arguments[index + 1])
}
guard let state = pathArgument("--state"), let command = pathArgument("--command"),
      let events = pathArgument("--events") else {
    fputs("Usage: coexistence-canary --state PATH --command PATH --events PATH\n", stderr)
    exit(2)
}
MainActor.assumeIsolated {
    do {
        let application = NSApplication.shared
        let fixture = try CanaryFixture(stateURL: state, commandURL: command, ledgerURL: events)
        application.setActivationPolicy(.regular)
        application.delegate = fixture
        application.run()
    } catch { fputs("Cannot start canary: \(error)\n", stderr); exit(1) }
}
