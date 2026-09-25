import AppKit
import ApplicationServices
import Foundation
import SCShared

/// Finds the AXUIElement for a CGWindowID, including windows on inactive Spaces, which
/// `kAXWindowsAttribute` omits. Called from several per-app queues; the lock guards only the
/// caches, never the (slow, app-blocking) AX calls.
final class WindowResolver: @unchecked Sendable {
    private struct Key: Hashable {
        let pid: pid_t
        let wid: CGWindowID
    }

    enum Engine { case native, electron, chromium }

    private let lock = NSLock()
    private var cache: [Key: AXUIElement] = [:]
    private var engines: [pid_t: Engine] = [:]
    private var preparedPIDs = Set<pid_t>()

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    func resolve(pid: pid_t, wid: CGWindowID) throws -> AXUIElement {
        guard AXIsProcessTrusted() else { throw SCError.permissionDenied("Accessibility") }
        prepare(pid)
        if let cached = cached(pid: pid, wid: wid) { return cached }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 2)
        let window = listedWindows(app)[wid]
            ?? [kAXMainWindowAttribute, kAXFocusedWindowAttribute].lazy.compactMap { app.element($0) }.first { $0.windowID == wid }
            ?? scanRemoteTokens(pid: pid, target: wid)[wid]
        guard let window else { throw SCError.windowNotFound(wid) }
        locked { cache[Key(pid: pid, wid: wid)] = window }
        return window
    }

    /// Cache hit that is still alive and still the same window; drops dead entries.
    func cached(pid: pid_t, wid: CGWindowID) -> AXUIElement? {
        let key = Key(pid: pid, wid: wid)
        guard let window = locked({ cache[key] }) else { return nil }
        if window.isAlive, window.windowID == wid { return window }
        locked { cache[key] = nil }
        return nil
    }

    func listedWindows(_ app: AXUIElement) -> [CGWindowID: AXUIElement] {
        var result: [CGWindowID: AXUIElement] = [:]
        for window in app.elements(kAXWindowsAttribute) {
            if let wid = window.windowID { result[wid] = window }
        }
        return result
    }

    func forget(windowsNotIn live: Set<CGWindowID>) {
        locked { cache = cache.filter { live.contains($0.key.wid) } }
    }

    func engine(_ pid: pid_t) -> Engine {
        if let engine = locked({ engines[pid] }) { return engine }
        let engine = Self.detectEngine(NSRunningApplication(processIdentifier: pid)?.bundleURL)
        locked { engines[pid] = engine }
        return engine
    }

    /// Chromium only builds its accessibility tree for clients that ask; AXManualAccessibility is
    /// the quiet switch. AXEnhancedUserInterface also works but disturbs window animations.
    private func prepare(_ pid: pid_t) {
        guard locked({ preparedPIDs.insert(pid).inserted }) else { return }
        let engine = engine(pid)
        guard engine != .native else { return }
        let app = AXUIElementCreateApplication(pid)
        if app.bool("AXManualAccessibility") == true { return }
        var enabled = app.set("AXManualAccessibility", kCFBooleanTrue) == .success
        if !enabled, engine == .chromium {
            enabled = app.set("AXEnhancedUserInterface", kCFBooleanTrue) == .success
        }
        if enabled { usleep(500_000) }
    }

    /// Element IDs are assigned lazily, so this only finds windows some AX client has touched.
    /// Electron vends its window element only after something asks a web area which window it is in,
    /// so web areas found by the scan are followed up to their window.
    private func scanRemoteTokens(pid: pid_t, target: CGWindowID) -> [CGWindowID: AXUIElement] {
        var token = Data(count: 20)
        token.replaceSubrange(0..<4, with: withUnsafeBytes(of: pid) { Data($0) })
        token.replaceSubrange(8..<12, with: withUnsafeBytes(of: Int32(0x636f_636f)) { Data($0) })
        var found: [CGWindowID: AXUIElement] = [:]
        func record(_ window: AXUIElement) {
            guard let wid = window.windowID else { return }
            found[wid] = window
            locked { cache[Key(pid: pid, wid: wid)] = window }
        }
        for id: UInt64 in 0..<50_000 where found[target] == nil {
            token.replaceSubrange(12..<20, with: withUnsafeBytes(of: id) { Data($0) })
            guard let element = _AXUIElementCreateWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
            AXUIElementSetMessagingTimeout(element, 0.2)
            switch element.role {
            case kAXWindowRole?:
                record(element)
            case "AXWebArea"?:
                if let window = element.element(kAXWindowAttribute) { record(window) }
            default:
                break
            }
        }
        return found
    }

    static func detectEngine(_ bundleURL: URL?) -> Engine {
        guard let frameworks = bundleURL?.appendingPathComponent("Contents/Frameworks") else { return .native }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: frameworks.path)) ?? []
        if names.contains("Electron Framework.framework") { return .electron }
        if names.contains("Chromium Embedded Framework.framework") { return .chromium }
        let chromeFamily = names.contains { name in
            name.hasSuffix(" Framework.framework")
                && FileManager.default.fileExists(atPath: frameworks.appendingPathComponent(name).appendingPathComponent("Helpers").path)
        }
        return chromeFamily ? .chromium : .native
    }
}
