import Foundation

/// Host-reported lifecycle. A quiet connection is never evidence that its task finished.
public enum SCTaskStatus: String, Codable, Sendable, CaseIterable {
    case queued, running, thinking, needsUser = "needs_user", paused, completed, failed, cancelled, interrupted

    public var isTerminal: Bool { self == .completed || self == .failed || self == .cancelled }
    public var permitsMutation: Bool { self == .queued || self == .running || self == .thinking }
}

public enum SCTaskControlAction: String, Codable, Sendable {
    case pause, takeover, resume, cancel
}

/// Target metadata deliberately excludes window titles, page URLs, and captured content.
public struct SCTaskTarget: Codable, Sendable, Equatable {
    public var key: String
    public var app: String?
    public var bundleID: String?
    public var windowID: UInt32?
    public var tabID: Int?
    public var browserID: String?

    public init(key: String, app: String? = nil, bundleID: String? = nil, windowID: UInt32? = nil, tabID: Int? = nil,
                browserID: String? = nil) {
        self.key = key
        self.app = app
        self.bundleID = bundleID
        self.windowID = windowID
        self.tabID = tabID
        self.browserID = browserID
    }

    public static func browserKey(browserID: String, tabID: Int) -> String { "browser:\(browserID):tab:\(tabID)" }
}

/// Supply a short, content-free description such as "Button became checked", never a prompt or tool arguments.
public struct SCTaskOutcome: Codable, Sendable, Equatable {
    public var summary: String
    public var at: Date

    public init(summary: String, at: Date = Date()) {
        self.summary = String(summary.prefix(240))
        self.at = at
    }
}

public struct SCTaskSnapshot: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var clientName: String?
    public var sessions: [String]
    public var status: SCTaskStatus
    public var createdAt: Date
    public var updatedAt: Date
    public var lastHeartbeatAt: Date?
    public var targets: [SCTaskTarget]
    public var lastVerifiedOutcome: SCTaskOutcome?
    public var inFlightCount: Int
    public var requiresFreshObservation: Bool
    public var controlGeneration: Int
    public var lastControlError: String?
}

/// A permit accounts for one whole tool operation. Recheck before *each* mutation in that operation;
/// a permit is not permission to continue a batch after the human pauses or takes over.
public struct SCTaskMutation: Sendable {
    public let id: UUID
    public let taskID: String
    public let session: String
    public let generation: Int
}

/// Delivered after gated input has drained, outside the registry lock. The server releases claims and
/// invalidates cached observations here. Resume never activates or raises the target window.
public struct SCTaskControlEvent: Sendable {
    public let taskID: String
    public let sessions: [String]
    public let action: SCTaskControlAction
    public let generation: Int
}

public extension Notification.Name {
    static let scTaskRegistryDidChange = Notification.Name("dev.secondcursor.taskRegistryDidChange")
}

/// Shared by the app UI and all server transports. Locking protects metadata only, never app IPC.
/// Persistent task metadata describes interrupted work; it never restores authority to operate an app.
public final class TaskRegistry: @unchecked Sendable {
    public static let shared = TaskRegistry(storageURL: SCPaths.supportDir.appendingPathComponent("tasks.json"))

    private struct Record {
        var snapshot: SCTaskSnapshot
        var observations: [String: Int] = [:]
        var mutations: Set<UUID> = []
        var pendingControl: SCTaskControlAction?
        var controlCleanupPending = false
    }

    private struct Journal: Codable {
        var version = 1
        var tasks: [SCTaskSnapshot]
        var sessionGenerations: [String: Int]?
    }

    private let lock = NSLock()
    private let controlDeliveryLock = NSRecursiveLock()
    private let storageURL: URL?
    private var records: [String: Record] = [:]
    private var sessionTasks: [String: String] = [:]
    private var sessionGenerations: [String: Int] = [:]
    private var controlHandler: (@Sendable (SCTaskControlEvent) -> Void)?
    private var controlRequestedHandler: (@Sendable (SCTaskControlEvent) -> Void)?
    private var storageError: String?
    private var unreadableJournal = false

