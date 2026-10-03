import AppKit
import SCShared
import SwiftUI

/// The app intentionally remains menu-bar-only. AppKit owns this on-demand utility window while SwiftUI
/// presents the registry and controls; opening it never changes an agent's target or ownership.
@MainActor
final class TaskPanel: NSObject, NSWindowDelegate {
    nonisolated static let requestName = Notification.Name("dev.secondcursor.show-tasks")
    private let model = TaskPanelModel()
    private let showSetup: () -> Void
    private var panel: NSPanel?
    private var requestObserver: NSObjectProtocol?

    init(showSetup: @escaping () -> Void) {
        self.showSetup = showSetup
        super.init()
        requestObserver = DistributedNotificationCenter.default().addObserver(
            forName: Self.requestName, object: SCPaths.supportDir.path, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.show() }
        }
    }

    nonisolated static func requestFromRunningInstance() {
        DistributedNotificationCenter.default().postNotificationName(
            requestName, object: SCPaths.supportDir.path, userInfo: nil, deliverImmediately: true)
    }

    func show() {
        let panel = self.panel ?? makePanel()
        self.panel = panel
        model.start()
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) { model.stop() }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 780, height: 500),
            styleMask: [.titled, .closable, .resizable, .utilityWindow], backing: .buffered, defer: false)
        panel.title = "Second Cursor Tasks"
        panel.minSize = NSSize(width: 660, height: 420)
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = false
        panel.collectionBehavior = [.fullScreenAuxiliary]
        panel.delegate = self
        panel.contentView = NSHostingView(rootView: TaskPanelView(model: model, showSetup: showSetup))
        panel.setFrameAutosaveName("SecondCursorTasks")
        if !panel.setFrameUsingName("SecondCursorTasks") { panel.center() }
        return panel
    }
}

