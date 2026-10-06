import Foundation
import SCServer
import SCShared

/// `SecondCursor install-mcp --client <name> [--dry-run] [--with-hook]`: merges one MCP entry into a client's
/// config. Everything else in the file is kept, the original is copied to `<file>.bak` before writing, and an
/// unchanged entry leaves the file alone. Client configs hold other servers' API keys, so output never echoes
/// file contents: a dry run shows only our entry (token shortened) and the names of what else is kept.
enum InstallMCP {
    enum Client: String, CaseIterable, Sendable {
        case cursor, claude, codex, gemini

        var displayName: String {
            switch self {
            case .cursor: return "Cursor"
            case .claude: return "Claude Code"
            case .codex: return "Codex"
            case .gemini: return "Gemini CLI"
            }
        }

        var relativeConfigPath: String {
            switch self {
            case .cursor: return ".cursor/mcp.json"
            case .claude: return ".claude.json"
            case .codex: return ".codex/config.toml"
            case .gemini: return ".gemini/settings.json"
            }
        }
    }

    static let serverName = "second-cursor"

    /// The HTTP entry Cursor uses; also what the menu's "Copy Cursor MCP config" copies.
    static func cursorEntry(_ config: SCConfig) -> [String: Any] {
        [
            "url": "http://\(SecondCursorServer.host):\(config.port)\(SecondCursorServer.path)",
            "headers": ["Authorization": "Bearer \(config.token)"],
        ]
    }

    static func run(_ arguments: ArraySlice<String>) -> Int32 {
        let names = Client.allCases.map(\.rawValue).joined(separator: "|")
        guard let name = CLI.value(after: "--client", in: arguments) else {
            CLI.printError("install-mcp needs --client <\(names)>\n\n\(CLI.usage)")
            return 2
        }
        guard let client = Client(rawValue: name) else {
            CLI.printError("Unknown client \"\(name)\"; use one of \(names).")
            return 2
        }
        let dryRun = arguments.contains("--dry-run")
        let withHook = arguments.contains("--with-hook")
        if withHook && client != .cursor {
            CLI.printError("--with-hook is only for --client cursor (Cursor's hooks run `SecondCursor --turn-ended`).")
            return 2
        }

        do {
            let messages = try install(client: client, dryRun: dryRun, withHook: withHook)
            for message in messages { print(message) }
            return 0
        } catch {
            CLI.printError("install-mcp: \(error.localizedDescription)")
            return 1
        }
    }

    static var homeDirectory: URL {
        URL(fileURLWithPath: ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory(), isDirectory: true)
    }

