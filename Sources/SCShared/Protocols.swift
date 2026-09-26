import Foundation

public enum SCError: Error, LocalizedError, Equatable {
    case notImplemented(String)
    case permissionDenied(String)
    case appNotFound(String)
    case windowNotFound(UInt32)
    case elementNotFound(Int)
    case staleSnapshot(UInt32)
    case actionFailed(String)
    case notAllowed(String)
    case paused
    case browserUnavailable(String)
    case invalidArgument(String)

    public var errorDescription: String? {
        switch self {
        case .notImplemented(let what): return "Not implemented: \(what)"
        case .permissionDenied(let what): return "Permission missing: \(what)"
        case .appNotFound(let query): return "No app matches \"\(query)\""
        case .windowNotFound(let id): return "Window \(id) not found; call list_windows again"
        case .elementNotFound(let index): return "Element \(index) not found in the latest snapshot"
        case .staleSnapshot(let id): return "No current snapshot for window \(id); call get_window_state first"
        case .actionFailed(let reason): return reason
        case .notAllowed(let reason): return reason
        case .paused: return "Second Cursor is paused by the user"
        case .browserUnavailable(let reason): return "Browser unavailable: \(reason)"
        case .invalidArgument(let reason): return reason
        }
    }
}

/// Desktop app control. Implementations must never move the user's cursor, change the
/// frontmost app, or switch Spaces unless a method says otherwise.
public protocol DesktopControlling: AnyObject, Sendable {
    func closeSession(_ sessionID: String)
    /// App processes watched for accessibility notifications: at most 16, least recently used evicted, exited ones dropped.
    func observedApps() -> Int
    func permissionStatus() -> PermissionStatus
    func listApps(includeNotRunning: Bool) async -> [AppInfo]
    /// `appQuery` matches app name or bundle id (case-insensitive substring); nil lists all.
    func listWindows(appQuery: String?) async -> [WindowInfo]
    /// The window an app name refers to. `query` matches like `listWindows`, narrowed to exact name or
    /// bundle id matches when there are any. Throws, listing the candidates, when no single window
    /// stands out as the app's main window.
    func appWindow(_ query: String) async throws -> UInt32
    /// Launches (or reuses) an app without activating it. Returns once it is running.
    /// `newInstance` starts a separate process so the agent never shares a window with the user.
    func launchApp(_ query: String, urls: [URL], newInstance: Bool) async throws -> AppInfo
    func windowState(windowID: UInt32, options: SnapshotOptions) async throws -> WindowSnapshot
    /// Searches the session's latest full read of the window, including elements its outline left out, without
    /// reading the app again; a window the session has not read yet is read once first. `text` matches labels,
    /// values and descriptions case-insensitively (exact, then prefix, then substring); `role` is an AX role
    /// with or without the "AX" prefix. At least one of them is set.
    func findElements(windowID: UInt32, text: String?, role: String?, limit: Int) async throws -> ElementSearch
    /// `modifiers` are held during the click, e.g. ["shift"] or ["cmd"].
    func click(windowID: UInt32, target: ClickTarget, button: MouseButton, count: Int, modifiers: [String]) async throws -> ActionResult
    /// Press at `from`, move to `to`, release; window-local points.
    func drag(windowID: UInt32, from: Point, to: Point, modifiers: [String]) async throws -> ActionResult
    /// Inserts text at the focused element of the window, or at `elementIndex` when given.
    /// `keystrokesOnly` skips the accessibility insert, for apps whose AX layer accepts text it never shows.
    func typeText(windowID: UInt32, text: String, elementIndex: Int?, keystrokesOnly: Bool) async throws -> ActionResult
    func setValue(windowID: UInt32, elementIndex: Int, value: String) async throws -> ActionResult
    /// `keys` is a `+`-separated chord such as "cmd+shift+t", "return", "down".
    func pressKey(windowID: UInt32, keys: String) async throws -> ActionResult
    func scroll(windowID: UInt32, target: ClickTarget?, direction: ScrollDirection, amount: Int) async throws -> ActionResult
    /// Invokes a named accessibility action (e.g. "AXShowMenu", "AXIncrement") exposed by the element.
    func performAction(windowID: UInt32, elementIndex: Int, action: String) async throws -> ActionResult
    func inspectElement(windowID: UInt32, index: Int, offset: Int, length: Int) async throws -> Data
    func selectText(windowID: UInt32, index: Int, text: String, prefix: String?, suffix: String?, mode: String) async throws -> ActionResult
    func paste(windowID: UInt32, text: String, format: String) async throws -> ActionResult
    func screenshot(windowID: UInt32) async throws -> Screenshot
    /// Runs `steps` strictly in order as one uninterrupted block on the batch window's app queue.
    /// `admit` runs before each step (pause/blocked gates, action log) and throws to refuse it.
    /// After a failure the remaining steps are skipped unless `stopOnError` is false. With
    /// `stopOnLayoutChange`, a pixel step whose window changed size since it was last observed stops the batch.
    func runBatch(windowID: UInt32, steps: [BatchStep], stopOnError: Bool, stopOnLayoutChange: Bool,
                  admit: @escaping @Sendable (BatchStep) throws -> Void) async throws -> [BatchStepResult]
    /// Returns once the window's app has finished reacting to its latest action, or after ~3 s.
    func settle(windowID: UInt32) async
    /// Evaluates `condition` against the window once.
    func checkCondition(windowID: UInt32, condition: WaitCondition) async throws -> ConditionResult
    /// Re-checks `condition` on each accessibility notification from the window's app (and every 250 ms) until it
    /// holds or `timeout` passes. The app's queue is free between checks, so other calls on the app go on meanwhile.
    func waitFor(windowID: UInt32, condition: WaitCondition, timeout: TimeInterval) async throws -> ConditionResult
    /// Keeps `session`'s on-screen presence (arrow and menu bar) alive; called for every admitted window or app
    /// call, with the app it works in. With `showArrow` (keyboard actions), an arrow that is not showing yet
    /// appears near the window's focused element, if the window is on the user's current Space.
    /// `caption` is shown beside the arrow: nil leaves the current one, an empty string clears it.
    func noteAgentActivity(session: String, windowID: UInt32?, app: String?, showArrow: Bool, caption: String?)
    /// `session`'s task is over (turn end, session close); presence goes once no session is active.
    func endAgentActivity(session: String)
    func agentActivity() -> AgentActivityStatus
}

