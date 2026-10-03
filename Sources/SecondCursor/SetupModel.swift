import AppKit
import SCShared
import SwiftUI

/// Window-scoped setup state. Reads are refreshed only while the window is visible; deliberate
/// installation actions are allowed to finish after closing it, without presenting the window again.
@MainActor
final class SetupModel: ObservableObject {
    struct ActionResult {
        let id = UUID()
        let succeeded: Bool
        let title: String
        let details: [String]
    }

    @Published private(set) var snapshot: SetupSnapshot?
    @Published private(set) var accessibility = Permissions.accessibility
    @Published private(set) var screenRecording = Permissions.screenRecording
    @Published private(set) var refreshing = false
    @Published private(set) var actionName: String?
    @Published private(set) var result: ActionResult?
    @Published var selectedClient: InstallMCP.Client = .codex

    private let config: SCConfig?
    private var pollTask: Task<Void, Never>?
    private var actionTask: Task<Void, Never>?
    private var presentation = 0
    private var resultSequence = 0
    private var visible = false

    init(config: SCConfig?) {
        self.config = config
    }

    var configurationAvailable: Bool { config != nil }
    var canCheckConnection: Bool { configurationAvailable && actionName == nil }
    var canTestBrowser: Bool { canCheckConnection && snapshot?.extensionConnected == true && snapshot?.paused == false }
    var canInstall: Bool { config != nil && actionName == nil }

    var selectedClientStatus: SetupClientStatus? {
        snapshot?.clients.first { $0.client == selectedClient }
    }

    func startPolling() {
        guard !visible else { return }
        visible = true
        presentation += 1
        let currentPresentation = presentation
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.visible, self.presentation == currentPresentation else { return }
                await self.refresh()
                do {
                    try await Task.sleep(nanoseconds: 4_000_000_000)
                } catch { return }
            }
        }
    }

    func stopPolling() {
        visible = false
        presentation += 1
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh() async {
        guard visible, !refreshing else { return }
        refreshing = true
        let currentPresentation = presentation
        accessibility = Permissions.accessibility
        screenRecording = Permissions.screenRecording
        let updated = await SetupDiagnostics.snapshot(config: config)
        refreshing = false
        guard visible, presentation == currentPresentation, !Task.isCancelled else { return }
        snapshot = updated
    }

    func installClient() {
        let client = selectedClient
        perform("Installing configuration…") {
            let details = try await Task.detached(priority: .userInitiated) {
                try InstallMCP.install(client: client)
            }.value
            return ActionResult(
                succeeded: true,
                title: "\(client.displayName) configuration installed",
                details: details + ["Restart \(client.displayName) or reload its MCP connection to use Second Cursor."])
        }
    }

    func installBrowserSupport() {
        perform("Installing browser support…") {
            let details = try await Task.detached(priority: .userInitiated) {
                try BrowserSetup.install()
            }.value
            if self.visible {
                NSWorkspace.shared.activateFileViewerSelecting([BrowserSetup.extensionDirectory])
            }
            return ActionResult(
                succeeded: true,
                title: "Browser support installed",
                details: details + ["In Chrome’s extension settings, enable Developer mode, choose Load unpacked, and select the extension folder."])
        }
    }

    func openBrowserSettings() {
        guard actionName == nil else { return }
        resultSequence += 1
        let sequence = resultSequence
        guard let chrome = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.google.Chrome") else {
            result = ActionResult(
                succeeded: false,
                title: "Chrome could not be found",
                details: ["Install or open Google Chrome, then enter chrome://extensions to load the Second Cursor extension."])
            return
        }
        result = nil
        NSWorkspace.shared.open(
            [BrowserSetup.installationURL], withApplicationAt: chrome,
            configuration: NSWorkspace.OpenConfiguration()
        ) { [weak self] _, error in
            guard let error else { return }
            Task { @MainActor in
                guard let self, self.resultSequence == sequence else { return }
                self.result = ActionResult(
                    succeeded: false,
                    title: "Open Chrome’s extension settings",
                    details: ["Enter chrome://extensions in Chrome, enable Developer mode, and choose Load unpacked to select the Second Cursor extension folder.", error.localizedDescription])
            }
        }
    }

    func revealBrowserExtension() {
        guard actionName == nil else { return }
        resultSequence += 1
        guard FileManager.default.fileExists(atPath: BrowserSetup.extensionDirectory.path) else {
            result = ActionResult(
                succeeded: false,
                title: "Install browser support first",
                details: ["The extension folder does not exist yet. Choose Install browser support to create it and register the browser connection."])
            return
        }
        result = nil
        NSWorkspace.shared.activateFileViewerSelecting([BrowserSetup.extensionDirectory])
    }

    func checkConnection() {
        let config = self.config
        perform("Checking connection…") {
            let check = await FirstTaskCheck.run(config: config)
            return ActionResult(succeeded: check.ok, title: check.summary, details: check.details)
        }
    }

    func testBrowser() {
        let config = self.config
        perform("Testing browser control…") {
            let check = await FirstTaskCheck.runBrowser(config: config)
            return ActionResult(succeeded: check.ok, title: check.summary, details: check.details)
        }
    }

    private func perform(_ name: String, operation: @escaping @MainActor () async throws -> ActionResult) {
        guard actionName == nil else { return }
        resultSequence += 1
        actionName = name
        result = nil
        actionTask = Task { [weak self] in
            let outcome: ActionResult
            do {
                outcome = try await operation()
            } catch {
                outcome = ActionResult(succeeded: false, title: "Setup needs attention", details: [error.localizedDescription])
            }
            guard let self else { return }
            self.result = outcome
            self.actionName = nil
            self.actionTask = nil
            await self.refresh()
        }
    }
}
