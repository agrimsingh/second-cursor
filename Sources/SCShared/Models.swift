import CoreGraphics
import Foundation

public struct Rect: Codable, Sendable, Equatable {
    public var x: Double
    public var y: Double
    public var width: Double
    public var height: Double

    public init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }

    public init(_ rect: CGRect) {
        self.init(x: rect.origin.x, y: rect.origin.y, width: rect.size.width, height: rect.size.height)
    }

    public var cgRect: CGRect { CGRect(x: x, y: y, width: width, height: height) }
}

public struct AppInfo: Codable, Sendable, Equatable {
    public var name: String
    public var bundleID: String?
    public var pid: Int32?
    public var path: String?
    public var isRunning: Bool
    public var isActive: Bool

    public init(name: String, bundleID: String?, pid: Int32?, path: String?, isRunning: Bool, isActive: Bool) {
        self.name = name
        self.bundleID = bundleID
        self.pid = pid
        self.path = path
        self.isRunning = isRunning
        self.isActive = isActive
    }
}

/// A top-level window. `frame` is in global screen points (top-left origin, as CGWindowList reports it).
public struct WindowInfo: Codable, Sendable, Equatable {
    public var id: UInt32
    public var pid: Int32
    public var appName: String
    public var bundleID: String?
    public var title: String
    public var frame: Rect
    public var spaceIDs: [UInt64]
    public var isOnScreen: Bool
    public var isMinimized: Bool?

    public init(
        id: UInt32, pid: Int32, appName: String, bundleID: String?, title: String,
        frame: Rect, spaceIDs: [UInt64], isOnScreen: Bool, isMinimized: Bool?
    ) {
        self.id = id
        self.pid = pid
        self.appName = appName
        self.bundleID = bundleID
        self.title = title
        self.frame = frame
        self.spaceIDs = spaceIDs
        self.isOnScreen = isOnScreen
        self.isMinimized = isMinimized
    }
}

/// PNG image. `scale` is image pixels per window point, so a pixel (px, py) maps to window-local point (px / scale, py / scale).
public struct Screenshot: Sendable, Equatable {
    public var pngData: Data
    public var width: Int
    public var height: Int
    public var scale: Double

    public init(pngData: Data, width: Int, height: Int, scale: Double) {
        self.pngData = pngData
        self.width = width
        self.height = height
        self.scale = scale
    }
}

public struct SnapshotOptions: Sendable, Equatable {
    public var includeScreenshot: Bool
    public var includeMenuBar: Bool
    public var maxElements: Int
    /// `false` returns a diff against the window's previous full snapshot when one exists.
    public var full: Bool
    /// When set, returns only the subtree rooted at this element index, at full depth.
    public var scope: Int?

    public init(includeScreenshot: Bool = true, includeMenuBar: Bool = false, maxElements: Int = 400,
                full: Bool = true, scope: Int? = nil) {
        self.includeScreenshot = includeScreenshot
        self.includeMenuBar = includeMenuBar
        self.maxElements = maxElements
        self.full = full
        self.scope = scope
    }
}

/// `tree` is a compact indented outline; actionable elements carry `[index]` markers that
/// `ClickTarget.element` and the other element-indexed actions refer to. Indexes are only valid
/// for the most recent snapshot of the same window.
public struct WindowSnapshot: Sendable, Equatable {
    public var window: WindowInfo
    public var snapshotID: String
    public var tree: String
    public var elementCount: Int
    public var truncated: Bool
    /// True when `tree` is a diff; `diffBase` is the snapshot id it was computed against.
    public var isDiff: Bool
    public var diffBase: String?
    public var scope: Int?
    public var screenshot: Screenshot?

    public init(window: WindowInfo, snapshotID: String, tree: String, elementCount: Int, truncated: Bool,
                isDiff: Bool = false, diffBase: String? = nil, scope: Int? = nil, screenshot: Screenshot? = nil) {
        self.window = window
        self.snapshotID = snapshotID
        self.tree = tree
        self.elementCount = elementCount
        self.truncated = truncated
        self.isDiff = isDiff
        self.diffBase = diffBase
        self.scope = scope
        self.screenshot = screenshot
    }
}

public struct Point: Codable, Sendable, Equatable {
    public var x: Double
    public var y: Double

    public init(x: Double, y: Double) {
        self.x = x
        self.y = y
    }
}

/// `point` is window-local, in points, top-left origin.
public enum ClickTarget: Sendable, Equatable {
    case element(index: Int)
    case point(x: Double, y: Double)
}

public enum MouseButton: String, Codable, Sendable {
    case left, right, middle
}

