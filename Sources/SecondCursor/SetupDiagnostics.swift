import Foundation
import SCShared

struct SetupClientStatus: Sendable {
    let client: InstallMCP.Client
    let configured: Bool
    let path: String
    let detail: String
}

struct SetupSnapshot: Sendable {
    let reachable: Bool
    let accessibility: Bool
    let screenRecording: Bool
    let paused: Bool
    let extensionConnected: Bool
    let summary: String
    let problems: [String]
    let notes: [String]
    let clients: [SetupClientStatus]
}

enum SetupDiagnostics {
    static func snapshot(config: SCConfig?) async -> SetupSnapshot {
        await Task.detached(priority: .utility) { inspect(config: config) }.value
    }

    static func inspect(config: SCConfig?) -> SetupSnapshot {
        let report = Doctor.report(config: config).report
        let permissions = report["permissions"] as? [String: Bool] ?? [:]
        let problems = report["problems"] as? [String] ?? []
        let reachable = report["reachable"] as? Bool ?? false
        let accessibility = permissions["accessibility"] ?? false
        let screenRecording = permissions["screen_recording"] ?? false
        let paused = report["paused"] as? Bool ?? false
        return SetupSnapshot(
            reachable: reachable, accessibility: accessibility, screenRecording: screenRecording,
            paused: paused, extensionConnected: report["extension_connected"] as? Bool ?? false,
            summary: problems.first ?? "Second Cursor is ready for native tasks.", problems: problems,
            notes: (report["notes"] as? [String] ?? []) + BrowserSetup.diagnosticNotes(),
            clients: InstallMCP.Client.allCases.map { clientStatus($0, config: config) })
    }

    private static func activeTOML(_ line: String) -> String {
        var quote: Character?
        var escaped = false
        var result = ""
        for character in line {
            if escaped { result.append(character); escaped = false; continue }
            if character == "\\", quote == "\"" { result.append(character); escaped = true; continue }
            if let current = quote {
                result.append(character)
                if character == current { quote = nil }
            } else if character == "#" { break }
            else {
                result.append(character)
                if character == "\"" || character == "'" { quote = character }
            }
        }
        return result
    }

    static func clientStatus(_ client: InstallMCP.Client, config: SCConfig?) -> SetupClientStatus {
        let path = InstallMCP.homeDirectory.appendingPathComponent(client.relativeConfigPath)
        func result(_ configured: Bool, _ detail: String) -> SetupClientStatus {
            SetupClientStatus(client: client, configured: configured, path: path.path, detail: detail)
        }
        guard FileManager.default.fileExists(atPath: path.path) else { return result(false, "Not configured") }
        guard let data = try? Data(contentsOf: path) else { return result(false, "Configuration could not be read") }
        if client == .codex {
            let text = String(decoding: data, as: UTF8.self)
            // Inspect only our table and never echo other providers' credentials.
            let lines = text.components(separatedBy: .newlines)
            guard let start = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "[mcp_servers.second_cursor]" }) else {
                return result(false, "Second Cursor has not been added")
            }
            let stanza = lines.dropFirst(start + 1).prefix { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("[") }
            var fields: [String: String] = [:]
            var unambiguous = true
            for line in stanza {
                let active = activeTOML(line).trimmingCharacters(in: .whitespaces)
                if active.isEmpty { continue }
                guard let separator = active.firstIndex(of: "=") else { unambiguous = false; continue }
                let key = active[..<separator].trimmingCharacters(in: .whitespaces)
                let value = active[active.index(after: separator)...].trimmingCharacters(in: .whitespaces)
                if fields.updateValue(value, forKey: key) != nil { unambiguous = false }
            }
            // The installer emits a single table with an inline env. Refuse to call alternate/ambiguous
            // layouts current: duplicate tables and env subtables could override an apparently valid line.
            let ownHeaders = lines.filter { InstallMCP.isOurHeader(activeTOML($0)) }
            let expectedEnv = SCPaths.customHome.map { "{ SC_HOME = \(InstallMCP.tomlString($0.path)) }" }
            let current = unambiguous && ownHeaders.count == 1
                && fields["command"] == InstallMCP.tomlString(InstallMCP.executablePath)
                && fields["args"] == "[\"mcp\"]" && fields["env"] == expectedEnv
                && (fields["enabled"] == nil || fields["enabled"] == "true")
            return result(current, current ? "Configured for this app" : "Existing entry needs updating")
        }
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return result(false, "Configuration is invalid; it will be left unchanged")
        }
        guard let entry = (root["mcpServers"] as? [String: Any])?[InstallMCP.serverName] as? [String: Any] else {
            return result(false, "Second Cursor has not been added")
        }
        if client == .cursor {
            guard let config = config ?? (try? CLI.loadConfig()) else { return result(false, "Start Second Cursor to validate its connection") }
            let current = (entry as NSDictionary).isEqual(to: InstallMCP.cursorEntry(config))
            return result(current, current ? "Configured for this app" : "Existing connection needs updating")
        }
        let current = entry["command"] as? String == InstallMCP.executablePath && entry["args"] as? [String] == ["mcp"]
            && (entry["env"] as? [String: String])?["SC_HOME"] == SCPaths.customHome?.path
        return result(current, current ? "Configured for this app" : "Existing entry needs updating")
    }
}

