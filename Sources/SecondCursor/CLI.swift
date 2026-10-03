import Foundation
import SCServer
import SCShared

/// Subcommands that run instead of the menu bar app. None of them take the instance lock or start a server.
enum CLI {
    static let usage = """
        Usage: SecondCursor [command]

        Without a command, starts the menu bar app.

        Commands:
          mcp                           Stdio MCP server for hosts that launch a command (Claude Code, Codex,
                                        Gemini CLI, opencode). Proxies to the running app and starts it if needed.
          doctor [--json]               Check that the app is running, permitted and reachable. Exits non-zero
                                        when something needs your action.
          install-mcp --client <cursor|claude|codex|gemini> [--dry-run] [--with-hook]
                                        Add Second Cursor to a client's MCP config, keeping everything else and
                                        writing a .bak first. --dry-run shows only our entry (token shortened)
                                        and what else the file keeps, without writing.
                                        --with-hook (cursor only) also adds the turn-end hook to ~/.cursor/hooks.json.
          install-browser               Install the bundled browser bridge and extension into this account.
          self-test [--browser] [--json] Run a read-only connection check; --browser additionally opens a
                                        disposable test tab, clicks its button, verifies the result, and closes it.
          --show-tasks                  Open the task panel in the running app.
          --turn-ended [--session <id>] Tell the app the agent's turn ended (for agent stop hooks). Reads and
                                        ignores stdin. Always exits 0.
          --pause, --resume             Pause or resume the agent.
          --show-onboarding             Open the permission onboarding panel.
          --help                        Show this help.

        Environment:
          SC_HOME                       Use the config and state of a separate (test) instance in that folder.
        """

