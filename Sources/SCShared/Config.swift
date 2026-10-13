import Foundation

/// Canonical on-disk locations shared by the app, the server, and the E2E harness.
public enum SCPaths {
    /// `SC_HOME` relocates everything the app writes, so disposable test instances never touch the user's
    /// files. Keep it short: the browser socket lives inside it and Unix socket paths cap at 104 bytes.
    /// sc-native-host duplicates this rule because it does not link SCShared.
    public static let customHome: URL? = {
        guard let path = ProcessInfo.processInfo.environment["SC_HOME"], !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path, isDirectory: true)
    }()

    public static let supportDir = customHome ?? FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/SecondCursor", isDirectory: true)
    public static let logsDir = customHome?.appendingPathComponent("logs", isDirectory: true)
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/SecondCursor", isDirectory: true)

    public static let configURL = supportDir.appendingPathComponent("config.json")
    public static let actionLogURL = logsDir.appendingPathComponent("actions.jsonl")
    public static let timingLogURL = logsDir.appendingPathComponent("timings.jsonl")
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
    /// "prompt" (nil) asks the user before the agent first reads or operates an app; "off" allows every unblocked app.
    public var appApproval: String?
    /// Bundle ids the user chose "Always allow" for.
    public var approvedBundleIDs: [String]?
    /// How every local approval is answered: "prompt" (nil) asks the user; "deny" refuses without UI; "allow" grants
    /// every app, sensitive browser operation and confirmation-required tool without UI. Blocked apps stay blocked.
    public var approvals: String?
    /// Which tools tools/list advertises: every tool but `act` and `browser_repl` (nil); "compact", which lists `act`
    /// instead of `batch` and the single-action tools; "full", which adds `browser_repl`; or "repl", which lists only
    /// `browser_repl`. Every tool stays callable either way.
    public var toolSurface: String?
    /// Draw the agent cursor for windows on the agent display, as if the user could see it, so e2e/cursor.py can run
    /// there instead of on the user's screen. Honored only when `SC_HOME` is set (disposable test instances).
    public var cursorOnAgentDisplay: Bool?
    /// Append each tool call's duration and the time each phase took (settling, the cursor, the tree walk, capture,
    /// the browser round trip) to logs/timings.jsonl. Results are unchanged.
    public var recordTimings: Bool?
    /// "rules" condenses full window and page reads of 80 lines or more (footers and side panels hidden, long lists cut
    /// to their first chunk), each hidden run returned by `expand`; "off" (nil) returns them whole.
    public var condenseOutlines: String?
    /// How long a browser read waits for the page to stop changing once every request its last action started has
    /// finished. Nil uses 300 ms. Shorter answers sooner but can return a reaction a timer has not finished drawing.
    public var browserQuietMs: Int?

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

    /// Loads the config, creating and persisting a default (with a fresh token) on first run. Only the app calls
    /// this; CLI commands must never create or rewrite the config. A file that exists but cannot be decoded is
    /// moved aside to `config.json.invalid-<timestamp>` before defaults are written, so the user's token,
    /// approvals and blocklist are never silently destroyed. An unreadable file throws instead.
    public static func loadOrCreate() throws -> SCConfig {
        try SCPaths.ensureDirectories()
        let url = SCPaths.configURL
        if FileManager.default.fileExists(atPath: url.path) {
            let data = try Data(contentsOf: url)
            if let config = try? JSONDecoder().decode(SCConfig.self, from: data) {
                return config
            }
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.dateFormat = "yyyyMMdd-HHmmss"
            let aside = url.deletingLastPathComponent()
                .appendingPathComponent("\(url.lastPathComponent).invalid-\(formatter.string(from: Date()))")
            try FileManager.default.moveItem(at: url, to: aside)
            NSLog("Second Cursor: %@ could not be decoded; moved it to %@ and wrote a default config", url.path, aside.path)
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

    /// Appends to `approvedBundleIDs` in the file on disk. Edits the JSON object in place rather than
    /// re-encoding a decoded config, so fields this build does not know about survive, and a missing or
    /// unreadable file is left alone instead of being replaced by defaults.
    public static func addApprovedBundleID(_ bundleID: String) throws {
        let data = try Data(contentsOf: SCPaths.configURL)
        guard var object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CocoaError(.fileReadCorruptFile)
        }
        var approved = object["approvedBundleIDs"] as? [String] ?? []
        guard !approved.contains(bundleID) else { return }
        approved.append(bundleID)
        object["approvedBundleIDs"] = approved
        try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            .write(to: SCPaths.configURL, options: .atomic)
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
            Self.append(line, to: SCPaths.actionLogURL)
        }
    }

    static func append(_ line: Data, to url: URL) {
        try? SCPaths.ensureDirectories()
        var data = line
        data.append(0x0A)
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            handle.seekToEndOfFile()
            handle.write(data)
        } else {
            try? data.write(to: url)
        }
    }
}

/// One line per tool call when `recordTimings` is on. Written off the call's path, unlike the action log.
public final class TimingLog: @unchecked Sendable {
    static let rotateBytes: UInt64 = 32 << 20
    private let queue = DispatchQueue(label: "dev.secondcursor.timinglog")
    public init() {}

    public func record(
        tool: String, ms: Double, ok: Bool, phases: [String: Double], error: String? = nil, session: String? = nil
    ) {
        let rounded = phases.mapValues { ($0 * 10).rounded() / 10 }
        // Milliseconds, so e2e/session_report.py can tell concurrent calls from back-to-back ones.
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var entry: [String: Any] = [
            "time": formatter.string(from: Date()), "tool": tool, "ms": (ms * 10).rounded() / 10, "ok": ok,
            "phases": rounded,
        ]
        if let error { entry["error"] = String(error.prefix(500)) }
        if let session { entry["session_id"] = session }
        queue.async {
            guard let line = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]) else { return }
            let url = SCPaths.timingLogURL
            let fm = FileManager.default
            if let size = (try? fm.attributesOfItem(atPath: url.path))?[.size] as? UInt64, size > Self.rotateBytes {
                let previous = url.appendingPathExtension("1")
                try? fm.removeItem(at: previous)
                try? fm.moveItem(at: url, to: previous)
            }
            ActionLog.append(line, to: url)
        }
    }
}
