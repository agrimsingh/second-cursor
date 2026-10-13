import Foundation

/// Propagated across the app queues. Admission happens again at dispatch, not just at HTTP receipt.
public struct ExecutionContext: @unchecked Sendable {
    public var sessionID: String
    public var observationID: String?
    public var browserID: String?
    public var deadline: Date?
    /// The tool call's operation_id, supplied or generated.
    public var operationID: String?
    public var browserApprovals: Set<String>
    /// Set when `recordTimings` is on; nil makes every `Timing.measure` a plain call.
    public var timings: CallTimings?
    public var beforeDispatch: @Sendable () throws -> Void
    public init(
        sessionID: String = "legacy", observationID: String? = nil, browserID: String? = nil, deadline: Date? = nil,
        operationID: String? = nil, browserApprovals: Set<String> = [], timings: CallTimings? = nil,
        beforeDispatch: @escaping @Sendable () throws -> Void = {}
    ) {
        self.sessionID = sessionID
        self.observationID = observationID
        self.browserID = browserID
        self.deadline = deadline
        self.operationID = operationID
        self.browserApprovals = browserApprovals
        self.timings = timings
        self.beforeDispatch = beforeDispatch
    }
}
public enum Execution {
    @TaskLocal public static var context = ExecutionContext()
}

/// Milliseconds spent in each phase of one tool call. A phase that runs more than once (two reads, a batch's steps)
/// adds up.
public final class CallTimings: @unchecked Sendable {
    private let lock = NSLock()
    private var phases: [String: Double] = [:]

    public init() {}

    public func add(_ phase: String, ms: Double) {
        lock.withLock { phases[phase, default: 0] += ms }
    }

    public var phasesMS: [String: Double] {
        lock.withLock { phases }
    }
}

public enum Timing {
    @discardableResult
    public static func measure<T>(_ phase: String, _ body: () throws -> T) rethrows -> T {
        guard let timings = Execution.context.timings else { return try body() }
        let started = DispatchTime.now().uptimeNanoseconds
        defer { timings.add(phase, ms: since(started)) }
        return try body()
    }

    @discardableResult
    public static func measureAsync<T>(_ phase: String, _ body: () async throws -> T) async rethrows -> T {
        guard let timings = Execution.context.timings else { return try await body() }
        let started = DispatchTime.now().uptimeNanoseconds
        defer { timings.add(phase, ms: since(started)) }
        return try await body()
    }

    public static func since(_ uptimeNanoseconds: UInt64) -> Double {
        Double(DispatchTime.now().uptimeNanoseconds - uptimeNanoseconds) / 1_000_000
    }
}

public struct ElementInfo: Codable, Sendable, Equatable {
    public var index: Int
    public var role: String
    public var name: String
    public var value: String?
    public var bounds: Rect?
    public var actions: [String]
    public var states: [String: Bool?]
    public var valueLength: Int?
    public init(
        index: Int, role: String, name: String, value: String?, bounds: Rect?, actions: [String],
        states: [String: Bool?], valueLength: Int?
    ) {
        self.index = index
        self.role = role
        self.name = name
        self.value = value
        self.bounds = bounds
        self.actions = actions
        self.states = states
        self.valueLength = valueLength
    }
}