    /// Pass nil for an in-memory disposable registry. Production code uses `shared`.
    public init(storageURL: URL? = nil) {
        self.storageURL = storageURL
        guard let storageURL, FileManager.default.fileExists(atPath: storageURL.path) else { return }
        do {
            let journal = try JSONDecoder().decode(Journal.self, from: Data(contentsOf: storageURL))
            guard journal.version == 1 else { throw SCError.invalidArgument("Unsupported task journal version") }
            sessionGenerations = journal.sessionGenerations ?? [:]
            // Never discard unfinished work to make room for history; a host may legitimately run many tasks.
            for var task in journal.tasks {
                if !task.status.isTerminal {
                    task.status = .interrupted
                    task.requiresFreshObservation = true
                    task.controlGeneration += 1
                    task.updatedAt = Date()
                }
                task.inFlightCount = 0
                // Sessions are labels in recovered metadata, not live server sessions. A host must explicitly
                // bind a live session to an interrupted task before it can resume that task.
                records[task.id] = Record(snapshot: task)
                rememberGenerationLocked(task)
            }
            pruneLocked()
            persistLocked()
        } catch {
            storageError = "Task history could not be loaded: \(error.localizedDescription)"
            unreadableJournal = true
        }
    }

    public var persistenceError: String? { locked { storageError } }

    public func setControlHandler(_ handler: (@Sendable (SCTaskControlEvent) -> Void)?) {
        locked { controlHandler = handler }
    }

    /// Stops remote browser input immediately, even when a long-running operation has not drained yet.
    public func setControlRequestedHandler(_ handler: (@Sendable (SCTaskControlEvent) -> Void)?) {
        locked { controlRequestedHandler = handler }
    }

    @discardableResult
    public func ensureTask(session: String, title: String? = nil, clientName: String? = nil) -> SCTaskSnapshot {
        let task = locked { () -> SCTaskSnapshot in
            if let id = sessionTasks[session], let record = records[id] { return record.snapshot }
            if let recovered = records.values.filter({ !$0.snapshot.status.isTerminal && $0.snapshot.sessions.contains(session) })
                .max(by: { $0.snapshot.updatedAt < $1.snapshot.updatedAt }) {
                // The transport has validated this live session. Reassociation preserves the interruption
                // gate; the host still has to explicitly resume and acquire fresh observations.
                sessionTasks[session] = recovered.snapshot.id
                return recovered.snapshot
            }
            let task = newTaskLocked(session: session, title: title, clientName: clientName)
            persistLocked()
            return task
        }
        changed()
        return task
    }

    /// Explicitly associates a live runtime session with a persisted task. An active session cannot silently
    /// escape a paused task by rebinding itself. Terminal sessions may begin a new named task via `create`.
    @discardableResult
    public func bind(session: String, to taskID: String) throws -> SCTaskSnapshot {
        let task = try locked { () throws -> SCTaskSnapshot in
            guard var record = records[taskID] else { throw SCError.invalidArgument("Unknown task") }
            guard !record.snapshot.status.isTerminal else { throw SCError.notAllowed("Task has finished; create a new task") }
            if let oldID = sessionTasks[session], oldID != taskID, let old = records[oldID] {
                guard old.mutations.isEmpty, !old.controlCleanupPending else {
                    throw SCError.notAllowed("Session still has input or control cleanup in flight")
                }
                if !old.snapshot.status.isTerminal {
                    guard old.snapshot.status == .queued, old.snapshot.targets.isEmpty else {
                        throw SCError.notAllowed("Session already belongs to an unfinished task")
                    }
                    var old = old
                    old.snapshot.sessions.removeAll { $0 == session }
                    if old.snapshot.sessions.isEmpty { records[oldID] = nil }
                    else { records[oldID] = old }
                }
            }
            sessionTasks[session] = taskID
            if !record.snapshot.sessions.contains(session) { record.snapshot.sessions.append(session) }
            record.snapshot.updatedAt = Date()
            record.snapshot.controlGeneration = max(record.snapshot.controlGeneration, sessionGenerations[session] ?? 0)
            records[taskID] = record
            persistLocked()
            return record.snapshot
        }
        changed()
        return task
    }