private struct TaskPanelView: View {
    @ObservedObject var model: TaskPanelModel
    let showSetup: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            if model.globallyPaused {
                HStack(spacing: 10) {
                    Image(systemName: "pause.circle.fill")
                    Text("All agent input is paused.")
                    Spacer()
                    Button("Resume all input") { model.resumeAllInput() }
                }
                .font(.callout)
                .padding(12)
                .background(.quaternary)
                Divider()
            }
            if let error = model.persistenceError {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.callout).foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                Divider()
            }
            HSplitView {
                taskList.frame(minWidth: 225, idealWidth: 255, maxWidth: 310)
                detail.frame(minWidth: 360, maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            HStack {
                Toggle("Show finished tasks", isOn: $model.showFinished)
                    .toggleStyle(.checkbox)
                Spacer()
                Button("Setup…", action: showSetup)
            }
            .font(.callout)
            .padding(12)
        }
        .onChange(of: model.selection) { _, _ in model.clearError() }
    }

    private var taskList: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Tasks").font(.headline).padding(.horizontal, 16).padding(.vertical, 12)
            if model.tasks.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text(model.showFinished ? "No tasks yet" : "No unfinished tasks").font(.headline)
                    Text("Tasks appear when a connected agent starts using Second Cursor.")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .font(.callout)
                .padding(16)
                Spacer()
            } else {
                List(selection: $model.selection) {
                    ForEach(model.tasks, id: \.id) { task in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: task.status.symbolName)
                                .foregroundStyle(task.status.color)
                                .frame(width: 18)
                                .padding(.top, 2)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(task.title).lineLimit(2)
                                Text(task.status.displayName)
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 3)
                        .tag(task.id)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(task.title), \(task.status.displayName)")
                        .accessibilityAction { model.selection = task.id }
                    }
                }
                .listStyle(.sidebar)
            }
        }
    }

    @ViewBuilder private var detail: some View {
        if let task = model.selectedTask {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header(task)
                    controls(task)
                    if let error = model.error {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .font(.callout).foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("task-control-error")
                    }
                    Divider()
                    targets(task)
                    Divider()
                    outcome(task)
                    Text("Updated \(task.updatedAt.formatted(date: .abbreviated, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .padding(24)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Task details for \(task.title)")
            }
        } else {
            VStack(spacing: 10) {
                Image(systemName: "cursorarrow.rays").font(.largeTitle).foregroundStyle(.secondary)
                Text(model.tasks.isEmpty ? "Your agent's work, in view" : "Select a task")
                    .font(.headline)
                Text(model.tasks.isEmpty
                     ? "See what it owns, take over when needed, and resume from your changes."
                     : "Choose a task to see its target, progress, and controls.")
                    .font(.callout).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).frame(maxWidth: 300)
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func header(_ task: SCTaskSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(task.title).font(.title2.weight(.semibold)).textSelection(.enabled)
            Label(task.status.displayName, systemImage: task.status.symbolName)
                .foregroundStyle(task.status.color)
                .accessibilityIdentifier("task-status")
            if let client = task.clientName, !client.isEmpty {
                Text(client).font(.callout).foregroundStyle(.secondary)
            }
            Text(explanation(task)).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func controls(_ task: SCTaskSnapshot) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) { controlButtons(task) }
            VStack(alignment: .leading, spacing: 8) { controlButtons(task) }
        }
    }

    @ViewBuilder private func controlButtons(_ task: SCTaskSnapshot) -> some View {
        if task.status.canResume {
            Button("Resume task") { model.control(id: task.id, action: .resume) }
                .disabled(task.inFlightCount > 0)
                .help("Requires a fresh observation of your changes before the next action.")
        }
        if task.status.canPause {
            Button("Pause") { model.control(id: task.id, action: .pause) }
        }
        if !task.status.isTerminal && task.status != .needsUser {
            Button("Take over") { model.control(id: task.id, action: .takeover) }
                .help("Stop further agent input and release its targets for you.")
        }
        if !task.status.isTerminal {
            Button("Cancel task", role: .destructive) { model.control(id: task.id, action: .cancel) }
        }
    }

    private func targets(_ task: SCTaskSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Targets").font(.headline)
            if task.targets.isEmpty {
                Text("No target has been claimed yet.").font(.callout).foregroundStyle(.secondary)
            }
            ForEach(task.targets, id: \.key) { target in
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(target.app ?? target.bundleID ?? (target.tabID == nil ? "App window" : "Browser tab"))
                        Text(targetDescription(target)).font(.caption).foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    Spacer(minLength: 8)
                    if let bundleID = target.bundleID {
                        Button("Show app") { model.showApp(bundleID: bundleID) }
                            .controlSize(.small)
                            .help("Bring this running app forward. This does not resume the task.")
                    }
                }
            }
        }
        .font(.callout)
    }

    private func outcome(_ task: SCTaskSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Last verified result").font(.headline)
            if let outcome = task.lastVerifiedOutcome {
                Text(outcome.summary).font(.callout).textSelection(.enabled)
                Text(outcome.at.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("No verified result yet.").font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private func targetDescription(_ target: SCTaskTarget) -> String {
        var details: [String] = []
        if let id = target.windowID { details.append("Window \(id)") }
        if let id = target.tabID { details.append("Tab \(id)") }
        if details.isEmpty { details.append(target.key) }
        return details.joined(separator: " · ")
    }

    private func explanation(_ task: SCTaskSnapshot) -> String {
        if task.inFlightCount > 0 && (task.status.canResume || task.status == .cancelled) {
            return "Waiting for input already in progress to stop. Further input is blocked."
        }
        if task.requiresFreshObservation && task.status == .thinking {
            return "Ready for your agent. It must observe the target again before acting on your changes."
        }
        switch task.status {
        case .queued: return "Waiting for the agent to start."
        case .running: return "The agent is using its targets. You can pause or take over at any time."
        case .thinking: return "The agent is between actions. Its task stays visible while it thinks."
        case .needsUser: return "Agent input is stopped. Make your changes, then resume when you are ready."
        case .paused: return "This task is paused. Other tasks can continue."
        case .completed: return "The agent has ended this task. Verified results are shown below."
        case .failed: return "This task failed. Its last verified result remains available below."
        case .cancelled: return "This task was cancelled. It will not send further input."
        case .interrupted: return "The task was interrupted. Resume to let the agent observe the current state again."
        }
    }
}
