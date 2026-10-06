// Compile the production browser router against a deterministic transport; this probe never starts an app or browser.
import Foundation
import MCP
import SCShared

struct ToolOutput { var json: [String: Any]; var images: [Screenshot] = [] }
struct ProbeLog { func record(_ entry: ActionLogEntry) {} }
struct ProbeREPL {
    func run(code: String, session: String, deadline: Date) async throws -> ToolOutput { ToolOutput(json: [:]) }
}
struct ProbeHiddenParts {
    enum Style { case browser }
    func condense(_ read: String, style: Style, target: String, tool: String) -> String { read }
}
enum BrowserPolicy { static func mutates(_ args: [String: Value]) -> Bool { false } }

final class ProbeBrowser {
    var calls: [[String: Any]] = []
    var inputs = 0
    let options: [String: Any]
    init(_ options: [String: Any]) { self.options = options }
    func instances() async -> Data {
        try! JSONSerialization.data(withJSONObject: ["browsers": [["id": "selected", "capabilities":
            options["old_extension"] as? Bool == true ? [] : ["browser_expect_v1"]]]])
    }
    func command(_ method: String, params: Data) async throws -> Data {
        let params = try JSONSerialization.jsonObject(with: params) as! [String: Any]
        calls.append(["method": method, "params": params])
        var reply: [String: Any] = ["ok": true]
        if method == "tab.expectPrepare" { reply = ["expect_token": "token"] }
        if method == "tab.expectFinish" {
            if options["finish_error"] as? Bool == true { throw SCError.browserUnavailable("fixture disconnected") }
            // Deliberately claim success even for complete=false; the server must enforce its own batch invariant.
            reply["expect"] = ["status": "verified"]
            reply["outcome"] = ["state": "verified", "verified": true]
            if params["observe"] as? String != "none" { reply["observation"] = ["outline": "Saved"] }
        }
        if ["tab.click", "tab.type", "tab.pressKey", "tab.scroll"].contains(method) {
            inputs += 1
            reply["events"] = ["input-\(inputs)"]
            if options["fail_first"] as? Bool == true && inputs == 1 {
                reply["ok"] = false
                reply["error"] = "fixture rejected input"
            }
            if options["uncertain_first"] as? Bool == true && inputs == 1 {
                reply["outcome"] = ["state": "uncertain", "verified": false]
                reply["uncertain"] = true
            }
            if params["expect"] != nil { reply["expect"] = ["status": "verified"] }
            if params["observe"] as? String == "screenshot" {
                reply["screenshot"] = ["data": Data([1]).base64EncodedString(), "width": 1, "height": 1, "scale": 1]
            } else if params["observe"] as? String != "none" { reply["observation"] = ["outline": "Saved"] }
        }
        if method == "tab.snapshot" { reply = ["outline": "fallback"] }
        return try JSONSerialization.data(withJSONObject: reply)
    }
    func status() async -> BrowserStatus { BrowserStatus(connected: true, extensionVersion: "test") }
    func listTabs() async throws -> [BrowserTab] { [] }
    func navigate(tabID: Int, url: String) async throws -> BrowserTab {
        BrowserTab(id: tabID, url: url, title: "test", isAgentTab: true)
    }
    func screenshot(tabID: Int) async throws -> Screenshot {
        Screenshot(pngData: Data([1]), width: 1, height: 1, scale: 1)
    }
}

final class ToolRouter {
    let browser: ProbeBrowser
    let log = ProbeLog()
    let repl = ProbeREPL()
    let hiddenParts: ProbeHiddenParts? = nil
    init(_ browser: ProbeBrowser) { self.browser = browser }
    static func caption(_ value: String) -> String { value }
    static func message(for error: Error) -> String { error.localizedDescription }
    func expandedParts(_ args: Arguments, target: String, noun: String) throws -> (text: String, ids: [String])? { nil }
    static func observeMode(_ args: Arguments) throws -> String {
        let mode = try args.optionalString("observe")?.lowercased() ?? "tree"
        guard ["tree", "screenshot", "both", "none"].contains(mode) else { throw SCError.invalidArgument("observe") }
        return mode
    }
    static func stepArguments(_ raw: [String: Value], index: Int, tool: String) throws -> [String: Value] {
        var result = raw.filter { $0.key != "tool" && $0.key != "args" }
        if let args = raw["args"], !args.isNull {
            guard let nested = Arguments.nestedObject(args) else { throw SCError.invalidArgument("args") }
            result.merge(nested) { $1 }
        }
        return result
    }
}

@main struct Probe {
    static func main() async throws {
        while let line = readLine() {
            let raw = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
            let tool = raw["tool"] as! String
            let args = try JSONDecoder().decode([String: Value].self,
                from: JSONSerialization.data(withJSONObject: raw["args"] ?? [:]))
            let browser = ProbeBrowser(raw["fake"] as? [String: Any] ?? [:])
            let router = ToolRouter(browser)
            var response: [String: Any]
            do {
                var result = try await Execution.$context.withValue(ExecutionContext(browserID: "selected")) {
                    tool == "batch" ? try await router.runTabBatch(Arguments(args))
                        : try await router.runBrowser(tool, Arguments(args))
                }
                ToolRouter.annotateOutcome(&result, mutating: true)
                response = ["result": result.json, "images": result.images.count]
            } catch { response = ["error": error.localizedDescription] }
            response["calls"] = browser.calls
            response["inputs"] = browser.inputs
            var data = try JSONSerialization.data(withJSONObject: response)
            data.append(10)
            FileHandle.standardOutput.write(data)
        }
    }
}
