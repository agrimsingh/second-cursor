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
            return "The user is interacting with this app. Wait until they yield, then get_window_state again before continuing."
        }
        if foreground && input && SkyLight.keyWindow(pid: pid) != wid {
            return "The user owns another key window in this foreground app. Background pointer or keyboard input would change their key window. Use a semantic AX action, an agent-owned browser tab, or a separate app instance."
        }
        return nil
    }

    static func check(pid: pid_t, wid: CGWindowID, input: Bool) throws {
        try Execution.context.beforeDispatch()
        if let reason = conflict(pid: pid, wid: wid, input: input) { throw SCError.notAllowed(reason) }
    }
}
