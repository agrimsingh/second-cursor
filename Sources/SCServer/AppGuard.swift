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