public enum ScrollDirection: String, Codable, Sendable {
    case up, down, left, right
}

/// `route` names the delivery path actually used (e.g. "ax_press", "ax_value", "pid_keyboard", "skylight_pointer").
public struct ActionResult: Codable, Sendable, Equatable {
    public var route: String
    public var verified: Bool?
    public var note: String?

    public init(route: String, verified: Bool? = nil, note: String? = nil) {
        self.route = route
        self.verified = verified
        self.note = note
    }
}

public enum BatchAction: Sendable, Equatable {
    case click(target: ClickTarget, button: MouseButton, count: Int)
    case typeText(text: String, elementIndex: Int?, keystrokesOnly: Bool)
    case pressKey(keys: String)
    case setValue(elementIndex: Int, value: String)
    case scroll(target: ClickTarget?, direction: ScrollDirection, amount: Int)
    case drag(from: Point, to: Point)
    case performAction(elementIndex: Int, action: String)
    case wait(milliseconds: Int)

    /// Whether the step addresses window pixels, which go stale when the layout changes.
    public var usesPixels: Bool {
        switch self {
        case .click(.point, _, _), .scroll(.point?, _, _), .drag: return true
        default: return false
        }
    }
}

/// `tool` is the step's tool name as the client wrote it; used for results and the action log.
public struct BatchStep: Sendable, Equatable {
    public var tool: String
    public var windowID: UInt32
    public var action: BatchAction
    public var logDetail: String?

    public init(tool: String, windowID: UInt32, action: BatchAction, logDetail: String? = nil) {
        self.tool = tool
        self.windowID = windowID
        self.action = action
        self.logDetail = logDetail
    }
}

public struct BatchStepResult: Sendable, Equatable {
    public var tool: String
    public var ok: Bool
    public var skipped: Bool
    public var result: ActionResult?
    public var error: String?

    public init(tool: String, ok: Bool, skipped: Bool = false, result: ActionResult? = nil, error: String? = nil) {
        self.tool = tool
        self.ok = ok
        self.skipped = skipped
        self.result = result
        self.error = error
    }
}

public struct PermissionStatus: Codable, Sendable, Equatable {
    public var accessibility: Bool
    public var screenRecording: Bool

    public init(accessibility: Bool, screenRecording: Bool) {
        self.accessibility = accessibility
        self.screenRecording = screenRecording
    }
}

public struct AgentDisplayStatus: Codable, Sendable, Equatable {
    public var isActive: Bool
    public var displayID: UInt32?
    public var bounds: Rect?
    public var hostedWindowIDs: [UInt32]
    public var cursorClampEnabled: Bool

    public init(isActive: Bool, displayID: UInt32?, bounds: Rect?, hostedWindowIDs: [UInt32], cursorClampEnabled: Bool) {
        self.isActive = isActive
        self.displayID = displayID
        self.bounds = bounds
        self.hostedWindowIDs = hostedWindowIDs
        self.cursorClampEnabled = cursorClampEnabled
    }
}

public struct WindowPlacement: Codable, Sendable, Equatable {
    public var windowID: UInt32
    public var originalFrame: Rect
    public var agentFrame: Rect

    public init(windowID: UInt32, originalFrame: Rect, agentFrame: Rect) {
        self.windowID = windowID
        self.originalFrame = originalFrame
        self.agentFrame = agentFrame
    }
}

public struct BrowserStatus: Codable, Sendable, Equatable {
    public var connected: Bool
    public var extensionVersion: String?

    public init(connected: Bool, extensionVersion: String?) {
        self.connected = connected
        self.extensionVersion = extensionVersion
    }
}

public struct BrowserTab: Codable, Sendable, Equatable {
    public var id: Int
    public var url: String
    public var title: String
    public var isAgentTab: Bool

    public init(id: Int, url: String, title: String, isAgentTab: Bool) {
        self.id = id
        self.url = url
        self.title = title
        self.isAgentTab = isAgentTab
    }
}

/// `outline` is a compact accessibility outline of the page; interactive nodes carry `[ref]` markers.
public struct BrowserSnapshot: Sendable, Equatable {
    public var tab: BrowserTab
    public var outline: String
    public var screenshot: Screenshot?

    public init(tab: BrowserTab, outline: String, screenshot: Screenshot?) {
        self.tab = tab
        self.outline = outline
        self.screenshot = screenshot
    }
}

/// `point` is in CSS pixels relative to the tab's viewport.
public enum BrowserTarget: Sendable, Equatable {
    case ref(String)
    case point(x: Double, y: Double)
}
