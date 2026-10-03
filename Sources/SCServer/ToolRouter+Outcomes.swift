import Foundation
import MCP
import SCShared

extension ToolRouter {
    static func mutates(_ name: String, _ args: Arguments) -> Bool {
        if name == "browser_command" { return BrowserPolicy.mutates(args.raw) }
        if name == "browser_repl" { return true }
        if name == "agent_display" { return (try? args.string("action")) != "status" }
        return ToolCatalog.actionTools.contains(name)
    }

    /// Delivery acknowledgements and screenshots cannot prove the user's requested effect. Keep the old
    /// delivery fields for clients, but make outcome verification explicit and consistent across backends.
    static func annotateOutcome(_ result: inout ToolOutput, mutating: Bool) {
        guard mutating, result.json["outcome"] == nil else { return }
        let expectation = result.json["expect"] as? [String: Any]
        let expectationStatus = expectation?["status"] as? String
        let steps = (result.json["steps"] as? [[String: Any]] ?? [])
            + (result.json["results"] as? [[String: Any]] ?? [])
        let partial = steps.contains {
            ($0["ok"] as? Bool) == false || ($0["skipped"] as? Bool) == true
                || ($0["outcome"] as? [String: Any])?["state"] as? String == "failed"
        }
        let uncertain = result.json["uncertain"] as? Bool == true || steps.contains {
            $0["uncertain"] as? Bool == true || ($0["outcome"] as? [String: Any])?["state"] as? String == "uncertain"
        }
        let state: String
        let reason: String
        if uncertain && !(expectationStatus == "verified" && !partial) {
            state = "uncertain"
            reason = "Input may still take effect after an uncertain delivery acknowledgement; inspect the target before retrying."
        } else if expectationStatus == "failed" || partial || result.json["ok"] as? Bool == false {
            state = "failed"
            reason = "The postcondition or at least one step failed; reconcile the current state before retrying."
        } else if expectationStatus == "verified" {
            state = "verified"
            reason = "The requested postcondition changed from false to true after input."
        } else if result.json["observation_error"] != nil {
            state = "uncertain"
            reason = "Input was dispatched, but its resulting state could not be observed."
        } else if result.json["observation"] is [String: Any] {
            state = "observed"
            reason = expectationStatus == "preexisting"
                ? "The desired state already held before input; this action's effect was not proven."
                : "The resulting UI was observed; no requested postcondition verified the effect."
        } else {
            state = "dispatched"
            reason = "Input was dispatched without a verified postcondition."
        }
        result.json["outcome"] = ["state": state, "verified": state == "verified", "reason": reason]
    }

    static func verifiedSummary(_ result: ToolOutput, tool: String) -> String? {
        guard let outcome = result.json["outcome"] as? [String: Any],
            outcome["state"] as? String == "verified", outcome["verified"] as? Bool == true
        else { return nil }
        return "\(tool.replacingOccurrences(of: "_", with: " ")): postcondition verified"
    }
}
