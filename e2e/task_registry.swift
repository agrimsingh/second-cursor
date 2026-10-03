// Compile the production registry with this dependency-free harness:
// swiftc Sources/SCShared/TaskRegistry.swift e2e/task_registry.swift -o /tmp/sc-task-registry-tests
// /tmp/sc-task-registry-tests
// The two host types are the registry's Foundation-only dependencies; app integration is tested by tasks.py.
import Foundation

enum SCPaths { static let supportDir = FileManager.default.temporaryDirectory.appendingPathComponent("sc-registry-test-unused") }
enum SCError: Error { case invalidArgument(String), notAllowed(String) }
enum CheckError: Error { case failed(String) }

@main
struct TaskRegistryChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        guard condition() else { throw CheckError.failed(message) }
        print("PASS \(message)")
    }

    static func refuses(_ message: String, _ body: () throws -> Void) throws {
        do { try body() } catch { print("PASS \(message)"); return }
        throw CheckError.failed(message)
    }

    static func main() throws {
        let registry = TaskRegistry()
        let task = try registry.create(session: "first", title: "Fixture")
        let target = SCTaskTarget(key: "window:1", app: "Fixture", windowID: 1)
        registry.noteTarget(session: "first", target: target)
        let first = try registry.beginMutation(session: "first", targetKey: target.key)
        let requested = DispatchSemaphore(value: 0)
        registry.setControlRequestedHandler { _ in requested.signal() }
        _ = try registry.control(id: task.id, action: .takeover)
        try check(requested.wait(timeout: .now()) == .success, "remote pause is requested before in-flight input drains")
        try refuses("queued input stops when the human takes over") { try registry.checkMutation(first, targetKey: target.key) }
        try refuses("resume waits for the old operation to drain") { _ = try registry.control(id: task.id, action: .resume) }
        registry.finishMutation(first)
        registry.noteObservation(session: "first", targetKey: target.key)
        let resumed = try registry.control(id: task.id, action: .resume)
        try refuses("reading during takeover is not fresh after resume") { try registry.checkMutation(session: "first", targetKey: target.key) }
        registry.noteObservation(session: "first", targetKey: target.key, generation: resumed.controlGeneration - 1)
        try refuses("a late pre-resume read does not clear the gate") { try registry.checkMutation(session: "first", targetKey: target.key) }
        registry.noteObservation(session: "first", targetKey: target.key, generation: resumed.controlGeneration)
        let current = try registry.beginMutation(session: "first", targetKey: target.key)
        _ = try registry.control(id: task.id, action: .cancel)
        try refuses("cancelled input cannot be replaced while it is in flight") {
            _ = try registry.create(session: "first", title: "Replacement")
        }
        registry.finishMutation(current)
        let replacement = try registry.create(session: "first", title: "Replacement")
        try check(replacement.id != task.id && replacement.controlGeneration > resumed.controlGeneration,
                  "new tasks retain monotonic transport generations")
        try refuses("an old mutation cannot run in the replacement task") { try registry.checkMutation(current) }
        let concurrent = TaskRegistry()
        let idsLock = NSLock()
        var createdIDs = Set<String>()
        DispatchQueue.concurrentPerform(iterations: 30) { index in
            if let created = try? concurrent.create(session: "same", title: "Caller \(index)") {
                idsLock.lock()
                createdIDs.insert(created.id)
                idsLock.unlock()
            }
        }
        try check(createdIDs.count == 1 && concurrent.list().count == 1, "concurrent creates preserve one session task")

        let cleanup = TaskRegistry()
        let cleanupTask = cleanup.ensureTask(session: "cleanup")
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        cleanup.setControlHandler { _ in
            entered.signal()
            _ = release.wait(timeout: .now() + 5)
        }
        DispatchQueue.global().async {
            _ = try? cleanup.control(id: cleanupTask.id, action: .pause)
            finished.signal()
        }
        try check(entered.wait(timeout: .now() + 2) == .success, "control cleanup entered")
        defer { release.signal() }
        try refuses("resume cannot overtake an older control cleanup") {
            _ = try cleanup.control(id: cleanupTask.id, action: .resume)
        }
        release.signal()
        try check(finished.wait(timeout: .now() + 2) == .success, "control cleanup completes")
        cleanup.setControlHandler(nil)
        _ = try cleanup.control(id: cleanupTask.id, action: .resume)
        try cleanup.checkMutation(session: "cleanup")
        print("PASS completed control cleanup permits fresh work")

        let root = FileManager.default.temporaryDirectory.appendingPathComponent("sc-registry-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("tasks.json")
        let persistent = TaskRegistry(storageURL: path)
        let durable = persistent.ensureTask(session: "durable")
        persistent.noteTarget(session: "durable", target: target)
        let recovered = TaskRegistry(storageURL: path)
        try check(recovered.snapshot(id: durable.id)?.status == .interrupted, "restart marks unfinished tasks interrupted")
        try refuses("recovered history is not a live session binding") { _ = try recovered.control(id: durable.id, action: .resume) }
        let recoveredTask = recovered.ensureTask(session: "durable")
        try check(recoveredTask.id == durable.id && recoveredTask.status == .interrupted,
                  "validated reused session recovers the existing gate")
        try refuses("reused transport cannot bypass interrupted state") { _ = try recovered.beginMutation(session: "durable") }
        _ = try recovered.control(id: durable.id, action: .resume)
        try refuses("restart resume requires the target to be observed again") { try recovered.checkMutation(session: "durable", targetKey: target.key) }
        recovered.noteObservation(session: "durable", targetKey: target.key)
        try recovered.checkMutation(session: "durable", targetKey: target.key)
        print("PASS explicit restart recovery admits fresh input")
        recovered.interrupt(session: "durable")
        try refuses("a closed session binding cannot be resumed by the panel") { _ = try recovered.control(id: durable.id, action: .resume) }

        let broken = Data("{ broken task journal".utf8)
        try broken.write(to: path)
        let corrupt = TaskRegistry(storageURL: path)
        _ = corrupt.ensureTask(session: "corrupt")
        try refuses("corrupt journal fails mutation closed") { _ = try corrupt.beginMutation(session: "corrupt") }
        let after = try Data(contentsOf: path)
        try check(after == broken && corrupt.persistenceError != nil, "corrupt journal is preserved for explicit repair")

        let backendFailure = TaskRegistry()
        let failing = backendFailure.ensureTask(session: "backend")
        backendFailure.controlFailed(taskID: failing.id, generation: failing.controlGeneration, reason: "Browser disconnected")
        try check(backendFailure.snapshot(id: failing.id)?.status == .needsUser, "failed control acknowledgement remains gated")
        try refuses("backend control failure prevents new input") { _ = try backendFailure.beginMutation(session: "backend") }
        print("Task registry checks complete")
    }
}
