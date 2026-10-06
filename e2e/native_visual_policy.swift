import Foundation

@main struct VisualPolicyChecks {
    static func main() {
        var checks = 0
        func check(_ condition: @autoclosure () -> Bool, _ message: String) {
            precondition(condition(), message)
            checks += 1
        }
        let initial: [UInt8] = [120, 120, 120, 255, 120, 120, 120, 255]
        let update: [UInt8] = [120, 120, 120, 255, 120, 160, 120, 255]
        for delay in [0.0, 0.3, 0.8, 1.5] {
            var policy = VisualSettlingPolicy(minimumUntil: 1.6, deadline: 2.2)
            var ended: Double?
            for tick in 0...44 {
                let now = Double(tick) * 0.05
                let decision = policy.sample(pixels: now >= delay ? update : initial, identity: "window", at: now)
                if decision == .quiet { ended = now; break }
                check(decision != .timedOut, "Delayed update should settle within the budget")
            }
            check(ended != nil && ended! >= max(1.6, delay + 0.18), "Wait for delayed paint and subsequent quiet")
        }

        var tiny = VisualSettlingPolicy(minimumUntil: 0, deadline: 2.2)
        _ = tiny.sample(pixels: [UInt8](repeating: 120, count: 192 * 128 * 4), identity: "window", at: 0)
        var badge = [UInt8](repeating: 120, count: 192 * 128 * 4)
        badge[99] = 150
        check(tiny.sample(pixels: badge, identity: "window", at: 0.2) == .waiting, "One changed channel must reset quiet")
        check(tiny.changed, "Do not discard a small meaningful region")
        check(tiny.sample(pixels: badge, identity: "window", at: 0.4) == .quiet, "A static badge should then settle")

        var color = VisualSettlingPolicy(minimumUntil: 0, deadline: 2.2)
        _ = color.sample(pixels: [255, 0, 0, 255], identity: "window", at: 0)
        check(color.sample(pixels: [0, 130, 0, 255], identity: "window", at: 0.2) == .waiting,
              "Equal-luminance colors must still count as changes")

        var drift = VisualSettlingPolicy(minimumUntil: 1.6, deadline: 2.2)
        _ = drift.sample(pixels: [120], identity: "window", at: 0)
        for tick in 1...9 { _ = drift.sample(pixels: [UInt8(120 + tick)], identity: "window", at: Double(tick) * 0.05) }
        check(drift.changed, "Slow cumulative drift must be compared against the last meaningful frame")

        var noise = VisualSettlingPolicy(minimumUntil: 0, deadline: 2.2)
        _ = noise.sample(pixels: [120], identity: "window", at: 0)
        check(noise.sample(pixels: [127], identity: "window", at: 0.2) == .quiet, "Small capture noise does not reset quiet")
        check(!noise.changed, "Noise is not reported as meaningful change")

        var animation = VisualSettlingPolicy(minimumUntil: 1.6, deadline: 2.2)
        for tick in 0...21 {
            check(animation.sample(pixels: tick % 2 == 0 ? initial : update, identity: "window", at: Double(tick) * 0.1) == .waiting,
                  "Continuous animation cannot count as quiet")
        }
        check(animation.decision(at: 2.2) == .timedOut, "Continuous animation must have a bounded wait")

        var caret = VisualSettlingPolicy(minimumUntil: 1.6, deadline: 2.2)
        var caretQuiet: Double?
        for tick in 0...22 {
            let now = Double(tick) * 0.1
            let decision = caret.sample(pixels: (tick / 5) % 2 == 0 ? initial : update, identity: "window", at: now)
            if decision == .quiet { caretQuiet = now; break }
        }
        check(caretQuiet != nil && caretQuiet! < 2.2, "Ordinary caret blinking allows a quiet interval without filtering out badges")

        var identity = VisualSettlingPolicy(minimumUntil: 0, deadline: 2.2)
        _ = identity.sample(pixels: initial, identity: "window:ax0", at: 0)
        check(identity.sample(pixels: initial, identity: "window:ax1", at: 0.2) == .waiting, "AX/window identity changes reset quiet")
        check(identity.sample(pixels: initial, identity: "window:ax1", at: 0.4) == .quiet, "Unchanged identity can become quiet")

        var deadline = VisualSettlingPolicy(minimumUntil: 1.6, deadline: 0.1)
        check(deadline.decision(at: 0.1) == .timedOut, "Exhausted call budget must stop before another capture")
        _ = deadline.sample(pixels: initial, identity: "window", at: 0)
        check(deadline.sample(pixels: initial, identity: "window", at: 0.2) == .timedOut, "Late captures cannot manufacture quiet after deadline")
        print("PASS: \(checks) native visual-settling checks")
    }
}
