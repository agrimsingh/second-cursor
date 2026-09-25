import Foundation
import SCShared

/// Single-instance guard. An flock is released by the kernel when the process dies,
/// so a crash never leaves a stale lock behind.
enum InstanceLock {
    private static var descriptor: Int32 = -1
    /// Lets the E2E harness stop exactly the instance it launched for an `SC_HOME`.
    private static let pidFileURL = SCPaths.supportDir.appendingPathComponent("instance.pid")

    static func acquire() -> Bool {
        try? SCPaths.ensureDirectories()
        let path = SCPaths.supportDir.appendingPathComponent("instance.lock").path
        let fd = open(path, O_CREAT | O_RDWR, 0o644)
        guard fd >= 0 else { return true }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            return false
        }
        descriptor = fd
        try? Data("\(getpid())\n".utf8).write(to: pidFileURL, options: .atomic)
        atexit { try? FileManager.default.removeItem(at: InstanceLock.pidFileURL) }
        return true
    }
}
