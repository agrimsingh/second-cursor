import Foundation

/// Propagated across the app queues. Admission happens again at dispatch, not just at HTTP receipt.
public struct ExecutionContext: @unchecked Sendable {
    public var sessionID: String
    public var observationID: String?
    public var browserID: String?
    public var deadline: Date?
    public var browserApprovals: Set<String>
    public var beforeDispatch: @Sendable () throws -> Void
    public init(
        sessionID: String = "legacy", observationID: String? = nil, browserID: String? = nil, deadline: Date? = nil,
        browserApprovals: Set<String> = [], beforeDispatch: @escaping @Sendable () throws -> Void = {}
    ) {
        self.sessionID = sessionID
        self.observationID = observationID
        self.browserID = browserID
        self.deadline = deadline
        self.browserApprovals = browserApprovals
        self.beforeDispatch = beforeDispatch
    }
}
public enum Execution {
    @TaskLocal public static var context = ExecutionContext()
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
