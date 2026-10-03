import AppKit
import QuartzCore
import SCCore
import SCShared

/// A borderless, click-through overlay that draws the agent's own cursor and moves it to each action, so the user
/// can watch the agent work. It never touches the real cursor; it only paints one. Between actions the cursor rests
/// where it last pointed for as long as the task is active: from any agent call until every active session's turn
/// has ended or it closed, or the user pauses. Registered task lifecycles outlive gaps between tool calls. The cursor belongs to its window:
/// the overlay sits directly above that window, so whatever covers the window covers the cursor, and it fades out
/// while the point is covered or the window is on another Space. The extension draws the same cursor inside agent
/// Chrome tabs (extension/cursor.js); keep the two designs in step.
final class AgentCursorOverlay: NSObject, AgentCursorPresenting, @unchecked Sendable {
    /// Compatibility fallback for direct engine callers that have no registered task. Registered tasks
    /// keep their presence while thinking, regardless of the interval between tool calls.
    static let idleTimeout: TimeInterval = 60

    /// Called on the main thread when activity starts or ends, or moves to another app.
    var onActivityChange: ((AgentActivityStatus) -> Void)?

    private let window: NSWindow
    private let root = CALayer()
    /// Fades the whole cursor while the user cannot see its point, independently of the task's own appear and hide.
    private let stage = CALayer()
    /// Sits exactly on the tip; everything below is positioned relative to it.
    private let cursor = CALayer()
    /// The arrow and its glow, rotated and pressed around the tip without moving the caption.
    private let pointer = CALayer()
    private let glow = CAShapeLayer()
    private let arrow = CAShapeLayer()
    private let breath = CAGradientLayer()
    private let pulse = CAShapeLayer()
    private let pill = CALayer()
    private let pillLabel = CATextLayer()

    // Main thread only.
    private var restingAt: Point?
    /// Bumped by every move and by hiding, so a finished animation only settles the move it started.
    private var generation = 0
    /// Releases the action waiting for the current move to arrive.
    private var pendingArrival: (() -> Void)?
    private var sessions: [String: Date] = [:]
    private var app: String?
    private var caption: String?
    private var typing: Timer?
    private var watchdog: Timer?
    /// The window the cursor sits above, its app, and whether the user can see the cursor's point.
    private var anchor: (window: UInt32, layer: Int, pid: Int32)?
    private var visible = false

    private let lock = NSLock()
    private var published = AgentActivityStatus()
    private var lastCall: Date?

    override init() {
        let frame = Self.screenFrame(containing: nil)
        window = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
        super.init()

        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]

        root.frame = CGRect(origin: .zero, size: frame.size)
        let host = NSView(frame: CGRect(origin: .zero, size: frame.size))
        host.wantsLayer = true
        host.layer = root
        window.contentView = host
        buildLayers()
        window.orderFrontRegardless()

