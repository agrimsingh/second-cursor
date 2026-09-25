import Foundation
import SCShared

/// Shared by every transport. Locks cover bookkeeping only, never app IPC.
final class Sessions: @unchecked Sendable {
    private struct Claim {
        var session: String
        var lastUsed: Date
        var inFlight = 0
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

        init(id: String, session: String, signature: String, deadline: Date, retain: Bool) {
            self.id = id
            self.session = session
            self.signature = signature
            self.deadline = deadline
            self.retain = retain
        }
    }

    private let lock = NSLock()
    private let claimIdleSeconds: TimeInterval
    private let retentionSeconds: TimeInterval
    private var live: Set<String> = ["legacy"]
    private var owners: [String: Claim] = [:]
    private var operations: [String: Operation] = [:]
    // FIFO queues make expiry and payload eviction amortized O(1), without sorting history.
    private var completed: [(id: String, generation: UUID, expires: Date)] = []
    private var completedHead = 0
    private var payloads: [String] = []
    private var payloadHead = 0
    private var retainedBytes = 0
    private var nextLeaseSweep = Date.distantPast

    init(claimIdleSeconds: TimeInterval = 300, retentionSeconds: TimeInterval = 3600) {
        self.claimIdleSeconds = max(1, claimIdleSeconds)
        self.retentionSeconds = max(1, retentionSeconds)
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func prune(now: Date = Date()) {
        if now >= nextLeaseSweep {
            owners = owners.filter { $0.value.inFlight > 0 || now.timeIntervalSince($0.value.lastUsed) < claimIdleSeconds }
            nextLeaseSweep = now.addingTimeInterval(min(60, claimIdleSeconds))
        }
        while completedHead < completed.count, completed[completedHead].expires <= now {
            let id = completed[completedHead].id
            if let op = operations[id], op.generation == completed[completedHead].generation {
                retainedBytes -= op.resultBytes
                operations[id] = nil
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

    func create() -> String {
        locked {
            prune()
            let id = UUID().uuidString
            live.insert(id)
            return id
        }
    }

    func validate(_ id: String) throws {
        try locked {
            prune()
            guard live.contains(id) else { throw SCError.notAllowed("Session is unknown or closed") }
        }
    }

    func close(_ id: String) {
        locked {
            if id != "legacy" { live.remove(id) }
            owners = owners.filter { $0.value.session != id }
            for op in operations.values where op.session == id {
                op.cancelled = true
                retainedBytes -= op.resultBytes
            }
            operations = operations.filter { $0.value.session != id }
            prune()
        }
    }

    private func claimLocked(_ target: String, session: String, automatic: Bool) throws {
        guard live.contains(session) else { throw SCError.notAllowed("Session closed") }
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

    func release(_ target: String, session: String) {
        locked {
            if owners[target]?.session == session, owners[target]?.inFlight == 0 { owners[target] = nil }
        }
    }

    func reserve(id: String, session: String, signature: String, deadline: Date, retain: Bool) throws -> (
        Operation, Bool
    ) {
        try locked {
            prune()
            if let op = operations[id] {
                guard op.session == session, op.signature == signature else {
                    throw SCError.invalidArgument("operation_id already belongs to a different request")
                }
                return (op, false)
            }
            let op = Operation(id: id, session: session, signature: signature, deadline: deadline, retain: retain)
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
            if let target {
                try claimLocked(target, session: op.session, automatic: true)
                if op.claimedTarget == nil, owners[target]?.session == op.session {
                    owners[target]?.inFlight += 1
                    op.claimedTarget = target
                }
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
            op.finished = true
            op.result = result
            op.error = error
            op.state = error == nil ? "completed" : (op.cancelled ? "cancelled" : "failed")
            op.resultBytes = bytes
            if let target = op.claimedTarget, owners[target]?.session == op.session {
                owners[target]?.inFlight -= 1
                owners[target]?.lastUsed = Date()
            }
            if op.retain, operations[op.id] === op {
                retainedBytes += bytes
                completed.append((op.id, op.generation, Date().addingTimeInterval(retentionSeconds)))
                payloads.append(op.id)
            } else {
                operations[op.id] = nil
            }
            prune()
        }
    }

    func output(_ op: Operation) -> ToolOutput? { locked { op.result } }

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
                "outcome_known": op.finished, "result_expired": op.resultExpired,
                "deadline": op.deadline.timeIntervalSince1970,
            ]
            if let result = op.result { info["result"] = result.json }
            if let error = op.error { info["error"] = error }
            return info
        }
    }
}
