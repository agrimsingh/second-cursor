import AppKit
import CoreGraphics
import SCShared

/// Enforces `SCConfig.blockedBundleIDs` independently of the desktop implementation,
/// so a bug there cannot let the agent touch a blocked app.
struct AppGuard {
    let config: SCConfig

    /// Owner of a window, read straight from the window server (no Accessibility needed).
    /// `.optionIncludingWindow` returns nothing for minimized windows and some off-Space ones, so fall back to the full list.
    static func owner(of windowID: UInt32) -> (pid: Int32, bundleID: String?)? {
        func find(_ list: [[String: Any]]) -> [String: Any]? {
            list.first { ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == windowID }
        }
        let single = CGWindowListCopyWindowInfo([.optionIncludingWindow], CGWindowID(windowID)) as? [[String: Any]] ?? []
        guard let info = find(single) ?? find(CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []),
              let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value
        else { return nil }
        return (pid, NSRunningApplication(processIdentifier: pid)?.bundleIdentifier)
    }

    func checkWindow(_ windowID: UInt32) throws -> (pid: Int32, bundleID: String?) {
        guard let owner = Self.owner(of: windowID) else { throw SCError.windowNotFound(windowID) }
        if owner.pid == ProcessInfo.processInfo.processIdentifier {
            throw SCError.notAllowed("The agent cannot control Second Cursor or its local approval windows")
        }
        if config.isBlocked(bundleID: owner.bundleID) {
            throw SCError.notAllowed("Window \(windowID) belongs to \(owner.bundleID ?? "a blocked app"), which is blocked for the agent")
        }
        return owner
    }

    /// Refuses a launch query that could resolve to a blocked app. Mirrors the desktop's
    /// case-insensitive substring matching, so "keychain" is refused, unless the query exactly
    /// names an allowed app.
    func checkLaunch(_ query: String, installedApps: () async -> [AppInfo]) async throws {
        let q = Self.normalized(query)
        guard !q.isEmpty else { throw SCError.invalidArgument("app is required") }

        if query.contains("/") {
            let path = (query as NSString).expandingTildeInPath
            if config.isBlocked(bundleID: Bundle(path: path)?.bundleIdentifier) { throw refusal(query) }
            return
        }

        let blocked = blockedNames()
        if blocked.contains(q) { throw refusal(query) }
        guard blocked.contains(where: { $0.contains(q) }) else { return }
        let exactAllowed = await installedApps().contains { app in
            !config.isBlocked(bundleID: app.bundleID)
                && (Self.normalized(app.name) == q || app.bundleID?.lowercased() == q)
        }
        if !exactAllowed { throw refusal(query) }
    }

    func checkLaunched(_ app: AppInfo) throws {
        if config.isBlocked(bundleID: app.bundleID) { throw refusal(app.name) }
    }

    /// Resolves a launch query the way the desktop does (bundle id, then name; running apps first; or an
    /// existing .app path), so approval covers the app that would actually start.
    static func launchTarget(_ query: String, apps: () async -> [AppInfo]) async throws -> AppTarget {
        let needle = query.trimmingCharacters(in: .whitespaces)
        if needle.hasSuffix(".app"), FileManager.default.fileExists(atPath: needle) {
            let url = URL(fileURLWithPath: needle)
            let bundleID = Bundle(url: url)?.bundleIdentifier
            return AppTarget(
                key: bundleID ?? url.path, name: url.deletingPathExtension().lastPathComponent,
                hasBundleID: bundleID != nil)
        }
        let all = await apps()
        let matchers: [(AppInfo) -> Bool] = [
            { $0.bundleID?.caseInsensitiveCompare(needle) == .orderedSame },
            { $0.name.caseInsensitiveCompare(needle) == .orderedSame },
        ]
        for matches in matchers {
            if let app = all.first(where: matches) {
                return AppTarget(key: app.bundleID ?? app.path ?? app.name, name: app.name, hasBundleID: app.bundleID != nil)
            }
        }
        throw SCError.appNotFound(query)
    }

    static func windowTarget(_ owner: WindowOwner) -> AppTarget {
        let app = NSRunningApplication(processIdentifier: owner.pid)
        let bundleID = owner.bundleID ?? app?.bundleIdentifier
        return AppTarget(
            key: bundleID ?? app?.bundleURL?.path ?? app?.executableURL?.path ?? "pid:\(owner.pid)",
            name: app?.localizedName ?? bundleID ?? "process \(owner.pid)",
            hasBundleID: bundleID != nil)
    }

    private func refusal(_ query: String) -> SCError {
        .notAllowed("\"\(query)\" is blocked for the agent (blocked apps are listed in \(SCPaths.configURL.path))")
    }

    private func blockedNames() -> Set<String> {
        var names = Set<String>()
        for bundleID in config.blockedBundleIDs {
            names.insert(bundleID.lowercased())
            for url in NSWorkspace.shared.urlsForApplications(withBundleIdentifier: bundleID) {
                names.insert(Self.normalized(url.lastPathComponent))
                names.insert(Self.normalized(FileManager.default.displayName(atPath: url.path)))
                let info = Bundle(url: url)?.localizedInfoDictionary ?? Bundle(url: url)?.infoDictionary ?? [:]
                for key in ["CFBundleDisplayName", "CFBundleName"] {
                    if let name = info[key] as? String { names.insert(Self.normalized(name)) }
                }
            }
        }
        names.remove("")
        return names
    }

    private static func normalized(_ name: String) -> String {
        var name = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if name.hasSuffix(".app") { name.removeLast(4) }
        return name
    }
}