    static var executablePath: String {
        (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])).resolvingSymlinksInPath().path
    }

    /// Structured entry point shared by setup UI and the CLI. Returned messages contain no complete tokens.
    static func install(client: Client, dryRun: Bool = false, withHook: Bool = false) throws -> [String] {
        guard !withHook || client == .cursor else { throw Refusal("Turn-end hooks are only supported for Cursor.") }
        let home = homeDirectory
        let command = executablePath
        let scHome = SCPaths.customHome?.path
        var stdio: [String: Any] = ["command": command, "args": ["mcp"]]
        if let scHome { stdio["env"] = ["SC_HOME": scHome] }
        let path = home.appendingPathComponent(client.relativeConfigPath)
        var messages: [String] = []
        switch client {
        case .cursor:
            let config = try CLI.loadConfig()
            messages += try update(path, dryRun: dryRun) {
                try mergeJSON($0, section: "mcpServers", key: serverName, value: cursorEntry(config))
            }
        case .claude:
            var entry = stdio
            entry["type"] = "stdio"
            messages += try update(path, dryRun: dryRun) {
                try mergeJSON($0, section: "mcpServers", key: serverName, value: entry)
            }
        case .gemini:
            messages += try update(path, dryRun: dryRun) {
                try mergeJSON($0, section: "mcpServers", key: serverName, value: stdio)
            }
        case .codex:
            messages += try update(path, dryRun: dryRun) {
                try mergeCodex($0, command: command, scHome: scHome)
            }
        }
        if withHook {
            messages += try update(home.appendingPathComponent(".cursor/hooks.json"), dryRun: dryRun) {
                try mergeHook($0, command: "\(CLI.shellQuoted(command)) --turn-ended")
            }
        }
        return messages
    }

    private struct Refusal: LocalizedError {
        let errorDescription: String?
        init(_ message: String) { errorDescription = message }
    }

    private struct Merge {
        var data: Data
        var change: String
        /// Our entry alone, in the file's own format, with the bearer token shortened.
        var preview: String
        /// Names of everything else in the file, all kept as is.
        var kept: [String]
    }

    /// Computes the new contents from the old (nil when missing) and writes them unless unchanged.
    private static func update(_ file: URL, dryRun: Bool, _ transform: (Data?) throws -> Merge) throws -> [String] {
        let target = file.resolvingSymlinksInPath()
        let fm = FileManager.default
        let original: Data?
        do { original = try Data(contentsOf: target) }
        catch let error as CocoaError where error.code == .fileReadNoSuchFile { original = nil }
        catch { throw Refusal("Could not read \(target.path); it was left unchanged. \(error.localizedDescription)") }
        let merge = try transform(original)
        let updated = merge.data
        let unchanged = updated == original

        if dryRun {
            let action = unchanged ? "unchanged" : original == nil ? "would create" : "would update"
            var messages = ["# dry run: \(action) \(target.path): \(merge.change)"]
            if !merge.kept.isEmpty {
                let shown = merge.kept.prefix(12).joined(separator: ", ")
                messages.append("# keeps: \(shown)\(merge.kept.count > 12 ? " and \(merge.kept.count - 12) more" : "")")
            }
            messages.append(merge.preview.trimmingCharacters(in: .newlines))
            return messages
        }
        // The files hold bearer tokens and API keys, so they and their backups are always private, even when an
        // earlier install or the client itself left them readable by other accounts.
        if unchanged {
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
            return ["\(target.path): \(merge.change); nothing to write"]
        }
        try fm.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        var note = "created"
        if original != nil {
            let backup = target.appendingPathExtension("bak")
            try? fm.removeItem(at: backup)
            try fm.copyItem(at: target, to: backup)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: backup.path)
            note = "backup: \(backup.path)"
        }
        try updated.write(to: target, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
        return ["Updated \(target.path): \(merge.change) (\(note))"]
    }

    private static func jsonObject(_ data: Data?, _ label: String) throws -> [String: Any] {
        guard let data, !String(decoding: data, as: UTF8.self).allSatisfy(\.isWhitespace) else { return [:] }
        let parsed: Any
        do {
            parsed = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw Refusal("\(label) is not valid JSON; fix or move it, then retry. Nothing was changed.")
        }
        guard let object = parsed as? [String: Any] else {
            throw Refusal("\(label) is not a JSON object; refusing to modify it.")
        }
        return object
    }

    private static func jsonData(_ object: [String: Any]) throws -> Data {
        var data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        data.append(0x0A)
        return data
    }

    private static func preview(_ object: [String: Any]) throws -> String {
        String(decoding: try jsonData(object), as: UTF8.self)
    }

    /// Our entry as printed: the bearer token cut to its last four characters.
    private static func redacted(_ entry: [String: Any]) -> [String: Any] {
        guard var headers = entry["headers"] as? [String: Any],
              let authorization = headers["Authorization"] as? String else { return entry }
        var copy = entry
        headers["Authorization"] = "Bearer …\(authorization.suffix(4))"
        copy["headers"] = headers
        return copy
    }

    private static func mergeJSON(_ data: Data?, section: String, key: String, value: [String: Any]) throws -> Merge {
        var root = try jsonObject(data, "The existing file")
        guard var servers = (root[section] ?? [String: Any]()) as? [String: Any] else {
            throw Refusal("\"\(section)\" in the existing file is not an object; refusing to modify it.")
        }
        let label = "\(section).\(key)"
        let shown = try preview([section: [key: redacted(value)]])
        let kept = servers.keys.filter { $0 != key }.sorted().map { "\(section).\($0)" }
            + root.keys.filter { $0 != section }.sorted()
        if let data, let existing = servers[key] as? NSDictionary, existing.isEqual(to: value) {
            return Merge(data: data, change: "\(label) already up to date", preview: shown, kept: kept)
        }
        let change = servers[key] == nil ? "added \(label)" : "replaced \(label)"
        servers[key] = value
        root[section] = servers
        return Merge(data: try jsonData(root), change: change, preview: shown, kept: kept)
    }

    private static func mergeHook(_ data: Data?, command: String) throws -> Merge {
        var root = try jsonObject(data, "The existing hooks.json")
        guard var hooks = (root["hooks"] ?? [String: Any]()) as? [String: Any],
              var stop = (hooks["stop"] ?? [Any]()) as? [Any] else {
            throw Refusal("hooks.json has an unexpected shape (\"hooks\" or \"hooks.stop\"); refusing to modify it.")
        }
        let entry: [String: Any] = ["command": command, "timeout": 10]
        let ours = stop.firstIndex { hook in
            let text = (hook as? [String: Any])?["command"] as? String ?? ""
            return text.contains("SecondCursor") && text.contains("--turn-ended")
        }
        let otherStops = stop.count - (ours == nil ? 0 : 1)
        let shown = try preview(["hooks": ["stop": [entry]]])
        let kept = hooks.keys.filter { $0 != "stop" }.sorted().map { "hooks.\($0)" }
            + (otherStops > 0 ? ["hooks.stop (\(otherStops) other)"] : [])
            + root.keys.filter { $0 != "hooks" }.sorted()
        if let data, let ours, (stop[ours] as? NSDictionary)?.isEqual(to: entry) == true, root["version"] != nil {
            return Merge(data: data, change: "stop hook already up to date", preview: shown, kept: kept)
        }
        let change: String
        if let ours {
            stop[ours] = entry
            change = "replaced the Second Cursor stop hook"
        } else {
            stop.append(entry)
            change = "added a stop hook running --turn-ended"
        }
        hooks["stop"] = stop
        root["hooks"] = hooks
        root["version"] = root["version"] ?? 1
        return Merge(data: try jsonData(root), change: change, preview: shown, kept: kept)
    }

    /// Replaces only our `[mcp_servers.second_cursor]` table, textually, so the rest of the file (comments,
    /// ordering, formatting) is untouched without a TOML parser.
    private static func mergeCodex(_ data: Data?, command: String, scHome: String?) throws -> Merge {
        let header = "[mcp_servers.second_cursor]"
        var table = [header, "command = \(tomlString(command))", "args = [\"mcp\"]"]
        if let scHome {
            table.append("env = { SC_HOME = \(tomlString(scHome)) }")
        }

        let text = data.map { String(decoding: $0, as: UTF8.self) } ?? ""
        var lines = text.isEmpty ? [] : text.components(separatedBy: "\n")
        if lines.last == "" {
            lines.removeLast()
        }

        // Each of our tables (and sub-tables) runs from its header to its last key line before the next header;
        // blank and comment lines just before the next header belong to what follows.
        var ranges: [Range<Int>] = []
        var index = 0
        while index < lines.count {
            guard isOurHeader(lines[index]) else {
                index += 1
                continue
            }
            var end = index + 1
            while end < lines.count && !isTOMLHeader(lines[end]) {
                end += 1
            }
            var last = end
            while last > index + 1 && isBlankOrComment(lines[last - 1]) {
                last -= 1
            }
            ranges.append(index..<last)
            index = end
        }

        let shown = table.joined(separator: "\n")
        let kept = keptNames(lines, outside: ranges)
        if let data, ranges.count == 1, Array(lines[ranges[0]]) == table {
            return Merge(data: data, change: "mcp_servers.second_cursor already up to date", preview: shown, kept: kept)
        }
        let change = ranges.isEmpty ? "added [mcp_servers.second_cursor]" : "replaced [mcp_servers.second_cursor]"
        if let first = ranges.first {
            for range in ranges.reversed() {
                lines.removeSubrange(range)
            }
            lines.insert(contentsOf: table, at: first.lowerBound)
        } else {
            if let last = lines.last, !last.trimmingCharacters(in: .whitespaces).isEmpty {
                lines.append("")
            }
            lines += table
        }
        return Merge(data: Data((lines.joined(separator: "\n") + "\n").utf8), change: change, preview: shown, kept: kept)
    }

    /// A table or array-of-tables header, whatever its key quoting (`[projects."/a"]`, `[mcp_servers."x:y"]`).
    private static func isTOMLHeader(_ line: String) -> Bool {
        line.first { !$0.isWhitespace } == "["
    }

    /// `[mcp_servers.second_cursor]` or one of its sub-tables, in any quoting or spacing, trailing comment ignored.
    static func isOurHeader(_ line: String) -> Bool {
        guard isTOMLHeader(line) else { return false }
        let name = String(line.prefix { $0 != "#" }).filter { !$0.isWhitespace }
        return ["second_cursor", "\"second_cursor\"", "'second_cursor'"].contains { key in
            name == "[mcp_servers.\(key)]" || name.hasPrefix("[mcp_servers.\(key).")
        }
    }

    private static func isBlankOrComment(_ line: String) -> Bool {
        let text = line.trimmingCharacters(in: .whitespaces)
        return text.isEmpty || text.hasPrefix("#")
    }

    /// Top-level keys and table headers outside our tables, in file order, for the dry-run summary.
    private static func keptNames(_ lines: [String], outside ranges: [Range<Int>]) -> [String] {
        var names: [String] = []
        var inTable = false
        for (number, line) in lines.enumerated() where !ranges.contains(where: { $0.contains(number) }) {
            var name: String?
            if isTOMLHeader(line) {
                inTable = true
                name = String(line.prefix { $0 != "#" }).trimmingCharacters(in: .whitespaces)
            } else if !inTable, let key = line.range(of: #"^\s*[A-Za-z0-9_.\-"']+(?=\s*=)"#, options: .regularExpression) {
                name = line[key].trimmingCharacters(in: .whitespaces)
            }
            if let name, !names.contains(name) {
                names.append(name)
            }
        }
        return names
    }

    static func tomlString(_ value: String) -> String {
        var escaped = ""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\\": escaped += "\\\\"
            case "\"": escaped += "\\\""
            case _ where scalar.value < 0x20 || scalar.value == 0x7F:
                escaped += String(format: "\\u%04X", scalar.value)
            default: escaped.unicodeScalars.append(scalar)
            }
        }
        return "\"\(escaped)\""
    }
}
