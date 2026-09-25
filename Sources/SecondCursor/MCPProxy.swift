import Foundation
import SCServer
import SCShared

/// `SecondCursor mcp`: newline-delimited JSON-RPC on stdin/stdout, forwarded to the running app's MCP endpoint.
/// Each message is POSTed as it arrives; responses are written as they finish, one line each, matched by id,
/// so a slow tool call never holds back a quick one. Stdout carries nothing but responses.
final class MCPProxy: @unchecked Sendable {
    /// How long a request keeps retrying while the app starts.
    private static let launchWindow: TimeInterval = 10

    private let config: SCConfig
    private let inFlight = DispatchGroup()
    private let outputLock = NSLock()
    private let stateLock = NSLock()
    private var protocolVersion = LocalHTTP.protocolVersion
    private var launched: Bool

    private init(config: SCConfig, launched: Bool) {
        self.config = config
        self.launched = launched
    }

    static func run() -> Int32 {
        signal(SIGPIPE, SIG_IGN)
        // Only the app creates the config (on its first launch), so a host that starts before the app ever
        // ran launches it and waits for the file instead of writing one itself.
        var launched = false
        if !FileManager.default.fileExists(atPath: SCPaths.configURL.path) {
            launchApp()
            launched = true
            let deadline = Date().addingTimeInterval(launchWindow)
            while !FileManager.default.fileExists(atPath: SCPaths.configURL.path) && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.25)
            }
        }
        let config: SCConfig
        do {
            config = try CLI.loadConfig()
        } catch {
            CLI.printError("Second Cursor mcp: \(error.localizedDescription)")
            return 1
        }
        let proxy = MCPProxy(config: config, launched: launched)
        while let line = readLine(strippingNewline: true) {
            proxy.handle(line)
        }
        // Hosts close stdin and then wait for exit, so answer what is already in flight first.
        _ = proxy.inFlight.wait(timeout: .now() + 90)
        return 0
    }

    private func handle(_ line: String) {
        let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let body = Data(text.utf8)
        guard let parsed = try? JSONSerialization.jsonObject(with: body) else {
            write(Self.error(id: NSNull(), code: -32700, message: "Parse error: stdin lines must be JSON-RPC messages"))
            return
        }
        let message = parsed as? [String: Any]
        let id = message?["id"].flatMap { $0 is NSNull ? nil : $0 }
        let isRequest = id != nil && message?["method"] != nil
        let isInitialize = message?["method"] as? String == "initialize"

        inFlight.enter()
        forward(body, deadline: Date().addingTimeInterval(Self.launchWindow)) { [self] outcome in
            defer { inFlight.leave() }
            guard isRequest, let id else { return }
            write(response(to: id, outcome, isInitialize: isInitialize))
        }
    }

    private enum Outcome {
        case http(status: Int, contentType: String, body: Data)
        case failed(Error)
    }

    private func forward(_ body: Data, deadline: Date, completion: @escaping @Sendable (Outcome) -> Void) {
        let headers = ["Accept": "application/json", "MCP-Protocol-Version": currentProtocolVersion()]
        let request = LocalHTTP.request(config, path: SecondCursorServer.path, body: body, headers: headers, timeout: 90)
        LocalHTTP.session.dataTask(with: request) { [self] data, response, error in
            if let error {
                if case .notRunning = CLIError(error), Date() < deadline {
                    launchAppOnce()
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.25) {
                        self.forward(body, deadline: deadline, completion: completion)
                    }
                    return
                }
                completion(.failed(error))
                return
            }
            let http = response as? HTTPURLResponse
            completion(.http(status: http?.statusCode ?? 0,
                             contentType: http?.value(forHTTPHeaderField: "Content-Type") ?? "",
                             body: data ?? Data()))
        }.resume()
    }

    private func response(to id: Any, _ outcome: Outcome, isInitialize: Bool) -> Data {
        switch outcome {
        case .failed(let error):
            if case .notRunning = CLIError(error) {
                return Self.error(id: id, code: -32000, message: """
                    Second Cursor is not running on 127.0.0.1:\(config.port) and did not start within \
                    \(Int(Self.launchWindow)) s. Start it with `\(CLI.startCommand)` or run `SecondCursor doctor`.
                    """)
            }
            return Self.error(id: id, code: -32603, message: "Could not reach Second Cursor: \(error.localizedDescription)")

        case .http(let status, let contentType, let body):
            let payload = contentType.contains("text/event-stream") ? Self.lastEvent(body) : body
            let message = (try? JSONSerialization.jsonObject(with: payload)) as? [String: Any]
            if (200..<300).contains(status), let message, message["result"] != nil || message["error"] != nil {
                if isInitialize, let version = (message["result"] as? [String: Any])?["protocolVersion"] as? String {
                    setProtocolVersion(version)
                }
                return Data(payload.map { $0 == 0x0A || $0 == 0x0D ? 0x20 : $0 })
            }
            let serverError = message?["error"] as? [String: Any]
            var text = serverError?["message"] as? String ?? String(decoding: payload.prefix(300), as: UTF8.self)
            if status == 401 {
                text = "Unauthorized (HTTP 401): the token in \(SCPaths.configURL.path) was rejected by the app on port \(config.port). \(text)"
            } else {
                text = "HTTP \(status) from Second Cursor: \(text.isEmpty ? "empty response" : text)"
            }
            return Self.error(id: id, code: serverError?["code"] as? Int ?? -32603, message: text)
        }
    }

    private func launchAppOnce() {
        stateLock.lock()
        defer { stateLock.unlock() }
        guard !launched else { return }
        launched = true
        Self.launchApp()
    }

    /// Starts the app this config belongs to: the enclosing bundle when run from one, else by bundle id.
    /// A test instance (SC_HOME) gets a new process with the same SC_HOME.
    private static func launchApp() {
        var arguments = ["-g"]
        if let home = SCPaths.customHome {
            arguments += ["-n", "--env", "SC_HOME=\(home.path)"]
        }
        if Bundle.main.bundleURL.pathExtension == "app" {
            arguments += ["-a", Bundle.main.bundlePath]
        } else {
            arguments += ["-b", CLI.bundleIdentifier]
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = arguments
        process.standardOutput = FileHandle.standardError
        process.standardError = FileHandle.standardError
        do {
            try process.run()
        } catch {
            CLI.printError("Second Cursor mcp: could not start the app: \(error.localizedDescription)")
        }
    }

    private func currentProtocolVersion() -> String {
        stateLock.lock()
        defer { stateLock.unlock() }
        return protocolVersion
    }

    private func setProtocolVersion(_ version: String) {
        stateLock.lock()
        protocolVersion = version
        stateLock.unlock()
    }

    private func write(_ line: Data) {
        outputLock.lock()
        defer { outputLock.unlock() }
        var data = line
        data.append(0x0A)
        data.withUnsafeBytes { buffer in
            guard let base = buffer.baseAddress else { return }
            var offset = 0
            while offset < buffer.count {
                let written = Darwin.write(STDOUT_FILENO, base + offset, buffer.count - offset)
                if written < 0 && errno == EINTR { continue }
                if written <= 0 { return }
                offset += written
            }
        }
    }

    private static func lastEvent(_ body: Data) -> Data {
        let lines = String(decoding: body, as: UTF8.self).split(whereSeparator: \.isNewline)
        guard let last = lines.last(where: { $0.hasPrefix("data:") }) else { return body }
        return Data(last.dropFirst(5).trimmingCharacters(in: .whitespaces).utf8)
    }

    private static func error(id: Any, code: Int, message: String) -> Data {
        let object: [String: Any] = ["jsonrpc": "2.0", "id": id, "error": ["code": code, "message": message]]
        return (try? JSONSerialization.data(withJSONObject: object, options: [.withoutEscapingSlashes])) ?? Data()
    }
}
