import AppKit
import QuartzCore
import SCShared

/// A borderless, click-through overlay that draws the agent's own arrow cursor and glides it to each
/// action, so the user can watch the agent work. It never touches the real cursor; it only paints one.
final class AgentCursorOverlay: NSObject, AgentCursorPresenting, @unchecked Sendable {
    private let window: NSWindow
    private let arrow = CALayer()
    private let pulse = CAShapeLayer()
    private var lastPoint = CGPoint(x: -100, y: -100)
    private var hideWorkItem: DispatchWorkItem?

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

        arrow.contents = Self.arrowImage()
        arrow.frame = CGRect(x: 0, y: 0, width: 28, height: 28)
        arrow.anchorPoint = CGPoint(x: 3.0 / 28, y: 1 - 4.0 / 28) // the arrow's tip
        arrow.opacity = 0
        root.addSublayer(arrow)

        window.orderFrontRegardless()
    }

    func present(_ event: AgentCursorEvent) {
        let semaphore = DispatchSemaphore(value: 0)
        DispatchQueue.main.async { [self] in
            switch event {
            case let .click(at, count):
                glide(to: at) { self.flashPulse(count: count); semaphore.signal() }
            case let .drag(from, to):
                glide(to: from) {
                    self.dragLine(from: from, to: to) { semaphore.signal() }
                }
            case let .scroll(at):
                glide(to: at) { semaphore.signal() }
            }
        }
        // Bounded so a missed completion never stalls the action the cursor is announcing.
        _ = semaphore.wait(timeout: .now() + 1.2)
        scheduleHide()
    }

    // MARK: Animation (main thread)

    private func glide(to global: Point, then: @escaping () -> Void) {
        let target = layerPoint(global)
        let distance = hypot(target.x - lastPoint.x, target.y - lastPoint.y)
        let appearing = arrow.opacity < 0.5
        let duration = appearing ? 0.28 : min(0.3, max(0.08, distance / 3200))
        lastPoint = target

        CATransaction.begin()
        CATransaction.setCompletionBlock(then)
        CATransaction.setAnimationDuration(duration)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeInEaseOut))
        arrow.opacity = 1
        arrow.position = target
        pulse.position = target
        CATransaction.commit()
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

    private func dragLine(from: Point, to: Point, then: @escaping () -> Void) {
        let end = layerPoint(to)
        lastPoint = end
        CATransaction.begin()
        CATransaction.setCompletionBlock(then)
        CATransaction.setAnimationDuration(0.35)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeInEaseOut))
        arrow.position = end
        pulse.position = end
        CATransaction.commit()
    }

    private func scheduleHide() {
        DispatchQueue.main.async { [self] in
            hideWorkItem?.cancel()
            let work = DispatchWorkItem { [weak self] in
                CATransaction.begin()
                CATransaction.setAnimationDuration(0.4)
                self?.arrow.opacity = 0
                CATransaction.commit()
            }
            hideWorkItem = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
        }
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
