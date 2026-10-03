import Foundation
import SCServer
import SCShared

/// `SecondCursor mcp`: newline-delimited JSON-RPC on stdin/stdout, forwarded to the running app's MCP endpoint.
/// After initialize establishes an owned session, messages run concurrently and responses are written as
/// they finish, one line each, matched by id. Stdout carries nothing but responses.
final class MCPProxy: @unchecked Sendable {
    /// How long initialization keeps retrying refused connections while the app starts.
    private static let launchWindow: TimeInterval = 10

    private let config: SCConfig
    private let inFlight = DispatchGroup()
    private let outputLock = NSLock()
    private let stateLock = NSLock()
    private var protocolVersion = LocalHTTP.protocolVersion
    private var launched: Bool
    private enum InitializationState { case uninitialized, initializing, ready, invalid }
    private var initializationState = InitializationState.uninitialized
    private var sessionID: String?
    private var initializationFailure: String?
    private var pending: [Message] = []

    private struct Message: @unchecked Sendable {
        let body: Data
        let id: Any?
        let isInitialize: Bool
    }

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
        proxy.inFlight.wait()
        proxy.closeSession()
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
        guard let object = parsed as? [String: Any], let method = object["method"] as? String else {
            write(Self.error(id: NSNull(), code: -32600, message: "Invalid JSON-RPC request"))
            return
        }
        let message = Message(body: body, id: object["id"].flatMap { $0 is NSNull ? nil : $0 },
                              isInitialize: method == "initialize")
        inFlight.enter()
        stateLock.lock()
        if message.isInitialize {
            guard message.id != nil, initializationState != .initializing, initializationState != .ready else {
                stateLock.unlock()
                reject(message, message: "An initialize request is already active. Reinitialize only after initialization fails or the session expires.")
                return
            }
            initializationState = .initializing
            initializationFailure = nil
            let retainedSession = sessionID
            stateLock.unlock()
            send(message, session: retainedSession)
        } else if initializationState == .initializing {
            // A host can pipeline initialize and its first calls in one stdin write. Retain them until
            // both the session header and initialize response are available, then keep normal concurrency.
            pending.append(message)
            stateLock.unlock()
        } else if initializationState == .ready, let session = sessionID {
            stateLock.unlock()
            send(message, session: session)
        } else {
            let reason = initializationFailure ?? "Send initialize before using this MCP connection."
            stateLock.unlock()
            reject(message, message: reason)
        }
    }

    private func reject(_ message: Message, message reason: String) {
        if let id = message.id { write(Self.error(id: id, code: -32000, message: reason)) }
        inFlight.leave()
    }

    private func send(_ message: Message, session: String?) {
        forward(message.body, session: session, allowLaunch: message.isInitialize,
                deadline: Date().addingTimeInterval(Self.launchWindow)) { [self] outcome in
            defer { inFlight.leave() }
            if message.isInitialize {
                finishInitialize(message, outcome)
            } else {
                let sessionUnavailable: Bool
                switch outcome {
                case .http(let status, _, _, _): sessionUnavailable = status == 404
                case .failed(let error):
                    if case .notRunning = CLIError(error) { sessionUnavailable = true }
                    else { sessionUnavailable = false }
                }
                if sessionUnavailable {
                    stateLock.lock()
                    if sessionID == session, initializationState == .ready {
                        initializationState = .invalid
                        initializationFailure = "The MCP session expired or the app restarted. Send initialize again; the previous action was not retried."
                        // A refused connection may recover a persisted session. Only the server's
                        // explicit unknown-session response establishes that this identity is gone.
                        if case .http(let status, _, _, _) = outcome, status == 404 { sessionID = nil }
                    }
                    stateLock.unlock()
                }
                if let id = message.id { write(response(to: id, outcome)) }
            }
        }
    }

    private enum Outcome {
        case http(status: Int, contentType: String, body: Data, session: String?)
        case failed(Error)
    }

    private func forward(_ body: Data, session: String?, allowLaunch: Bool, deadline: Date,
                         completion: @escaping @Sendable (Outcome) -> Void) {
        var headers = ["Accept": "application/json", "MCP-Protocol-Version": currentProtocolVersion()]
        if let session { headers["Mcp-Session-Id"] = session }
        let request = LocalHTTP.request(config, path: SecondCursorServer.path, body: body, headers: headers, timeout: 90)
        LocalHTTP.session.dataTask(with: request) { [self] data, response, error in
            if let error {
                if allowLaunch, case .notRunning = CLIError(error), Date() < deadline {
                    launchAppOnce()
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.25) {
                        self.forward(body, session: session, allowLaunch: allowLaunch, deadline: deadline, completion: completion)
                    }
                    return
                }
                completion(.failed(error))
                return
            }
            let http = response as? HTTPURLResponse
            completion(.http(status: http?.statusCode ?? 0,
                             contentType: http?.value(forHTTPHeaderField: "Content-Type") ?? "",
                             body: data ?? Data(), session: http?.value(forHTTPHeaderField: "Mcp-Session-Id")))
        }.resume()
    }

    private func finishInitialize(_ message: Message, _ outcome: Outcome) {
        guard let id = message.id else { return }
        var reply = response(to: id, outcome)
        var initializedSession: String?
        if case .http(let status, let contentType, let body, let issuedSession) = outcome,
           (200..<300).contains(status) {
            let payload = contentType.contains("text/event-stream") ? Self.lastEvent(body) : body
            let object = (try? JSONSerialization.jsonObject(with: payload)) as? [String: Any]
            if let result = object?["result"] as? [String: Any] {
                if let issuedSession, !issuedSession.isEmpty {
                    initializedSession = issuedSession
                    stateLock.lock()
                    sessionID = issuedSession
                    if let version = result["protocolVersion"] as? String { protocolVersion = version }
                    stateLock.unlock()
                } else {
                    reply = Self.error(id: id, code: -32603, message: "Initialize did not return Mcp-Session-Id. Update the Second Cursor app before using this proxy.")
                }
            }
        }
        let object = (try? JSONSerialization.jsonObject(with: reply)) as? [String: Any]
        let reason = (object?["error"] as? [String: Any])?["message"] as? String ?? "MCP initialization failed."
        let waiting: [Message]
        if initializedSession != nil {
            // Install the header before the host sees success, and write success before releasing calls.
            write(reply)
            stateLock.lock()
            initializationState = .ready
            initializationFailure = nil
            waiting = pending
            pending.removeAll()
            stateLock.unlock()
        } else {
            // A host can retry immediately upon reading failure, so expose retryable state first.
            stateLock.lock()
            initializationState = .uninitialized
            initializationFailure = reason
            if case .http(let status, _, _, _) = outcome, status == 404 { sessionID = nil }
            waiting = pending
            pending.removeAll()
            stateLock.unlock()
            write(reply)
        }
        for queued in waiting {
            if let initializedSession { send(queued, session: initializedSession) }
            else { reject(queued, message: reason) }
        }
    }

    private func response(to id: Any, _ outcome: Outcome) -> Data {
        switch outcome {
        case .failed(let error):
            if case .notRunning = CLIError(error) {
                return Self.error(id: id, code: -32000, message: """
                    Second Cursor is not running on 127.0.0.1:\(config.port). Start it with \
                    `\(CLI.startCommand)` or run `SecondCursor doctor`, then initialize again.
                    """)
            }
            return Self.error(id: id, code: -32603, message: "Could not reach Second Cursor: \(error.localizedDescription)")

        case .http(let status, let contentType, let body, _):
            let payload = contentType.contains("text/event-stream") ? Self.lastEvent(body) : body
            let message = (try? JSONSerialization.jsonObject(with: payload)) as? [String: Any]
            if (200..<300).contains(status), let message, message["result"] != nil || message["error"] != nil {
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

    /// EOF belongs to this transport, not to every legacy or explicitly created runtime session.
    /// Cleanup is best effort and never starts the app or retries a request after a restart.
    private func closeSession() {
        stateLock.lock()
        let ownedSession = sessionID
        stateLock.unlock()
        guard let ownedSession else { return }
        var request = LocalHTTP.request(config, path: SecondCursorServer.path, body: Data(), headers: [
            "Mcp-Session-Id": ownedSession,
            "MCP-Protocol-Version": currentProtocolVersion(),
        ], timeout: 5)
        request.httpMethod = "DELETE"
        request.httpBody = nil
        let done = DispatchSemaphore(value: 0)
        LocalHTTP.session.dataTask(with: request) { _, response, error in
            if let error {
                CLI.printError("Second Cursor mcp: session cleanup failed: \(error.localizedDescription)")
            } else if let status = (response as? HTTPURLResponse)?.statusCode, !(200..<300).contains(status), status != 404 {
                CLI.printError("Second Cursor mcp: session cleanup returned HTTP \(status)")
            }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 6)
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
