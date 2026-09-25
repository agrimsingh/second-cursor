import Foundation
import SCShared

/// The JSON shapes of the tool contract (snake_case keys, `null` for unknown values).
enum Shape {
    static func app(_ app: AppInfo) -> [String: Any] {
        [
            "name": app.name,
            "bundle_id": app.bundleID ?? NSNull(),
            "pid": app.pid ?? NSNull(),
            "path": app.path ?? NSNull(),
            "is_running": app.isRunning,
            "is_active": app.isActive,
        ]
    }

    static func window(_ window: WindowInfo, agentBounds: Rect?) -> [String: Any] {
        [
            "id": window.id,
            "pid": window.pid,
            "app": window.appName,
            "bundle_id": window.bundleID ?? NSNull(),
            "title": window.title,
            "frame": rect(window.frame),
            "space_ids": window.spaceIDs,
            "is_on_screen": window.isOnScreen,
            "is_minimized": window.isMinimized ?? NSNull(),
            "on_agent_display": agentBounds.map { contains($0, x: window.frame.x, y: window.frame.y) } ?? false,
        ]
    }

    static func rect(_ rect: Rect) -> [String: Any] {
        ["x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height]
    }

    static func display(_ status: AgentDisplayStatus) -> [String: Any] {
        [
            "is_active": status.isActive,
            "display_id": status.displayID ?? NSNull(),
            "bounds": status.bounds.map(rect) ?? NSNull(),
            "hosted_window_ids": status.hostedWindowIDs,
            "cursor_clamp_enabled": status.cursorClampEnabled,
        ]
    }

    static func placement(_ placement: WindowPlacement) -> [String: Any] {
        [
            "window_id": placement.windowID,
            "original_frame": rect(placement.originalFrame),
            "agent_frame": rect(placement.agentFrame),
        ]
    }

    static func browser(_ status: BrowserStatus) -> [String: Any] {
        ["connected": status.connected, "extension_version": status.extensionVersion ?? NSNull()]
    }

    static func tab(_ tab: BrowserTab) -> [String: Any] {
        ["id": tab.id, "url": tab.url, "title": tab.title, "is_agent_tab": tab.isAgentTab]
    }

    static func action(_ result: ActionResult) -> [String: Any] {
        ["ok": true, "route": result.route, "verified": result.verified ?? NSNull(), "note": result.note ?? NSNull()]
    }

    static func screenshot(_ shot: Screenshot) -> [String: Any] {
        ["width": shot.width, "height": shot.height, "scale": shot.scale]
    }

    static let ok: [String: Any] = ["ok": true]

    /// Matches e2e's `inside`: the window's origin lies within the display bounds.
    private static func contains(_ bounds: Rect, x: Double, y: Double) -> Bool {
        bounds.x <= x && x < bounds.x + bounds.width && bounds.y <= y && y < bounds.y + bounds.height
    }
}
