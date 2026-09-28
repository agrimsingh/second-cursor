import Foundation
import SCShared

extension ToolRouter {
    func runBrowser(_ name: String, _ args: Arguments) async throws -> ToolOutput {
        let observing = ToolCatalog.observingBrowserTools.contains(name)
        let mode = observing ? try Self.observeMode(args) : "none"
        if observing {
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
            return await observed(["tab": Shape.tab(tab)], tabID: try args.int("tab_id"), mode: mode)

        case "browser_snapshot":
            let params = try JSONSerialization.data(withJSONObject: [
                "tabId": args.int("tab_id"), "full": args.bool("full", default: false),
                "structured": args.bool("structured", default: false),
            ])
            var json = try JSONSerialization.jsonObject(with: await browser.command("tab.snapshot",params:params)) as? [String:Any] ?? [:]
            var images: [Screenshot] = []
            if try args.bool("include_screenshot",default:false) {
                let shot = try await browser.screenshot(tabID:args.int("tab_id")); images=[shot]; json["screenshot"]=Shape.screenshot(shot)
            }
            return ToolOutput(json:json,images:images)

        case "browser_click":
            var params: [String: Any] = ["tabId": try args.int("tab_id")]
            if let ref = try args.optionalString("ref") {
                params["ref"] = ref
            } else if let x = try args.optionalDouble("x"), let y = try args.optionalDouble("y") {
                params["x"] = x
                params["y"] = y
            } else {
                throw SCError.invalidArgument("Pass ref, or both x and y")
            }
            return await observed(try await reported("tab.click", params), tabID: try args.int("tab_id"), mode: mode)

        case "browser_type":
            var params: [String: Any] = [
                "tabId": try args.int("tab_id"), "text": try args.string("text"),
                "submit": try args.bool("submit", default: false),
            ]
            params["ref"] = try args.optionalString("ref")
            return await observed(try await reported("tab.type", params), tabID: try args.int("tab_id"), mode: mode)

        case "browser_press_key":
            let result = try await reported(
                "tab.pressKey", ["tabId": try args.int("tab_id"), "keys": try args.string("keys")])
            return await observed(result, tabID: try args.int("tab_id"), mode: mode)

        case "browser_scroll":
            let result = try await reported("tab.scroll", [
                "tabId": try args.int("tab_id"), "direction": try args.direction().rawValue,
                "amount": max(try args.int("amount", default: 3), 1),
            ])
            return await observed(result, tabID: try args.int("tab_id"), mode: mode)

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

    /// An action answers ok, plus the `[system]` lines the extension queued for the session since its last reply
    /// (a tab the page opened, a dialog, a download).
    private func reported(_ method: String, _ params: [String: Any]) async throws -> [String: Any] {
        let reply = try JSONSerialization.jsonObject(
            with: await browser.command(method, params: JSONSerialization.data(withJSONObject: params))) as? [String: Any]
        guard let events = reply?["events"] else { return Shape.ok }
        return Shape.ok.merging(["events": events]) { $1 }
    }

    /// Adds `observation` to a browser action's result, like attachObservation for native actions: the tab read once
    /// it settles, as a diff against this session's last read, which it replaces. The action already happened, so a
    /// failed read is reported as `observation_error` rather than thrown; with `both`, a failed capture keeps the tree.
    /// Events queued while the page settled come back on the read and join the action's.
    private func observed(_ result: [String: Any], tabID: Int, mode: String) async -> ToolOutput {
        var json = result
        do {
            switch mode {
            case "tree", "both":
                let params = try JSONSerialization.data(withJSONObject: ["tabId": tabID, "full": false])
                var observation = try JSONSerialization.jsonObject(
                    with: await browser.command("tab.snapshot", params: params)) as? [String: Any] ?? [:]
                if let events = observation.removeValue(forKey: "events") as? [Any] {
                    json["events"] = (json["events"] as? [Any] ?? []) + events
                }
                json["observation"] = observation
                guard mode == "both" else { return ToolOutput(json: json) }
                let shot = try await browser.screenshot(tabID: tabID)
                observation["screenshot"] = Shape.screenshot(shot)
                json["observation"] = observation
                return ToolOutput(json: json, images: [shot])
            case "screenshot":
                let shot = try await browser.screenshot(tabID: tabID)
                json["observation"] = Shape.screenshot(shot)
                return ToolOutput(json: json, images: [shot])
            default:
                return ToolOutput(json: json)
            }
        } catch {
            json["observation_error"] = Self.message(for: error)
            return ToolOutput(json: json)
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
