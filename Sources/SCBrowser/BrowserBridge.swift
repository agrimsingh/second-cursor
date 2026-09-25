import Foundation
import SCShared

/// Drives Chrome through the Second Cursor extension, reached via sc-native-host on a Unix socket.
public final class BrowserBridge: BrowserControlling, @unchecked Sendable {
    public static let socketURL = SCPaths.supportDir.appendingPathComponent("browser.sock")

    private let link = ExtensionLink(socketURL: BrowserBridge.socketURL)

    public init() {}

    /// Listens for sc-native-host. Throws if another process already serves the socket.
    public func start() throws {
        try link.start()
    }

    public func instances() async -> Data { link.instances() }

    public func command(_ method: String, params: Data) async throws -> Data {
        let object = try JSONSerialization.jsonObject(with:params) as? [String:Any] ?? [:]
        if method == "session.close" { await link.closeSession(object); return Data("{}".utf8) }
        return try JSONSerialization.data(withJSONObject: await link.call(method,object))
    }

    public func status() async -> BrowserStatus {
        link.status
    }

    public func listTabs() async throws -> [BrowserTab] {
        let result = try await link.call("tabs.list", [:])
        return try (result["tabs"] as? [[String: Any]] ?? []).map(Self.tab)
    }

    public func openTab(url: String) async throws -> BrowserTab {
        let result = try await link.call("tabs.open", ["url": url])
        return try Self.tab(result["tab"])
    }

    public func closeTab(tabID: Int) async throws {
        _ = try await link.call("tabs.close", ["tabId": tabID])
    }

    public func navigate(tabID: Int, url: String) async throws -> BrowserTab {
        let result = try await link.call("tab.navigate", ["tabId": tabID, "url": url])
        return try Self.tab(result["tab"])
    }

    public func snapshot(tabID: Int, includeScreenshot: Bool) async throws -> BrowserSnapshot {
        let result = try await link.call("tab.snapshot", ["tabId": tabID])
        guard let outline = result["outline"] as? String else {
            throw SCError.actionFailed("Extension returned a snapshot without an outline")
        }
        let shot = includeScreenshot ? try await screenshot(tabID: tabID) : nil
        return BrowserSnapshot(tab: try Self.tab(result["tab"]), outline: outline, screenshot: shot)
    }

    public func click(tabID: Int, target: BrowserTarget) async throws {
        var params: [String: Any] = ["tabId": tabID]
        switch target {
        case .ref(let ref):
            params["ref"] = ref
        case .point(let x, let y):
            params["x"] = x
            params["y"] = y
        }
        _ = try await link.call("tab.click", params)
    }

    public func type(tabID: Int, ref: String?, text: String, submit: Bool) async throws {
        var params: [String: Any] = ["tabId": tabID, "text": text, "submit": submit]
        params["ref"] = ref
        _ = try await link.call("tab.type", params)
    }

    public func pressKey(tabID: Int, keys: String) async throws {
        _ = try await link.call("tab.pressKey", ["tabId": tabID, "keys": keys])
    }

    public func scroll(tabID: Int, direction: ScrollDirection, amount: Int) async throws {
        _ = try await link.call("tab.scroll", ["tabId": tabID, "direction": direction.rawValue, "amount": amount])
    }

    public func screenshot(tabID: Int) async throws -> Screenshot {
        let result = try await link.call("tab.screenshot", ["tabId": tabID])
        guard let base64 = result["data"] as? String, let png = Data(base64Encoded: base64) else {
            throw SCError.actionFailed("Extension returned a screenshot without PNG data")
        }
        return Screenshot(
            pngData: png,
            width: (result["width"] as? NSNumber)?.intValue ?? 0,
            height: (result["height"] as? NSNumber)?.intValue ?? 0,
            scale: (result["scale"] as? NSNumber)?.doubleValue ?? 1
        )
    }

    private static func tab(_ value: Any?) throws -> BrowserTab {
        guard let object = value as? [String: Any], let id = (object["id"] as? NSNumber)?.intValue else {
            throw SCError.actionFailed("Extension returned a malformed tab")
        }
        return BrowserTab(
            id: id,
            url: object["url"] as? String ?? "",
            title: object["title"] as? String ?? "",
            isAgentTab: object["isAgentTab"] as? Bool ?? true
        )
    }
}
