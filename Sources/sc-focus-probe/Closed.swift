// Closed-window mode for e2e: an app that keeps a window it no longer shows, as Electron apps do when their window is
// closed to the background, next to a window it does show.
//   usage: sc-focus-probe --closed --state <path> [--origin x,y]
// Writes {"pid","shown","closed"} (window numbers) once the closed window has been ordered out. Both open at --origin,
// else behind other windows; it never activates and stays out of the Dock.
import AppKit

final class Closed: NSObject, NSApplicationDelegate {
    private let statePath: String?
    private let origin: CGPoint?
    private var shown: NSWindow!
    private var closed: NSWindow!

    init(statePath: String?, origin: CGPoint?) {
        self.statePath = statePath
        self.origin = origin
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let rect = fixtureRect(NSSize(width: 360, height: 200), origin: origin)
        shown = NSWindow(contentRect: rect, styleMask: [.titled], backing: .buffered, defer: false)
        shown.title = "SC Shown Probe"
        orderIn(shown, content: rect, front: false)
        closed = NSWindow(contentRect: rect.offsetBy(dx: 40, dy: -40), styleMask: [.titled], backing: .buffered, defer: false)
        closed.title = "SC Closed Probe"
        closed.isReleasedWhenClosed = false
        orderIn(closed, content: rect.offsetBy(dx: 40, dy: -40), front: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [self] in
            closed.orderOut(nil)
            guard let statePath else { return }
            let state: [String: Any] = ["pid": Int(getpid()), "shown": shown.windowNumber, "closed": closed.windowNumber]
            guard let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]) else { return }
            try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
        }
    }
}
