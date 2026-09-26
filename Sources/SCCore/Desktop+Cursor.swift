import ApplicationServices
import Foundation
import SCShared

extension Desktop {
    public func noteAgentActivity(session: String, windowID: UInt32?, app: String?, showArrow: Bool, caption: String?) {
        guard let cursor else { return }
        var near: Point?
        // Same rule as pointer actions: never aim the arrow at a window on a Space the user is not viewing.
        if showArrow, cursor.activity().arrow == nil, let windowID, let record = WindowList.record(windowID),
           record.isOnScreen {
            near = Self.restingPoint(record)
        }
        cursor.present(.activity(session: session, app: app, near: near, caption: caption))
    }

    public func endAgentActivity(session: String) {
        cursor?.present(.end(session: session))
    }

    public func agentActivity() -> AgentActivityStatus {
        cursor?.activity() ?? AgentActivityStatus()
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
