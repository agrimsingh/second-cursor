import CoreGraphics
import Foundation

/// Keeps the user's pointer off the agent display. A listen-only session tap watches pointer
/// motion on its own run-loop thread and warps the cursor back onto the nearest real display
/// whenever it crosses in.
final class CursorClamp: @unchecked Sendable {
    private let displayID: CGDirectDisplayID
    private let started = DispatchSemaphore(value: 0)
    private let finished = DispatchSemaphore(value: 0)
    private var tap: CFMachPort?
    private var runLoop: CFRunLoop?

    init(displayID: CGDirectDisplayID) {
        self.displayID = displayID
    }

    /// Returns false when the event tap can't be created (e.g. missing permission).
    func start() -> Bool {
        let thread = Thread { [self] in run() }
        thread.name = "dev.secondcursor.cursor-clamp"
        thread.start()
        started.wait()
        return tap != nil
    }

    func stop() {
        guard let runLoop else { return }
        // Queued rather than a direct CFRunLoopStop so it also works if the loop hasn't entered yet.
        CFRunLoopPerformBlock(runLoop, CFRunLoopMode.commonModes.rawValue) { CFRunLoopStop(CFRunLoopGetCurrent()) }
        CFRunLoopWakeUp(runLoop)
        finished.wait()
        self.runLoop = nil
    }

    private func run() {
        let types: [CGEventType] = [.mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged]
        let mask = types.reduce(CGEventMask(0)) { $0 | (1 << $1.rawValue) }
        tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap, place: .tailAppendEventTap, options: .listenOnly, eventsOfInterest: mask,
            callback: { _, type, event, info in
                Unmanaged<CursorClamp>.fromOpaque(info!).takeUnretainedValue().handle(type, event)
                return Unmanaged.passUnretained(event)
            },
            userInfo: Unmanaged.passUnretained(self).toOpaque()
        )
        guard let tap else {
            started.signal()
            return
        }
        runLoop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(runLoop, CFMachPortCreateRunLoopSource(nil, tap, 0), .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        started.signal()
        CFRunLoopRun()
        CGEvent.tapEnable(tap: tap, enable: false)
        CFMachPortInvalidate(tap)
        finished.signal()
    }

    private func handle(_ type: CGEventType, _ event: CGEvent) {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return
        }
        let bounds = CGDisplayBounds(displayID)
        // The event itself may be synthetic; only the real (HID) cursor position decides.
        guard bounds.contains(event.location),
              let cursor = CGEvent(source: nil)?.location, bounds.contains(cursor)
        else { return }
        CGWarpMouseCursorPosition(Self.nearestPoint(inside: CGDisplayBounds(CGMainDisplayID()), to: cursor))
        // Without this the pointer freezes for ~250 ms after a warp.
        CGAssociateMouseAndMouseCursorPosition(1)
    }

    static func nearestPoint(inside display: CGRect, to point: CGPoint) -> CGPoint {
        CGPoint(x: min(max(point.x, display.minX), display.maxX - 1), y: min(max(point.y, display.minY), display.maxY - 1))
    }
}
