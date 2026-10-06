import Foundation

/// A readiness hint, not an action-success test. Keep time and capture outside this policy so delayed
/// reactions, tiny changes and deadline handling can be exercised without a running desktop.
struct VisualSettlingPolicy {
    enum Decision: String { case waiting, quiet, timedOut }

    let minimumUntil: TimeInterval
    let deadline: TimeInterval
    let quietTime: TimeInterval
    let channelNoise: Int
    private var previousPixels: [UInt8]?
    private var previousIdentity: String?
    private var lastChange: TimeInterval?
    private(set) var samples = 0
    private(set) var changed = false

    init(minimumUntil: TimeInterval, deadline: TimeInterval, quietTime: TimeInterval = 0.18,
         channelNoise: Int = 8) {
        self.minimumUntil = minimumUntil
        self.deadline = deadline
        self.quietTime = quietTime
        self.channelNoise = channelNoise
    }

    mutating func sample(pixels: [UInt8], identity: String, at now: TimeInterval) -> Decision {
        if let previousPixels, let previousIdentity {
            // Do not discard a small changed region: a single surviving thumbnail pixel may be a
            // meaningful badge or checkbox. RGB also catches color changes with equal luminance.
            if identity != previousIdentity || Self.differs(previousPixels, pixels, noise: channelNoise) {
                lastChange = now
                changed = true
                self.previousPixels = pixels
                self.previousIdentity = identity
            }
        } else {
            lastChange = now
            previousPixels = pixels
            previousIdentity = identity
        }
        samples += 1
        return decision(at: now)
    }

    func decision(at now: TimeInterval) -> Decision {
        if now >= deadline { return .timedOut }
        if samples >= 2, let lastChange, now >= minimumUntil, now - lastChange >= quietTime {
            return .quiet
        }
        return .waiting
    }

    private static func differs(_ lhs: [UInt8], _ rhs: [UInt8], noise: Int) -> Bool {
        guard lhs.count == rhs.count else { return true }
        return zip(lhs, rhs).contains { abs(Int($0) - Int($1)) > noise }
    }
}
