// Fixed controls and a deliberately inaccessible drawing surface. Only a public
// AXPress starts a change; the state file is an independent, read-only receipt.
import AppKit

let args = CommandLine.arguments
func argument(_ key: String, _ fallback: String) -> String {
    guard let i = args.firstIndex(of: key), i + 1 < args.count else { return fallback }
    return args[i + 1]
}
let statePath = argument("--state", "/tmp/arc-settling-state.json")
let delay = Int(argument("--delay-ms", "800")) ?? 800
let small = args.contains("--small")
let blink = args.contains("--blink")
let wantedDisplay = UInt32(argument("--display-id", "0")) ?? 0

final class Trigger: NSTextField {
    var pressed: (() -> Void)?
    override func accessibilityRole() -> NSAccessibility.Role? { .button }
    override func accessibilityLabel() -> String? { "Run action" }
    override func accessibilityPerformPress() -> Bool { pressed?(); return true }
}

final class Canvas: NSView {
    var ready = false
    var caret = true
    var drawn: (() -> Void)?
    override func isAccessibilityElement() -> Bool { false }
    override func accessibilityChildren() -> [Any]? { [] }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.setFill(); bounds.fill()
        NSColor.darkGray.setFill()
        if caret { NSRect(x: 360, y: 40, width: 2, height: 20).fill() }
        (ready ? NSColor(deviceRed: 1, green: 0, blue: 1, alpha: 1) : NSColor.gray).setFill()
        NSRect(x: 60, y: 90, width: small ? 10 : 180, height: small ? 10 : 90).fill()
        if ready { drawn?() }
    }
}

final class Fixture: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let canvas = Canvas(frame: NSRect(x: 0, y: 0, width: 500, height: 220))
    var actions = 0
    var actionAt: Double?
    var drawAt: Double?
    var timer: Timer?
    var placementReady = false
    var placementAttempts = 0
    func state() {
        guard window != nil else { return }
        var value: [String: Any] = ["window": window.windowNumber, "pid": ProcessInfo.processInfo.processIdentifier,
                                   "actions": actions, "ready": canvas.ready, "delay_ms": delay,
                                   "small": small, "blink": blink, "placement_ready": placementReady,
                                   "display_id": wantedDisplay, "placement_attempts": placementAttempts]
        if let actionAt { value["action_at_uptime"] = actionAt }
        if let drawAt { value["draw_at_uptime"] = drawAt }
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
        }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        awaitDisplay(until: ProcessInfo.processInfo.systemUptime + 8)
    }
    func setupFailed(_ reason: String) {
        let value: [String: Any] = ["setup_error": reason, "display_id": wantedDisplay,
                                   "placement_attempts": placementAttempts]
        if let data = try? JSONSerialization.data(withJSONObject: value) {
            try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
        }
        NSApplication.shared.terminate(nil)
    }
    func awaitDisplay(until deadline: Double) {
        let display = NSScreen.screens.first { screen in
            let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
            return wantedDisplay == 0
                ? CGDisplayVendorNumber(id) == 0xA6E7 && CGDisplayModelNumber(id) == 0x0001
                : id == wantedDisplay
        }
        guard let display else {
            if ProcessInfo.processInfo.systemUptime >= deadline {
                setupFailed("The requested agent display never became available to AppKit")
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.awaitDisplay(until: deadline) }
            }
            return
        }
        createWindow(on: display)
    }
    func createWindow(on display: NSScreen) {
        let origin = NSPoint(x: display.frame.minX + 50, y: display.frame.maxY - 400)
        let content = NSRect(origin: origin, size: NSSize(width: 500, height: 280))
        window = NSWindow(contentRect: content, styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "SC Arc Settling"
        window.contentView?.addSubview(canvas)
        let trigger = Trigger(labelWithString: "Run action")
        trigger.frame = NSRect(x: 30, y: 232, width: 200, height: 30)
        trigger.pressed = { [weak self] in
            guard let self, self.placementReady else { return }
            self.actions += 1
            self.actionAt = ProcessInfo.processInfo.systemUptime
            self.state()
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(delay)) {
                self.canvas.ready = true
                self.canvas.needsDisplay = true
            }
        }
        canvas.drawn = { [weak self] in
            guard let self, self.drawAt == nil else { return }
            self.drawAt = ProcessInfo.processInfo.systemUptime
            self.state()
        }
        window.contentView?.addSubview(trigger)
        let target = window.frameRect(forContentRect: content)
        window.alphaValue = 0
        window.orderFront(nil)
        // WindowServer may relocate a new window while a virtual display is
        // registering. Do not publish readiness or reveal it until WindowServer
        // confirms its actual position, rather than AppKit's cached frame.
        placeWindow(at: target, until: ProcessInfo.processInfo.systemUptime + 8)
        if blink {
            timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                guard let self else { return }
                self.canvas.caret.toggle()
                self.canvas.needsDisplay = true
            }
        }
    }
    func placeWindow(at target: NSRect, until deadline: Double) {
        placementAttempts += 1
        let primaryHeight = NSScreen.screens.first?.frame.maxY ?? 0
        let targetQuartz = CGRect(x: target.minX, y: primaryHeight - target.maxY,
                                  width: target.width, height: target.height)
        let info = CGWindowListCopyWindowInfo(.optionIncludingWindow, CGWindowID(window.windowNumber)) as? [[String: Any]]
        let actual = (info?.first?[kCGWindowBounds as String] as? NSDictionary).flatMap {
            CGRect(dictionaryRepresentation: $0 as CFDictionary)
        }
        if let actual, abs(actual.minX - targetQuartz.minX) < 1,
           abs(actual.minY - targetQuartz.minY) < 1,
           abs(actual.width - targetQuartz.width) < 1, abs(actual.height - targetQuartz.height) < 1 {
            placementReady = true
            window.alphaValue = 1
            state()
            return
        }
        guard ProcessInfo.processInfo.systemUptime < deadline else {
            setupFailed("WindowServer did not place the fixture on the requested agent display")
            return
        }
        window.setFrameOrigin(NSPoint(x: target.minX + 1, y: target.minY))
        window.setFrame(target, display: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { self.placeWindow(at: target, until: deadline) }
    }
}
let app = NSApplication.shared
let fixture = Fixture()
app.setActivationPolicy(.accessory)
app.delegate = fixture
app.run()
