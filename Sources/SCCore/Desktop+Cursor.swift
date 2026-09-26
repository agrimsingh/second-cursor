import AppKit
import ApplicationServices
import SCShared

extension Desktop {
    public func noteAgentActivity(session: String, windowID: UInt32?, app: String?, showArrow: Bool, caption: String?) {
        guard let cursor else { return }
        var near: Point?
        // Same rule as pointer actions: never aim the arrow at a window on a Space the user is not viewing.
        if showArrow, cursor.activity().arrow == nil, let windowID, let record = WindowList.record(windowID),
           record.isOnScreen {
            let point = Self.restingPoint(record)
            cursor.present(Self.target(for: point, in: record))
            near = point
        }
        cursor.present(.activity(session: session, app: app, near: near, caption: caption))
    }

    public func endAgentActivity(session: String) {
        cursor?.present(.end(session: session))
    }

    public func agentActivity() -> AgentActivityStatus {
        cursor?.activity() ?? AgentActivityStatus()
    }

    static func target(for point: Point, in record: CGWindowRecord) -> AgentCursorEvent {
        let anchor = cursorAnchor(at: point, pid: record.pid) ?? CursorAnchor(window: record.id, layer: record.layer, visible: false)
        return .target(window: anchor.window, layer: anchor.layer, pid: record.pid, visible: anchor.visible)
    }

    public struct CursorAnchor: Sendable, Equatable {
        public let window: UInt32
        public let layer: Int
        public let visible: Bool
    }

    /// The frontmost on-screen window of `pid` under `point` (global, top-left origin), and whether another app's
    /// window covers the point in front of it. Nil when no window of `pid` on the current Space is under the point.
    /// Windows of any Second Cursor process never count as covering: each instance (the installed app, a test
    /// instance) has its own full-screen, transparent cursor overlay.
    public static func cursorAnchor(at point: Point, pid: Int32) -> CursorAnchor? {
        let location = CGPoint(x: point.x, y: point.y)
        // Nobody looks at the agent display (SCDisplayPrivate.m creates it with this vendor and product).
        var display: CGDirectDisplayID = 0
        var count: UInt32 = 0
        if CGGetDisplaysWithPoint(location, 1, &display, &count) == .success, count == 1,
           CGDisplayVendorNumber(display) == 0xA6E7, CGDisplayModelNumber(display) == 0x0001 {
            return nil
        }
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] ?? []
        var covered = false
        for info in list {
            guard let record = CGWindowRecord(info), record.bounds.contains(location),
                  (info[kCGWindowAlpha as String] as? Double ?? 1) > 0.01
            else { continue }
            if record.pid == pid { return CursorAnchor(window: record.id, layer: record.layer, visible: !covered) }
            if !isSecondCursor(record.pid) { covered = true }
        }
        return nil
    }

    private static func isSecondCursor(_ pid: pid_t) -> Bool {
        if pid == getpid() { return true }
        guard let own = Bundle.main.bundleIdentifier else { return false }
        return NSRunningApplication(processIdentifier: pid)?.bundleIdentifier == own
    }

    /// Where typing goes: the leading edge of the focused element when it is in this window, else the window's center.
    private static func restingPoint(_ record: CGWindowRecord) -> Point {
        let app = AXUIElementCreateApplication(record.pid)
        // Runs on the caller's thread, so a hung app may only hold it briefly.
        AXUIElementSetMessagingTimeout(app, 0.25)
        if let focused = app.element(kAXFocusedUIElementAttribute) {
            AXUIElementSetMessagingTimeout(focused, 0.25)
            if let frame = focused.frame {
                let point = CGPoint(x: frame.minX + min(frame.width / 2, 24), y: frame.minY + min(frame.height / 2, 16))
                if record.bounds.contains(point) { return Point(x: point.x, y: point.y) }
            }
        }
        return Point(x: record.bounds.midX, y: record.bounds.midY)
    }
}
