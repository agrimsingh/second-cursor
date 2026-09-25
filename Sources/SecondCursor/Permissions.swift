import AppKit
import ApplicationServices
import CoreGraphics

/// Granting happens in System Settings, guided by `OnboardingPanel`; the system prompts are not used because
/// they compete with the panel and do not help the user find the list.
enum Permissions {
    static var accessibility: Bool { AXIsProcessTrusted() }
    static var screenRecording: Bool { CGPreflightScreenCaptureAccess() }

    static func openSettings(accessibility: Bool) {
        let pane = accessibility ? "Privacy_Accessibility" : "Privacy_ScreenCapture"
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
            NSWorkspace.shared.open(url)
        }
    }
}
