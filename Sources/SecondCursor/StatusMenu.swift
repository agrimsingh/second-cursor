import AppKit
import SCServer
import SCShared

@MainActor
final class StatusMenu: NSObject, NSMenuDelegate {
    enum ServerState {
        case starting
        case running
        case failed(String)
    }

    struct Actions {
        var tearDownAgentDisplay: () -> Void
        var quit: () -> Void
    }

    var serverState: ServerState = .starting {
        didSet { refresh() }
    }

    private let config: SCConfig?
    private let actions: Actions
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let statusLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let accessibilityItem = NSMenuItem(title: "Grant Accessibility permission…", action: nil, keyEquivalent: "")
    private let screenRecordingItem = NSMenuItem(title: "Grant Screen Recording permission (then relaunch)…", action: nil, keyEquivalent: "")
    private let pauseItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let copyConfigItem = NSMenuItem(title: "Copy Cursor MCP config", action: nil, keyEquivalent: "")
    private var timer: Timer?

    init(config: SCConfig?, actions: Actions) {
        self.config = config
        self.actions = actions
        super.init()

        statusLine.isEnabled = false
        let items: [(NSMenuItem, Selector?)] = [
            (statusLine, nil),
            (accessibilityItem, #selector(grantAccessibility)),
            (screenRecordingItem, #selector(grantScreenRecording)),
            (.separator(), nil),
            (pauseItem, #selector(togglePause)),
            (copyConfigItem, #selector(copyCursorConfig)),
            (NSMenuItem(title: "Open action log", action: nil, keyEquivalent: ""), #selector(openActionLog)),
            (NSMenuItem(title: "Tear down agent display", action: nil, keyEquivalent: ""), #selector(tearDownAgentDisplay)),
            (.separator(), nil),
            (NSMenuItem(title: "Quit Second Cursor", action: nil, keyEquivalent: "q"), #selector(quit)),
        ]
        for (item, action) in items {
            if let action {
                item.action = action
                item.target = self
            }
            menu.addItem(item)
        }
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        refresh()
    }

    private var endpoint: String {
        "\(SecondCursorServer.host):\(config?.port ?? 8787)"
    }

    private func refresh() {
        let paused = PauseState.isPaused
        let symbol: String
        switch serverState {
        case .starting:
            statusLine.title = "Starting on \(endpoint)…"
            symbol = "cursorarrow.rays"
        case .running:
            statusLine.title = paused ? "Paused" : "Running on \(endpoint)"
            symbol = paused ? "pause.circle" : "cursorarrow.rays"
        case .failed(let message):
            statusLine.title = message
            symbol = "exclamationmark.triangle"
        }
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Second Cursor")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Second Cursor — \(statusLine.title)"
        }

        pauseItem.title = paused ? "Resume agent" : "Pause agent"
        accessibilityItem.isHidden = Permissions.accessibility
        screenRecordingItem.isHidden = Permissions.screenRecording
        copyConfigItem.isEnabled = config != nil
    }

    @objc private func grantAccessibility() {
        Permissions.requestMissing()
        Permissions.openSettings(accessibility: true)
    }

    @objc private func grantScreenRecording() {
        Permissions.requestMissing()
        Permissions.openSettings(accessibility: false)
    }

    @objc private func togglePause() {
        if PauseState.isPaused {
            PauseState.resume()
        } else {
            try? PauseState.pause()
        }
        refresh()
    }

    @objc private func copyCursorConfig() {
        guard let config else { return }
        let entry: [String: Any] = [
            "mcpServers": [
                "second-cursor": [
                    "url": "http://\(SecondCursorServer.host):\(config.port)\(SecondCursorServer.path)",
                    "headers": ["Authorization": "Bearer \(config.token)"],
                ],
            ],
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: entry, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(String(decoding: data, as: UTF8.self), forType: .string)
    }

    @objc private func openActionLog() {
        let url = SCPaths.actionLogURL
        if !FileManager.default.fileExists(atPath: url.path) {
            try? SCPaths.ensureDirectories()
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        if !NSWorkspace.shared.open(url),
           let textEdit = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.TextEdit") {
            NSWorkspace.shared.open([url], withApplicationAt: textEdit, configuration: NSWorkspace.OpenConfiguration())
        }
    }

    @objc private func tearDownAgentDisplay() {
        actions.tearDownAgentDisplay()
    }

    @objc private func quit() {
        actions.quit()
    }
}
