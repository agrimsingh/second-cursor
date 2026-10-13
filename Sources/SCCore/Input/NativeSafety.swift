import AppKit
import CoreGraphics
import SCShared

/// Human activity always wins over process-addressed input. A quiet foreground window can be delegated,
/// but changing the key window of the user's active process is never a safe background operation.
enum NativeSafety {
    static let quietInterval: TimeInterval = 2

    static func conflict(pid: pid_t, wid: CGWindowID, input: Bool) -> String? {
        let state = HumanInputMonitor.shared.state
        let foreground = NSWorkspace.shared.frontmostApplication?.processIdentifier == pid
        if foreground && !state.monitoring {
            return "Human input monitoring is unavailable, so Second Cursor cannot safely mutate the foreground app. Switch to another app or use an agent-owned browser tab."
        }
        if state.lastInputPID == pid, let last = state.lastInputAt, Date().timeIntervalSince(last) < quietInterval {
            return "The user is interacting with this app. Wait until they yield, then get_window_state again before continuing." + webPageAdvice(pid)
        }
        if foreground && input && SkyLight.keyWindow(pid: pid) != wid {
            return "The user owns another key window in this foreground app. Background pointer or keyboard input would change their key window. Use a semantic AX action, an agent-owned browser tab, or a separate app instance." + webPageAdvice(pid)
        }
        return nil
    }

    /// Waiting out the user is the wrong remedy in their browser: an agent tab is never blocked by their input.
    static func webPageAdvice(_ pid: pid_t) -> String {
        guard BrowserStatus.hostsExtension(bundleID: NSRunningApplication(processIdentifier: pid)?.bundleIdentifier) else { return "" }
        return " For a web page, do not wait: open it in your own tab with browser_open, which the user's input does not interrupt."
    }

    static func check(pid: pid_t, wid: CGWindowID, input: Bool) throws {
        try Execution.context.beforeDispatch()
        if let reason = conflict(pid: pid, wid: wid, input: input) { throw SCError.notAllowed(reason) }
    }
}
