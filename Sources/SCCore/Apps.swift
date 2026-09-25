import AppKit
import Foundation
import SCShared

enum Apps {
    static let searchDirectories: [URL] = [
        URL(fileURLWithPath: "/Applications"),
        URL(fileURLWithPath: "/System/Applications"),
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications"),
        URL(fileURLWithPath: "/System/Library/CoreServices/Applications"),
    ]

    static func running() -> [AppInfo] {
        NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular }
            .map(info)
    }

    static func info(_ app: NSRunningApplication) -> AppInfo {
        AppInfo(
            name: app.localizedName ?? app.bundleURL?.deletingPathExtension().lastPathComponent ?? "pid \(app.processIdentifier)",
            bundleID: app.bundleIdentifier,
            pid: app.processIdentifier,
            path: app.bundleURL?.path,
            isRunning: true,
            isActive: app.isActive
        )
    }

    /// App bundles in the standard folders, descending one level into plain folders such as Utilities.
    static func installed() -> [AppInfo] {
        var result: [AppInfo] = []
        var seenPaths = Set<String>()
        func scan(_ directory: URL, depth: Int) {
            let items = (try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles]
            )) ?? []
            for url in items {
                if url.pathExtension == "app" {
                    let path = url.resolvingSymlinksInPath().path
                    guard seenPaths.insert(path).inserted else { continue }
                    result.append(AppInfo(
                        name: url.deletingPathExtension().lastPathComponent,
                        bundleID: Bundle(url: url)?.bundleIdentifier,
                        pid: nil, path: url.path, isRunning: false, isActive: false
                    ))
                } else if depth == 0, (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true {
                    scan(url, depth: 1)
                }
            }
        }
        for directory in searchDirectories { scan(directory, depth: 0) }
        return result
    }

    static func list(includeNotRunning: Bool) -> [AppInfo] {
        let running = running()
        guard includeNotRunning else { return running }
        let runningIDs = Set(running.compactMap(\.bundleID))
        let runningPaths = Set(running.compactMap(\.path))
        let idle = installed().filter { app in
            if let id = app.bundleID, runningIDs.contains(id) { return false }
            return !runningPaths.contains(app.path ?? "")
        }
        return running + idle
    }

    /// Bundle id first, then app name, both case-insensitive; running apps win over installed copies.
    static func resolve(_ query: String) -> AppInfo? {
        let needle = query.trimmingCharacters(in: .whitespaces)
        if needle.hasSuffix(".app"), FileManager.default.fileExists(atPath: needle) {
            let url = URL(fileURLWithPath: needle)
            return AppInfo(name: url.deletingPathExtension().lastPathComponent, bundleID: Bundle(url: url)?.bundleIdentifier,
                           pid: nil, path: url.path, isRunning: false, isActive: false)
        }
        let running = running()
        let installed = installed()
        let matchers: [(AppInfo) -> Bool] = [
            { $0.bundleID?.caseInsensitiveCompare(needle) == .orderedSame },
            { $0.name.caseInsensitiveCompare(needle) == .orderedSame },
        ]
        for matches in matchers {
            if let app = running.first(where: matches) ?? installed.first(where: matches) { return app }
        }
        return nil
    }

    static func launch(_ app: AppInfo, urls: [URL], newInstance: Bool) async throws -> AppInfo {
        if app.isRunning, !newInstance, urls.isEmpty { return app }
        guard let path = app.path else { throw SCError.appNotFound(app.name) }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.addsToRecentItems = false
        configuration.createsNewApplicationInstance = newInstance
        configuration.promptsUserIfNeeded = false
        // Without this, every fresh instance restores the documents of instances we killed earlier.
        if newInstance { configuration.arguments = ["-ApplePersistenceIgnoreState", "YES"] }
        let appURL = URL(fileURLWithPath: path)
        let launched: NSRunningApplication
        do {
            if urls.isEmpty {
                launched = try await NSWorkspace.shared.openApplication(at: appURL, configuration: configuration)
            } else {
                launched = try await NSWorkspace.shared.open(urls, withApplicationAt: appURL, configuration: configuration)
            }
        } catch {
            throw SCError.actionFailed("Could not launch \(app.name): \(error.localizedDescription)")
        }
        let pid = launched.processIdentifier
        let deadline = Date().addingTimeInterval(10)
        while pid <= 0 || kill(pid, 0) != 0 {
            guard Date() < deadline else { throw SCError.actionFailed("\(app.name) did not start") }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        var result = info(launched)
        if result.path == nil { result.path = path }
        return result
    }
}
