import ApplicationServices
import CoreGraphics
import Foundation
import SCDisplayPrivate
import SCShared

/// The agent-owned virtual display: a 1440x900 HiDPI monitor that touches the main display at
/// a single corner, with a cursor clamp keeping the user's pointer off it. Work runs serially on
/// a private queue, so the short polls below never block Swift's cooperative pool.
public actor AgentDisplay: AgentDisplayControlling {
    public static let displayName = "Second Cursor Agent"
    static let size = CGSize(width: 1440, height: 900)
    /// Keeps hosted windows clear of the agent display's menu bar.
    static let topInset: CGFloat = 40
    /// Apps round window frames; flush-left windows can land a point left of the display.
    static let leftInset: CGFloat = 20
    static let cascadeStep: CGFloat = 30
    static let cascadeLimit = 10

    private struct Hosted {
        let pid: pid_t
        let originalFrame: CGRect
        let resized: Bool
    }

    private let queue = DispatchSerialQueue(label: "dev.secondcursor.agent-display")
    public nonisolated var unownedExecutor: UnownedSerialExecutor { queue.asUnownedSerialExecutor() }

    private var display: SCVirtualDisplay?
    private var clamp: CursorClamp?
    private var hosted: [CGWindowID: Hosted] = [:]

    public init() {}

    deinit {
        clamp?.stop()
    }

    public func status() -> AgentDisplayStatus {
        guard let id = liveDisplayID() else {
            return AgentDisplayStatus(isActive: false, displayID: nil, bounds: nil, hostedWindowIDs: [], cursorClampEnabled: false)
        }
        hosted = hosted.filter { WindowServer.window($0.key) != nil }
        return AgentDisplayStatus(
            isActive: true, displayID: id, bounds: Rect(CGDisplayBounds(id)),
            hostedWindowIDs: hosted.keys.sorted(), cursorClampEnabled: clamp != nil
        )
    }

    public func ensureDisplay() throws -> AgentDisplayStatus {
        try Execution.context.beforeDispatch()
        if liveDisplayID() == nil {
            try createDisplay()
        }
        return status()
    }

    public func moveToAgentDisplay(windowID: UInt32, pid: Int32) throws -> WindowPlacement {
        try Execution.context.beforeDispatch()
        guard AXIsProcessTrusted() else { throw SCError.permissionDenied("Accessibility") }
        if liveDisplayID() == nil {
            try createDisplay()
        }
        guard let displayID = liveDisplayID() else { throw SCError.actionFailed("The agent display disappeared") }
        let bounds = CGDisplayBounds(displayID)

        guard let current = WindowServer.window(windowID) else { throw SCError.windowNotFound(windowID) }
        guard current.pid == pid else {
            throw SCError.invalidArgument("Window \(windowID) belongs to pid \(current.pid), not \(pid)")
        }
        if let entry = hosted[windowID], Self.isOn(bounds, current.frame) {
            return WindowPlacement(windowID: windowID, originalFrame: Rect(entry.originalFrame), agentFrame: Rect(current.frame))
        }
        guard let element = AXWindow.find(pid: pid, windowID: windowID) else {
            if WindowServer.isOnHiddenSpace(windowID) { throw SCError.windowOnHiddenSpace(windowID) }
            throw SCError.actionFailed("Window \(windowID) is not reachable through Accessibility")
        }
        if AXWindow.flag(element, kAXMinimizedAttribute) {
            throw SCError.actionFailed("Window \(windowID) is minimized; restore it before moving it to the agent display")
        }
        if AXWindow.flag(element, "AXFullScreen") {
            throw SCError.actionFailed("Window \(windowID) is full screen; exit full screen before moving it to the agent display")
        }

        // A window that opened on the agent display has no user-visible home; return it to the main display.
        let original = hosted[windowID]?.originalFrame
            ?? (Self.isOn(bounds, current.frame) ? Self.mainDisplayFrame(size: current.frame.size) : current.frame)
        let cascade = Self.cascadeStep * CGFloat(hosted.keys.filter { $0 != windowID }.count % Self.cascadeLimit)
        let area = CGRect(
            x: bounds.minX + Self.leftInset + cascade, y: bounds.minY + Self.topInset + cascade,
            width: bounds.width - Self.leftInset - cascade, height: bounds.height - Self.topInset - cascade
        )
        let fitted = CGSize(width: min(current.frame.width, area.width), height: min(current.frame.height, area.height))
        let resize = fitted != current.frame.size
        let agentSpace = WindowServer.currentSpace(of: displayID)

        var placed: ServerWindow?
        var landed = false
        var attemptedPlacement = false
        do {
            for _ in 0..<2 where !landed {
                try Execution.context.beforeDispatch()
                attemptedPlacement = true
                try Self.check(AXWindow.setPosition(element, area.origin), "Moving window \(windowID)")
                if resize {
                    try Execution.context.beforeDispatch()
                    _ = AXWindow.setSize(element, fitted)
                    try Execution.context.beforeDispatch()
                    _ = AXWindow.setPosition(element, area.origin)
                }
                landed = poll(timeout: 2) {
                    placed = WindowServer.window(windowID)
                    guard let placed, Self.isOn(bounds, placed.frame), placed.isOnScreen else { return false }
                    return agentSpace.map { WindowServer.spaces(of: windowID).contains($0) } ?? true
                }
            }
        } catch {
            if attemptedPlacement {
                // Restoring a partially moved window is cleanup for the admitted operation, not a new move.
                // It must still run when cancellation blocks the next placement or resize checkpoint.
                _ = AXWindow.setPosition(element, original.origin)
                if resize { _ = AXWindow.setSize(element, original.size) }
            }
            throw error
        }
        guard landed, let placed else {
            _ = AXWindow.setPosition(element, original.origin)
            if resize { _ = AXWindow.setSize(element, original.size) }
            let frame = placed.map { "frame \(Rect($0.frame)), onScreen \($0.isOnScreen)" } ?? "window gone"
            let spaces = "spaces \(WindowServer.spaces(of: windowID)), agent Space \(agentSpace.map(String.init) ?? "unknown")"
            throw SCError.actionFailed("Window \(windowID) did not land on the agent display (\(frame), \(spaces), bounds \(Rect(bounds)))")
        }
        hosted[windowID] = Hosted(pid: pid, originalFrame: original, resized: resize || hosted[windowID]?.resized == true)
        return WindowPlacement(windowID: windowID, originalFrame: Rect(original), agentFrame: Rect(placed.frame))
    }

    public func hostLaunched(windowID: UInt32, pid: Int32) throws -> WindowPlacement {
        try Execution.context.beforeDispatch()
        guard !WindowServer.isOnHiddenSpace(windowID) else {
            // A detached retry would outlive this operation's task ownership, deadline, and cancellation.
            // The caller must explicitly observe and request a new move if the window becomes reachable.
            throw SCError.notAllowed("Window \(windowID) opened on a desktop Space the user is not viewing and was left in place. No move is queued. Observe the window again and submit a new move_to_agent_display request when it is reachable.")
        }
        return try moveToAgentDisplay(windowID: windowID, pid: pid)
    }

    public func returnWindow(windowID: UInt32) throws {
        try Execution.context.beforeDispatch()
        guard let entry = hosted[windowID] else {
            throw SCError.invalidArgument("Window \(windowID) is not on the agent display")
        }
        guard WindowServer.window(windowID) != nil else {
            hosted[windowID] = nil
            throw SCError.windowNotFound(windowID)
        }
        guard AXIsProcessTrusted() else { throw SCError.permissionDenied("Accessibility") }
        guard let element = AXWindow.find(pid: entry.pid, windowID: windowID) else {
            throw SCError.actionFailed("Window \(windowID) is not reachable through Accessibility")
        }

        try Execution.context.beforeDispatch()
        try Self.check(AXWindow.setPosition(element, entry.originalFrame.origin), "Returning window \(windowID)")
        if entry.resized {
            try Execution.context.beforeDispatch()
            _ = AXWindow.setSize(element, entry.originalFrame.size)
            try Execution.context.beforeDispatch()
            _ = AXWindow.setPosition(element, entry.originalFrame.origin)
        }
        if let id = liveDisplayID() {
            let bounds = CGDisplayBounds(id)
            let left = poll(timeout: 2) { WindowServer.window(windowID).map { !Self.isOn(bounds, $0.frame) } ?? true }
            guard left else { throw SCError.actionFailed("Window \(windowID) did not leave the agent display") }
        }
        hosted[windowID] = nil
    }

    public func teardown() {
        for id in hosted.keys.sorted() {
            guard (try? Execution.context.beforeDispatch()) != nil else { return }
            try? returnWindow(windowID: id)
        }
        guard (try? Execution.context.beforeDispatch()) != nil else { return }
        hosted.removeAll()
        clamp?.stop()
        clamp = nil
        guard let display else { return }
        let id = display.displayID
        display.invalidate()
        self.display = nil
        _ = poll(timeout: 5) { !WindowServer.activeDisplays().contains(id) }
    }

    // MARK: - Display lifecycle

    private func createDisplay() throws {
        guard let created = SCVirtualDisplay(
            name: Self.displayName, width: UInt32(Self.size.width), height: UInt32(Self.size.height), hiDPI: true
        ) else {
            throw SCError.actionFailed("macOS refused to create the agent display (CGVirtualDisplay unavailable)")
        }
        let id = created.displayID
        guard poll(timeout: 3, { WindowServer.activeDisplays().contains(id) }) else {
            created.invalidate()
            throw SCError.actionFailed("The agent display never became active")
        }
        place(id)
        guard poll(timeout: 3, { WindowServer.currentSpace(of: id) != nil }) else {
            created.invalidate()
            throw SCError.actionFailed("The agent display never got its own Space (is \"Displays have separate Spaces\" off?)")
        }
        display = created
        let clamp = CursorClamp(displayID: id)
        self.clamp = clamp.start() ? clamp : nil
    }

    /// Only the agent display's top-left corner touches the main display's bottom-right corner,
    /// so the pointer can reach it through a single point, which the clamp covers.
    private func place(_ id: CGDirectDisplayID) {
        let main = CGDisplayBounds(CGMainDisplayID())
        let origin = CGPoint(x: main.maxX, y: main.maxY)
        guard CGDisplayBounds(id).origin != origin else { return }
        var config: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&config) == .success, let config else { return }
        CGConfigureDisplayOrigin(config, id, Int32(origin.x), Int32(origin.y))
        guard CGCompleteDisplayConfiguration(config, .forSession) == .success else { return }
        _ = poll(timeout: 3) { CGDisplayBounds(id).origin == origin }
    }

    /// Returns nil (and forgets the display) if the window server dropped it on its own; macOS
    /// has then already moved its windows onto another display.
    private func liveDisplayID() -> CGDirectDisplayID? {
        guard let display else { return nil }
        let id = display.displayID
        if !display.isTerminated, WindowServer.onlineDisplays().contains(id) {
            return id
        }
        clamp?.stop()
        clamp = nil
        display.invalidate()
        self.display = nil
        hosted.removeAll()
        return nil
    }

    // MARK: - Geometry

    /// Matches how callers judge placement: a window is on a display when its origin is.
    private static func isOn(_ bounds: CGRect, _ frame: CGRect) -> Bool {
        frame.minX >= bounds.minX && frame.minX < bounds.maxX && frame.minY >= bounds.minY && frame.minY < bounds.maxY
    }

    private static func mainDisplayFrame(size: CGSize) -> CGRect {
        let main = CGDisplayBounds(CGMainDisplayID())
        let origin = CGPoint(x: max(main.minX, main.midX - size.width / 2), y: max(main.minY + topInset, main.midY - size.height / 2))
        return CGRect(origin: origin, size: size)
    }

    private static func check(_ error: AXError, _ what: String) throws {
        guard error == .success else { throw SCError.actionFailed("\(what) failed (AXError \(error.rawValue))") }
    }
}
