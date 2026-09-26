import AppKit
import QuartzCore
import SCShared

/// A borderless, click-through overlay that draws the agent's own arrow cursor and glides it to each
/// action, so the user can watch the agent work. It never touches the real cursor; it only paints one.
/// Between actions the arrow rests where it last pointed for as long as the task is active: from any agent
/// call until every active session's turn has ended or it closed, the user pauses, or no call came for
/// `idleTimeout`.
final class AgentCursorOverlay: NSObject, AgentCursorPresenting, @unchecked Sendable {
    /// For hosts without a turn-end hook, which never say that a task is over.
    static let idleTimeout: TimeInterval = 60

    /// Called on the main thread when activity starts or ends, or moves to another app.
    var onActivityChange: ((AgentActivityStatus) -> Void)?

    private let window: NSWindow
    private let arrow = CALayer()
    private let pulse = CAShapeLayer()
    private let halo = CAShapeLayer()

    // Main thread only.
    private var lastPoint = CGPoint(x: -100, y: -100)
    private var restingAt: Point?
    /// Bumped by every move and by hiding, so a finished animation only settles the arrow it started.
    private var generation = 0
    private var sessions: [String: Date] = [:]
    private var app: String?
    private var watchdog: Timer?

    private let lock = NSLock()
    private var published = AgentActivityStatus()
    private var lastCall: Date?

    override init() {
        let frame = Self.desktopFrame()
        window = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
        super.init()

        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.ignoresMouseEvents = true
        window.level = NSWindow.Level(rawValue: Int(CGShieldingWindowLevel()))
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]

        let root = CALayer()
        root.frame = CGRect(origin: .zero, size: frame.size)
        let host = NSView(frame: CGRect(origin: .zero, size: frame.size))
        host.wantsLayer = true
        host.layer = root
        window.contentView = host

        pulse.path = CGPath(ellipseIn: CGRect(x: -22, y: -22, width: 44, height: 44), transform: nil)
        pulse.fillColor = NSColor.clear.cgColor
        pulse.strokeColor = Self.accent.cgColor
        pulse.lineWidth = 2.5
        pulse.opacity = 0
        root.addSublayer(pulse)

        halo.path = CGPath(ellipseIn: CGRect(x: -16, y: -16, width: 32, height: 32), transform: nil)
        halo.fillColor = Self.accent.cgColor
        halo.opacity = 0
        root.addSublayer(halo)

        arrow.contents = Self.arrowImage()
        arrow.frame = CGRect(x: 0, y: 0, width: 28, height: 28)
        arrow.anchorPoint = CGPoint(x: 3.0 / 28, y: 1 - 4.0 / 28) // the arrow's tip
        arrow.opacity = 0
        root.addSublayer(arrow)

