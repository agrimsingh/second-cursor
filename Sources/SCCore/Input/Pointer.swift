// Click and wheel recipes ported from cua-driver (MIT, github.com/trycua/cua); see LICENSES/cua-driver-MIT.txt.
import CoreGraphics
import Foundation
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

    static func click(pid: pid_t, wid: CGWindowID, at location: Location, button: MouseButton, count: Int, flags: CGEventFlags = []) {
        withFocus(pid: pid, wid: wid) {
            switch button {
            case .left: leftClick(pid: pid, wid: wid, at: location, count: count, flags: flags)
            case .right: buttonClick(pid: pid, wid: wid, at: location, count: count, button: .right, flags: flags)
            case .middle: buttonClick(pid: pid, wid: wid, at: location, count: count, button: .center, flags: flags)
            }
        }
    }

    /// Down at `from`, interpolated drags, up at `to`. Drags carry the same window addressing as
    /// clicks, so selection, note drawing and slider moves work without the real cursor.
    static func drag(pid: pid_t, wid: CGWindowID, from: Location, to: Location, flags: CGEventFlags = [], steps: Int = 16) {
        withFocus(pid: pid, wid: wid) {
            let source = CGEventSource(stateID: .hidSystemState)
            let group = clickGroupID()
            func post(_ type: CGEventType, _ location: Location, clickState: Int64, delayMs: UInt32) {
                guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: location.screen, mouseButton: .left)
                else { return }
                stamp(event, pid: pid, wid: wid, location: location, clickState: clickState, buttonNumber: 0, group: group)
                event.flags = flags
                SkyLight.post(event, to: pid, authenticated: false)
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

    private static func withFocus(pid: pid_t, wid: CGWindowID, _ body: () -> Void) {
        let handoff = SkyLight.focusWithoutRaise(pid: pid, wid: wid)
        if handoff != nil { usleep(50_000) }
        body()
        if let handoff {
            usleep(50_000)
            SkyLight.restore(handoff)
        }
    }

    /// Stamped mouseMoved, then a down/up primer at (-1,-1) that satisfies Chromium's
    /// user-activation gate (and absorbs AppKit's first-click) without hitting any element,
    /// then the real pairs.
    private static func leftClick(pid: pid_t, wid: CGWindowID, at location: Location, count: Int, flags: CGEventFlags) {
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
            SkyLight.post(event, to: pid, authenticated: false)
            if step.delayMs > 0 { usleep(step.delayMs * 1000) }
        }
    }

    private static func buttonClick(pid: pid_t, wid: CGWindowID, at location: Location, count: Int, button: CGMouseButton, flags: CGEventFlags) {
        let source = CGEventSource(stateID: .hidSystemState)
        let group = clickGroupID()
        let (down, up, number): (CGEventType, CGEventType, Int64) = button == .right
            ? (.rightMouseDown, .rightMouseUp, 1) : (.otherMouseDown, .otherMouseUp, 2)
        postMovedPrimer(pid: pid, wid: wid, at: location, source: source, group: group)
        for pair in 1...min(max(count, 1), 3) {
            for type in [down, up] {
                guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: location.screen, mouseButton: button)
                else { continue }
                stamp(event, pid: pid, wid: wid, location: location, clickState: Int64(pair), buttonNumber: number, group: group)
                event.flags = flags
                postBoth(event, pid: pid)
                usleep(type == down ? 28_000 : 80_000)
            }
        }
    }

    /// `ticks` wheel notches at `location`; positive `dy` reveals content above, positive `dx` content to the left.
    static func scroll(pid: pid_t, wid: CGWindowID, at location: Location, dx: Int32, dy: Int32, ticks: Int) {
        let source = CGEventSource(stateID: .hidSystemState)
        postMovedPrimer(pid: pid, wid: wid, at: location, source: source, group: clickGroupID())
        for _ in 0..<max(ticks, 1) {
            guard let event = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)
            else { continue }
            event.location = location.screen
            SkyLight.setWindowLocation(event, location.local)
            SkyLight.setField(event, Field.windowNumber, Int64(wid))
            SkyLight.setField(event, Field.windowUnderPointer, Int64(wid))
            SkyLight.setField(event, Field.windowUnderPointerThatCanHandle, Int64(wid))
            SkyLight.setField(event, Field.targetPID, Int64(pid))
            postBoth(event, pid: pid)
            usleep(30_000)
        }
    }

    /// A window that never saw a move keeps stale cursor-tracking state and ignores the next down.
    private static func postMovedPrimer(pid: pid_t, wid: CGWindowID, at location: Location, source: CGEventSource?, group: Int64) {
        guard let event = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: location.screen, mouseButton: .left)
        else { return }
        stamp(event, pid: pid, wid: wid, location: location, clickState: 0, buttonNumber: 0, group: group)
        postBoth(event, pid: pid)
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

    /// SkyLight reaches backgrounded Chromium/Catalyst windows; the public path lands where SkyLight mouse delivery drops.
    private static func postBoth(_ event: CGEvent, pid: pid_t) {
        SkyLight.post(event, to: pid, authenticated: false)
        event.postToPid(pid)
    }

    private static func clickGroupID() -> Int64 {
        Int64(DispatchTime.now().uptimeNanoseconds % 1_000_000_000)
    }
}
