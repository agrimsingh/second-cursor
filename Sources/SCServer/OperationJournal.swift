import CryptoKit
import Darwin
import Foundation
import SCShared

/// Durable metadata only. Typed text, paths, URLs, screenshots and result bodies never reach this store.
/// Sessions serializes access; atomic replacement and fsync put each admission boundary on disk before input.
final class OperationJournal {
    struct SessionRecord: Codable {
        let id: String
        let transport: Bool
        var expires: Date
    }

    struct Record: Codable {
        let version: Int
        let id: String
        let session: String
        let signature: String
        let deadline: Date
        var state: String
        var finished: Bool
        var outcomeKnown: Bool
        var cancelled: Bool
        var expires: Date
        var outcomeState: String?
        var verified: Bool?
    }

    private let directory: URL
    private let encoder: JSONEncoder
    private let decoder = JSONDecoder()

    init(directory: URL = SCPaths.supportDir.appendingPathComponent("operation-journal", isDirectory: true)) throws {
        self.directory = directory
        encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
    }

    static func digest(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func operationURL(_ id: String) -> URL {
        directory.appendingPathComponent("operation-\(Self.digest(id)).json")
    }

    func load() throws -> (sessions: [SessionRecord], operations: [Record]) {
        let now = Date()
        let registry = directory.appendingPathComponent("sessions.json")
        let sessions = FileManager.default.fileExists(atPath: registry.path)
            ? try decoder.decode([SessionRecord].self, from: Data(contentsOf: registry)).filter { $0.expires > now }
            : []
        let live = Set(sessions.map(\.id)).union(["legacy"])
        var operations: [Record] = []
        for url in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            where url.lastPathComponent.hasPrefix("operation-") && url.pathExtension == "json"
        {
            let record = try decoder.decode(Record.self, from: Data(contentsOf: url))
            guard record.version == 1 else { throw SCError.actionFailed("Unsupported operation journal version") }
            // Retention starts at completion, never while input may still be in flight. An interrupted
            // nonterminal record receives a fresh reconciliation interval when Sessions recovers it.
            if (!record.finished || record.expires > now), live.contains(record.session) {
                operations.append(record)
            } else {
                try FileManager.default.removeItem(at: url)
            }
        }
        return (sessions, operations)
    }

    func saveSessions(_ records: [SessionRecord]) throws {
        try write(try encoder.encode(records.sorted { $0.id < $1.id }), to: directory.appendingPathComponent("sessions.json"))
    }

    func save(_ record: Record) throws {
        try write(try encoder.encode(record), to: operationURL(record.id))
    }

    func remove(_ id: String) {
        try? FileManager.default.removeItem(at: operationURL(id))
    }

    private func write(_ data: Data, to destination: URL) throws {
        // A private directory protects both the replacement file and the final file throughout the write.
        try data.write(to: destination, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
        let handle = try FileHandle(forWritingTo: destination)
        defer { try? handle.close() }
        try handle.synchronize()
        // Persist the rename too. Without the directory sync a power loss could lose an acknowledged intent.
        let descriptor = Darwin.open(directory.path, O_RDONLY)
        guard descriptor >= 0 else { throw POSIXError(.EIO) }
        defer { Darwin.close(descriptor) }
        guard fsync(descriptor) == 0 else { throw POSIXError(.EIO) }
    }
}