/// What the visible agent cursor should show. Points are global screen points, top-left origin.
public enum AgentCursorEvent: Sendable, Equatable {
    case click(at: Point, count: Int)
    case drag(from: Point, to: Point)
    case scroll(at: Point)
    /// A call that moves nothing (a read or a keyboard action) keeps `session` active without moving the
    /// arrow. `near` shows a hidden arrow there; nil leaves it as it is. `caption` replaces the text beside
    /// the arrow: nil leaves it, an empty string clears it.
    case activity(session: String, app: String?, near: Point?, caption: String?)
    case end(session: String)
}

/// Whether an agent task is showing on screen, as the menu bar and the `status` tool report it.
public struct AgentActivityStatus: Sendable, Equatable {
    public var active: Bool
    /// The app of the latest call.
    public var app: String?
    public var sessions: [String]
    /// Where the arrow's tip rests; nil while it is hidden.
    public var arrow: Point?
    /// The text beside the arrow, if any.
    public var caption: String?
    public var idleSeconds: Double?

    public init(active: Bool = false, app: String? = nil, sessions: [String] = [], arrow: Point? = nil,
                caption: String? = nil, idleSeconds: Double? = nil) {
        self.active = active
        self.app = app
        self.sessions = sessions
        self.arrow = arrow
        self.caption = caption
        self.idleSeconds = idleSeconds
    }
}

/// Draws the agent's own on-screen cursor (never the user's) and keeps it up while a task runs. Pointer
/// events return once the cursor has reached its target (bounded to 1.5 s), so the user sees where the action
/// lands before it happens; `activity` and `end` return at once.
public protocol AgentCursorPresenting: AnyObject, Sendable {
    func present(_ event: AgentCursorEvent)
    func activity() -> AgentActivityStatus
}

/// The agent-owned virtual display. Windows hosted there are on an always-active Space
/// that the user never looks at, so background pixel input works on them.
public protocol AgentDisplayControlling: AnyObject, Sendable {
    func status() async -> AgentDisplayStatus
    func ensureDisplay() async throws -> AgentDisplayStatus
    func moveToAgentDisplay(windowID: UInt32, pid: Int32) async throws -> WindowPlacement
    /// Moves a hosted window back to where it was before `moveToAgentDisplay`.
    func returnWindow(windowID: UInt32) async throws
    /// Returns every hosted window, then removes the display.
    func teardown() async
}

/// Chrome control through the Second Cursor extension. Agent tabs are tabs the extension
/// opened itself; implementations must not act on the user's own tabs.
public protocol BrowserControlling: AnyObject, Sendable {
    func instances() async -> Data
    func command(_ method: String, params: Data) async throws -> Data
    func status() async -> BrowserStatus
    func listTabs() async throws -> [BrowserTab]
    func openTab(url: String) async throws -> BrowserTab
    func closeTab(tabID: Int) async throws
    func navigate(tabID: Int, url: String) async throws -> BrowserTab
    func snapshot(tabID: Int, includeScreenshot: Bool) async throws -> BrowserSnapshot
    func click(tabID: Int, target: BrowserTarget) async throws
    func type(tabID: Int, ref: String?, text: String, submit: Bool) async throws
    func pressKey(tabID: Int, keys: String) async throws
    func scroll(tabID: Int, direction: ScrollDirection, amount: Int) async throws
    func screenshot(tabID: Int) async throws -> Screenshot
}
