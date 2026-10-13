import Foundation
import SCShared

/// Shared by every transport. Locks cover bookkeeping only, never app IPC.
final class Sessions: @unchecked Sendable {
    enum TransportValidationError: Error { case unknownOrClosed }

    struct DisplayMutationLease: Sendable {
        fileprivate let id = UUID()
        fileprivate let session: String
    }

    private struct Claim {
        var session: String
        var lastUsed: Date
        var inFlight = 0
        var releaseWhenIdle = false
    }

    final class Operation: @unchecked Sendable {
        let id: String
        let generation = UUID()
        let session: String
        let signature: String
        let deadline: Date
        var state = "queued"
        var cancelled = false
        var retain: Bool
        var result: ToolOutput?
        var error: String?
        var resultBytes = 0
        var resultExpired = false
        var claimedTarget: String?
        var finished = false
        let durable: Bool
        var outcomeKnown = false
        var recovered = false
        var outcomeState: String?
        var verified: Bool?
        var expires: Date?

        init(id: String, session: String, signature: String, deadline: Date, retain: Bool, durable: Bool = false) {
            self.id = id
            self.session = session
            self.signature = signature
            self.deadline = deadline
            self.retain = retain
            self.durable = durable
        }
    }

    /// A window a session read, so `app` can keep addressing the window whose indexes and coordinates the agent holds.
    struct ObservedWindow {
        let windowID: UInt32
        let pid: Int32
        let appName: String
        let bundleID: String?
        var at = Date()
    }

    /// The latest observation of an agent tab a session received: its snapshot, when the session last got that page's
    /// whole outline (nil when it holds only diffs of it), the tool whose result carried it if an action's did, and how
    /// many reads in a row before it found the page unchanged.
    struct BrowserRead {
        let snapshotID: String
        let wholeAt: Date?
        let action: String?
        var repeats = 0
    }

    private let lock = NSLock()
    private let claimIdleSeconds: TimeInterval
    private let retentionSeconds: TimeInterval
    private var live: Set<String> = ["legacy"]
    private var owners: [String: Claim] = [:]
    /// Includes legacy operations, which deliberately do not retain automatic claims. Generation keys
    /// survive close()/ID reuse until the actual app work drains, so display teardown cannot race them.
    private var nativeInFlight: Set<UUID> = []
    private var displayMutation: DisplayMutationLease?
    private var observed: [String: [UInt32: ObservedWindow]] = [:]
    private var browserReads: [String: [String: BrowserRead]] = [:]
    private var operations: [String: Operation] = [:]
    // FIFO queues make expiry and payload eviction amortized O(1), without sorting history.
    private var completed: [(id: String, generation: UUID, expires: Date)] = []
    private var completedHead = 0
    private var payloads: [String] = []
    private var payloadHead = 0
    private var retainedBytes = 0
    private var nextLeaseSweep = Date.distantPast
    private var journal: OperationJournal?
    private var journalFailure: String?
    private var sessionRecords: [String: OperationJournal.SessionRecord] = [:]

