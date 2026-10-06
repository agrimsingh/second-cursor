import Foundation
import MCP
import SCShared

extension ToolRouter {
    func runBrowser(_ name: String, _ args: Arguments) async throws -> ToolOutput {
        let expectation = try Self.browserExpectation(args)
        if name == "browser_command", let raw = args.raw["arguments"],
           let nested = Arguments.nestedObject(raw), !(nested["expect"]?.isNull ?? true) {
            throw SCError.invalidArgument("browser_command does not support expect; use browser_click, browser_type, browser_press_key, browser_scroll or batch with tab_id")
        }
        if name == "browser_command", try args.optionalString("operation") == "batch",
           try args.objects("steps").contains(where: { !($0["expect"]?.isNull ?? true) }) {
            throw SCError.invalidArgument("browser_command batch does not support expect; use batch with tab_id and a top-level expect")
        }
        if expectation != nil && !ToolCatalog.expectingBrowserTools.contains(name) {
            throw SCError.invalidArgument("expect is supported by browser_click, browser_type, browser_press_key, browser_scroll and batch with tab_id; not \(name)")
        }
        if expectation != nil { try await requireBrowserExpectationSupport() }
        let observing = ToolCatalog.observingBrowserTools.contains(name)
        let mode = observing ? try Self.observeMode(args) : "none"
        if observing {
            await showCaption(args)
        }
        switch name {
        case "browser_repl":
            return try await repl.run(
                code: args.string("code"), session: Execution.context.sessionID,
                deadline: Execution.context.deadline ?? Date().addingTimeInterval(60))
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
            var json = try JSONSerialization.jsonObject(with:data) as? [String:Any] ?? [:]
            if var observation = json["observation"] as? [String: Any], let tab = (json["tab"] as? [String: Any])?["id"] as? Int {
                condense(&observation, tabID: tab)
                json["observation"] = observation
            }
            return ToolOutput(json: json)

        case "browser_navigate":
            let tab = try await browser.navigate(tabID: try args.int("tab_id"), url: try args.string("url"))
            return await observed([:], base: ["tab": Shape.tab(tab)], tabID: try args.int("tab_id"), mode: mode)

        case "browser_snapshot":
            if let expanded = try expandedParts(args, target: "tab:\(try args.int("tab_id"))", noun: "tab") {
                return ToolOutput(json: ["outline": expanded.text, "expanded": expanded.ids, "is_diff": false])
            }
            let params = try JSONSerialization.data(withJSONObject: [
                "tabId": args.int("tab_id"), "full": args.bool("full", default: false),
                "structured": args.bool("structured", default: false),
            ])
            var json = try JSONSerialization.jsonObject(with: await browser.command("tab.snapshot",params:params)) as? [String:Any] ?? [:]
            condense(&json, tabID: try args.int("tab_id"))
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
            return await observed(try await acted("tab.click", params, mode, expectation: expectation), tabID: try args.int("tab_id"), mode: mode)

        case "browser_type":
            var params: [String: Any] = [
                "tabId": try args.int("tab_id"), "text": try args.string("text"),
                "submit": try args.bool("submit", default: false),
            ]
            params["ref"] = try args.optionalString("ref")
            return await observed(try await acted("tab.type", params, mode, expectation: expectation), tabID: try args.int("tab_id"), mode: mode)

        case "browser_press_key":
            let reply = try await acted(
                "tab.pressKey", ["tabId": try args.int("tab_id"), "keys": try args.string("keys")], mode,
                expectation: expectation)
            return await observed(reply, tabID: try args.int("tab_id"), mode: mode)

        case "browser_scroll":
            let reply = try await acted("tab.scroll", [
                "tabId": try args.int("tab_id"), "direction": try args.direction().rawValue,
                "amount": max(try args.int("amount", default: 3), 1),
            ], mode, expectation: expectation)
            return await observed(reply, tabID: try args.int("tab_id"), mode: mode)

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

    /// Step names a tab batch takes, with the native names models carry over, and the extension method each sends.
    private static let tabSteps: [String: (tool: String, method: String)] = [
        "browser_click": ("browser_click", "tab.click"), "click": ("browser_click", "tab.click"),
        "browser_type": ("browser_type", "tab.type"), "type_text": ("browser_type", "tab.type"),
        "browser_press_key": ("browser_press_key", "tab.pressKey"), "press_key": ("browser_press_key", "tab.pressKey"),
        "browser_scroll": ("browser_scroll", "tab.scroll"), "scroll": ("browser_scroll", "tab.scroll"),
        "wait": ("wait", "wait"),
    ]

    private struct TabStep {
        let tool: String
        let method: String
        let params: [String: Any]
        let waitMS: Int
        let logDetail: String?
        let caption: String?
    }

    static func isTabBatch(_ name: String, _ args: Arguments) -> Bool {
        name == "batch" && !(args.raw["tab_id"]?.isNull ?? true)
    }

    /// `batch` with tab_id: every step is parsed before any runs, then each is sent as its own action without a read
    /// (one that navigates still waits for the load), and the tab is read once after the last. Each step passes the
    /// pause gate and gets its own action log line, like a native batch's.
    func runTabBatch(_ args: Arguments) async throws -> ToolOutput {
        let tabID = try args.int("tab_id")
        guard args.raw["window_id"]?.isNull ?? true else {
            throw SCError.invalidArgument("Pass tab_id or window_id, not both: a batch acts in one tab or one window")
        }
        let expectation = try Self.browserExpectation(args)
        let rawSteps = try args.objects("steps")
        guard !rawSteps.isEmpty else { throw SCError.invalidArgument("steps must contain at least one step") }
        guard rawSteps.count <= 50 else { throw SCError.invalidArgument("A batch holds at most 50 steps") }
        let steps = try rawSteps.enumerated().map { try Self.tabStep($0.element, index: $0.offset, tabID: tabID) }
        let mode = try Self.observeMode(args)
        let stopOnError = try args.bool("stop_on_error", default: true)
        let full = try args.bool("full", default: false)
        let expectToken: String?
        if let expectation {
            let prepared = try await browserReply("tab.expectPrepare", ["tabId": tabID, "expect": expectation])
            guard let token = prepared["expect_token"] as? String, !token.isEmpty else {
                throw SCError.invalidArgument("Browser extension did not prepare expect; update the extension before retrying")
            }
            expectToken = token
        } else {
            expectToken = nil
        }
        await showCaption(args)

        var results: [[String: Any]] = []
        var events: [Any] = []
        var stopped = false
        for (index, step) in steps.enumerated() {
            var result: [String: Any] = ["index": index, "tool": step.tool]
            guard !stopped else {
                result["ok"] = false
                result["skipped"] = true
                results.append(result)
                continue
            }
            do {
                if PauseState.isPaused {
                    log.record(ActionLogEntry(
                        tool: step.tool, target: "tab_id=\(tabID)", detail: "refused: \(Self.message(for: SCError.paused))"))
                    throw SCError.paused
                }
                log.record(ActionLogEntry(tool: step.tool, target: "tab_id=\(tabID)", detail: step.logDetail))
                if let caption = step.caption { await showCaption(caption, tabID: tabID) }
                if step.method == "wait" {
                    try await Task.sleep(nanoseconds: UInt64(step.waitMS) * 1_000_000)
                    result["ok"] = true
                } else {
                    let reply = try await acted(step.method, step.params, "none")
                    events += reply["events"] as? [Any] ?? []
                    if let outcome = reply["outcome"] { result["outcome"] = outcome }
                    if let uncertain = reply["uncertain"] { result["uncertain"] = uncertain }
                    let state = (reply["outcome"] as? [String: Any])?["state"] as? String
                    let failed = reply["ok"] as? Bool == false || reply["uncertain"] as? Bool == true
                        || state == "failed" || state == "uncertain"
                    result["ok"] = !failed
                    if failed {
                        result["error"] = reply["error"] ?? "The browser step failed or its delivery is uncertain; inspect the target before retrying"
                        stopped = stopOnError
                    }
                }
            } catch {
                result["ok"] = false
                result["error"] = Self.message(for: error)
                stopped = stopOnError
            }
            results.append(result)
        }
        var base: [String: Any] = [
            "steps": results, "completed": results.filter { $0["ok"] as? Bool == true }.count,
            "stopped_early": results.contains { $0["skipped"] != nil }, "tab_id": tabID,
        ]
        if mode == "none" { base["observation"] = NSNull() }
        var reply: [String: Any] = [:]
        if let expectToken {
            let complete = results.allSatisfy { $0["ok"] as? Bool == true }
            do {
                reply = try await browserReply("tab.expectFinish", [
                    "tabId": tabID, "expect_token": expectToken,
                    "complete": complete, "observe": mode, "full": full,
                ])
                if !complete {
                    // Preserve this invariant even when talking to a mismatched extension version.
                    reply["expect"] = ["status": "failed", "waited_ms": 0, "reason": "batch_incomplete",
                                       "detail": "At least one batch step failed or was skipped; reconcile before retrying"]
                    reply.removeValue(forKey: "outcome")
                }
            } catch {
                // Some steps may already have committed. A failed check cannot turn their delivery into a tool
                // error that tempts clients to repeat the batch; keep the step ledger and failed postcondition.
                reply["expect"] = ["status": "failed", "waited_ms": 0, "reason": "check_unavailable",
                                   "detail": Self.message(for: error)]
                // Do not try the same broken connection again merely to decorate this known partial result.
                reply["observation_error"] = Self.message(for: error)
            }
        }
        events += reply["events"] as? [Any] ?? []
        if !events.isEmpty { reply["events"] = events }
        return await observed(reply, base: base, tabID: tabID, mode: mode, full: full)
    }

    private static func tabStep(_ raw: [String: Value], index: Int, tabID: Int) throws -> TabStep {
        let named = try Arguments(raw).string("tool").lowercased()
        guard let (tool, method) = tabSteps[named] else {
            throw SCError.invalidArgument(
                "Step \(index): with tab_id, tool must be one of browser_click, browser_type, browser_press_key, "
                    + "browser_scroll, wait")
        }
        let args = Arguments(try stepArguments(raw, index: index, tool: named))
        guard args.raw["expect"]?.isNull ?? true else {
            throw SCError.invalidArgument("Step \(index) (\(tool)): put expect on the batch, not individual steps")
        }
        guard args.raw["element_index"] == nil, args.raw["window_id"] == nil else {
            throw SCError.invalidArgument(
                "Step \(index) (\(tool)): a tab batch targets elements by ref from the tab's outline, not element_index")
        }
        func required(_ key: String) throws -> String {
            guard let value = try args.optionalString(key) else {
                throw SCError.invalidArgument("Step \(index) (\(tool)): \(key) is required")
            }
            return value
        }
        var params: [String: Any] = ["tabId": tabID]
        var waitMS = 0
        switch tool {
        case "browser_click":
            if let ref = try args.optionalString("ref") {
                params["ref"] = ref
            } else if let x = try args.optionalDouble("x"), let y = try args.optionalDouble("y") {
                params["x"] = x
                params["y"] = y
            } else {
                throw SCError.invalidArgument("Step \(index) (browser_click): pass ref, or both x and y")
            }
        case "browser_type":
            params["text"] = try required("text")
            params["submit"] = try args.bool("submit", default: false)
            params["ref"] = try args.optionalString("ref")
        case "browser_press_key":
            params["keys"] = try required("keys")
        case "browser_scroll":
            params["direction"] = try args.direction().rawValue
            params["amount"] = max(try args.int("amount", default: 3), 1)
        default:
            guard let ms = try args.optionalInt("ms") ?? args.optionalInt("milliseconds") else {
                throw SCError.invalidArgument("Step \(index) (wait): ms is required")
            }
            waitMS = max(ms, 0)
        }
        return TabStep(
            tool: tool, method: method, params: params, waitMS: waitMS, logDetail: args.logDetail,
            caption: try args.optionalString("caption"))
    }

    /// Sends an input action with the observation the call wants, which the extension reads in the same message once
    /// the page settles.
    private func acted(
        _ method: String, _ params: [String: Any], _ mode: String, expectation: [String: Any]? = nil
    ) async throws -> [String: Any] {
        var params = params
        params["observe"] = mode
        if let expectation { params["expect"] = expectation }
        return try await browserReply(method, params)
    }

    private func browserReply(_ method: String, _ params: [String: Any]) async throws -> [String: Any] {
        try JSONSerialization.jsonObject(
            with: await browser.command(method, params: JSONSerialization.data(withJSONObject: params))) as? [String: Any]
            ?? [:]
    }

    /// Captions are separate browser calls that count as dispatch in the operation journal. Refuse an old loaded
    /// extension before even that call; ExtensionLink also gates the actual input against its selected connection.
    private func requireBrowserExpectationSupport() async throws {
        let data = await browser.instances()
        let instances = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["browsers"] as? [[String: Any]] ?? []
        let selected: [String: Any]?
        if let id = Execution.context.browserID {
            selected = instances.first { $0["id"] as? String == id }
        } else {
            selected = instances.count == 1 ? instances.first : nil
        }
        guard let selected, (selected["capabilities"] as? [String] ?? []).contains("browser_expect_v1") else {
            throw SCError.browserUnavailable("the selected extension does not support browser expect; reload the updated Second Cursor extension before retrying")
        }
    }

    /// Validate the entire browser condition before dispatch. Keep these scalar types strict: coercing a malformed
    /// value or dropping a misspelled key would silently weaken the requested proof. Native conditions are separate.
    static func browserExpectation(_ args: Arguments) throws -> [String: Any]? {
        guard let value = args.raw["expect"], !value.isNull else { return nil }
        guard let object = Arguments.nestedObject(value) else {
            throw SCError.invalidArgument("expect must be an object or JSON object string, e.g. {\"text\":\"Saved\"}")
        }
        let unknown = object.keys.filter { !["text", "ref", "value", "gone", "timeout_ms"].contains($0) }.sorted()
        guard unknown.isEmpty else {
            throw SCError.invalidArgument("expect: unrecognized fields \(unknown.joined(separator: ", ")); use text, ref with value, gone and timeout_ms")
        }
        var result: [String: Any] = [:]
        for key in ["text", "ref", "value"] {
            guard let value = object[key] else { continue }
            guard case .string(let text) = value else {
                throw SCError.invalidArgument("expect.\(key) must be a string")
            }
            if key != "value" && text.isEmpty {
                throw SCError.invalidArgument("expect.\(key) must not be empty")
            }
            result[key] = text
        }
        guard result["text"] != nil || result["ref"] != nil || result["value"] != nil else {
            throw SCError.invalidArgument("expect: pass text, or ref with value")
        }
        guard (result["ref"] == nil) == (result["value"] == nil) else {
            throw SCError.invalidArgument("expect: ref and value go together; value must match exactly")
        }
        if let gone = object["gone"] {
            guard case .bool(let bool) = gone else { throw SCError.invalidArgument("expect.gone must be true or false") }
            result["gone"] = bool
        }
        if let timeout = object["timeout_ms"] {
            guard let ms = Int(timeout, strict: true) else {
                throw SCError.invalidArgument("expect.timeout_ms must be an integer")
            }
            guard (0...10_000).contains(ms) else {
                throw SCError.invalidArgument("expect.timeout_ms must be between 0 and 10000")
            }
            result["timeout_ms"] = ms
        }
        return result
    }

    /// An action answers ok, plus the `[system]` lines the extension queued for the session since its last reply
    /// (a tab the page opened, a dialog, a download), plus `observation`, like attachObservation for native actions:
    /// the tab read once it settles, as a diff against this session's last read, which it replaces. The action already
    /// happened, so a failed read is reported as `observation_error` rather than thrown; with `both`, a failed capture
    /// keeps the tree. An extension that did not read in the action's message (one from before observe, or an action a
    /// dialog interrupted) is asked for the read separately.
    private func observed(
        _ reply: [String: Any], base: [String: Any] = Shape.ok, tabID: Int, mode: String, full: Bool = false
    ) async -> ToolOutput {
        var json = base
        if let events = reply["events"] { json["events"] = events }
        if let outcome = reply["outcome"] { json["outcome"] = outcome }
        if let settling = reply["settling"] { json["settling"] = settling }
        if let expectation = reply["expect"] { json["expect"] = expectation }
        guard mode != "none" else { return ToolOutput(json: json) }
        let shot = Self.screenshot(reply["screenshot"])
        if reply["observation"] != nil || shot != nil || reply["observation_error"] != nil {
            var images: [Screenshot] = []
            if mode == "screenshot" {
                if let shot { json["observation"] = Shape.screenshot(shot) }
            } else if var observation = reply["observation"] as? [String: Any] {
                if mode == "both", let shot { observation["screenshot"] = Shape.screenshot(shot) }
                json["observation"] = observation
            }
            if let shot, mode != "tree" { images = [shot] }
            if let error = reply["observation_error"] { json["observation_error"] = error }
            return ToolOutput(json: json, images: images)
        }
        do {
            switch mode {
            case "tree", "both":
                let params = try JSONSerialization.data(withJSONObject: ["tabId": tabID, "full": full])
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

    /// Only reads are condensed: an action's observation shows what the action revealed, which can sit in a part the
    /// rules would hide (a menu opened far down a list, on a page that re-renders whole).
    private func condense(_ read: inout [String: Any], tabID: Int) {
        guard let hiddenParts, read["is_diff"] as? Bool != true, let outline = read["outline"] as? String else { return }
        read["outline"] = hiddenParts.condense(outline, style: .browser, target: "tab:\(tabID)", tool: "browser_snapshot")
    }

    private static func screenshot(_ value: Any?) -> Screenshot? {
        guard let object = value as? [String: Any], let encoded = object["data"] as? String,
              let png = Data(base64Encoded: encoded) else { return nil }
        return Screenshot(
            pngData: png, width: (object["width"] as? NSNumber)?.intValue ?? 0,
            height: (object["height"] as? NSNumber)?.intValue ?? 0, scale: (object["scale"] as? NSNumber)?.doubleValue ?? 1)
    }

    /// The extension draws the caption beside its in-page cursor (browser_command carries its own). Best effort:
    /// an extension that predates captions refuses the method, and the action still runs.
    private func showCaption(_ args: Arguments) async {
        guard let tab = try? args.int("tab_id") else { return }
        await showCaption((try? args.optionalString("caption")).flatMap { $0 }, tabID: tab)
    }

    private func showCaption(_ caption: String?, tabID: Int) async {
        guard let params = try? JSONSerialization.data(
            withJSONObject: ["tabId": tabID, "caption": caption.map(Self.caption) ?? ""])
        else { return }
        _ = try? await browser.command("cursor.caption", params: params)
    }
}
