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
        var showOnboarding: () -> Void
        var showTasks: () -> Void
        var showSetup: () -> Void
        var tearDownAgentDisplay: () -> Void
        var quit: () -> Void
    }

    var serverState: ServerState = .starting {
        didSet { refresh() }
    }

    /// Shown so the user can tell a task is running even when the agent works on another Space.
    var agentActivity = AgentActivityStatus() {
        didSet { refresh() }
    }

    private static let accent = NSColor(srgbRed: 0xf5 / 255.0, green: 0x4e / 255.0, blue: 0, alpha: 1)

    private let config: SCConfig?
    private let actions: Actions
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let statusLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let activityLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let accessibilityItem = NSMenuItem(title: "Grant Accessibility permission…", action: nil, keyEquivalent: "")
    private let screenRecordingItem = NSMenuItem(title: "Allow Screen Recording…", action: nil, keyEquivalent: "")
    private let pauseItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let copyConfigItem = NSMenuItem(title: "Copy Cursor MCP config", action: nil, keyEquivalent: "")
    private var timer: Timer?

    init(config: SCConfig?, actions: Actions) {
        self.config = config
        self.actions = actions
        super.init()

        statusLine.isEnabled = false
        activityLine.isEnabled = false
        let items: [(NSMenuItem, Selector?)] = [
            (statusLine, nil),
            (activityLine, nil),
            (NSMenuItem(title: "Tasks…", action: nil, keyEquivalent: "t"), #selector(showTasks)),
            (NSMenuItem(title: "Setup…", action: nil, keyEquivalent: ","), #selector(showSetup)),
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
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
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
        let running = if case .running = serverState { true } else { false }
        let tasks = TaskRegistry.shared.list(includeFinished: false)
        let workingTasks = tasks.filter { $0.status.permitsMutation }
        let needsYou = tasks.filter { $0.status == .needsUser || $0.status == .interrupted }
        let working = running && !paused && (!workingTasks.isEmpty || agentActivity.active)
        if paused && !tasks.isEmpty {
            activityLine.title = "\(tasks.count) task\(tasks.count == 1 ? "" : "s") · input paused"
        } else if !needsYou.isEmpty {
            activityLine.title = "\(needsYou.count) task\(needsYou.count == 1 ? "" : "s") need\(needsYou.count == 1 ? "s" : "") you"
        } else if tasks.count == 1, let task = tasks.first {
            activityLine.title = Self.shortTitle("\(task.status.displayName): \(task.title)")
        } else if !tasks.isEmpty {
            activityLine.title = "\(workingTasks.count) active · \(tasks.count) tasks"
        } else {
            activityLine.title = working ? Self.shortTitle("Agent active: \(agentActivity.app ?? "unknown app")") : "Idle"
        }
        statusLine.title = Self.shortTitle(statusLine.title)
        if let button = statusItem.button {
            var image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Second Cursor")
            if working || !needsYou.isEmpty {
                let tint = needsYou.isEmpty ? Self.accent : NSColor.systemOrange
                image = image?.withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [tint]))
                image?.isTemplate = false
            } else {
                image?.isTemplate = true
            }
            button.image = image
            button.toolTip = "Second Cursor — \(statusLine.title) — \(activityLine.title)"
        }

        pauseItem.title = paused ? "Resume all input" : "Pause all input"
        accessibilityItem.isHidden = Permissions.accessibility
        screenRecordingItem.isHidden = Permissions.screenRecording
        copyConfigItem.isEnabled = config != nil
    }

    private static func shortTitle(_ title: String) -> String {
        title.count > 30 ? String(title.prefix(29)) + "…" : title
    }

    @objc private func showTasks() { actions.showTasks() }
    @objc private func showSetup() { actions.showSetup() }

    @objc private func grantAccessibility() {
        actions.showOnboarding()
    }

    @objc private func grantScreenRecording() {
        actions.showOnboarding()
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
        let entry = ["mcpServers": [InstallMCP.serverName: InstallMCP.cursorEntry(config)]]
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