    /// Starts a named task for a session. Naming an automatically created, unused queued task preserves its ID.
    @discardableResult
    public func create(session: String, title: String, clientName: String? = nil) throws -> SCTaskSnapshot {
        let task = try locked { () throws -> SCTaskSnapshot in
            if let id = sessionTasks[session], var record = records[id] {
                guard record.mutations.isEmpty, !record.controlCleanupPending else {
                    throw SCError.notAllowed("Session still has input or control cleanup in flight")
                }
                if !record.snapshot.status.isTerminal {
                    guard record.snapshot.status == .queued, record.snapshot.targets.isEmpty else {
                        throw SCError.notAllowed("Session already has an unfinished task; update it or finish it first")
                    }
                    record.snapshot.title = Self.label(title, fallback: record.snapshot.title)
                    if let clientName { record.snapshot.clientName = Self.optionalLabel(clientName) }
                    record.snapshot.updatedAt = Date()
                    records[id] = record
                    persistLocked()
                    return record.snapshot
                }
            }
            let task = newTaskLocked(session: session, title: title, clientName: clientName)
            persistLocked()
            return task
        }
        changed()
        return task
    }

    public func snapshot(id: String) -> SCTaskSnapshot? { locked { records[id]?.snapshot } }
    public func snapshot(session: String) -> SCTaskSnapshot? {
        locked { sessionTasks[session].flatMap { records[$0]?.snapshot } }
    }

    public func list(includeFinished: Bool = true) -> [SCTaskSnapshot] {
        locked {
            records.values.map(\.snapshot).filter { includeFinished || !$0.status.isTerminal }
                .sorted { lhs, rhs in
                    if lhs.status.isTerminal != rhs.status.isTerminal { return !lhs.status.isTerminal }
                    return lhs.updatedAt > rhs.updatedAt
                }
        }
    }

    /// Status is explicit host information. Updating a title or heartbeat cannot undo a user control gate.
    @discardableResult
    public func heartbeat(session: String, status: SCTaskStatus? = nil, title: String? = nil,
                          clientName: String? = nil) throws -> SCTaskSnapshot {
        _ = ensureTask(session: session)
        var event: SCTaskControlEvent?
        var requested: SCTaskControlEvent?
        let task = try locked { () throws -> SCTaskSnapshot in
            guard let id = sessionTasks[session], var record = records[id] else {
                throw SCError.notAllowed("Session binding changed; reconnect before updating the task")
            }
            if let status, status != record.snapshot.status {
                guard !record.snapshot.status.isTerminal else { throw SCError.notAllowed("Task has finished") }
                guard status != .paused, status != .cancelled, status != .interrupted else {
                    throw SCError.invalidArgument("Use task controls to pause, cancel, or resume a task")
                }
                guard record.snapshot.status.permitsMutation || status == .completed || status == .failed else {
                    throw SCError.notAllowed("Task is paused or awaiting the user; use resume before changing its status")
                }
                guard !status.isTerminal || record.mutations.isEmpty else {
                    throw SCError.notAllowed("Task still has input in flight; reconcile it before finishing")
                }
                if status == .needsUser {
                    record.snapshot.controlGeneration += 1
                    record.snapshot.requiresFreshObservation = true
                    record.observations.removeAll()
                    record.pendingControl = .takeover
                    requested = controlEventLocked(record, action: .takeover)
                }
                record.snapshot.status = status
            }
            if let title { record.snapshot.title = Self.label(title, fallback: record.snapshot.title) }
            if let clientName { record.snapshot.clientName = Self.optionalLabel(clientName) }
            record.snapshot.lastHeartbeatAt = Date()
            record.snapshot.updatedAt = Date()
            event = drainEventLocked(&record)
            records[id] = record
            persistLocked()
            return record.snapshot
        }
        changed(event, requested: requested)
        return task
    }

