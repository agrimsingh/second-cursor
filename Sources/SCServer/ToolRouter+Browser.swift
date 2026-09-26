import Foundation
import SCShared

extension ToolRouter {
    func runBrowser(_ name: String, _ args: Arguments) async throws -> ToolOutput {
        if ["browser_click", "browser_type", "browser_press_key", "browser_scroll", "browser_navigate"].contains(name) {
            await showCaption(args)
        }
        switch name {
        case "browser_instances":
            return ToolOutput(json: (try? JSONSerialization.jsonObject(with: await browser.instances())) as? [String:Any] ?? [:])
        case "browser_command":
            var params = args.raw.mapValues { value -> Any in
                // Use MCP's Codable representation to preserve nested structured arguments.
                (try? JSONSerialization.jsonObject(with: JSONEncoder().encode(value), options:.fragmentsAllowed)) ?? NSNull()
            }
            if let tid = try args.optionalInt("tab_id") { params["tabId"] = tid }
            if let caption = try args.optionalString("caption") { params["caption"] = Self.caption(caption) }
            if try args.optionalString("operation") == "upload" {
                for path in try args.strings("files") {
                    var directory: ObjCBool = false
                    guard path.hasPrefix("/"), FileManager.default.fileExists(atPath:path,isDirectory:&directory), !directory.boolValue else {
                        throw SCError.invalidArgument("Upload file does not exist or is not a regular file: \(path)")
                    }
                }
            }
            let data = try await browser.command("extended",params: JSONSerialization.data(withJSONObject:params))
            var json = try JSONSerialization.jsonObject(with:data) as? [String:Any] ?? [:]
            var images: [Screenshot] = []
            if let encoded = json.removeValue(forKey:"data") as? String, let png = Data(base64Encoded:encoded) {
                images = [Screenshot(pngData:png,width:json["width"] as? Int ?? 0,height:json["height"] as? Int ?? 0,scale:json["scale"] as? Double ?? 1)]
            }
            return ToolOutput(json:json,images:images)
        case "browser_status":
            return ToolOutput(json: Shape.browser(await browser.status()))

        case "browser_tabs":
            return ToolOutput(json: ["tabs": try await browser.listTabs().map(Shape.tab)])

        case "browser_open":
            let data=try await browser.command("tabs.open",params:JSONSerialization.data(withJSONObject:["url":args.string("url")]))
            return ToolOutput(json:try JSONSerialization.jsonObject(with:data) as? [String:Any] ?? [:])

        case "browser_navigate":
            let tab = try await browser.navigate(tabID: try args.int("tab_id"), url: try args.string("url"))
            return ToolOutput(json: ["tab": Shape.tab(tab)])

        case "browser_snapshot":
            let params = try JSONSerialization.data(withJSONObject: [
                "tabId": args.int("tab_id"), "full": args.bool("full", default: true),
                "structured": args.bool("structured", default: false),
            ])
            var json = try JSONSerialization.jsonObject(with: await browser.command("tab.snapshot",params:params)) as? [String:Any] ?? [:]
            var images: [Screenshot] = []
            if try args.bool("include_screenshot",default:false) {
                let shot = try await browser.screenshot(tabID:args.int("tab_id")); images=[shot]; json["screenshot"]=Shape.screenshot(shot)
            }
            return ToolOutput(json:json,images:images)

        case "browser_click":
            let target: BrowserTarget
            if let ref = try args.optionalString("ref") {
                target = .ref(ref)
            } else if let x = try args.optionalDouble("x"), let y = try args.optionalDouble("y") {
                target = .point(x: x, y: y)
            } else {
                throw SCError.invalidArgument("Pass ref, or both x and y")
            }
            try await browser.click(tabID: try args.int("tab_id"), target: target)
            return ToolOutput(json: Shape.ok)

        case "browser_type":
            try await browser.type(
                tabID: try args.int("tab_id"), ref: try args.optionalString("ref"),
                text: try args.string("text"), submit: try args.bool("submit", default: false))
            return ToolOutput(json: Shape.ok)

        case "browser_press_key":
            try await browser.pressKey(tabID: try args.int("tab_id"), keys: try args.string("keys"))
            return ToolOutput(json: Shape.ok)

        case "browser_scroll":
            try await browser.scroll(
                tabID: try args.int("tab_id"), direction: try args.direction(),
                amount: max(try args.int("amount", default: 3), 1))
            return ToolOutput(json: Shape.ok)

        case "browser_screenshot":
            let shot = try await browser.screenshot(tabID: try args.int("tab_id"))
            return ToolOutput(json: Shape.screenshot(shot), images: [shot])

        case "browser_close":
            let params = try JSONSerialization.data(withJSONObject: ["tabId": try args.int("tab_id")])
            let reply = try JSONSerialization.jsonObject(with: await browser.command("tabs.close", params: params))
                as? [String: Any] ?? [:]
            return ToolOutput(json: Shape.ok.merging(reply) { $1 })

        default:
            throw SCError.invalidArgument("Unknown tool \"\(name)\"")
        }
    }

    /// The extension draws the caption beside its in-page cursor (browser_command carries its own). Best effort:
    /// an extension that predates captions refuses the method, and the action still runs.
    private func showCaption(_ args: Arguments) async {
        let caption = (try? args.optionalString("caption")).flatMap { $0 }.map(Self.caption) ?? ""
        guard let tab = try? args.int("tab_id"),
              let params = try? JSONSerialization.data(withJSONObject: ["tabId": tab, "caption": caption])
        else { return }
        _ = try? await browser.command("cursor.caption", params: params)
    }
}
