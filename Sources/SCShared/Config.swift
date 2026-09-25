import Foundation

/// Canonical on-disk locations shared by the app, the server, and the E2E harness.
public enum SCPaths {
    public static let supportDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/SecondCursor", isDirectory: true)
    public static let logsDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/SecondCursor", isDirectory: true)

    public static let configURL = supportDir.appendingPathComponent("config.json")
    public static let actionLogURL = logsDir.appendingPathComponent("actions.jsonl")
    /// Presence of this file means the user paused the agent. Checked before every action.
    public static let pauseFlagURL = supportDir.appendingPathComponent("paused")

    public static func ensureDirectories() throws {
        try FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: logsDir, withIntermediateDirectories: true)
    }
}

public struct SCConfig: Codable, Sendable {
    public var port: Int
    public var token: String
    /// Bundle ids the agent may never touch (password managers, security tools, etc.).
    public var blockedBundleIDs: [String]
    /// Tool names that require user confirmation before running (send, delete, purchase, etc.).
    public var confirmationRequiredTools: [String]

    /// Native ownership expires after this many idle seconds. Nil uses five minutes.
    public var claimIdleSeconds: Double?
    /// Completed explicit retry IDs expire after this many seconds. Nil uses one hour.
    public var operationRetentionSeconds: Double?

    public init(port: Int, token: String, blockedBundleIDs: [String], confirmationRequiredTools: [String]) {
        self.port = port
        self.token = token
        self.blockedBundleIDs = blockedBundleIDs
        self.confirmationRequiredTools = confirmationRequiredTools
    }

    public static let defaultBlockedBundleIDs = [
        "com.apple.keychainaccess",
        "com.agilebits.onepassword7",
        "com.1password.1password",
        "com.apple.systempreferences",
        "com.apple.preference.security",
    ]

    public static func makeDefault() -> SCConfig {
        SCConfig(
            port: 8787,
            token: UUID().uuidString,
            blockedBundleIDs: defaultBlockedBundleIDs,
            confirmationRequiredTools: []
        )
    }

    /// Loads the config, creating and persisting a default (with a fresh token) on first run.
    public static func loadOrCreate() throws -> SCConfig {
        try SCPaths.ensureDirectories()
        if let data = try? Data(contentsOf: SCPaths.configURL),
           let config = try? JSONDecoder().decode(SCConfig.self, from: data) {
            return config
        }
        let config = makeDefault()
        try config.save()
        return config
    }

    public func save() throws {
        try SCPaths.ensureDirectories()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: SCPaths.configURL, options: .atomic)
    }

    public func isBlocked(bundleID: String?) -> Bool {
        guard let bundleID else { return false }
        return blockedBundleIDs.contains(bundleID)
    }
}

public enum PauseState {
    public static var isPaused: Bool { FileManager.default.fileExists(atPath: SCPaths.pauseFlagURL.path) }

    public static func pause() throws {
        try SCPaths.ensureDirectories()
        try Data().write(to: SCPaths.pauseFlagURL)
    }

    public static func resume() {
        try? FileManager.default.removeItem(at: SCPaths.pauseFlagURL)
    }
}

/// Append-only action log. Every tool that changes state writes one line before acting.
public struct ActionLogEntry: Codable, Sendable {
    public var time: String
    public var tool: String
    public var target: String?
    public var detail: String?

    public init(tool: String, target: String? = nil, detail: String? = nil) {
        self.time = ISO8601DateFormatter().string(from: Date())
        self.tool = tool
        self.target = target
        self.detail = detail
    }
}

public final class ActionLog: @unchecked Sendable {
    private let queue = DispatchQueue(label: "dev.secondcursor.actionlog")
    public init() {}

    public func record(_ entry: ActionLogEntry) {
        queue.sync {
            guard let line = try? JSONEncoder().encode(entry) else { return }
            try? SCPaths.ensureDirectories()
            if let handle = try? FileHandle(forWritingTo: SCPaths.actionLogURL) {
                defer { try? handle.close() }
                handle.seekToEndOfFile()
                handle.write(line)
                handle.write("\n".data(using: .utf8)!)
            } else {
                var data = line
                data.append("\n".data(using: .utf8)!)
                try? data.write(to: SCPaths.actionLogURL)
            }
        }
    }
}
