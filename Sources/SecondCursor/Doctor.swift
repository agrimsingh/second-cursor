import Foundation
import SCServer
import SCShared

/// `SecondCursor doctor [--json]`: asks the running app (never this process, whose permissions don't matter)
/// for its state. Exits 1 when something needs the user's action. Never starts the app or writes a config.
enum Doctor {
    struct Item {
        enum Level { case ok, problem, note }
        var level: Level
        var text: String
    }

    static func report(config: SCConfig? = nil) -> (report: [String: Any], items: [Item]) {
        var report: [String: Any] = [
            "config_path": SCPaths.configURL.path, "url": NSNull(), "reachable": false, "version": NSNull(),
            "permissions": NSNull(), "paused": PauseState.isPaused, "extension_connected": NSNull(),
        ]
        var items: [Item] = []

        do {
            let config = try config ?? CLI.loadConfig()
            report["url"] = LocalHTTP.url(config, SecondCursorServer.path).absoluteString
            items += check(config, into: &report)
        } catch {
            items.append(Item(level: .problem, text: error.localizedDescription))
        }

        let problems = items.filter { $0.level == .problem }.map(\.text)
        report["problems"] = problems
        report["notes"] = items.filter { $0.level == .note }.map(\.text)
        report["ok"] = problems.isEmpty
        report["browser_setup_notes"] = BrowserSetup.diagnosticNotes()
        report["extension_directory"] = BrowserSetup.extensionDirectory.path
        report["clients"] = InstallMCP.Client.allCases.map { client in
            let status = SetupDiagnostics.clientStatus(client, config: config)
            return ["client": client.rawValue, "configured": status.configured, "path": status.path, "detail": status.detail] as [String: Any]
        }
        return (report, items)
    }

    static func run(json: Bool) -> Int32 {
        let (report, items) = report()
        let problems = report["problems"] as? [String] ?? []
        if json {
            let data = (try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])) ?? Data()
            print(String(decoding: data, as: UTF8.self))
        } else {
            print("Second Cursor doctor")
            for item in items {
                let mark = switch item.level {
                case .ok: "✓"
                case .problem: "✗"
                case .note: "·"
                }
                print("\(mark) \(item.text)")
            }
            for note in report["browser_setup_notes"] as? [String] ?? [] { print("· \(note)") }
            print("  Config: \(SCPaths.configURL.path)")
        }
        return problems.isEmpty ? 0 : 1
    }

    private static func check(_ config: SCConfig, into report: inout [String: Any]) -> [Item] {
        let url = LocalHTTP.url(config, SecondCursorServer.path).absoluteString
        let connection = LocalMCPConnection(config: config)
        defer { connection.close() }
        let initialize: Result<[String: Any], CLIError> = Result { try connection.initialize(name: "second-cursor-doctor") }
            .mapError { ($0 as? CLIError) ?? CLIError($0) }
        let serverInfo: [String: Any]
        switch initialize {
        case .success(let result):
            serverInfo = result["serverInfo"] as? [String: Any] ?? [:]
        case .failure(.notRunning):
            return [Item(level: .problem, text: """
                App not running: nothing answers at \(url). Start it with `\(CLI.startCommand)`.
                """)]
        case .failure(.unauthorized):
            return [Item(level: .problem, text: """
                The app on port \(config.port) rejected the token in the config. Another program may be using \
                that port, or the config changed after the app started; quit and reopen Second Cursor.
                """)]
        case .failure(.message(let message)):
            return [Item(level: .problem, text: "Something answers at \(url) but not as Second Cursor: \(message)")]
        }
        report["reachable"] = true
        let version = serverInfo["version"] as? String
        report["version"] = version ?? NSNull()
        var items = [Item(level: .ok, text: "App reachable at \(url) (version \(version ?? "unknown"))")]

        let status: Result<[String: Any], CLIError> = Result {
            try connection.rpc(method: "tools/call", params: ["name": "status", "arguments": [String: Any]()])
        }.mapError { ($0 as? CLIError) ?? CLIError($0) }
        guard case .success(let result) = status,
              let text = (result["content"] as? [[String: Any]])?.first(where: { $0["type"] as? String == "text" })?["text"] as? String,
              let state = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any]
        else {
            items.append(Item(level: .problem, text: "The status tool did not answer: \(status)"))
            return items
        }

        let permissions = state["permissions"] as? [String: Any] ?? [:]
        let accessibility = permissions["accessibility"] as? Bool ?? false
        let screenRecording = permissions["screen_recording"] as? Bool ?? false
        let paused = state["paused"] as? Bool ?? false
        let extensionConnected = (state["browser"] as? [String: Any])?["connected"] as? Bool ?? false
        report["permissions"] = ["accessibility": accessibility, "screen_recording": screenRecording]
        report["paused"] = paused
        report["extension_connected"] = extensionConnected

        items.append(accessibility
            ? Item(level: .ok, text: "Accessibility granted")
            : Item(level: .problem, text: """
                Accessibility not granted: run `SecondCursor --show-onboarding`, or turn Second Cursor on in \
                System Settings > Privacy & Security > Accessibility.
                """))
        items.append(screenRecording
            ? Item(level: .ok, text: "Screen Recording granted")
            : Item(level: .problem, text: """
                Screen Recording not granted: turn Second Cursor on in System Settings > Privacy & Security > \
                Screen & System Audio Recording, then quit and reopen it.
                """))
        items.append(paused
            ? Item(level: .problem, text: "Agent is paused: resume it from the menu bar or run `SecondCursor --resume`.")
            : Item(level: .ok, text: "Agent not paused"))
        items.append(extensionConnected
            ? Item(level: .ok, text: "Chrome extension connected")
            : Item(level: .note, text: "Chrome extension not connected (only needed for browser tools)"))
        return items
    }
}
