import AppKit
import SCShared
import SwiftUI

/// A single, explicitly opened utility window for permissions, agent configuration, and connection checks.
@MainActor
final class SetupPanel: NSObject, NSWindowDelegate {
    private let model: SetupModel
    private let showPermission: (OnboardingPanel.Step) -> Void
    private var window: NSWindow?

    init(config: SCConfig?, showPermission: @escaping (OnboardingPanel.Step) -> Void) {
        model = SetupModel(config: config)
        self.showPermission = showPermission
        super.init()
    }

    func show() {
        let window = self.window ?? makeWindow()
        self.window = window
        model.startPolling()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        model.stopPolling()
    }

    private func makeWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 620, height: 730),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Second Cursor Setup"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 540, height: 520)
        window.delegate = self
        window.contentView = NSHostingView(rootView: SetupView(model: model, showPermission: showPermission))
        window.center()
        return window
    }
}

private struct SetupView: View {
    @ObservedObject var model: SetupModel
    let showPermission: (OnboardingPanel.Step) -> Void

    var body: some View {
        ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    introduction
                    permissionSection
                    clientSection
                    browserSection
                    checkSection
                    if let result = model.result {
                        SetupResultView(result: result)
                            .id("setup-result")
                    }
                }
                .padding(24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: model.result?.id) { _, id in
                if id != nil { scroll.scrollTo("setup-result", anchor: .bottom) }
            }
        }
        .accessibilityIdentifier("setup-window-content")
    }

    private var introduction: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Set up Second Cursor")
                .font(.title2.weight(.semibold))
            Text("Connect your agent, allow access to app windows, and check that everything can communicate.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: model.snapshot == nil ? "ellipsis.circle" : appReady ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                    .foregroundStyle(appReady ? Color.green : Color.secondary)
                    .accessibilityHidden(true)
                Text(model.snapshot?.summary ?? "Checking the running app…")
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button("Refresh") { Task { await model.refresh() } }
                    .disabled(model.refreshing)
                    .accessibilityIdentifier("setup-refresh")
            }
            if model.snapshot?.paused == true {
                Text("Agent input is paused. Resume it from the menu bar before testing browser control.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.configurationAvailable {
                Text("The app’s configuration could not be loaded. Quit and reopen Second Cursor before installing connections or running checks.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var appReady: Bool {
        model.snapshot?.reachable == true && model.snapshot?.problems.isEmpty == true
    }

    private var permissionSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionTitle("Allow app access")
            permissionRow(
                "Accessibility", detail: "Read and operate app windows in the background.",
                granted: model.accessibility, step: .accessibility)
            permissionRow(
                "Screen Recording", detail: "Take window screenshots so your agent can see its work.",
                granted: model.screenRecording, step: .screenRecording)
        }
    }

    private var clientSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("Connect an agent")
            Text("Install the MCP configuration for your agent, then restart the agent to load it.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Picker("Agent", selection: $model.selectedClient) {
                    ForEach(InstallMCP.Client.allCases, id: \.self) { client in
                        Text(client.displayName).tag(client)
                    }
                }
                .frame(maxWidth: 280)
                .disabled(model.actionName != nil)
                .accessibilityIdentifier("setup-client")
                Spacer(minLength: 8)
                Button(model.selectedClientStatus?.configured == true ? "Update configuration" : "Install configuration") {
                    model.installClient()
                }
                .disabled(!model.canInstall)
                .accessibilityIdentifier("setup-install-client")
            }
            if let status = model.selectedClientStatus {
                Label(status.detail, systemImage: status.configured ? "checkmark.circle" : "circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(status.path)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var browserSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("Connect Chrome")
            SetupStatusRow(
                title: model.snapshot?.extensionConnected == true ? "Browser extension connected" : "Browser extension not connected",
                detail: "The extension enables browser control. Native app access works independently.",
                ready: model.snapshot?.extensionConnected == true)
            HStack(spacing: 10) {
                Button("Install browser support") { model.installBrowserSupport() }
                    .disabled(!model.canInstall)
                    .accessibilityIdentifier("setup-install-browser")
                Button("Open extension settings") { model.openBrowserSettings() }
                    .disabled(model.actionName != nil)
                    .accessibilityIdentifier("setup-browser-settings")
            }
            Text("After installation, enable Developer mode at chrome://extensions, choose Load unpacked, and select the extension folder.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Button("Show extension folder") { model.revealBrowserExtension() }
                .buttonStyle(.link)
                .disabled(model.actionName != nil)
                .accessibilityIdentifier("setup-reveal-extension")
        }
    }

    private var checkSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionTitle("Verify the connection")
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Read-only connection check").fontWeight(.medium)
                    Text("Checks the running app and lists available apps. It does not send input or change your documents.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Button("Check connection") { model.checkConnection() }
                    .disabled(!model.canCheckConnection)
                    .accessibilityIdentifier("setup-check-connection")
            }
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Browser control check").fontWeight(.medium)
                    Text("Opens a temporary test tab, clicks its button, verifies the result, then closes the tab.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Button("Test browser control") { model.testBrowser() }
                    .disabled(!model.canTestBrowser)
                    .accessibilityIdentifier("setup-test-browser")
            }
            if let name = model.actionName {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(name).foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func sectionTitle(_ title: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            Divider()
        }
    }

    private func permissionRow(_ title: String, detail: String, granted: Bool, step: OnboardingPanel.Step) -> some View {
        HStack(alignment: .center, spacing: 16) {
            SetupStatusRow(title: title, detail: granted ? "Allowed. \(detail)" : detail, ready: granted)
            Spacer(minLength: 0)
            Button(granted ? "Review…" : "Allow…") { showPermission(step) }
                .accessibilityLabel("\(granted ? "Review" : "Allow") \(title)")
                .accessibilityIdentifier(step == .accessibility ? "setup-accessibility" : "setup-screen-recording")
        }
    }
}

private struct SetupStatusRow: View {
    let title: String
    let detail: String
    let ready: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: ready ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(ready ? Color.green : Color.secondary)
                .frame(width: 18)
                .padding(.top, 2)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).fontWeight(.medium)
                Text(detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct SetupResultView: View {
    let result: SetupModel.ActionResult

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            Label(result.title, systemImage: result.succeeded ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .font(.headline)
                .foregroundStyle(result.succeeded ? Color.primary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(Array(result.details.enumerated()), id: \.offset) { _, detail in
                Text(detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("setup-result")
    }
}
