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

/// Evidence attached to a capture, kept separate from an application's unobservable repaint time.
public struct FrameFreshness: Codable, Sendable, Equatable {
    /// `unverified` means capture completed, but the application's last repaint time is unknown.
    public var state: String
    public var capturedAt: Double
    public var reason: String
    public var windowOnScreen: Bool?
    /// Identical pixels do not prove a frozen app, and changed pixels do not prove the expected action succeeded.
    public var changedSincePreviousCapture: Bool?

    public init(state: String = "unverified", capturedAt: Double = Date().timeIntervalSince1970,
                reason: String, windowOnScreen: Bool? = nil, changedSincePreviousCapture: Bool? = nil) {
        self.state = state
        self.capturedAt = capturedAt
        self.reason = reason
        self.windowOnScreen = windowOnScreen
        self.changedSincePreviousCapture = changedSincePreviousCapture
    }

    enum CodingKeys: String, CodingKey {
        case state, reason
        case capturedAt = "captured_at"
        case windowOnScreen = "window_on_screen"
        case changedSincePreviousCapture = "changed_since_previous_capture"
    }
}

public struct NativeGestureCapability: Codable, Sendable, Equatable {
    public var supported: Bool
    public var reason: String?
    public var alternative: String?

    public init(supported: Bool, reason: String? = nil, alternative: String? = nil) {
        self.supported = supported
        self.reason = reason
        self.alternative = alternative
    }
}

public struct NativeCapabilityProfile: Codable, Sendable, Equatable {
    public var backend: String
    public var osVersion: String
    public var inputMonitoringAvailable: Bool
    public var capabilities: [String: NativeGestureCapability]

    public init(backend: String = "native_macos", osVersion: String,
                inputMonitoringAvailable: Bool, capabilities: [String: NativeGestureCapability]) {
        self.backend = backend
        self.osVersion = osVersion
        self.inputMonitoringAvailable = inputMonitoringAvailable
        self.capabilities = capabilities
    }

    enum CodingKeys: String, CodingKey {
        case backend, capabilities
        case osVersion = "os_version"
        case inputMonitoringAvailable = "input_monitoring_available"
    }
}

/// PNG image. `scale` is image pixels per window point, so a pixel (px, py) maps to window-local point (px / scale, py / scale).
public struct Screenshot: Sendable, Equatable {
    public var pngData: Data
    public var width: Int
    public var height: Int
    public var scale: Double
    public var freshness: FrameFreshness?

    public init(pngData: Data, width: Int, height: Int, scale: Double, freshness: FrameFreshness? = nil) {
        self.pngData = pngData
        self.width = width
        self.height = height
        self.scale = scale
        self.freshness = freshness
    }
}

public struct SnapshotOptions: Sendable, Equatable {
    public var includeScreenshot: Bool
    public var includeMenuBar: Bool
    /// Most indexed elements to list. nil keeps the value of the window's previous read (400 at first), so a
    /// diff never compares outlines cut at different lengths.
    public var maxElements: Int?
    /// `false` returns a diff against the window's previous full snapshot when one exists.
    public var full: Bool
    /// When set, returns only the subtree rooted at this element index, at full depth.
    public var scope: Int?
    /// Like `scope`, naming the container by its label (exact match first, then case-insensitive).
    public var scopeLabel: String?
    /// Recognizes the text on the window's pixels and merges what accessibility does not already show.
    public var ocr: Bool
    /// Explicit bounded visual-readiness probe for delayed custom drawing after a recent action.
    public var visualSettle: Bool

    public init(includeScreenshot: Bool = true, includeMenuBar: Bool = false, maxElements: Int? = nil,
                full: Bool = true, scope: Int? = nil, scopeLabel: String? = nil, ocr: Bool = false,
                visualSettle: Bool = false) {
        self.includeScreenshot = includeScreenshot
        self.includeMenuBar = includeMenuBar
        self.maxElements = maxElements
        self.full = full
        self.scope = scope
        self.scopeLabel = scopeLabel
        self.ocr = ocr
        self.visualSettle = visualSettle
    }
}

