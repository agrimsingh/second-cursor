import AppKit
import SCShared
import SwiftUI

/// A view of the shared runtime registry. Controls always address the selected task's stable id.
@MainActor
final class TaskPanelModel: ObservableObject {
    @Published private(set) var tasks: [SCTaskSnapshot] = []
    @Published var selection: String?
    @Published private(set) var error: String?
    @Published private(set) var globallyPaused = PauseState.isPaused
    @Published private(set) var persistenceError: String?
    @Published var showFinished = true { didSet { refresh() } }

    private var changeObserver: NSObjectProtocol?
    private var timer: Timer?

    var selectedTask: SCTaskSnapshot? { tasks.first { $0.id == selection } }

    init() {
        changeObserver = NotificationCenter.default.addObserver(
            forName: .scTaskRegistryDidChange, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
        refresh()
    }

    deinit {
        if let changeObserver { NotificationCenter.default.removeObserver(changeObserver) }
        timer?.invalidate()
    }

    func start() {
        refresh()
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    func refresh() {
        let latest = TaskRegistry.shared.list(includeFinished: showFinished)
        if tasks != latest { tasks = latest }
        globallyPaused = PauseState.isPaused
        persistenceError = TaskRegistry.shared.persistenceError
        if selection == nil { selection = tasks.first?.id }
        // Do not select another row after a lifecycle change. This keeps a queued click from controlling
        // the next task when the selected one finishes between drawing the button and processing input.
    }

    func control(id: String, action: SCTaskControlAction) {
        do {
            _ = try TaskRegistry.shared.control(id: id, action: action)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        refresh()
    }

    func resumeAllInput() {
        PauseState.resume()
        refresh()
    }

    func clearError() { error = nil }

    func showApp(bundleID: String) {
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first,
              !app.isTerminated else {
            error = "This app is no longer running. Open it yourself, then resume the task when you are ready."
            return
        }
        if !app.activate(options: []) {
            error = "macOS could not bring this app forward. You can switch to it yourself."
        } else {
            error = nil
        }
    }
}

extension SCTaskStatus {
    var displayName: String {
        switch self {
        case .queued: "Queued"
        case .running: "Working"
        case .thinking: "Thinking"
        case .needsUser: "Needs you"
        case .paused: "Paused"
        case .completed: "Completed"
        case .failed: "Failed"
        case .cancelled: "Cancelled"
        case .interrupted: "Interrupted"
        }
    }

    var symbolName: String {
        switch self {
        case .queued: "clock"
        case .running: "cursorarrow.rays"
        case .thinking: "ellipsis.bubble"
        case .needsUser: "hand.raised"
        case .paused: "pause.circle"
        case .completed: "checkmark.circle"
        case .failed: "exclamationmark.circle"
        case .cancelled: "xmark.circle"
        case .interrupted: "arrow.clockwise.circle"
        }
    }

    var color: Color {
        switch self {
        case .needsUser, .interrupted: .orange
        case .failed: .red
        case .completed: .green
        case .running: .accentColor
        default: .secondary
        }
    }

    var canResume: Bool { self == .paused || self == .needsUser || self == .interrupted }
    var canPause: Bool { self == .queued || self == .running || self == .thinking }
}