        window.orderFrontRegardless()
    }

    func present(_ event: AgentCursorEvent) {
        switch event {
        case let .activity(session, app, near):
            DispatchQueue.main.async { self.noteActivity(session: session, app: app, near: near) }
        case let .end(session):
            DispatchQueue.main.async { self.end([session]) }
        case .click, .drag, .scroll:
            let arrived = DispatchSemaphore(value: 0)
            DispatchQueue.main.async { self.point(event) { arrived.signal() } }
            // Bounded so a busy main thread never stalls the action the cursor is announcing.
            _ = arrived.wait(timeout: .now() + 0.25)
        }
    }

    func activity() -> AgentActivityStatus {
        lock.lock()
        defer { lock.unlock() }
        var status = published
        status.idleSeconds = status.active ? lastCall.map { Date().timeIntervalSince($0) } : nil
        return status
    }

    // MARK: Lifecycle (main thread)

    private func noteActivity(session: String, app: String?, near: Point?) {
        sessions[session] = Date()
        if let app { self.app = app }
        if let near, restingAt == nil { glide(to: near, rest: true) {} }
        watch()
        publish()
    }

    /// The arrow goes once no session is left; the sessions that remain keep it.
    private func end(_ ended: [String]) {
        for session in ended { sessions[session] = nil }
        if sessions.isEmpty { hide() } else { publish() }
    }

    private func hide() {
        sessions.removeAll()
        app = nil
        watchdog?.invalidate()
        watchdog = nil
        if restingAt != nil {
            restingAt = nil
            generation += 1
            halo.removeAnimation(forKey: "breathe")
            CATransaction.begin()
            CATransaction.setAnimationDuration(0.4)
            arrow.opacity = 0
            CATransaction.commit()
        }
        publish()
    }

    /// Pausing and the idle timeout have no event of their own, so they are polled while anything is showing.
    private func watch() {
        guard watchdog == nil else { return }
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in self?.checkLiveness() }
        RunLoop.main.add(timer, forMode: .common)
        watchdog = timer
    }

    private func checkLiveness() {
        if PauseState.isPaused {
            hide()
            return
        }
        let cutoff = Date().addingTimeInterval(-Self.idleTimeout)
        let stale = sessions.filter { $0.value < cutoff }.map(\.key)
        if !stale.isEmpty || sessions.isEmpty { end(stale) }
    }

    private func publish() {
        let status = AgentActivityStatus(
            active: !sessions.isEmpty, app: sessions.isEmpty ? nil : app, sessions: sessions.keys.sorted(),
            arrow: restingAt)
        lock.lock()
        let changed = published.active != status.active || published.app != status.app
        published = status
        lastCall = sessions.values.max()
        lock.unlock()
        if changed { onActivityChange?(status) }
    }

    // MARK: Animation (main thread)

    private func point(_ event: AgentCursorEvent, arrived: @escaping () -> Void) {
        switch event {
        case let .click(at, count):
            glide(to: at, rest: true) { self.flashPulse(count: count); arrived() }
        case let .drag(from, to):
            glide(to: from, rest: false) { self.dragLine(to: to); arrived() }
        case let .scroll(at):
            glide(to: at, rest: true, then: arrived)
        case .activity, .end:
            arrived()
        }
        watch()
        publish()
    }

    /// At most ~0.15 s (0.18 s when appearing): the action waits for the arrow to arrive.
    private func glide(to global: Point, rest: Bool, then: @escaping () -> Void) {
        let target = layerPoint(global)
        let appearing = restingAt == nil
        let distance = hypot(target.x - lastPoint.x, target.y - lastPoint.y)
        let duration = appearing ? 0.18 : min(0.15, max(0.06, distance / 4000))
        generation += 1
        let move = generation
        halo.removeAnimation(forKey: "breathe")
        if appearing {
            // Fly in from just below the target instead of from wherever the arrow was last hidden.
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            arrow.position = CGPoint(x: target.x + 14, y: target.y - 18)
            CATransaction.commit()
        }
        lastPoint = target
        restingAt = global

        CATransaction.begin()
        CATransaction.setCompletionBlock {
            if rest, move == self.generation { self.rest() }
            then()
        }
        CATransaction.setAnimationDuration(duration)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))
        arrow.opacity = 1
        arrow.position = target
        CATransaction.commit()

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        pulse.position = target
        halo.position = target
        CATransaction.commit()
    }

    /// The idle pose between actions: a slow, faint glow around the tip, or a still arrow under Reduce Motion.
    private func rest() {
        guard restingAt != nil, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.0
        fade.toValue = 0.28
        let grow = CABasicAnimation(keyPath: "transform.scale")
        grow.fromValue = 0.6
        grow.toValue = 1.0
        let breathe = CAAnimationGroup()
        breathe.animations = [fade, grow]
        breathe.duration = 1.4
        breathe.autoreverses = true
        breathe.repeatCount = .infinity
        breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        halo.add(breathe, forKey: "breathe")
    }

    private func flashPulse(count: Int) {
        pulse.position = arrow.position
        let scale = CABasicAnimation(keyPath: "transform.scale")
        scale.fromValue = 0.4
        scale.toValue = 1.0
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.9
        fade.toValue = 0.0
        let group = CAAnimationGroup()
        group.animations = [scale, fade]
        group.duration = count > 1 ? 0.42 : 0.32
        group.timingFunction = CAMediaTimingFunction(name: .easeOut)
        pulse.add(group, forKey: "pulse")
    }

    /// Runs alongside the real drag instead of before it: Pointer.drag presses ~0.2 s after the arrow
    /// arrives and then moves for ~0.2 s.
    private func dragLine(to global: Point) {
        let start = arrow.position
        let end = layerPoint(global)
        generation += 1
        let move = generation
        lastPoint = end
        restingAt = global

        let slide = CABasicAnimation(keyPath: "position")
        slide.fromValue = NSValue(point: start)
        slide.toValue = NSValue(point: end)
        slide.beginTime = CACurrentMediaTime() + 0.2
        slide.duration = 0.2
        slide.fillMode = .backwards
        slide.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        CATransaction.setCompletionBlock { if move == self.generation { self.rest() } }
        arrow.position = end
        pulse.position = end
        halo.position = end
        arrow.add(slide, forKey: "drag")
        CATransaction.commit()
        publish()
    }

    // MARK: Geometry

    /// Quartz global point (top-left origin) to a point in the overlay layer (bottom-left origin).
    private func layerPoint(_ global: Point) -> CGPoint {
        let primaryTop = NSScreen.screens.first?.frame.maxY ?? 0
        let cocoa = CGPoint(x: global.x, y: primaryTop - global.y)
        return CGPoint(x: cocoa.x - window.frame.minX, y: cocoa.y - window.frame.minY)
    }

    private static func desktopFrame() -> CGRect {
        NSScreen.screens.reduce(CGRect.null) { $0.union($1.frame) }
    }

    private static let accent = NSColor(red: 0.96, green: 0.31, blue: 0.0, alpha: 1) // Cursor brand accent

    private static func arrowImage() -> CGImage {
        let size = CGSize(width: 28, height: 28)
        // The path is in top-left-origin coordinates (tip at 3,4).
        let image = NSImage(size: size, flipped: true) { _ in
            let path = NSBezierPath()
            path.move(to: NSPoint(x: 3, y: 25))
            path.line(to: NSPoint(x: 3, y: 4))
            path.line(to: NSPoint(x: 18, y: 12))
            path.line(to: NSPoint(x: 11, y: 12))
            path.line(to: NSPoint(x: 16, y: 22))
            path.line(to: NSPoint(x: 12, y: 24))
            path.line(to: NSPoint(x: 8, y: 15))
            path.close()
            accent.setFill()
            path.fill()
            NSColor.white.setStroke()
            path.lineWidth = 1.5
            path.stroke()
            return true
        }
        var rect = CGRect(origin: .zero, size: size)
        return image.cgImage(forProposedRect: &rect, context: nil, hints: nil)!
    }
}
