import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit
import SCShared
import UniformTypeIdentifiers

enum Capture {
    /// Clients shrink larger images before a model sees them (Cursor to about 1024 px wide), and a model reading
    /// positions off a shrunk image would click off by the shrink factor.
    static let maxEdge = 1024

    /// Captures only the window's own pixels at one pixel per point, scaled down (never up) so the long edge is at
    /// most `maxEdge`. `scale` is image pixels per window point. Works for occluded windows and windows on other Spaces.
    static func window(_ wid: CGWindowID) async throws -> Screenshot {
        let (image, points) = try await image(wid, maxEdge: maxEdge)
        return Screenshot(pngData: try Timing.measure("capture_encode") { try png(image) }, width: image.width, height: image.height,
                          scale: Double(image.width) / Double(points.width))
    }

    /// The window's own pixels and its size in points. `maxEdge` nil keeps the display's backing resolution, for
    /// text recognition, which misreads small text on a scaled-down image.
    static func image(_ wid: CGWindowID, maxEdge: Int?) async throws -> (CGImage, CGSize) {
        // Always attempt the ScreenCaptureKit call: it is what makes macOS list the app under
        // Screen Recording and show the prompt; a preflight-only check never registers the app.
        let content: SCShareableContent
        do {
            content = try await Timing.measureAsync("capture_list") {
                try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            }
        } catch {
            if !CGPreflightScreenCaptureAccess() { throw SCError.permissionDenied("Screen Recording") }
            throw SCError.actionFailed("Could not list shareable windows: \(error.localizedDescription)")
        }
        guard let window = content.windows.first(where: { $0.windowID == wid }) else { throw SCError.windowNotFound(wid) }
        let points = CGSize(width: max(1, window.frame.width.rounded()), height: max(1, window.frame.height.rounded()))
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let configuration = SCStreamConfiguration()
        if let maxEdge {
            let factor = min(1, CGFloat(maxEdge) / max(points.width, points.height))
            configuration.width = min(maxEdge, max(1, Int((points.width * factor).rounded())))
            configuration.height = min(maxEdge, max(1, Int((points.height * factor).rounded())))
        } else {
            let scale = CGFloat(max(1, filter.pointPixelScale))
            configuration.width = max(1, Int((points.width * scale).rounded()))
            configuration.height = max(1, Int((points.height * scale).rounded()))
        }
        configuration.showsCursor = false
        configuration.ignoreShadowsSingleWindow = true
        do {
            let image = try await Timing.measureAsync("capture_shot") {
                try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
            }
            return (image, points)
        } catch {
            throw SCError.actionFailed("Could not capture window \(wid): \(error.localizedDescription)")
        }
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
