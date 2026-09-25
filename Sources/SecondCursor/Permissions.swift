import AppKit
import ApplicationServices
import CoreGraphics
import ScreenCaptureKit

enum Permissions {
    static var accessibility: Bool { AXIsProcessTrusted() }
    static var screenRecording: Bool { CGPreflightScreenCaptureAccess() }

    /// Shows the system prompts for whichever permissions are missing.
    static func requestMissing() {
        if !accessibility {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
        }
        if !screenRecording {
            _ = CGRequestScreenCaptureAccess()
            // CGRequestScreenCaptureAccess alone does not add the app to the Screen Recording list on recent macOS.
            Task { _ = try? await SCShareableContent.current }
        }
    }

    static func openSettings(accessibility: Bool) {
        let pane = accessibility ? "Privacy_Accessibility" : "Privacy_ScreenCapture"
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
            NSWorkspace.shared.open(url)
        }
    }
}
