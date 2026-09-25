import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit
import SCShared
import UniformTypeIdentifiers

enum Capture {
    /// Captures only the window's own pixels at one pixel per point, so screenshot pixels equal
    /// window-local points. Works for occluded windows and windows on other Spaces.
    static func window(_ wid: CGWindowID) async throws -> Screenshot {
        // Always attempt the ScreenCaptureKit call: it is what makes macOS list the app under
        // Screen Recording and show the prompt; a preflight-only check never registers the app.
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            if !CGPreflightScreenCaptureAccess() { throw SCError.permissionDenied("Screen Recording") }
            throw SCError.actionFailed("Could not list shareable windows: \(error.localizedDescription)")
        }
        guard let window = content.windows.first(where: { $0.windowID == wid }) else { throw SCError.windowNotFound(wid) }
        let width = max(1, Int(window.frame.width.rounded()))
        let height = max(1, Int(window.frame.height.rounded()))
        let configuration = SCStreamConfiguration()
        configuration.width = width
        configuration.height = height
        configuration.showsCursor = false
        configuration.ignoreShadowsSingleWindow = true
        let image: CGImage
        do {
            image = try await SCScreenshotManager.captureImage(
                contentFilter: SCContentFilter(desktopIndependentWindow: window), configuration: configuration
            )
        } catch {
            throw SCError.actionFailed("Could not capture window \(wid): \(error.localizedDescription)")
        }
        return Screenshot(pngData: try png(image), width: image.width, height: image.height, scale: Double(image.width) / Double(width))
    }

    static func png(_ image: CGImage) throws -> Data {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else {
            throw SCError.actionFailed("PNG encoder unavailable")
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { throw SCError.actionFailed("PNG encoding failed") }
        return data as Data
    }
}