struct SetupCheckResult: Sendable {
    let ok: Bool
    let summary: String
    let details: [String]
}

enum FirstTaskCheck {
    static func run(config: SCConfig?) async -> SetupCheckResult {
        await Task.detached(priority: .utility) { check(config: config, browser: false) }.value
    }

    static func runBrowser(config: SCConfig?) async -> SetupCheckResult {
        await Task.detached(priority: .utility) { check(config: config, browser: true) }.value
    }

    static func runCLI(browser: Bool, json: Bool) -> Int32 {
        let result = check(config: nil, browser: browser)
        if json {
            let data = (try? JSONSerialization.data(withJSONObject: ["ok": result.ok, "summary": result.summary, "details": result.details], options: [.prettyPrinted, .sortedKeys])) ?? Data()
            print(String(decoding: data, as: UTF8.self))
        } else {
            print(result.summary)
            for detail in result.details { print("  \(detail)") }
        }
        return result.ok ? 0 : 1
    }

    private static func check(config: SCConfig?, browser: Bool) -> SetupCheckResult {
        var details: [String] = []
        do {
            let config = try config ?? CLI.loadConfig()
            let connection = LocalMCPConnection(config: config)
            defer { connection.close() }
            _ = try connection.initialize(name: "second-cursor-setup-check")
            let status = try connection.call("status")
            details.append("Authenticated MCP connection established.")
            let permissions = status["permissions"] as? [String: Bool] ?? [:]
            if status["paused"] as? Bool == true { throw CLIError.message("Resume Second Cursor before testing control.") }
            if browser {
                guard (status["browser"] as? [String: Any])?["connected"] as? Bool == true else {
                    throw CLIError.message("Connect the browser extension, then retry the browser control test.")
                }
                _ = try connection.call("task", arguments: ["action": "create", "title": "Browser setup check", "client_name": "Second Cursor Setup"])
                do {
                    try checkBrowser(connection: connection, details: &details)
                    _ = try connection.call("task", arguments: ["action": "update", "status": "completed"])
                } catch {
                    _ = try? connection.call("task", arguments: ["action": "update", "status": "failed"])
                    throw error
                }
                return SetupCheckResult(ok: true, summary: "Browser control verified.", details: details)
            }
            guard permissions["accessibility"] == true, permissions["screen_recording"] == true else {
                throw CLIError.message("Grant Accessibility and Screen Recording before starting native tasks.")
            }
            _ = try connection.call("list_apps")
            details.append("The running app reports both permissions and can list available apps.")
            details.append("This connection check is read-only; native clicks and typing were not exercised.")
            return SetupCheckResult(ok: true, summary: "Connection check passed.", details: details)
        } catch {
            return SetupCheckResult(ok: false, summary: error.localizedDescription, details: details)
        }
    }

    private static func checkBrowser(connection: LocalMCPConnection, details: inout [String]) throws {
        let marker = UUID().uuidString
        let html = """
            <!doctype html><html><head><title>Second Cursor control check</title></head>
            <body style="font:20px system-ui;padding:40px"><h1>Second Cursor control check</h1>
            <p>This temporary page contains no personal information.</p>
            <button onclick="document.getElementById('result').textContent='Verified \(marker)'">Verify control</button>
            <p id="result" role="status">Waiting for the test click.</p></body></html>
            """
        let url = "data:text/html;base64," + Data(html.utf8).base64EncodedString()
        let opened = try connection.call("browser_open", arguments: ["url": url], timeout: 20)
        guard let id = (opened["tab"] as? [String: Any])?["id"] as? Int else {
            throw CLIError.message("The browser did not return a test tab; control was not verified.")
        }
        var closed = false
        defer {
            if !closed {
                if (try? connection.call("browser_close", arguments: ["tab_id": id])) == nil {
                    details.append("The test tab could not be closed; close the tab titled Second Cursor control check manually.")
                }
            }
        }
        _ = try connection.call("browser_command", arguments: ["tab_id": id, "operation": "click", "locator": ["role": "button", "name": "Verify control"]], timeout: 20)
        let snapshot = try connection.call("browser_snapshot", arguments: ["tab_id": id, "full": true], timeout: 15)
        guard let outline = snapshot["outline"] as? String, outline.contains("Verified \(marker)") else {
            throw CLIError.message("The browser accepted a click but the independently read result did not confirm it.")
        }
        details.append("Opened a disposable tab, clicked its test button, and independently verified the changed page.")
        _ = try connection.call("browser_close", arguments: ["tab_id": id])
        closed = true
        details.append("Closed the test tab.")
    }
}
