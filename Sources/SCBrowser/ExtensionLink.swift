import Darwin
import Foundation
import SCShared

/// Newline-delimited JSON over a Unix socket to sc-native-host. A connection becomes active once
/// the extension's hello arrives through it; a newer hello supersedes the previous connection.
final class ExtensionLink: @unchecked Sendable {
    struct Reply: @unchecked Sendable {
        let value: [String: Any]
    }

    private final class Connection: @unchecked Sendable {
        let fd: Int32
        let id: Int
        private let writeLock = NSLock()

        init(fd: Int32, id: Int) {
            self.fd = fd
            self.id = id
        }

        func send(_ object: [String: Any]) -> Bool {
            guard var data = try? JSONSerialization.data(withJSONObject: object) else { return false }
            data.append(0x0A)
            writeLock.lock()
            defer { writeLock.unlock() }
            return data.withUnsafeBytes { raw -> Bool in
                var pointer = raw.baseAddress!
                var remaining = raw.count
                while remaining > 0 {
                    let written = write(fd, pointer, remaining)
                    if written < 0 {
                        if errno == EINTR { continue }
                        return false
                    }
                    remaining -= written
                    pointer += written
                }
                return true
            }
        }

        /// Wakes the reader thread, which owns closing the descriptor.
        func hangUp() {
            shutdown(fd, SHUT_RDWR)
        }
    }

    private struct Pending {
        let connectionID: Int
        let continuation: CheckedContinuation<Reply, Error>
    }

    let socketURL: URL
    private let lock = NSLock()
    private let scheduler = BrowserScheduler()
    private var listenFD: Int32 = -1
    private var active: [String: Connection] = [:]
    private var metadata: [String: [String: Any]] = [:]
    private var extensionVersion: String?
    private var nextConnectionID = 1
    private var nextCallID = 1
    private var pending: [Int: Pending] = [:]

    init(socketURL: URL) {
        self.socketURL = socketURL
    }

    var status: BrowserStatus {
        lock.lock()
        defer { lock.unlock() }
        return BrowserStatus(connected: !active.isEmpty, extensionVersion: active.isEmpty ? nil : extensionVersion)
    }

    func start() throws {
        lock.lock()
        defer { lock.unlock() }
        guard listenFD < 0 else { return }

        try SCPaths.ensureDirectories()
        let path = socketURL.path
        var address = try Self.address(for: path)

        if Self.isServed(&address) {
            throw SCError.actionFailed("Another Second Cursor process is already serving \(path)")
        }
        unlink(path)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Self.posixError("socket") }
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0 else {
            let error = Self.posixError("bind \(path)")
            close(fd)
            throw error
        }
        guard chmod(path, 0o600) == 0, listen(fd, 8) == 0 else {
            let error = Self.posixError("listen \(path)")
            close(fd)
            unlink(path)
            throw error
        }
        listenFD = fd