    init(claimIdleSeconds: TimeInterval = 300, retentionSeconds: TimeInterval = 3600) {
        self.claimIdleSeconds = max(1, claimIdleSeconds)
        self.retentionSeconds = max(1, retentionSeconds)
        do {
            let journal = try OperationJournal()
            let recovered = try journal.load()
            self.journal = journal
            for record in recovered.sessions {
                sessionRecords[record.id] = record
                live.insert(record.id)
            }
            for record in recovered.operations where live.contains(record.session) {
                let op = Operation(id: record.id, session: record.session, signature: record.signature,
                                   deadline: record.deadline, retain: true, durable: true)
                op.cancelled = record.cancelled
                op.finished = true // Nothing from the previous process is still running.
                op.recovered = true
                op.resultExpired = true
                op.expires = record.finished ? record.expires : Date().addingTimeInterval(self.retentionSeconds)
                op.outcomeState = record.outcomeState
                op.verified = record.verified
                if record.finished {
                    op.state = record.state
                    op.outcomeKnown = record.outcomeKnown
                } else if record.state == "intent" {
                    op.state = "interrupted"
                    op.outcomeKnown = true
                    op.outcomeState = "failed"
                    op.verified = false
                    op.error = "Process stopped before dispatch; this operation was not executed"
                } else {
                    op.state = "uncertain"
                    op.outcomeKnown = false
                    op.outcomeState = "uncertain"
                    op.verified = false
                    op.error = "Process stopped after dispatch; inspect the target before choosing a new operation ID"
                }
                operations[op.id] = op
                completed.append((op.id, op.generation, op.expires!))
                if !record.finished { try persist(op, state: op.state) }
            }
            completed.sort { $0.expires < $1.expires }
        } catch {
            // Read-only tools remain available for diagnosis; no mutation can bypass an unreadable journal.
            journalFailure = "Operation journal is unavailable; restore access before sending mutations"
            NSLog("Second Cursor operation journal could not be opened: %@", error.localizedDescription)
        }
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func prune(now: Date = Date()) {
        if now >= nextLeaseSweep {
            owners = owners.filter { $0.value.inFlight > 0 || now.timeIntervalSince($0.value.lastUsed) < claimIdleSeconds }
            let expiredSessions = Set(sessionRecords.values.filter { $0.expires <= now }.map(\.id))
            for id in expiredSessions {
                live.remove(id)
                sessionRecords[id] = nil
                observed[id] = nil
            }
            owners = owners.filter { !expiredSessions.contains($0.value.session) || $0.value.inFlight > 0 }
            nextLeaseSweep = now.addingTimeInterval(min(60, claimIdleSeconds))
        }
        while completedHead < completed.count, completed[completedHead].expires <= now {
            let id = completed[completedHead].id
            if let op = operations[id], op.generation == completed[completedHead].generation {
                retainedBytes -= op.resultBytes
                operations[id] = nil
                if op.durable { journal?.remove(id) }
            }
            completedHead += 1
        }
        if completedHead > 1024, completedHead * 2 > completed.count {
            completed.removeFirst(completedHead)
            completedHead = 0
        }
        while payloadHead < payloads.count {
            let id = payloads[payloadHead]
            if let op = operations[id], op.resultBytes > 0 {
                guard retainedBytes > 32 * 1024 * 1024 else { break }
                retainedBytes -= op.resultBytes
                op.resultBytes = 0
                op.result = nil
                op.resultExpired = true
            }
            payloadHead += 1
        }
        if payloadHead > 1024, payloadHead * 2 > payloads.count {
            payloads.removeFirst(payloadHead)
            payloadHead = 0
        }
    }

    func statistics() -> [String: Int] {
        locked {
            prune()
            return ["operations": operations.count, "retained_result_bytes": retainedBytes]
        }
    }

    private func requireJournal() throws -> OperationJournal {
        guard journalFailure == nil, let journal else {
            throw SCError.actionFailed(journalFailure ?? "Operation journal is unavailable")
        }
        return journal
    }

    func create(transport: Bool = false) throws -> String {
        try locked {
            prune()
            let id = UUID().uuidString
            var records = sessionRecords
            records[id] = OperationJournal.SessionRecord(id: id, transport: transport,
                expires: Date().addingTimeInterval(max(86400, retentionSeconds + 300)))
            try requireJournal().saveSessions(Array(records.values))
            sessionRecords = records
            live.insert(id)
            return id
        }
    }

    /// The bearer token grants host access; the unguessable header selects a reconnectable transport.
    func validateTransport(_ id: String) throws {
        try locked {
            guard live.contains(id), sessionRecords[id]?.transport == true,
                  (sessionRecords[id]?.expires ?? .distantPast) > Date() else {
                throw TransportValidationError.unknownOrClosed
            }
            try renewSessionLocked(id)
        }
    }

    private func renewSessionLocked(_ id: String) throws {
        guard var record = sessionRecords[id], record.expires.timeIntervalSinceNow < max(82800, retentionSeconds) else { return }
        record.expires = Date().addingTimeInterval(max(86400, retentionSeconds + 300))
        var records = sessionRecords
        records[id] = record
        try requireJournal().saveSessions(Array(records.values))
        sessionRecords = records
    }

    func validate(_ id: String) throws {
        try locked {
            prune()
            guard live.contains(id), id == "legacy" || (sessionRecords[id]?.expires ?? .distantPast) > Date() else {
                throw SCError.notAllowed("Session is unknown or closed")
            }
            try renewSessionLocked(id)
        }
    }

    func noteObserved(_ window: ObservedWindow, session: String) {
        locked {
            var windows = observed[session] ?? [:]
            windows[window.windowID] = window
            // Only the latest window per app is ever used; the cap bounds sessions that never end a turn.
            if windows.count > 64, let oldest = windows.values.min(by: { $0.at < $1.at }) {
                windows[oldest.windowID] = nil
            }
            observed[session] = windows
        }
    }

    /// The session's observed windows, most recent first.
    func observations(session: String) -> [ObservedWindow] {
        locked { (observed[session] ?? [:]).values.sorted { $0.at > $1.at } }
    }

    func forgetObservations(session: String) {
        locked {
            observed[session] = nil
            browserReads[session] = nil
        }
    }

    func browserRead(tab: String, session: String) -> BrowserRead? {
        locked { browserReads[session]?[tab] }
    }

    /// nil forgets the tab's, after input whose effect the session has not been shown.
    func noteBrowserRead(_ read: BrowserRead?, tab: String, session: String) {
        locked {
            var reads = browserReads[session] ?? [:]
            reads[tab] = read
            // The cap bounds sessions that never end a turn; a forgotten tab only loses its short answers.
            if reads.count > 64, let any = reads.keys.first(where: { $0 != tab }) { reads[any] = nil }
            browserReads[session] = reads
        }
    }

    func forgetBrowserReads(session: String) {
        locked { browserReads[session] = nil }
    }

    func close(_ id: String) throws {
        try locked {
            if id != "legacy", sessionRecords[id] != nil {
                var records = sessionRecords
                records[id] = nil
                try requireJournal().saveSessions(Array(records.values))
                sessionRecords = records
            }
            if id != "legacy" { live.remove(id) }
            observed[id] = nil
            browserReads[id] = nil
            // Already dispatched app IPC cannot be recalled. Keep its claim until finish so another
            // session cannot type into the same window while the closing session's input is still landing.
            owners = owners.filter { $0.value.session != id || $0.value.inFlight > 0 }
            for target in Array(owners.keys) where owners[target]?.session == id { owners[target]?.releaseWhenIdle = true }
            for op in operations.values where op.session == id {
                op.cancelled = true
                retainedBytes -= op.resultBytes
                if op.durable { journal?.remove(op.id) }
            }
            operations = operations.filter { $0.value.session != id }
            prune()
        }
    }

    private func claimLocked(_ target: String, session: String, automatic: Bool) throws {
        guard live.contains(session) else { throw SCError.notAllowed("Session closed") }
        if target.hasPrefix("window:"), displayMutation != nil {
            throw SCError.notAllowed("The agent display is being changed; wait for that operation before claiming or operating a window")
        }
        let now = Date()
        if let owner = owners[target], owner.inFlight == 0,
            now.timeIntervalSince(owner.lastUsed) >= claimIdleSeconds
        {
            owners[target] = nil
        }
        if let owner = owners[target], owner.session != session {
            throw SCError.notAllowed(
                "Target is claimed by another session; release it or wait for its idle lease to expire")
        }
        // Compatibility clients cannot strand a window by merely using it.
        if automatic && session == "legacy" { return }
        if owners[target] == nil { owners[target] = Claim(session: session, lastUsed: now) }
        owners[target]?.lastUsed = now
    }

    func claim(_ target: String, session: String) throws {
        try locked { try claimLocked(target, session: session, automatic: false) }
    }

    /// Admits a batch step's window the way `dispatch` admits a single action's target.
    func use(_ target: String, session: String) throws {
        try locked { try claimLocked(target, session: session, automatic: true) }
    }

    func release(_ target: String, session: String) {
        locked {
            if owners[target]?.session == session, owners[target]?.inFlight == 0 { owners[target] = nil }
        }
    }

    /// Call after a task is suspended; in-flight input keeps its claim until its completion boundary.
    func releaseAllTargets(session: String) {
        locked {
            owners = owners.filter { $0.value.session != session || $0.value.inFlight > 0 }
            for target in Array(owners.keys) where owners[target]?.session == session { owners[target]?.releaseWhenIdle = true }
        }
    }

    func cancelQueued(session: String) {
        locked {
            for op in operations.values where op.session == session && op.state == "queued" && !op.finished {
                op.cancelled = true
            }
        }
    }

    /// Teardown can return every hosted window, so it must exclude all simultaneous native input and
    /// claims, including another request from the same task. The caller releases this lease in a defer
    /// around the actual display work, never merely when its HTTP deadline passes.
    func beginDisplayMutation(session: String) throws -> DisplayMutationLease {
        try locked {
            prune()
            guard live.contains(session) else { throw SCError.notAllowed("Session closed") }
            guard displayMutation == nil else {
                throw SCError.notAllowed("Another operation is changing the agent display")
            }
            guard nativeInFlight.isEmpty, !owners.values.contains(where: { $0.inFlight > 0 }) else {
                throw SCError.notAllowed("Native input is still in flight; wait for it to finish before changing the agent display")
            }
            guard !owners.contains(where: { $0.key.hasPrefix("window:") && $0.value.session != session }) else {
                throw SCError.notAllowed("Another task owns a native window; release its claims before changing the agent display")
            }
            let lease = DisplayMutationLease(session: session)
            displayMutation = lease
            return lease
        }
    }

    func endDisplayMutation(_ lease: DisplayMutationLease) {
        locked {
            if displayMutation?.id == lease.id { displayMutation = nil }
        }
    }

    func reserve(id: String, session: String, signature: String, deadline: Date, retain: Bool, mutating: Bool = true) throws -> (
        Operation, Bool
    ) {
        try locked {
            prune()
            let signature = OperationJournal.digest(signature)
            if let op = operations[id] {
                guard op.session == session, op.signature == signature else {
                    throw SCError.invalidArgument("operation_id already belongs to a different request")
                }
                return (op, false)
            }
            let op = Operation(id: id, session: session, signature: signature, deadline: deadline, retain: retain,
                               durable: mutating)
            if mutating { try persist(op, state: "intent") }
            operations[id] = op
            return (op, true)
        }
    }

    private func checkLocked(_ op: Operation) throws {
        guard live.contains(op.session), !op.cancelled else {
            throw SCError.notAllowed("Operation cancelled or session closed")
        }
        if PauseState.isPaused { throw SCError.paused }
        guard Date() < op.deadline else { throw SCError.actionFailed("Operation deadline expired before dispatch") }
    }

    func mayApprove(_ op: Operation) -> Bool {
        locked { (try? checkLocked(op)) != nil }
    }

    func dispatch(_ op: Operation, target: String? = nil) throws {
        try locked {
            try checkLocked(op)
            // Repeated dispatch checkpoints within a batch do not need another durable write. The initial
            // boundary already says some input may have happened, and recovery must never replay the batch.
            if op.durable, op.state != "dispatched" { try persist(op, state: "dispatched") }
            if let target {
                try claimLocked(target, session: op.session, automatic: true)
                if op.claimedTarget == nil, owners[target]?.session == op.session {
                    owners[target]?.inFlight += 1
                    op.claimedTarget = target
                }
                if target.hasPrefix("window:") { nativeInFlight.insert(op.generation) }
            }
            op.state = "dispatched"
        }
    }

    func keepForReconciliation(_ op: Operation) {
        locked {
            op.retain = true
            if operations[op.id] == nil, live.contains(op.session) {
                operations[op.id] = op
                if op.finished {
                    retainedBytes += op.resultBytes
                    completed.append((op.id, op.generation, Date().addingTimeInterval(retentionSeconds)))
                    payloads.append(op.id)
                }
            }
        }
    }

    func finish(_ op: Operation, result: ToolOutput?, error: String?) {
        let bytes =
            result.map {
                ((try? JSONSerialization.data(withJSONObject: $0.json).count) ?? 0)
                    + $0.images.reduce(0) { $0 + $1.pngData.count }
            } ?? 0
        locked {
            guard !op.finished else { return }
            nativeInFlight.remove(op.generation)
            let uncertain = op.durable && op.state == "dispatched" && error != nil
            op.finished = true
            op.result = result
            op.error = error
            op.state = uncertain ? "uncertain" : (error == nil ? "completed" : (op.cancelled ? "cancelled" : "failed"))
            op.outcomeKnown = !uncertain
            if let outcome = result?.json["outcome"] as? [String: Any],
               let state = outcome["state"] as? String,
               ["verified", "observed", "dispatched", "uncertain", "failed"].contains(state) {
                op.outcomeState = state
                op.verified = outcome["verified"] as? Bool
                if state == "uncertain" { op.outcomeKnown = false }
            }
            if uncertain { op.outcomeState = "uncertain"; op.verified = false }
            op.expires = Date().addingTimeInterval(retentionSeconds)
            if op.durable, live.contains(op.session), operations[op.id] === op {
                do { try persist(op, state: op.state) }
                catch {
                    // The existing dispatched record is deliberately retained. A restart must reconcile it
                    // as uncertain, even when the caller received the in-memory result before the disk failed.
                    journalFailure = "Operation journal write failed; restore access before sending more mutations"
                    NSLog("Second Cursor operation outcome could not be persisted: %@", error.localizedDescription)
                }
            }
            op.resultBytes = bytes
            if let target = op.claimedTarget, owners[target]?.session == op.session {
                owners[target]?.inFlight -= 1
                owners[target]?.lastUsed = Date()
                if owners[target]?.inFlight == 0,
                   !live.contains(op.session) || owners[target]?.releaseWhenIdle == true { owners[target] = nil }
            }
            if (op.retain || op.durable), operations[op.id] === op {
                // Automatic IDs still leave bounded metadata for crash reconciliation; only explicitly
                // retained operations keep potentially sensitive response payloads in memory.
                if !op.retain {
                    op.result = nil
                    op.resultBytes = 0
                    op.resultExpired = true
                }
                retainedBytes += op.resultBytes
                completed.append((op.id, op.generation, Date().addingTimeInterval(retentionSeconds)))
                if op.resultBytes > 0 { payloads.append(op.id) }
            } else if operations[op.id] === op {
                operations[op.id] = nil
            }
            prune()
        }
    }

    func output(_ op: Operation) -> ToolOutput? { locked { op.result } }

    private func persist(_ op: Operation, state: String) throws {
        let record = OperationJournal.Record(version: 1, id: op.id, session: op.session, signature: op.signature,
            deadline: op.deadline, state: state, finished: op.finished, outcomeKnown: op.outcomeKnown,
            cancelled: op.cancelled, expires: op.expires ?? Date().addingTimeInterval(retentionSeconds),
            outcomeState: op.outcomeState, verified: op.verified)
        try requireJournal().save(record)
    }

    func info(_ id: String, session: String, cancel: Bool = false) throws -> [String: Any] {
        try locked {
            prune()
            guard let op = operations[id], op.session == session else {
                throw SCError.invalidArgument(
                    "Unknown or expired operation_id for this session (retention: \(Int(retentionSeconds))s)")
            }
            if cancel && !op.finished { op.cancelled = true }
            var info: [String: Any] = [
                "operation_id": id, "state": op.state, "cancel_requested": op.cancelled,
                "outcome_known": op.outcomeKnown, "result_expired": op.resultExpired, "recovered": op.recovered,
                "deadline": op.deadline.timeIntervalSince1970,
            ]
            if let result = op.result { info["result"] = result.json }
            if let error = op.error { info["error"] = error }
            if let state = op.outcomeState { info["outcome"] = ["state": state, "verified": op.verified ?? false] }
            return info
        }
    }
}