/// Every local approval goes through here: per-app consent (`SCConfig.appApproval`), sensitive browser
/// operations and `confirmationRequiredTools`, answered as `SCConfig.approvals` says. Per-app grants change
/// at runtime, so they live behind a lock instead of in `AppGuard.config`. Blocked apps are refused first.
final class AppApprovals: @unchecked Sendable {
    enum Mode {
        case prompt, deny, allow
    }

    private final class Pending: @unchecked Sendable {
        var decision: ApprovalDecision?
        var waiters: [UUID: (deadline: Date, stillAllowed: @Sendable () -> Bool)] = [:]
    }

    let appApprovalRequired: Bool
    let mode: Mode
    private let lock = NSLock()
    private var always: Set<String>
    private var bySession: [String: Set<String>] = [:]
    /// The session's current turn; values are never reused, so a turn that ended can't match a later one.
    private var generations: [String: Int] = [:]
    private var lastGeneration = 0
    private var pending: [String: Pending] = [:]
    private var configModified: Date?
    private var nextConfigCheck = Date.distantPast

    init(config: SCConfig) {
        appApprovalRequired = config.appApproval != "off"
        switch config.approvals {
        case "deny": mode = .deny
        // Granting without a person is only for disposable test instances, never the user's own app.
        case "allow" where SCPaths.customHome != nil: mode = .allow
        default: mode = .prompt
        }
        always = Set(config.approvedBundleIDs ?? [])
        configModified = Self.configModificationDate()
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    func isApproved(_ app: AppTarget, session: String) -> Bool {
        locked { isApprovedLocked(app.key, session: session) }
    }

    private func isApprovedLocked(_ key: String, session: String) -> Bool {
        guard appApprovalRequired else { return true }
        reloadAlwaysLocked()
        return always.contains(key) || bySession[session]?.contains(key) == true
    }

    /// The prompt promises "until you remove it from the approved apps list", so edits to config.json
    /// must apply without a restart.
    private func reloadAlwaysLocked() {
        let now = Date()
        guard now >= nextConfigCheck else { return }
        nextConfigCheck = now.addingTimeInterval(1)
        let modified = Self.configModificationDate()
        guard modified != configModified else { return }
        configModified = modified
        if let data = try? Data(contentsOf: SCPaths.configURL),
            let config = try? JSONDecoder().decode(SCConfig.self, from: data)
        {
            always = Set(config.approvedBundleIDs ?? [])
        }
    }

    private static func configModificationDate() -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: SCPaths.configURL.path))?[.modificationDate] as? Date
    }

    /// Drops "Allow for this session" grants and starts a new turn; returns how many grants there were.
    @discardableResult
    func clear(session: String) -> Int {
        locked {
            generations[session] = nil
            return bySession.removeValue(forKey: session)?.count ?? 0
        }
    }

    private func generationLocked(_ session: String) -> Int {
        if let generation = generations[session] { return generation }
        lastGeneration += 1
        generations[session] = lastGeneration
        return lastGeneration
    }

    /// Shows the prompt unless it is no longer needed. Requests that only ask for the same app in the same
    /// session share one prompt, which stays up while any of them is still waiting.
    func authorize(
        _ request: ApprovalRequest, deadline: Date, stillAllowed: @escaping @Sendable () -> Bool
    ) async -> Bool {
        switch mode {
        case .deny: return false
        case .allow: return true
        case .prompt: break
        }
        let generation = locked { generationLocked(request.session) }
        guard let app = request.app, request.grants.isEmpty else {
            let decision = await Approval.request(request, deadline: deadline, stillAllowed: stillAllowed)
            record(decision, request, generation: generation)
            return decision != .deny
        }
        let key = request.session + "\n" + app.key
        let id = UUID()
        let entry: Pending? = locked {
            if isApprovedLocked(app.key, session: request.session) { return nil }
            let entry: Pending
            if let existing = pending[key] {
                entry = existing
            } else {
                entry = Pending()
                pending[key] = entry
                Task {
                    let decision = await Approval.request(request, deadline: .distantFuture) {
                        self.anyoneWaiting(entry)
                    }
                    self.record(decision, request, generation: generation)
                    self.locked {
                        entry.decision = decision
                        if self.pending[key] === entry { self.pending[key] = nil }
                    }
                }
            }
            entry.waiters[id] = (deadline, stillAllowed)
            return entry
        }
        guard let entry else { return true }
        defer { locked { entry.waiters[id] = nil } }
        while true {
            if let decision = locked({ entry.decision }) { return decision != .deny }
            guard Date() < deadline, stillAllowed() else { return false }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    private func anyoneWaiting(_ entry: Pending) -> Bool {
        let waiters = locked { Array(entry.waiters.values) }
        let now = Date()
        return waiters.contains { now < $0.deadline && $0.stillAllowed() }
    }

    /// A session grant answered after its turn ended lets the waiting calls through once but is not kept.
    private func record(_ decision: ApprovalDecision, _ request: ApprovalRequest, generation: Int) {
        guard let app = request.app else { return }
        let persist: Bool = locked {
            switch decision {
            case .session:
                if generations[request.session] == generation {
                    bySession[request.session, default: []].insert(app.key)
                }
                return false
            case .always:
                return always.insert(app.key).inserted
            case .once, .deny:
                return false
            }
        }
        guard persist else { return }
        do {
            try SCConfig.addApprovedBundleID(app.key)
        } catch {
            NSLog("Second Cursor: could not save %@ to the approved apps list: %@", app.key, "\(error)")
        }
    }
}