    public func noteTarget(session: String, target: SCTaskTarget) {
        _ = ensureTask(session: session)
        let didChange = locked { () -> Bool in
            guard let id = sessionTasks[session], var record = records[id] else { return false }
            if record.snapshot.targets.first(where: { $0.key == target.key }) == target { return false }
            record.snapshot.targets.removeAll { $0.key == target.key }
            record.snapshot.targets.append(target)
            if record.snapshot.targets.count > 32 { record.snapshot.targets.removeFirst() }
            record.snapshot.updatedAt = Date()
            refreshFlagLocked(&record)
            records[id] = record
            persistLocked()
            return true
        }
        if didChange { changed() }
    }

    /// Call only after obtaining a new observation, never for a cached snapshot. Reads while paused or during
    /// takeover do not satisfy the resume requirement: the human may still be editing that target.
    public func noteObservation(session: String, targetKey: String, generation: Int? = nil) {
        let didChange = locked { () -> Bool in
            guard let id = sessionTasks[session], var record = records[id], record.snapshot.status.permitsMutation else { return false }
            if let generation, generation != record.snapshot.controlGeneration { return false }
            record.observations[targetKey] = record.snapshot.controlGeneration
            let before = record.snapshot.requiresFreshObservation
            refreshFlagLocked(&record)
            records[id] = record
            if before != record.snapshot.requiresFreshObservation { persistLocked(); return true }
            return false
        }
        if didChange { changed() }
    }

    public func checkMutation(session: String, targetKey: String? = nil) throws {
        try locked {
            guard let id = sessionTasks[session], let record = records[id] else {
                throw SCError.notAllowed("No task is bound to this session")
            }
            try checkLocked(record, targetKey: targetKey)
        }
    }

    /// Also verifies a queued operation's generation. A pause/resume must not revive old queued input.
    public func checkMutation(_ mutation: SCTaskMutation, targetKey: String? = nil) throws {
        try locked {
            guard let record = records[mutation.taskID], record.mutations.contains(mutation.id),
                  record.snapshot.controlGeneration == mutation.generation else {
                throw SCError.notAllowed("Task control changed while this input was queued; submit it again after a fresh observation")
            }
            try checkLocked(record, targetKey: targetKey)
        }
    }

    public func beginMutation(session: String, targetKey: String? = nil) throws -> SCTaskMutation {
        _ = ensureTask(session: session)
        let token = try locked { () throws -> SCTaskMutation in
            guard let id = sessionTasks[session], var record = records[id] else {
                throw SCError.notAllowed("Session binding changed; reconnect before sending input")
            }
            try checkLocked(record, targetKey: targetKey)
            let token = SCTaskMutation(id: UUID(), taskID: id, session: session, generation: record.snapshot.controlGeneration)
            let previousStatus = record.snapshot.status
            record.mutations.insert(token.id)
            record.snapshot.inFlightCount = record.mutations.count
            record.snapshot.status = .running
            record.snapshot.updatedAt = Date()
            records[id] = record
            persistLocked()
            if let storageError {
                record.mutations.remove(token.id)
                record.snapshot.inFlightCount = record.mutations.count
                record.snapshot.status = previousStatus
                records[id] = record
                throw SCError.notAllowed("Input is blocked because task history is unavailable: \(storageError)")
            }
            return token
        }
        changed()
        return token
    }

    public func finishMutation(_ mutation: SCTaskMutation, verifiedSummary: String? = nil) {
        var event: SCTaskControlEvent?
        let didChange = locked { () -> Bool in
            guard var record = records[mutation.taskID], record.mutations.remove(mutation.id) != nil else { return false }
            record.snapshot.inFlightCount = record.mutations.count
            record.snapshot.updatedAt = Date()
            if let verifiedSummary { record.snapshot.lastVerifiedOutcome = SCTaskOutcome(summary: verifiedSummary) }
            if record.mutations.isEmpty, record.snapshot.status == .running { record.snapshot.status = .thinking }
            event = drainEventLocked(&record)
            records[mutation.taskID] = record
            persistLocked()
            return true
        }
        if didChange { changed(event) }
    }