    static func value(after flag: String, in arguments: ArraySlice<String>) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.index(after: index) < arguments.endIndex else {
            return nil
        }
        return arguments[arguments.index(after: index)]
    }

    static func printError(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8))
    }

    struct ConfigProblem: LocalizedError {
        let errorDescription: String?
    }

    /// The config as the app saved it. Never creates, rewrites or moves the file: hooks and MCP hosts run these
    /// commands unattended, and resetting the config would rotate the token and drop the user's approvals.
    static func loadConfig() throws -> SCConfig {
        let path = SCPaths.configURL.path
        let data: Data
        do {
            data = try Data(contentsOf: SCPaths.configURL)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            throw ConfigProblem(errorDescription: """
                No config at \(path): Second Cursor has not run yet. Start it with `\(startCommand)`.
                """)
        } catch {
            throw ConfigProblem(errorDescription: "Could not read \(path): \(error.localizedDescription)")
        }
        do {
            return try JSONDecoder().decode(SCConfig.self, from: data)
        } catch {
            throw ConfigProblem(errorDescription: """
                \(path) could not be parsed and was left as is. Fix it, or quit and reopen Second Cursor to start \
                from defaults (it keeps the broken file as config.json.invalid-<time>).
                """)
        }
    }

    static var bundleIdentifier: String { Bundle.main.bundleIdentifier ?? "dev.secondcursor.app" }

    /// How to start the instance this process's config belongs to.
    static var startCommand: String {
        let target = Bundle.main.bundleURL.pathExtension == "app"
            ? "-a \(shellQuoted(Bundle.main.bundlePath))" : "-b \(bundleIdentifier)"
        if let home = SCPaths.customHome {
            return "open -n -g --env SC_HOME=\(shellQuoted(home.path)) \(target)"
        }
        return "open \(target)"
    }

    static func shellQuoted(_ value: String) -> String {
        value.allSatisfy { $0.isLetter || $0.isNumber || "/._-".contains($0) }
            ? value : "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}

/// Blocking and callback POSTs to the app's localhost listener.
enum LocalHTTP {
    static let protocolVersion = "2025-06-18"

    static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        // A system HTTP proxy must never see localhost traffic carrying the bearer token.
        configuration.connectionProxyDictionary = [:]
        configuration.httpMaximumConnectionsPerHost = 16
        return URLSession(configuration: configuration)
    }()

    static func url(_ config: SCConfig, _ path: String) -> URL {
        URL(string: "http://\(SecondCursorServer.host):\(config.port)\(path)")!
    }

    static func request(_ config: SCConfig, path: String, body: Data, headers: [String: String] = [:],
                        timeout: TimeInterval) -> URLRequest {
        var request = URLRequest(url: url(config, path), timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.httpBody = body
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        return request
    }

    static func post(_ config: SCConfig, path: String, body: Data, headers: [String: String] = [:],
                     timeout: TimeInterval) -> Result<(status: Int, body: Data), Error> {
        exchange(request(config, path: path, body: body, headers: headers, timeout: timeout))
            .map { (status: $0.status, body: $0.body) }
    }

    static func exchange(_ request: URLRequest) -> Result<(status: Int, headers: [AnyHashable: Any], body: Data), Error> {
        let done = DispatchSemaphore(value: 0)
        let box = ResultBox()
        session.dataTask(with: request) { data, response, error in
            if let error {
                box.result = .failure(error)
            } else {
                let response = response as? HTTPURLResponse
                box.result = .success((response?.statusCode ?? 0, response?.allHeaderFields ?? [:], data ?? Data()))
            }
            done.signal()
        }.resume()
        done.wait()
        return box.result
    }

    private final class ResultBox: @unchecked Sendable {
        var result: Result<(status: Int, headers: [AnyHashable: Any], body: Data), Error> = .failure(URLError(.unknown))
    }
}

/// One short-lived HTTP MCP connection. Setup and diagnostics use the same handshake as external clients.
final class LocalMCPConnection {
    let config: SCConfig
    private var sessionID: String?
    private var nextID = 0

    init(config: SCConfig) { self.config = config }

    func initialize(name: String) throws -> [String: Any] {
        let result = try rpc(method: "initialize", params: [
            "protocolVersion": LocalHTTP.protocolVersion, "capabilities": [String: Any](),
            "clientInfo": ["name": name, "version": "1"],
        ])
        guard (result["serverInfo"] as? [String: Any])?["name"] as? String == "second-cursor" else {
            throw CLIError.message("The service on this port did not identify itself as Second Cursor.")
        }
        return result
    }

    func call(_ name: String, arguments: [String: Any] = [:], timeout: TimeInterval = 15) throws -> [String: Any] {
        let result = try rpc(method: "tools/call", params: ["name": name, "arguments": arguments], timeout: timeout)
        let text = (result["content"] as? [[String: Any]])?.first(where: { $0["type"] as? String == "text" })?["text"] as? String ?? ""
        if result["isError"] as? Bool == true { throw CLIError.message(text.isEmpty ? "\(name) failed" : text) }
        guard let object = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else {
            throw CLIError.message("\(name) returned an unreadable result")
        }
        return object
    }

    func close() {
        guard let sessionID else { return }
        self.sessionID = nil
        var request = LocalHTTP.request(config, path: SecondCursorServer.path, body: Data(),
                                        headers: ["MCP-Session-Id": sessionID], timeout: 5)
        request.httpMethod = "DELETE"
        _ = LocalHTTP.exchange(request)
    }

    func rpc(method: String, params: [String: Any], timeout: TimeInterval = 5) throws -> [String: Any] {
        nextID += 1
        let body = try JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": nextID, "method": method, "params": params])
        var headers = ["Accept": "application/json", "MCP-Protocol-Version": LocalHTTP.protocolVersion]
        if let sessionID { headers["MCP-Session-Id"] = sessionID }
        let request = LocalHTTP.request(config, path: SecondCursorServer.path, body: body, headers: headers, timeout: timeout)
        let response: (status: Int, headers: [AnyHashable: Any], body: Data)
        do { response = try LocalHTTP.exchange(request).get() }
        catch { throw CLIError(error) }
        if response.status == 401 { throw CLIError.unauthorized }
        let message = (try? JSONSerialization.jsonObject(with: response.body)) as? [String: Any]
        guard response.status == 200, let result = message?["result"] as? [String: Any] else {
            let detail = (message?["error"] as? [String: Any])?["message"] as? String ?? "unreadable response"
            throw CLIError.message("HTTP \(response.status): \(detail)")
        }
        if method == "initialize" {
            sessionID = response.headers.first(where: { String(describing: $0.key).lowercased() == "mcp-session-id" })?.value as? String
        }
        return result
    }
}

enum CLIError: LocalizedError {
    case notRunning
    case unauthorized
    case message(String)

    var errorDescription: String? {
        switch self {
        case .notRunning: return "Second Cursor is not running. Open the app and retry."
        case .unauthorized: return "The running app rejected this configuration. Quit and reopen Second Cursor, then retry."
        case .message(let text): return text
        }
    }

    init(_ error: Error) {
        if let error = error as? URLError, error.code == .cannotConnectToHost {
            self = .notRunning
        } else {
            self = .message(error.localizedDescription)
        }
    }
}

/// `SecondCursor --turn-ended`: contract with the app's POST /turn-ended. Hook hosts pipe JSON on stdin and
/// must never see a failure, so problems go to stderr and the exit code is always 0.
enum TurnEnded {
    static func run(session: String?) -> Int32 {
        let drained = drainStandardInput()
        defer { _ = drained.wait(timeout: .now() + 0.5) }
        let config: SCConfig
        do {
            config = try CLI.loadConfig()
        } catch {
            CLI.printError("Second Cursor --turn-ended: \(error.localizedDescription)")
            return 0
        }
        let body = (try? JSONSerialization.data(withJSONObject: ["session_id": session ?? "legacy"])) ?? Data()
        switch LocalHTTP.post(config, path: "/turn-ended", body: body, timeout: 5) {
        case .success(let response) where response.status == 200:
            print(String(decoding: response.body, as: UTF8.self))
        case .success(let response):
            let detail = String(decoding: response.body.prefix(300), as: UTF8.self)
            CLI.printError("Second Cursor --turn-ended: HTTP \(response.status): \(detail)")
        case .failure(let error):
            let reason = CLIError(error)
            if case .notRunning = reason {
                CLI.printError("Second Cursor --turn-ended: the app is not running on port \(config.port); nothing to end.")
            } else {
                CLI.printError("Second Cursor --turn-ended: \(error.localizedDescription)")
            }
        }
        return 0
    }

    /// Reads stdin to EOF in the background so a hook host never blocks writing to us, without waiting on a
    /// host that keeps the pipe open. A terminal is left alone.
    private static func drainStandardInput() -> DispatchSemaphore {
        let done = DispatchSemaphore(value: 0)
        guard isatty(STDIN_FILENO) == 0 else {
            done.signal()
            return done
        }
        Thread.detachNewThread {
            _ = FileHandle.standardInput.readDataToEndOfFile()
            done.signal()
        }
        return done
    }
}
