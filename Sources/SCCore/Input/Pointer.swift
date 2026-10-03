// Click and wheel recipes ported from cua-driver (MIT, github.com/trycua/cua); see LICENSES/cua-driver-MIT.txt.
import AppKit
import CoreGraphics
import SCShared

/// Pixel input addressed to one window of one process. Events go to the process, never to the
/// HID tap, so the real cursor, the frontmost app and the active Space are untouched. Events are
/// addressed to the window, so they land in place even when it is on another Space.
enum Pointer {
    private enum Field {
        static let mouseEventNumber: UInt32 = 0
        static let clickState: UInt32 = 1
        static let buttonNumber: UInt32 = 3
        static let subtype: UInt32 = 7
        static let targetPID: UInt32 = 40
        static let windowNumber: UInt32 = 51
        static let clickGroup: UInt32 = 58
        static let windowUnderPointer: UInt32 = 91
        static let windowUnderPointerThatCanHandle: UInt32 = 92
    }

    /// A point in global screen coordinates and the same point relative to the window's top-left.
    struct Location {
        var screen: CGPoint
        var local: CGPoint
    }

    private static let touchSubtype: Int64 = 3
    private static let offscreen = Location(screen: CGPoint(x: -1, y: -1), local: CGPoint(x: -1, y: -1))

    /// Each of these returns when its first event that can change anything (a press or a wheel tick) went out: the
    /// app's answer can come before the call returns. A refusal after partial dispatch still releases a held button.
    @discardableResult
    static func click(pid: pid_t, wid: CGWindowID, window: AXUIElement? = nil, at location: Location, button: MouseButton, count: Int,
                      flags: CGEventFlags = []) throws -> Date {
        try requireSupportedModifiers(flags)
        let both = !isBareSurface(pid: pid, wid: wid, window: window, at: location.screen)
        return try withFocus(pid: pid, wid: wid) {
            switch button {
            case .left: return try leftClick(pid: pid, wid: wid, at: location, count: count, flags: flags, both: both)
            case .right: return try buttonClick(pid: pid, wid: wid, at: location, count: count, button: .right, flags: flags, both: both)
            case .middle: return try buttonClick(pid: pid, wid: wid, at: location, count: count, button: .center, flags: flags, both: both)
            }
        }
    }