        let thread = Thread { [weak self] in self?.acceptLoop(fd) }
        thread.name = "SCBrowser.accept"
        thread.start()
    }

    func call(_ method: String, _ params: [String: Any], timeout: TimeInterval = 60) async throws -> [String: Any] {
        let key = (Execution.context.browserID ?? "default") + ":" + String(describing: params["tabId"] ?? "browser")
        return try await scheduler.run(key: key) {
            if method == "extended", params["operation"] as? String == "batch" {
                guard let steps = params["steps"] as? [[String: Any]], steps.count <= 50 else {
                    throw SCError.invalidArgument("Batch requires at most 50 steps")
                }
                var results: [[String: Any]] = []
                for var step in steps {
                    guard step["operation"] as? String != "batch" else {
                        throw SCError.invalidArgument("Nested batch not allowed")
                    }
                    step["tabId"] = params["tabId"]
                    results.append(try await self.callNow("extended", step, timeout: timeout))
                }
                let observation = try await self.callNow(
                    "extended", ["tabId": params["tabId"] as Any, "operation": "dom_snapshot"], timeout: timeout)
                return ["results": results, "observation": observation]
            }
            return try await self.callNow(method, params, timeout: timeout)
        }
    }

    private func callNow(_ method: String, _ params: [String: Any], timeout: TimeInterval) async throws -> [String: Any]
    {
        try Execution.context.beforeDispatch()
        let (connection, id) = try reserveCall()
        var params = params
        // Override untrusted arguments; only the local approval flow can populate this grant.
        params["local_approvals"] = Array(Execution.context.browserApprovals)
        params["session_id"] = Execution.context.sessionID
        params["deadline"] = Execution.context.deadline.map { $0.timeIntervalSince1970 * 1000 }
        params["observation_id"] = Execution.context.observationID
        let reply = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Reply, Error>) in
            lock.lock()
            pending[id] = Pending(connectionID: connection.id, continuation: continuation)
            lock.unlock()

            if !connection.send(["id": id, "method": method, "params": params]) {
                finish(id, .failure(SCError.browserUnavailable("lost the connection to the Chrome extension")))
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { [weak self] in
                self?.finish(
                    id, .failure(SCError.actionFailed("Browser call \(method) timed out after \(Int(timeout))s")))
            }
        }
        return reply.value
    }

    private func reserveCall() throws -> (Connection, Int) {
        lock.lock()
        defer { lock.unlock() }
        let connection: Connection?
        if let id = Execution.context.browserID {
            connection = active[id]
        } else if active.count == 1 {
            connection = active.values.first
        } else if active.count > 1 {
            throw SCError.invalidArgument("Multiple browsers connected; provide browser_id from browser_instances")
        } else {
            connection = nil
        }
        guard let connection else {
            throw SCError.browserUnavailable("the Second Cursor Chrome extension is not connected")
        }
        let id = nextCallID
        nextCallID += 1
        return (connection, id)
    }

    private func finish(_ id: Int, _ result: Result<Reply, Error>) {
        lock.lock()
        let entry = pending.removeValue(forKey: id)
        lock.unlock()
        entry?.continuation.resume(with: result)
    }

    private func acceptLoop(_ listenFD: Int32) {
        while true {
            let fd = accept(listenFD, nil, nil)
            if fd < 0 {
                if errno == EINTR || errno == ECONNABORTED { continue }
                NSLog("SCBrowser: accept failed: %@", String(cString: strerror(errno)))
                return
            }
            var one: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))

            lock.lock()
            let connection = Connection(fd: fd, id: nextConnectionID)
            nextConnectionID += 1
            lock.unlock()

            let thread = Thread { [weak self] in self?.readLoop(connection) }
            thread.name = "SCBrowser.connection.\(connection.id)"
            thread.start()
        }
    }

    private func readLoop(_ connection: Connection) {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = read(connection.fd, &chunk, chunk.count)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { break }
            buffer.append(contentsOf: chunk[0..<n])
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = Data(buffer[buffer.startIndex..<newline])
                buffer.removeSubrange(buffer.startIndex...newline)
                if let message = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                    handle(message, from: connection)
                }
            }
        }
        close(connection.fd)
        drop(connection, reason: "the Chrome extension disconnected")
    }

    private func handle(_ message: [String: Any], from connection: Connection) {
        if message["type"] as? String == "hello" {
            promote(connection, message: message)
            return
        }
        guard let id = (message["id"] as? NSNumber)?.intValue else { return }
        lock.lock()
        let belongs = pending[id]?.connectionID == connection.id
        lock.unlock()
        guard belongs else { return }

        if let error = message["error"] {
            finish(id, .failure(Self.scError(from: error)))
        } else {
            finish(id, .success(Reply(value: message["result"] as? [String: Any] ?? [:])))
        }
    }

    func instances() -> Data {
        lock.lock()
        defer { lock.unlock() }
        let browsers = active.keys.sorted().map { id -> [String: Any] in
            var info = metadata[id] ?? [:]
            info["id"] = id
            return info
        }
        return (try? JSONSerialization.data(withJSONObject: ["browsers": browsers])) ?? Data()
    }

    private func instanceIDs() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return Array(active.keys)
    }

    /// Sends a session-wide method to every connected browser; unreachable ones are skipped.
    func broadcast(_ method: String, _ params: [String: Any], timeout: TimeInterval = 60) async -> [[String: Any]] {
        var replies: [[String: Any]] = []
        for id in instanceIDs() {
            var context = Execution.context
            context.browserID = id
            context.sessionID = params["session_id"] as? String ?? context.sessionID
            if let reply = try? await Execution.$context.withValue(context, operation: {
                try await call(method, params, timeout: timeout)
            }) {
                replies.append(reply)
            }
        }
        return replies
    }

    private func promote(_ connection: Connection, message: [String: Any]) {
        let id = message["instance_id"] as? String ?? "legacy"
        lock.lock()
        let previous = active[id]
        active[id] = connection
        metadata[id] = [
            "name": message["name"] ?? "Chromium", "profile_name": message["profile_name"] ?? "",
            "extension_version": message["version"] ?? "", "capabilities": message["capabilities"] ?? [],
        ]
        extensionVersion = message["version"] as? String
        lock.unlock()
        guard let previous, previous !== connection else { return }
        failPending(on: previous.id, reason: "browser instance reconnected")
        _ = previous.send(["type": "superseded"])
        previous.hangUp()
    }

    private func drop(_ connection: Connection, reason: String) {
        lock.lock()
        for id in active.keys.filter({ active[$0] === connection }) {
            active[id] = nil
            metadata[id] = nil
        }
        lock.unlock()
        failPending(on: connection.id, reason: reason)
    }

    private func failPending(on connectionID: Int, reason: String) {
        lock.lock()
        let ids = pending.filter { $0.value.connectionID == connectionID }.map(\.key)
        lock.unlock()
        for id in ids {
            finish(id, .failure(SCError.browserUnavailable(reason)))
        }
    }

    private static func scError(from payload: Any) -> SCError {
        let object = payload as? [String: Any]
        let message = object?["message"] as? String ?? payload as? String ?? "Browser call failed"
        switch object?["code"] as? String {
        case "not_allowed": return .notAllowed(message)
        case "invalid_argument": return .invalidArgument(message)
        default: return .actionFailed(message)
        }
    }

    private static func address(for path: String) throws -> sockaddr_un {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        guard path.utf8.count < MemoryLayout.size(ofValue: address.sun_path) else {
            throw SCError.actionFailed("Socket path is too long for a Unix socket: \(path)")
        }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: path.utf8)
            raw[path.utf8.count] = 0
        }
        return address
    }

    /// A socket file left by a crashed process refuses connections; a live owner accepts them.
    private static func isServed(_ address: inout sockaddr_un) -> Bool {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        return result == 0
    }

    private static func posixError(_ what: String) -> SCError {
        SCError.actionFailed("\(what) failed: \(String(cString: strerror(errno)))")
    }
}

private actor BrowserScheduler {
    private var tails: [String: Task<Void, Never>] = [:]
    private var generations: [String: UUID] = [:]
    func run(key: String, work: @escaping () async throws -> [String: Any]) async throws -> [String: Any] {
        let previous = tails[key]
        let generation = UUID()
        let task = Task {
            await previous?.value
            return try await work()
        }
        tails[key] = Task { _ = try? await task.value }
        generations[key] = generation
        defer {
            if generations[key] == generation {
                tails[key] = nil
                generations[key] = nil
            }
        }
        return try await task.value
    }
}