        // Raising the target app, or switching Spaces, changes what the user can see before the next poll.
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didActivateApplicationNotification, NSWorkspace.activeSpaceDidChangeNotification] {
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.refreshAnchor() }
        }
        NotificationCenter.default.addObserver(
            forName: .scTaskRegistryDidChange, object: nil, queue: .main
        ) { [weak self] _ in self?.checkLiveness() }
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.screensChanged() }
    }

    func present(_ event: AgentCursorEvent) {
        switch event {
        case let .activity(session, app, near, caption):
            DispatchQueue.main.async { self.noteActivity(session: session, app: app, near: near, caption: caption) }
        case let .end(session):
            DispatchQueue.main.async { self.end([session]) }
        case let .target(window, layer, pid, visible):
            DispatchQueue.main.async { self.setAnchor((window, layer, pid), visible: visible) }
        case .click, .drag, .scroll:
            let arrived = DispatchSemaphore(value: 0)
            DispatchQueue.main.async { self.point(event) { arrived.signal() } }
            // Bounded so a busy main thread never stalls the action the cursor is announcing.
            _ = arrived.wait(timeout: .now() + Self.arrivalTimeout)
        }
    }

    /// Like Codex: the action waits until the user can see the cursor land, but never longer than this.
    static let arrivalTimeout: TimeInterval = 0.4
    /// How long any move or appearance takes to reach its target, whatever the distance: long enough to follow by
    /// eye, short enough that the action waiting on it barely slows. extension/cursor.js uses the same.
    private static let glide: TimeInterval = 0.15

    func activity() -> AgentActivityStatus {
        lock.lock()
        defer { lock.unlock() }
        var status = published
        status.idleSeconds = status.active ? lastCall.map { Date().timeIntervalSince($0) } : nil
        return status
    }

    // MARK: Lifecycle (main thread)

    private func noteActivity(session: String, app: String?, near: Point?, caption: String?) {
        sessions[session] = Date()
        if let app { self.app = app }
        if let caption { setCaption(caption.isEmpty ? nil : caption) }
        if let near, restingAt == nil { move(to: near, arrived: {}) { self.rest() } }
        watch()
        publish()
    }

    /// The cursor goes once no session is left; the sessions that remain keep it.
    private func end(_ ended: [String]) {
        for session in ended { sessions[session] = nil }
        if sessions.isEmpty { hide() } else { publish() }
    }

    private func hide() {
        sessions.removeAll()
        app = nil
        watchdog?.invalidate()
        watchdog = nil
        setCaption(nil)
        releaseArrival()
        anchor = nil
        if restingAt != nil {
            restingAt = nil
            generation += 1
            stopResting()
            CATransaction.begin()
            CATransaction.setAnimationDuration(0.3)
            cursor.opacity = 0
            cursor.transform = CATransform3DMakeScale(0.85, 0.85, 1)
            CATransaction.commit()
        }
        publish()
    }

    /// Global pause and anchor visibility are polled; task control changes also arrive through the registry.
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
        let stale = sessions.filter { session, lastCall in
            if let task = TaskRegistry.shared.snapshot(session: session) {
                return !task.status.permitsMutation
            }
            return lastCall < cutoff
        }.map(\.key)
        if !stale.isEmpty || sessions.isEmpty { end(stale) }
        refreshAnchor()
    }

    /// Windows move, get covered, and change Spaces without telling us, so the anchor is checked again while resting.
    private func refreshAnchor() {
        guard let restingAt, let anchor else { return }
        if let found = Desktop.cursorAnchor(at: restingAt, pid: anchor.pid) {
            setAnchor((found.window, found.layer, anchor.pid), visible: found.visible)
        } else {
            setAnchor(anchor, visible: false)
        }
    }

    /// Orders the overlay directly above the anchor window, at its layer, so windows in front of that window are in
    /// front of the cursor too; an app's own menus are anchors themselves and stay beneath it.
    private func setAnchor(_ new: (window: UInt32, layer: Int, pid: Int32), visible: Bool) {
        anchor = new
        if visible {
            window.level = NSWindow.Level(rawValue: new.layer)
            window.order(.above, relativeTo: Int(new.window))
        }
        guard visible != self.visible else { return }
        self.visible = visible
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.15)
        stage.opacity = visible ? 1 : 0
        CATransaction.commit()
        publish()
    }

    private func publish() {
        let active = !sessions.isEmpty
        let status = AgentActivityStatus(
            active: active, app: active ? app : nil, sessions: sessions.keys.sorted(), arrow: restingAt,
            arrowVisible: restingAt != nil && visible, caption: active ? caption : nil)
        lock.lock()
        let changed = published.active != status.active || published.app != status.app
        published = status
        lastCall = sessions.values.max()
        lock.unlock()
        if changed { onActivityChange?(status) }
    }

    // MARK: Motion (main thread)

    private var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    /// `arrived` releases the action this event announces once the cursor reaches its target.
    private func point(_ event: AgentCursorEvent, arrived: @escaping () -> Void) {
        switch event {
        case let .click(at, count):
            move(to: at, arrived: arrived) {
                self.press(count: count)
                self.rest()
            }
        case let .drag(from, to):
            move(to: from, arrived: arrived) { self.slide(to: to) }
        case let .scroll(at):
            move(to: at, arrived: arrived) { self.rest() }
        case .activity, .end, .target:
            arrived()
        }
        watch()
        publish()
    }

    /// Short moves spring straight to the target; long ones travel an arc that bows away from the straight line,
    /// leaning toward the direction of travel. A cursor that is not showing appears at the target. Arrival is when
    /// the cursor reaches the target, not when its spring stops settling. A cursor the user cannot see jumps there
    /// and arrives at once, so it is in place when its window comes back into view.
    private func move(to global: Point, arrived: @escaping () -> Void, then landed: @escaping () -> Void) {
        let changedDisplay = fit(to: global)
        let target = layerPoint(global)
        generation += 1
        let move = generation
        releaseArrival()
        stopResting()
        let appearing = restingAt == nil || changedDisplay
        let start = cursor.presentation()?.position ?? cursor.position
        restingAt = global

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        cursor.position = target
        placePill(at: target)
        if !visible {
            cursor.removeAnimation(forKey: "move")
            cursor.opacity = 1
            cursor.transform = CATransform3DIdentity
        }
        CATransaction.commit()

        guard visible else {
            if appearing, let caption { showPill(caption) }
            arrived()
            landed()
            return
        }
        pendingArrival = arrived
        let arrival: TimeInterval
        if appearing {
            appear()
            if let caption { showPill(caption) }
            arrival = Self.glide
        } else {
            arrival = travel(from: start, to: target)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + arrival) {
            guard move == self.generation else { return }
            self.releaseArrival()
            landed()
        }
    }

    /// An action still waiting on a move that a newer move or a hide replaced goes at once.
    private func releaseArrival() {
        pendingArrival?()
        pendingArrival = nil
    }

    private func appear() {
        cursor.opacity = 1
        cursor.transform = CATransform3DIdentity
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0
        fade.toValue = 1
        fade.duration = Self.glide
        cursor.add(fade, forKey: "appear-fade")
        guard !reduceMotion else { return }
        let grow = Self.spring("transform.scale", response: 0.42, damping: 0.86)
        grow.fromValue = 0.6
        grow.toValue = 1
        cursor.add(grow, forKey: "appear-scale")
    }

    /// Returns when the cursor reaches the target.
    private func travel(from start: CGPoint, to target: CGPoint) -> TimeInterval {
        let dx = target.x - start.x, dy = target.y - start.y
        let distance = hypot(dx, dy)
        guard distance >= 0.5 else { return 0 }
        if reduceMotion {
            let slide = CABasicAnimation(keyPath: "position")
            slide.fromValue = NSValue(point: start)
            slide.toValue = NSValue(point: target)
            slide.duration = 0.12
            cursor.add(slide, forKey: "move")
            return slide.duration
        }
        let duration: CFTimeInterval
        let arrival: TimeInterval
        if distance < 200 {
            let spring = Self.spring("position", response: Self.glide, damping: 0.86)
            spring.fromValue = NSValue(point: start)
            spring.toValue = NSValue(point: target)
            duration = spring.duration
            // An underdamped spring first reaches its target after about one response period.
            arrival = Self.glide
            cursor.add(spring, forKey: "move")
        } else {
            let arc = CAKeyframeAnimation(keyPath: "position")
            let path = CGMutablePath()
            path.move(to: start)
            path.addQuadCurve(to: target, control: arcControl(from: start, to: target))
            arc.path = path
            arc.calculationMode = .paced
            arc.duration = Self.glide
            arc.timingFunction = CAMediaTimingFunction(controlPoints: 0.3, 0.9, 0.35, 1)
            duration = arc.duration
            arrival = arc.duration
            cursor.add(arc, forKey: "move")
        }
        // Clockwise (negative in this y-up space) when moving right, as if the tip were pulled along.
        let lean = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        lean.values = [0, -(dx / distance) * 10 * .pi / 180, 0]
        lean.keyTimes = [0, 0.35, 1]
        lean.duration = duration
        lean.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        pointer.add(lean, forKey: "lean")
        return arrival
    }

    /// The control point sits a quarter of the distance off the midpoint, on whichever side stays on screen,
    /// preferring the side that bows upward.
    private func arcControl(from start: CGPoint, to target: CGPoint) -> CGPoint {
        let distance = hypot(target.x - start.x, target.y - start.y)
        let mid = CGPoint(x: (start.x + target.x) / 2, y: (start.y + target.y) / 2)
        let normal = CGPoint(x: -(target.y - start.y) / distance, y: (target.x - start.x) / distance)
        let sides = [1.0, -1.0].map { side in
            CGPoint(x: mid.x + normal.x * distance * 0.25 * side, y: mid.y + normal.y * distance * 0.25 * side)
        }.sorted { $0.y > $1.y }
        return sides.first { root.bounds.insetBy(dx: 8, dy: 8).contains($0) } ?? mid
    }

    /// The drag's second leg runs alongside the real drag, which is already moving by the time the cursor lands.
    private func slide(to global: Point) {
        let end = layerPoint(global)
        let start = cursor.presentation()?.position ?? cursor.position
        generation += 1
        let move = generation
        restingAt = global
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        cursor.position = end
        placePill(at: end)
        CATransaction.commit()

        let slide = CABasicAnimation(keyPath: "position")
        slide.fromValue = NSValue(point: start)
        slide.toValue = NSValue(point: end)
        slide.duration = reduceMotion ? 0.12 : 0.25
        slide.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        CATransaction.begin()
        CATransaction.setCompletionBlock { if move == self.generation { self.rest() } }
        cursor.add(slide, forKey: "move")
        CATransaction.commit()
        publish()
    }

    private func press(count: Int) {
        let ring = CAAnimationGroup()
        let scale = CABasicAnimation(keyPath: "transform.scale")
        scale.fromValue = 0.4
        scale.toValue = 1.0
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.9
        fade.toValue = 0.0
        ring.animations = [scale, fade]
        ring.duration = count > 1 ? 0.42 : 0.32
        ring.timingFunction = CAMediaTimingFunction(name: .easeOut)
        pulse.add(ring, forKey: "pulse")
        guard !reduceMotion else { return }
        let dip = CAKeyframeAnimation(keyPath: "transform.scale")
        dip.values = [1, 0.88, 1]
        dip.keyTimes = [0, 0.35, 1]
        dip.duration = 0.18
        pointer.add(dip, forKey: "dip")
    }

    /// The idle pose between actions: a slow glow around the tip and, after a moment, a small wobble every few
    /// seconds, so a task that is still running looks alive. A still cursor under Reduce Motion.
    private func rest() {
        guard restingAt != nil, !reduceMotion else { return }
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.15
        fade.toValue = 0.35
        let grow = CABasicAnimation(keyPath: "transform.scale")
        grow.fromValue = 0.8
        grow.toValue = 1.0
        let breathe = CAAnimationGroup()
        breathe.animations = [fade, grow]
        breathe.duration = 1.4
        breathe.autoreverses = true
        breathe.repeatCount = .infinity
        breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        breath.add(breathe, forKey: "breathe")

        let wobble = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        wobble.values = [0, -9, 7, -5, 3, 0, 0].map { $0 * Double.pi / 180 }
        wobble.keyTimes = [0, 0.08, 0.17, 0.26, 0.33, 0.39, 1]
        wobble.duration = 3.6
        wobble.repeatCount = .infinity
        wobble.beginTime = CACurrentMediaTime() + 1.2
        pointer.add(wobble, forKey: "wobble")
    }

    private func stopResting() {
        breath.removeAnimation(forKey: "breathe")
        pointer.removeAnimation(forKey: "wobble")
    }

    private static func spring(_ keyPath: String, response: Double, damping: Double) -> CASpringAnimation {
        let spring = CASpringAnimation(keyPath: keyPath)
        let stiffness = pow(2 * .pi / response, 2)
        spring.mass = 1
        spring.stiffness = stiffness
        spring.damping = 2 * damping * stiffness.squareRoot()
        spring.duration = spring.settlingDuration
        return spring
    }

    // MARK: Caption (main thread)

    private func setCaption(_ text: String?) {
        guard text != caption else { return }
        caption = text
        typing?.invalidate()
        typing = nil
        if let text {
            if restingAt != nil { showPill(text) }
        } else {
            CATransaction.begin()
            CATransaction.setAnimationDuration(0.15)
            pill.opacity = 0
            CATransaction.commit()
        }
        publish()
    }

    /// Types the caption out, at most 0.4 s, with a caret while it runs. The pill takes its final width at once so
    /// it does not grow while typing.
    private func showPill(_ text: String) {
        let size = (text as NSString).size(withAttributes: [.font: Self.captionFont])
        let width = min(320, ceil(size.width) + 18)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        pill.bounds = CGRect(x: 0, y: 0, width: width, height: 26)
        pillLabel.frame = CGRect(x: 9, y: 5, width: width - 18, height: 16)
        placePill(at: cursor.position)
        CATransaction.commit()

        CATransaction.begin()
        CATransaction.setAnimationDuration(0.12)
        pill.opacity = 1
        CATransaction.commit()

        let characters = Array(text)
        guard !reduceMotion, characters.count > 1 else {
            setLabel(text)
            return
        }
        let started = CACurrentMediaTime()
        let total = min(0.4, 0.018 * Double(characters.count))
        setLabel("▏")
        let timer = Timer(timeInterval: 0.018, repeats: true) { [weak self] timer in
            guard let self else { return timer.invalidate() }
            let progress = (CACurrentMediaTime() - started) / total
            let shown = min(characters.count, Int((Double(characters.count) * progress).rounded(.up)))
            if shown >= characters.count {
                self.setLabel(text)
                timer.invalidate()
            } else {
                self.setLabel(String(characters.prefix(shown)) + "▏")
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        typing = timer
    }

    private func setLabel(_ text: String) {
        pillLabel.string = NSAttributedString(
            string: text, attributes: [.font: Self.captionFont, .foregroundColor: Self.captionText])
    }

    /// Below and to the right of the tip, flipped left or above near the screen's edges.
    private func placePill(at tip: CGPoint) {
        let size = pill.bounds.size
        var origin = CGPoint(x: 14, y: -20 - size.height)
        if tip.x + origin.x + size.width > root.bounds.maxX - 4 { origin.x = -14 - size.width }
        if tip.y + origin.y < root.bounds.minY + 4 { origin.y = 20 }
        pill.frame = CGRect(origin: origin, size: size)
    }

    // MARK: Layers

    private func buildLayers() {
        let scale = NSScreen.screens.map(\.backingScaleFactor).max() ?? 2
        stage.frame = root.bounds
        stage.opacity = 0
        root.addSublayer(stage)
        cursor.bounds = .zero
        cursor.opacity = 0
        stage.addSublayer(cursor)

        breath.type = .radial
        breath.colors = [Self.accent.withAlphaComponent(0.5).cgColor, Self.accent.withAlphaComponent(0).cgColor]
        breath.startPoint = CGPoint(x: 0.5, y: 0.5)
        breath.endPoint = CGPoint(x: 1, y: 1)
        // Centered on the arrow's body, 8 pt right of and below the tip.
        breath.frame = CGRect(x: -8, y: -24, width: 32, height: 32)
        breath.opacity = 0
        cursor.addSublayer(breath)

        pulse.path = CGPath(ellipseIn: CGRect(x: -22, y: -22, width: 44, height: 44), transform: nil)
        pulse.fillColor = NSColor.clear.cgColor
        pulse.strokeColor = Self.accent.cgColor
        pulse.lineWidth = 2.5
        pulse.opacity = 0
        cursor.addSublayer(pulse)

        // 24 pt box, tip at (3, 3) from the top left; this layer is y-up, so the tip is at (3, 21).
        pointer.bounds = CGRect(x: 0, y: 0, width: 24, height: 24)
        pointer.anchorPoint = CGPoint(x: 3.0 / 24, y: 21.0 / 24)
        pointer.position = .zero
        cursor.addSublayer(pointer)
        let path = Self.arrowPath()
        for (layer, radius, opacity) in [(glow, 9.0, Float(0.45)), (arrow, 3.5, Float(0.85))] {
            layer.frame = pointer.bounds
            layer.path = path
            layer.fillColor = Self.ink.cgColor
            layer.strokeColor = NSColor.white.cgColor
            layer.lineWidth = 1.5
            layer.lineJoin = .round
            layer.shadowPath = path
            layer.shadowColor = Self.accent.cgColor
            layer.shadowRadius = radius
            layer.shadowOpacity = opacity
            layer.shadowOffset = .zero
            pointer.addSublayer(layer)
        }

        pill.anchorPoint = .zero
        pill.backgroundColor = Self.ink.withAlphaComponent(0.94).cgColor
        pill.cornerRadius = 8
        pill.borderWidth = 1
        pill.borderColor = NSColor.white.withAlphaComponent(0.08).cgColor
        pill.shadowColor = NSColor.black.cgColor
        pill.shadowOpacity = 0.25
        pill.shadowRadius = 5
        pill.shadowOffset = CGSize(width: 0, height: -2)
        pill.opacity = 0
        pillLabel.contentsScale = scale
        pillLabel.truncationMode = .end
        pillLabel.isWrapped = false
        pillLabel.actions = ["contents": NSNull(), "string": NSNull(), "bounds": NSNull(), "position": NSNull()]
        pill.addSublayer(pillLabel)
        cursor.addSublayer(pill)
    }

    /// An arrowhead without a tail, pointing up and to the left at 45 degrees.
    private static func arrowPath() -> CGPath {
        let path = CGMutablePath()
        path.move(to: CGPoint(x: 3, y: 21))
        path.addLine(to: CGPoint(x: 21, y: 13.8))
        path.addLine(to: CGPoint(x: 12.6, y: 11.4))
        path.addLine(to: CGPoint(x: 10.2, y: 3))
        path.closeSubpath()
        return path
    }

    // MARK: Geometry

    /// Quartz global point (top-left origin) to a point in the overlay layer (bottom-left origin).
    private func layerPoint(_ global: Point) -> CGPoint {
        let primaryTop = NSScreen.screens.first?.frame.maxY ?? 0
        let cocoa = CGPoint(x: global.x, y: primaryTop - global.y)
        return CGPoint(x: cocoa.x - window.frame.minX, y: cocoa.y - window.frame.minY)
    }

    /// With "Displays have separate Spaces" (the macOS default) a window draws on one display only, so the overlay
    /// covers the display the cursor is on rather than the whole desktop. Returns whether the overlay moved.
    @discardableResult
    private func fit(to global: Point?) -> Bool {
        let frame = Self.screenFrame(containing: global)
        guard frame != window.frame else { return false }
        window.setFrame(frame, display: false)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        root.frame = CGRect(origin: .zero, size: frame.size)
        stage.frame = root.bounds
        CATransaction.commit()
        return true
    }

    /// A display was added (a monitor, the agent display), removed, or rearranged.
    private func screensChanged() {
        guard fit(to: restingAt), let restingAt else { return }
        let point = layerPoint(restingAt)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        cursor.removeAnimation(forKey: "move")
        cursor.position = point
        placePill(at: point)
        CATransaction.commit()
    }

    /// The Cocoa frame of the screen holding `global` (Quartz, top-left origin), else of the primary screen.
    private static func screenFrame(containing global: Point?) -> CGRect {
        let screens = NSScreen.screens
        let primaryTop = screens.first?.frame.maxY ?? 0
        let holder = global.flatMap { point in
            screens.first { screen in
                let frame = screen.frame
                return CGRect(x: frame.minX, y: primaryTop - frame.maxY, width: frame.width, height: frame.height)
                    .contains(CGPoint(x: point.x, y: point.y))
            }
        }
        return (holder ?? screens.first)?.frame ?? .zero
    }

    private static let accent = NSColor(srgbRed: 245 / 255, green: 78 / 255, blue: 0, alpha: 1) // Cursor brand accent
    private static let ink = NSColor(srgbRed: 20 / 255, green: 18 / 255, blue: 11 / 255, alpha: 1)
    private static let captionText = NSColor(srgbRed: 237 / 255, green: 236 / 255, blue: 236 / 255, alpha: 1)
    private static let captionFont = NSFont.systemFont(ofSize: 12, weight: .medium)
}