/// Pixel quietness only; it does not establish task success or the freshness of an off-Space surface.
public struct VisualSettleResult: Codable, Sendable, Equatable {
    public var status: String
    public var elapsedMS: Int
    public var samples: Int
    public var changedDuringProbe: Bool
    public var reason: String

    public init(status: String, elapsedMS: Int, samples: Int, changedDuringProbe: Bool, reason: String) {
        self.status = status
        self.elapsedMS = elapsedMS
        self.samples = samples
        self.changedDuringProbe = changedDuringProbe
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case status, samples, reason
        case elapsedMS = "elapsed_ms", changedDuringProbe = "changed_during_probe"
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
    public var scopeLabel: String?
    public var screenshot: Screenshot?
    /// Set instead of `screenshot` when the capture failed; the tree is still valid.
    public var screenshotError: String?
    public var elements: [ElementInfo] = []
    public var capabilities: NativeCapabilityProfile?
    /// For `ocr` reads: capture plus recognition time, how many recognized texts were merged, or why it failed.
    public var ocrMS: Int?
    public var ocrCount: Int?
    public var ocrError: String?
    public var visualSettle: VisualSettleResult?
    /// Why the tree may be missing content the window would normally show.
    public var hint: String?

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

/// Elements of a window's latest full read that matched a search, best first, as outline lines.
public struct ElementSearch: Sendable, Equatable {
    public var lines: [String]
    /// Matches before `limit` was applied.
    public var total: Int
    /// The read that was searched.
    public var snapshotID: String
    /// True when this session had not read the window yet, so the search read it first.
    public var readWindow: Bool

    public init(lines: [String], total: Int, snapshotID: String, readWindow: Bool) {
        self.lines = lines
        self.total = total
        self.snapshotID = snapshotID
        self.readWindow = readWindow
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
    /// The action may still execute or only part of it was confirmed. An observation or retry must reconcile it.
    public var uncertain: Bool?

    public init(route: String, verified: Bool? = nil, note: String? = nil, uncertain: Bool? = nil) {
        self.route = route
        self.verified = verified
        self.note = note
        self.uncertain = uncertain
    }
}

public enum BatchAction: Sendable, Equatable {
    case click(target: ClickTarget, button: MouseButton, count: Int)
    case typeText(text: String, elementIndex: Int?, keystrokesOnly: Bool)
    case selectText(elementIndex: Int, text: String, prefix: String?, suffix: String?, mode: String)
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
    /// Shown beside the agent cursor while this step runs; nil keeps the batch's caption.
    public var caption: String?

    public init(tool: String, windowID: UInt32, action: BatchAction, logDetail: String? = nil, caption: String? = nil) {
        self.tool = tool
        self.windowID = windowID
        self.action = action
        self.logDetail = logDetail
        self.caption = caption
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

/// What `expect` and `wait_for` wait for: `text` in some element's title, value or description, and/or the element
/// `elementIndex` (of the session's latest observation) having exactly `value`. `gone` waits for it to stop holding.
public struct WaitCondition: Sendable, Equatable {
    public var text: String?
    public var elementIndex: Int?
    public var value: String?
    public var gone: Bool

    public init(text: String? = nil, elementIndex: Int? = nil, value: String? = nil, gone: Bool = false) {
        self.text = text
        self.elementIndex = elementIndex
        self.value = value
        self.gone = gone
    }
}

public struct ConditionResult: Sendable, Equatable {
    /// Whether the condition, with `gone` applied, holds.
    public var met: Bool
    /// What was last seen, e.g. the element's actual value.
    public var detail: String
    /// 0 when it held at the first check.
    public var waited: TimeInterval

    public init(met: Bool, detail: String, waited: TimeInterval = 0) {
        self.met = met
        self.detail = detail
        self.waited = waited
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
