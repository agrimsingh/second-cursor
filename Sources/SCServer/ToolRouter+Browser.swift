import Foundation
import SCShared

extension ToolRouter {
    func runBrowser(_ name: String, _ args: Arguments) async throws -> ToolOutput {
        switch name {
        case "browser_status":
            return ToolOutput(json: Shape.browser(await browser.status()))

        case "browser_tabs":
            return ToolOutput(json: ["tabs": try await browser.listTabs().map(Shape.tab)])

        case "browser_open":
            return ToolOutput(json: ["tab": Shape.tab(try await browser.openTab(url: try args.string("url")))])

        case "browser_navigate":
            let tab = try await browser.navigate(tabID: try args.int("tab_id"), url: try args.string("url"))
            return ToolOutput(json: ["tab": Shape.tab(tab)])

        case "browser_snapshot":
            let snapshot = try await browser.snapshot(
                tabID: try args.int("tab_id"), includeScreenshot: try args.bool("include_screenshot", default: false))
            return ToolOutput(
                json: ["tab": Shape.tab(snapshot.tab), "outline": snapshot.outline],
                images: snapshot.screenshot.map { [$0] } ?? []
            )

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
            try await browser.closeTab(tabID: try args.int("tab_id"))
            return ToolOutput(json: Shape.ok)

        default:
            throw SCError.invalidArgument("Unknown tool \"\(name)\"")
        }
    }
}