    @discardableResult
    public func control(id: String, action: SCTaskControlAction) throws -> SCTaskSnapshot {
        var event: SCTaskControlEvent?
        var requested: SCTaskControlEvent?
        let task = try locked { () throws -> SCTaskSnapshot in
            guard var record = records[id] else { throw SCError.invalidArgument("Unknown task") }
            guard !record.snapshot.status.isTerminal else {
                if action == .cancel, record.snapshot.status == .cancelled { return record.snapshot }
                throw SCError.notAllowed("Task has finished; create a new task")
            }
            if action == .resume {
                guard record.mutations.isEmpty, !record.controlCleanupPending else {
                    throw SCError.notAllowed("Input is still in flight; wait for it to settle before resuming")
                }
                guard !record.snapshot.status.permitsMutation else { return record.snapshot }
                guard record.snapshot.sessions.contains(where: { sessionTasks[$0] == id }) else {
                    throw SCError.notAllowed("Task was interrupted; bind a live session before resuming")
                }
                record.snapshot.status = .thinking
            } else {
                record.snapshot.status = action == .pause ? .paused : action == .takeover ? .needsUser : .cancelled
            }
            record.snapshot.controlGeneration += 1
            record.snapshot.requiresFreshObservation = !record.snapshot.targets.isEmpty
            record.observations.removeAll()
            record.snapshot.updatedAt = Date()
            record.snapshot.lastControlError = nil
            record.pendingControl = action
            if action != .resume { requested = controlEventLocked(record, action: action) }
            event = drainEventLocked(&record)
            records[id] = record
            persistLocked()
            return record.snapshot
        }
        changed(event, requested: requested)
        return task
    }

    /// A failed backend acknowledgement keeps the task gated. This does not dispatch another control event,
    /// so an unavailable bridge cannot create an endless retry/control loop.
    public func controlFailed(taskID: String, generation: Int, reason: String) {
        let didChange = locked { () -> Bool in
            guard var record = records[taskID], record.snapshot.controlGeneration == generation else { return false }
            if !record.snapshot.status.isTerminal { record.snapshot.status = .needsUser }
            record.snapshot.lastControlError = String(reason.prefix(240))
            record.snapshot.requiresFreshObservation = true
            record.snapshot.updatedAt = Date()
            record.observations.removeAll()
            records[taskID] = record
            persistLocked()
            return true
        }
        if didChange { changed() }
    }

    /// Transport loss preserves useful task state and gates input. No socket timeout claims task completion.
    public func interrupt(session: String) {
        var event: SCTaskControlEvent?
        var requested: SCTaskControlEvent?
        let didChange = locked { () -> Bool in
            guard let id = sessionTasks[session], var record = records[id], !record.snapshot.status.isTerminal else { return false }
            record.snapshot.status = .interrupted
            record.snapshot.controlGeneration += 1
            record.snapshot.requiresFreshObservation = true
            record.snapshot.updatedAt = Date()
            record.observations.removeAll()
            record.pendingControl = .pause
            requested = controlEventLocked(record, action: .pause)
            event = drainEventLocked(&record)
            records[id] = record
            sessionTasks[session] = nil
            persistLocked()
            return true
        }
        if didChange { changed(event, requested: requested) }
    }

    private func checkLocked(_ record: Record, targetKey: String?) throws {
        if let storageError { throw SCError.notAllowed("Input is blocked because task history is unavailable: \(storageError)") }
        guard !record.controlCleanupPending else { throw SCError.notAllowed("Task control cleanup is still in progress") }
        guard record.snapshot.status.permitsMutation else {
            throw SCError.notAllowed("Task is \(record.snapshot.status.rawValue); resume it before sending input")
        }
        if let targetKey, record.snapshot.controlGeneration > 0,
           record.observations[targetKey] != record.snapshot.controlGeneration {
            throw SCError.notAllowed("Task resumed after a control change; read target \(targetKey) again before sending input")
        }
    }

    private func refreshFlagLocked(_ record: inout Record) {
        record.snapshot.requiresFreshObservation = record.snapshot.controlGeneration > 0 && record.snapshot.targets.contains {
            record.observations[$0.key] != record.snapshot.controlGeneration
        }
    }