    /// Down at `from`, interpolated drags, up at `to`. Drags carry the same window addressing as
    /// clicks, so selection, note drawing and slider moves work without the real cursor.
    @discardableResult
    static func drag(pid: pid_t, wid: CGWindowID, window: AXUIElement? = nil, from: Location, to: Location,
                     flags: CGEventFlags = [], steps: Int = 16) throws -> Date {
        try requireSupportedModifiers(flags)
        let both = !isBareSurface(pid: pid, wid: wid, window: window, at: from.screen)
        return try withFocus(pid: pid, wid: wid) {
            let source = CGEventSource(stateID: .hidSystemState)
            let group = clickGroupID()
            try postMovedPrimer(pid: pid, wid: wid, at: from, source: source, group: group, both: both)
            _ = try postPair(pid: pid, wid: wid, at: offscreen, source: source, group: group,
                             button: .left, clickState: 1, downPhase: 1, upPhase: 2, flags: [], both: both,
                             downDelay: 1_000, upDelay: 100_000)
            // Allocate the release before dispatching the down. Even allocation failure, pause, or human takeover
            // during the drag must leave the app with its button released at the last point we actually delivered.
            let release = try mouseEvent(.leftMouseUp, pid: pid, wid: wid, at: from, source: source,
                                         group: group, button: .left, clickState: 1, flags: flags)
            let down = try mouseEvent(.leftMouseDown, pid: pid, wid: wid, at: from, source: source,
                                      group: group, button: .left, clickState: 1, flags: flags)
            try NativeSafety.check(pid: pid, wid: wid, input: true)
            var last = from
            let pressed = Date()
            deliver(down, pid: pid, both: both)
            defer {
                release.location = last.screen
                stamp(release, pid: pid, wid: wid, location: last, clickState: 1, buttonNumber: 0, group: group)
                // This is cleanup for an already-dispatched down, not a new action: never gate its release.
                deliver(release, pid: pid, both: both)
            }
            usleep(40_000)
            let count = max(steps, 2)
            for step in 1...count {
                let t = CGFloat(step) / CGFloat(count)
                func lerp(_ a: CGPoint, _ b: CGPoint) -> CGPoint { CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t) }
                let location = Location(screen: lerp(from.screen, to.screen), local: lerp(from.local, to.local))
                let event = try mouseEvent(.leftMouseDragged, pid: pid, wid: wid, at: location, source: source,
                                          group: group, button: .left, clickState: 1, flags: flags)
                try NativeSafety.check(pid: pid, wid: wid, input: true)
                last = location
                deliver(event, pid: pid, both: both)
                usleep(12_000)
            }
            return pressed
        }
    }

    /// SkyLight delivery is asynchronous: the target needs ~40 ms to take its synthetic focus, and keeps
    /// it ~100 ms after the last event so Chromium's renderer hop consumes the mouse-up before it goes.
    private static func withFocus<T>(pid: pid_t, wid: CGWindowID, _ body: () throws -> T) throws -> T {
        try SkyLight.requirePointerSupport()
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        let handoff = try SkyLight.focusWithoutRaise(pid: pid, wid: wid)
        guard handoff != nil || SkyLight.isFront(pid: pid, wid: wid) else {
            throw SCError.notAllowed("The target window could not receive safe background pointer focus. Read its state again or use a semantic AX action.")
        }
        defer {
            if let handoff {
                usleep(100_000)
                SkyLight.restore(handoff)
            }
        }
        if handoff != nil { usleep(40_000) }
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        return try body()
    }

    /// Stamped mouseMoved, then a down/up primer at (-1,-1) that satisfies Chromium's
    /// user-activation gate (and absorbs AppKit's first-click) without hitting any element,
    /// then the real pairs.
    private static func leftClick(pid: pid_t, wid: CGWindowID, at location: Location, count: Int, flags: CGEventFlags, both: Bool) throws -> Date {
        let source = CGEventSource(stateID: .hidSystemState)
        let group = clickGroupID()
        let move = try mouseEvent(.mouseMoved, pid: pid, wid: wid, at: location, source: source,
                                  group: group, button: .left, clickState: 0, flags: flags)
        SkyLight.setField(move, Field.mouseEventNumber, 2)
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        deliver(move, pid: pid, both: both)
        usleep(15_000)
        _ = try postPair(pid: pid, wid: wid, at: offscreen, source: source, group: group,
                         button: .left, clickState: 1, downPhase: 1, upPhase: 2, flags: [], both: both,
                         downDelay: 1_000, upDelay: 100_000)
        let pairs = min(max(count, 1), 3)
        var pressed = Date()
        for pair in 1...pairs {
            let pairPressed = try postPair(pid: pid, wid: wid, at: location, source: source, group: group,
                                           button: .left, clickState: Int64(pair), downPhase: 3, upPhase: 3,
                                           flags: flags, both: both, downDelay: 1_000, upDelay: pair < pairs ? 80_000 : 0)
            if pair == 1 { pressed = pairPressed }
        }
        return pressed
    }

    private static func buttonClick(pid: pid_t, wid: CGWindowID, at location: Location, count: Int, button: CGMouseButton, flags: CGEventFlags, both: Bool) throws -> Date {
        let source = CGEventSource(stateID: .hidSystemState)
        let group = clickGroupID()
        try postMovedPrimer(pid: pid, wid: wid, at: location, source: source, group: group, both: both)
        var pressed = Date()
        for pair in 1...min(max(count, 1), 3) {
            let pairPressed = try postPair(pid: pid, wid: wid, at: location, source: source, group: group,
                                           button: button, clickState: Int64(pair), flags: flags, both: both,
                                           downDelay: 28_000, upDelay: 80_000)
            if pair == 1 { pressed = pairPressed }
        }
        return pressed
    }

    /// Construct both halves before the down. Once it is sent, the up is unconditional so a pause or takeover
    /// cannot leave a control tracking a pressed button. The next pair must pass a fresh safety checkpoint.
    private static func postPair(pid: pid_t, wid: CGWindowID, at location: Location, source: CGEventSource?, group: Int64,
                                 button: CGMouseButton, clickState: Int64, downPhase: Int64? = nil, upPhase: Int64? = nil,
                                 flags: CGEventFlags, both: Bool, downDelay: UInt32, upDelay: UInt32) throws -> Date {
        let downType: CGEventType = button == .left ? .leftMouseDown : button == .right ? .rightMouseDown : .otherMouseDown
        let upType: CGEventType = button == .left ? .leftMouseUp : button == .right ? .rightMouseUp : .otherMouseUp
        let down = try mouseEvent(downType, pid: pid, wid: wid, at: location, source: source,
                                  group: group, button: button, clickState: clickState, flags: flags)
        let up = try mouseEvent(upType, pid: pid, wid: wid, at: location, source: source,
                                group: group, button: button, clickState: clickState, flags: flags)
        if let downPhase { SkyLight.setField(down, Field.mouseEventNumber, downPhase) }
        if let upPhase { SkyLight.setField(up, Field.mouseEventNumber, upPhase) }
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        let pressed = Date()
        deliver(down, pid: pid, both: both)
        if downDelay > 0 { usleep(downDelay) }
        deliver(up, pid: pid, both: both)
        if upDelay > 0 { usleep(upDelay) }
        return pressed
    }

    /// `ticks` wheel notches at `location`; positive `dy` reveals content above, positive `dx` content to the left.
    @discardableResult
    static func scroll(pid: pid_t, wid: CGWindowID, at location: Location, dx: Int32, dy: Int32, ticks: Int) throws -> Date {
        try SkyLight.requirePointerSupport()
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        let source = CGEventSource(stateID: .hidSystemState)
        try postMovedPrimer(pid: pid, wid: wid, at: location, source: source, group: clickGroupID(), both: true)
        var ticked = Date()
        for tick in 0..<max(ticks, 1) {
            guard let event = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)
            else { throw SCError.actionFailed("Could not construct a scroll event; scrolling stopped.") }
            event.location = location.screen
            SkyLight.setWindowLocation(event, location.local)
            SkyLight.setField(event, Field.windowNumber, Int64(wid))
            SkyLight.setField(event, Field.windowUnderPointer, Int64(wid))
            SkyLight.setField(event, Field.windowUnderPointerThatCanHandle, Int64(wid))
            SkyLight.setField(event, Field.targetPID, Int64(pid))
            try NativeSafety.check(pid: pid, wid: wid, input: true)
            if tick == 0 { ticked = Date() }
            deliver(event, pid: pid, both: true)
            usleep(30_000)
        }
        return ticked
    }

    /// A window that never saw a move keeps stale cursor-tracking state and ignores the next down.
    private static func postMovedPrimer(pid: pid_t, wid: CGWindowID, at location: Location, source: CGEventSource?, group: Int64, both: Bool) throws {
        let event = try mouseEvent(.mouseMoved, pid: pid, wid: wid, at: location, source: source,
                                  group: group, button: .left, clickState: 0, flags: [])
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        deliver(event, pid: pid, both: both)
        usleep(12_000)
    }

    private static func mouseEvent(_ type: CGEventType, pid: pid_t, wid: CGWindowID, at location: Location,
                                   source: CGEventSource?, group: Int64, button: CGMouseButton,
                                   clickState: Int64, flags: CGEventFlags) throws -> CGEvent {
        guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: location.screen, mouseButton: button)
        else { throw SCError.actionFailed("Could not construct a pointer event; input stopped.") }
        let number: Int64 = button == .left ? 0 : button == .right ? 1 : 2
        stamp(event, pid: pid, wid: wid, location: location, clickState: clickState, buttonNumber: number, group: group)
        event.flags = flags
        return event
    }

    private static func requireSupportedModifiers(_ flags: CGEventFlags) throws {
        guard flags.isEmpty else {
            throw SCError.notAllowed("Background pointer modifiers cannot be delivered reliably. Use a semantic AX action or the browser backend.")
        }
    }

    /// AppKit reads the hit point from the window location, which must be window-local: stamping
    /// the screen point makes the click land outside the window and get dropped.
    private static func stamp(_ event: CGEvent, pid: pid_t, wid: CGWindowID, location: Location, clickState: Int64, buttonNumber: Int64, group: Int64) {
        SkyLight.setField(event, Field.clickState, clickState)
        SkyLight.setField(event, Field.buttonNumber, buttonNumber)
        SkyLight.setField(event, Field.subtype, touchSubtype)
        SkyLight.setField(event, Field.targetPID, Int64(pid))
        SkyLight.setField(event, Field.windowNumber, Int64(wid))
        SkyLight.setField(event, Field.windowUnderPointer, Int64(wid))
        SkyLight.setField(event, Field.windowUnderPointerThatCanHandle, Int64(wid))
        SkyLight.setField(event, Field.clickGroup, group)
        SkyLight.setWindowLocation(event, location.local)
        switch event.type {
        case .leftMouseDown, .rightMouseDown, .otherMouseDown: event.setDoubleValueField(.mouseEventPressure, value: 1)
        default: event.setDoubleValueField(.mouseEventPressure, value: 0)
        }
    }

    /// SkyLight reaches backgrounded Chromium/Catalyst windows; the public path lands where SkyLight mouse delivery
    /// drops, including windows on other Spaces. An AppKit control acts once on the pair (a button tracks one press,
    /// a caret click lands twice in the same place), but a bare custom surface acts on every copy, so it gets only
    /// the public path (`isBareSurface`).
    private static func deliver(_ event: CGEvent, pid: pid_t, both: Bool) {
        HumanInputMonitor.stamp(event)
        if both { SkyLight.post(event, to: pid, authenticated: false) }
        event.postToPid(pid)
    }

    /// A custom-drawn surface in a native AppKit window on the current Space: below the window and plain containers,
    /// no accessibility element lies under the point. Views like that (canvases, DAW panels, game views) act on each
    /// copy of an event, so a toggle would flip back. Anything accessible under the point, and anything inside a web
    /// view, keeps both copies. The app's hit test settles the common case, a control in front, in one call; otherwise
    /// the window's own tree is walked, because the hit test answers for whatever window covers the point.
    private static func isBareSurface(pid: pid_t, wid: CGWindowID, window: AXUIElement?, at point: CGPoint) -> Bool {
        guard var node = window, WindowList.record(wid)?.isOnScreen == true, isPlainAppKit(pid) else { return false }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.25)
        var hit: AXUIElement?
        if AXUIElementCopyElementAtPosition(app, Float(point.x), Float(point.y), &hit) == .success, let hit {
            AXUIElementSetMessagingTimeout(hit, 0.25)
            if let role = hit.role, !containerRoles.contains(role), let owner = hit.element(kAXWindowAttribute),
               CFEqual(owner, node) {
                return false
            }
        }
        for _ in 0..<16 {
            AXUIElementSetMessagingTimeout(node, 0.25)
            guard let role = node.role, containerRoles.contains(role) else { return false }
            let children = node.elements(kAXChildrenAttribute)
            children.forEach { AXUIElementSetMessagingTimeout($0, 0.25) }
            guard let child = children.last(where: { $0.frame?.contains(point) == true }) else { return true }
            node = child
        }
        return false
    }

    private static let containerRoles: Set<String> = [
        kAXWindowRole, kAXGroupRole, kAXScrollAreaRole, kAXSplitGroupRole, kAXUnknownRole, "AXLayoutArea",
    ]

    private static let bundleLock = NSLock()
    /// By bundle path rather than pid, so a reused pid never inherits another app's answer.
    private static var plainAppKitBundles: [String: Bool] = [:]

    /// Not Chromium, Electron or Catalyst, which need SkyLight's copy in the background.
    private static func isPlainAppKit(_ pid: pid_t) -> Bool {
        let running = NSRunningApplication(processIdentifier: pid)
        guard let url = running?.bundleURL ?? running?.executableURL else { return false }
        bundleLock.lock()
        defer { bundleLock.unlock() }
        if let known = plainAppKitBundles[url.path] { return known }
        let catalyst = Bundle(url: url)?.infoDictionary?["UIDeviceFamily"] != nil
        let plain = !catalyst && WindowResolver.detectEngine(url) == .native
        plainAppKitBundles[url.path] = plain
        return plain
    }

    private static func clickGroupID() -> Int64 {
        Int64(DispatchTime.now().uptimeNanoseconds % 1_000_000_000)
    }
}
