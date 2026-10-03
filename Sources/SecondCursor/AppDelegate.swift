import AppKit
import SCBrowser
import SCCore
import SCDisplay
import SCServer
import SCShared

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let desktop = Desktop()
    private let agentCursor = AgentCursorOverlay()
    private let agentDisplay = AgentDisplay()
    private let browserBridge = BrowserBridge()
    private let onboarding = OnboardingPanel()
    private var setup: SetupPanel?
    private lazy var tasks = TaskPanel(showSetup: { [weak self] in self?.setup?.show() })
    private var server: SecondCursorServer?
    private var menu: StatusMenu?
    private var signalSources: [DispatchSourceSignal] = []
    private var shuttingDown = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        installSignalHandlers()
        onboarding.listenForRequests()
        _ = tasks // Listen for explicit requests even before the panel has been opened.
        // A test instance (SC_HOME) must never put UI in front of the user unless asked to.
        if CommandLine.arguments.contains("--show-onboarding") || (SCPaths.customHome == nil && OnboardingPanel.needed) {
            onboarding.show()
        }
        desktop.cursor = agentCursor

        let config: SCConfig
        do {
            config = try SCConfig.loadOrCreate()
        } catch {
            makeMenu(config: nil).serverState = .failed("Could not load config: \(error.localizedDescription)")
            if CommandLine.arguments.contains("--show-tasks") { tasks.show() }
            return
        }
        let menu = makeMenu(config: config)
        if CommandLine.arguments.contains("--show-tasks") { tasks.show() }
        Desktop.cursorOnAgentDisplay = SCPaths.customHome != nil && config.cursorOnAgentDisplay == true

        startBrowserBridge()

        let server = SecondCursorServer(desktop: desktop, agentDisplay: agentDisplay, browser: browserBridge)
        self.server = server
        Task {
            do {
                try await server.start(config: config)
                menu.serverState = .running
            } catch {
                NSLog("Second Cursor: server failed to start: \(error)")
                menu.serverState = .failed(error.localizedDescription)
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !shuttingDown else { return .terminateLater }
        shuttingDown = true
        let server = self.server
        let agentDisplay = self.agentDisplay
        Task.detached {
            await server?.stop()
            // Hosted windows must go back to the user's screen, but a stuck teardown must not block quitting.
            await runWithDeadline(seconds: 10) { await agentDisplay.teardown() }
            RunLoop.main.perform(inModes: [.common]) { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    /// Retries so a stale socket owner (e.g. a dev bridge) doesn't leave the app without browser control until relaunch.
    private func startBrowserBridge() {
        do {
            try browserBridge.start()
        } catch {
            NSLog("Second Cursor: browser bridge failed to start, retrying in 5s: \(error)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.startBrowserBridge() }
        }
    }

    private func makeMenu(config: SCConfig?) -> StatusMenu {
        let agentDisplay = self.agentDisplay
        setup = SetupPanel(config: config, showPermission: { [weak self] step in
            self?.onboarding.show(step: step)
        })
        let menu = StatusMenu(config: config, actions: .init(
            showOnboarding: { [weak self] in self?.onboarding.show() },
            showTasks: { [weak self] in self?.tasks.show() },
            showSetup: { [weak self] in self?.setup?.show() },
            tearDownAgentDisplay: { Task { await agentDisplay.teardown() } },
            quit: { NSApp.terminate(nil) }
        ))
        agentCursor.onActivityChange = { [weak menu] activity in
            MainActor.assumeIsolated { menu?.agentActivity = activity }
        }
        self.menu = menu
        return menu
    }

    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                RunLoop.main.perform(inModes: [.common]) { NSApp.terminate(nil) }
            }
            source.resume()
            signalSources.append(source)
        }
    }
}

/// Returns when `operation` finishes or after `seconds`, whichever comes first.
private func runWithDeadline(seconds: Double, _ operation: @escaping @Sendable () async -> Void) async {
    let once = ResumeOnce()
    await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
        Task {
            await operation()
            if once.claim() { continuation.resume() }
        }
        Task {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            if once.claim() { continuation.resume() }
        }
    }
}

private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }
}