    private func drainEventLocked(_ record: inout Record) -> SCTaskControlEvent? {
        guard record.mutations.isEmpty, let action = record.pendingControl else { return nil }
        record.pendingControl = nil
        record.controlCleanupPending = true
        return controlEventLocked(record, action: action)
    }

    private func controlEventLocked(_ record: Record, action: SCTaskControlAction) -> SCTaskControlEvent {
        return SCTaskControlEvent(taskID: record.snapshot.id, sessions: record.snapshot.sessions,
                                  action: action, generation: record.snapshot.controlGeneration)
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func changed(_ event: SCTaskControlEvent? = nil, requested: SCTaskControlEvent? = nil) {
        if let requested {
            let handler = locked { controlRequestedHandler }
            handler?(requested)
        }
        if let event {
            // Serial delivery plus the admission gate prevents an older cleanup that already passed its
            // generation check from running after a newer resume has acquired fresh target state.
            controlDeliveryLock.lock()
            let current = locked { records[event.taskID]?.snapshot.controlGeneration == event.generation }
            if current {
                let handler = locked { controlHandler }
                handler?(event)
                locked {
                    if records[event.taskID]?.snapshot.controlGeneration == event.generation {
                        records[event.taskID]?.controlCleanupPending = false
                    }
                }
            }
            controlDeliveryLock.unlock()
        }
        NotificationCenter.default.post(name: .scTaskRegistryDidChange, object: self)
    }

    private func pruneLocked() {
        let finished = records.values.filter { $0.snapshot.status.isTerminal && $0.mutations.isEmpty }
            .sorted { $0.snapshot.updatedAt < $1.snapshot.updatedAt }
        for record in finished.prefix(max(0, records.count - 200)) {
            records[record.snapshot.id] = nil
            sessionTasks = sessionTasks.filter { $0.value != record.snapshot.id }
        }
    }

    private func newTaskLocked(session: String, title: String?, clientName: String?) -> SCTaskSnapshot {
        let now = Date()
        let generation = sessionGenerations[session].map { $0 + 1 } ?? 0
        let task = SCTaskSnapshot(
            id: UUID().uuidString, title: Self.label(title, fallback: "Agent task"),
            clientName: Self.optionalLabel(clientName), sessions: [session], status: .queued,
            createdAt: now, updatedAt: now, lastHeartbeatAt: nil, targets: [], lastVerifiedOutcome: nil,
            inFlightCount: 0, requiresFreshObservation: false, controlGeneration: generation, lastControlError: nil)
        records[task.id] = Record(snapshot: task)
        sessionTasks[session] = task.id
        rememberGenerationLocked(task)
        pruneLocked()
        return task
    }

    private func persistLocked() {
        for record in records.values { rememberGenerationLocked(record.snapshot) }
        guard let storageURL else { return }
        // Preserve unreadable/corrupt evidence. Only an explicit operator repair and restart may replace it.
        guard !unreadableJournal else { return }
        do {
            try FileManager.default.createDirectory(at: storageURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            // Store only the latest completed outcome; arguments, prompts, screenshots, and page content never enter this journal.
            let tasks = records.values.map(\.snapshot).sorted { $0.updatedAt < $1.updatedAt }
            try encoder.encode(Journal(tasks: tasks, sessionGenerations: sessionGenerations)).write(to: storageURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: storageURL.path)
            storageError = nil
        } catch {
            storageError = "Task history could not be saved: \(error.localizedDescription)"
        }
    }

    private func rememberGenerationLocked(_ task: SCTaskSnapshot) {
        for session in task.sessions { sessionGenerations[session] = max(sessionGenerations[session] ?? 0, task.controlGeneration) }
    }

    private static func optionalLabel(_ text: String?) -> String? {
        guard let text else { return nil }
        let label = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return label.isEmpty ? nil : String(label.prefix(120))
    }

    private static func label(_ text: String?, fallback: String) -> String { optionalLabel(text) ?? fallback }
}
