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

    static func click(pid: pid_t, wid: CGWindowID, window: AXUIElement? = nil, at location: Location, button: MouseButton, count: Int,
                      flags: CGEventFlags = []) {
        let both = !isBareSurface(pid: pid, wid: wid, window: window, at: location.screen)
        withFocus(pid: pid, wid: wid) {
            switch button {
            case .left: leftClick(pid: pid, wid: wid, at: location, count: count, flags: flags, both: both)
            case .right: buttonClick(pid: pid, wid: wid, at: location, count: count, button: .right, flags: flags, both: both)
            case .middle: buttonClick(pid: pid, wid: wid, at: location, count: count, button: .center, flags: flags, both: both)
            }
        }
    }

    /// Down at `from`, interpolated drags, up at `to`. Drags carry the same window addressing as
    /// clicks, so selection, note drawing and slider moves work without the real cursor.
    static func drag(pid: pid_t, wid: CGWindowID, window: AXUIElement? = nil, from: Location, to: Location,
                     flags: CGEventFlags = [], steps: Int = 16) {
        let both = !isBareSurface(pid: pid, wid: wid, window: window, at: from.screen)
        withFocus(pid: pid, wid: wid) {
            let source = CGEventSource(stateID: .hidSystemState)
            let group = clickGroupID()
            func post(_ type: CGEventType, _ location: Location, clickState: Int64, delayMs: UInt32) {
                guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: location.screen, mouseButton: .left)
                else { return }
                stamp(event, pid: pid, wid: wid, location: location, clickState: clickState, buttonNumber: 0, group: group)
                event.flags = flags
                deliver(event, pid: pid, both: both)
                usleep(delayMs * 1000)
            }
            post(.mouseMoved, from, clickState: 0, delayMs: 15)
            post(.leftMouseDown, offscreen, clickState: 1, delayMs: 1)
            post(.leftMouseUp, offscreen, clickState: 1, delayMs: 100)
            post(.leftMouseDown, from, clickState: 1, delayMs: 40)
            let count = max(steps, 2)
            for step in 1...count {
                let t = CGFloat(step) / CGFloat(count)
                func lerp(_ a: CGPoint, _ b: CGPoint) -> CGPoint { CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t) }
                post(.leftMouseDragged, Location(screen: lerp(from.screen, to.screen), local: lerp(from.local, to.local)), clickState: 1, delayMs: 12)
            }
            post(.leftMouseUp, to, clickState: 1, delayMs: 0)
        }
    }

    /// SkyLight delivery is asynchronous: the target needs ~40 ms to take its synthetic focus, and keeps
    /// it ~100 ms after the last event so Chromium's renderer hop consumes the mouse-up before it goes.
    private static func withFocus(pid: pid_t, wid: CGWindowID, _ body: () -> Void) {
        let handoff = SkyLight.focusWithoutRaise(pid: pid, wid: wid)
        if handoff != nil { usleep(40_000) }
        body()
        if let handoff {
            usleep(100_000)
            SkyLight.restore(handoff)
        }
    }

    /// Stamped mouseMoved, then a down/up primer at (-1,-1) that satisfies Chromium's
    /// user-activation gate (and absorbs AppKit's first-click) without hitting any element,
    /// then the real pairs.
    private static func leftClick(pid: pid_t, wid: CGWindowID, at location: Location, count: Int, flags: CGEventFlags, both: Bool) {
        let source = CGEventSource(stateID: .hidSystemState)
        let group = clickGroupID()
        let pairs = min(max(count, 1), 3)
        typealias Step = (type: CGEventType, location: Location, phase: Int64, clickState: Int64, delayMs: UInt32)
        var steps: [Step] = [
            (.mouseMoved, location, 2, 0, 15),
            (.leftMouseDown, offscreen, 1, 1, 1),
            (.leftMouseUp, offscreen, 2, 1, 100),
        ]
        for pair in 1...pairs {
            steps.append((.leftMouseDown, location, 3, Int64(pair), 1))
            steps.append((.leftMouseUp, location, 3, Int64(pair), pair < pairs ? 80 : 0))
        }
        for step in steps {
            guard let event = CGEvent(mouseEventSource: source, mouseType: step.type, mouseCursorPosition: step.location.screen, mouseButton: .left)
            else { continue }
            stamp(event, pid: pid, wid: wid, location: step.location, clickState: step.clickState, buttonNumber: 0, group: group)
            SkyLight.setField(event, Field.mouseEventNumber, step.phase)
            event.flags = step.location.screen == offscreen.screen ? [] : flags
            deliver(event, pid: pid, both: both)
            if step.delayMs > 0 { usleep(step.delayMs * 1000) }
        }
    }

    private static func buttonClick(pid: pid_t, wid: CGWindowID, at location: Location, count: Int, button: CGMouseButton, flags: CGEventFlags, both: Bool) {
        let source = CGEventSource(stateID: .hidSystemState)
        let group = clickGroupID()
        let (down, up, number): (CGEventType, CGEventType, Int64) = button == .right
            ? (.rightMouseDown, .rightMouseUp, 1) : (.otherMouseDown, .otherMouseUp, 2)
        postMovedPrimer(pid: pid, wid: wid, at: location, source: source, group: group, both: both)
        for pair in 1...min(max(count, 1), 3) {
            for type in [down, up] {
                guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: location.screen, mouseButton: button)
                else { continue }
                stamp(event, pid: pid, wid: wid, location: location, clickState: Int64(pair), buttonNumber: number, group: group)
                event.flags = flags
                deliver(event, pid: pid, both: both)
                usleep(type == down ? 28_000 : 80_000)
            }
        }
    }

    /// `ticks` wheel notches at `location`; positive `dy` reveals content above, positive `dx` content to the left.
    static func scroll(pid: pid_t, wid: CGWindowID, at location: Location, dx: Int32, dy: Int32, ticks: Int) {
        let source = CGEventSource(stateID: .hidSystemState)
        postMovedPrimer(pid: pid, wid: wid, at: location, source: source, group: clickGroupID(), both: true)
        for _ in 0..<max(ticks, 1) {
            guard let event = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)
            else { continue }
            event.location = location.screen
            SkyLight.setWindowLocation(event, location.local)
            SkyLight.setField(event, Field.windowNumber, Int64(wid))
            SkyLight.setField(event, Field.windowUnderPointer, Int64(wid))
            SkyLight.setField(event, Field.windowUnderPointerThatCanHandle, Int64(wid))
            SkyLight.setField(event, Field.targetPID, Int64(pid))
            deliver(event, pid: pid, both: true)
            usleep(30_000)
        }
    }

    /// A window that never saw a move keeps stale cursor-tracking state and ignores the next down.
    private static func postMovedPrimer(pid: pid_t, wid: CGWindowID, at location: Location, source: CGEventSource?, group: Int64, both: Bool) {
        guard let event = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: location.screen, mouseButton: .left)
        else { return }
        stamp(event, pid: pid, wid: wid, location: location, clickState: 0, buttonNumber: 0, group: group)
        deliver(event, pid: pid, both: both)
        usleep(12_000)
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
