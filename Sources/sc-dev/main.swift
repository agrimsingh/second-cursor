import Foundation
import SCCore
import SCShared

let usage = """
usage: sc-dev <command> ...
  perms
  list-apps [--all]
  list-windows [query]
  launch <app> [--new] [urls...]
  state <wid> [--shot out.png] [--menu] [--max N]
  click <wid> <index> [--right] [--count N] [--mod shift,cmd]  |  click <wid> --xy X Y [--right] [--count N] [--mod ...]
  drag <wid> X1 Y1 X2 Y2 [--mod shift,cmd]
  type <wid> <text> [--index N] [--keys]
  set <wid> <index> <value>
  key <wid> <chord>
  scroll <wid> <up|down|left|right> [amount] [--index N | --xy X Y]
  action <wid> <index> <name>
  shot <wid> <out.png>
  script <wid> '<step>' '<step>' ...   steps are the commands above without <wid>, e.g. 'key cmd+a'
Single commands that take an index snapshot the window first; script steps use the latest `state` step.
"""

let desktop = Desktop()
let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]

func printJSON<T: Encodable>(_ value: T) {
    print(String(data: (try? encoder.encode(value)) ?? Data(), encoding: .utf8) ?? "")
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func option(_ name: String, in args: inout [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    let value = args[i + 1]
    args.removeSubrange(i...(i + 1))
    return value
}

func flag(_ name: String, in args: inout [String]) -> Bool {
    guard let i = args.firstIndex(of: name) else { return false }
    args.remove(at: i)
    return true
}

func number<T: LosslessStringConvertible>(_ text: String?, _ what: String) -> T {
    guard let text, let value = T(text) else { fail("expected \(what), got \(text ?? "nothing")\n\n\(usage)") }
    return value
}

/// Splits a script step like a shell would for plain words and "double quoted" text.
func words(_ step: String) -> [String] {
    var result: [String] = []
    var current = ""
    var quoted = false
    var pending = false
    var escaped = false
    for character in step {
        if escaped {
            current.append(character == "n" ? "\n" : character == "t" ? "\t" : character)
            escaped = false
        } else if character == "\\" {
            escaped = true
            pending = true
        } else if character == "\"" {
            quoted.toggle()
            pending = true
        } else if character == " ", !quoted {
            if pending { result.append(current) }
            current = ""
            pending = false
        } else {
            current.append(character)
            pending = true
        }
    }
    if pending { result.append(current) }
    return result
}

func state(_ wid: UInt32, options: SnapshotOptions, shot: String?) async throws {
    let snapshot = try await desktop.windowState(windowID: wid, options: options)
    print(snapshot.tree)
    print("-- snapshot \(snapshot.snapshotID): \(snapshot.elementCount) indexed, truncated=\(snapshot.truncated)")
    printJSON(snapshot.window)
    if let shot, let image = snapshot.screenshot {
        try image.pngData.write(to: URL(fileURLWithPath: shot))
        print("-- screenshot \(image.width)x\(image.height) scale=\(image.scale) -> \(shot)")
    }
}

func run(_ command: String, wid: UInt32, _ rest: [String], autoSnapshot: Bool) async throws {
    var args = rest
    let quiet = SnapshotOptions(includeScreenshot: false)
    func prepareIndex() async throws {
        if autoSnapshot { _ = try await desktop.windowState(windowID: wid, options: quiet) }
    }
    switch command {
    case "state":
        let shot = option("--shot", in: &args)
        let menu = flag("--menu", in: &args)
        let max: Int = option("--max", in: &args).map { number($0, "--max N") } ?? 400
        try await state(wid, options: SnapshotOptions(includeScreenshot: shot != nil, includeMenuBar: menu, maxElements: max), shot: shot)
    case "click":
        let button: MouseButton = flag("--right", in: &args) ? .right : flag("--middle", in: &args) ? .middle : .left
        let count: Int = option("--count", in: &args).map { number($0, "--count N") } ?? 1
        let target: ClickTarget
        if let i = args.firstIndex(of: "--xy") {
            guard i + 2 < args.count else { fail(usage) }
            target = .point(x: number(args[i + 1], "X"), y: number(args[i + 2], "Y"))
        } else {
            try await prepareIndex()
            target = .element(index: number(args.first, "<index>"))
        }
        let modifiers = option("--mod", in: &args)?.split(separator: ",").map(String.init) ?? []
        printJSON(try await desktop.click(windowID: wid, target: target, button: button, count: count, modifiers: modifiers))
    case "drag":
        let modifiers = option("--mod", in: &args)?.split(separator: ",").map(String.init) ?? []
        guard args.count >= 4 else { fail(usage) }
        let from = Point(x: number(args[0], "X1"), y: number(args[1], "Y1"))
        let to = Point(x: number(args[2], "X2"), y: number(args[3], "Y2"))
        printJSON(try await desktop.drag(windowID: wid, from: from, to: to, modifiers: modifiers))
    case "type":
        let index: Int? = option("--index", in: &args).map { number($0, "--index N") }
        let keystrokesOnly = flag("--keys", in: &args)
        guard !args.isEmpty else { fail(usage) }
        if index != nil { try await prepareIndex() }
        printJSON(try await desktop.typeText(windowID: wid, text: args.joined(separator: " "), elementIndex: index, keystrokesOnly: keystrokesOnly))
    case "set":
        guard args.count >= 2 else { fail(usage) }
        try await prepareIndex()
        printJSON(try await desktop.setValue(windowID: wid, elementIndex: number(args[0], "<index>"), value: args.dropFirst().joined(separator: " ")))
    case "key":
        guard let chord = args.first else { fail(usage) }
        printJSON(try await desktop.pressKey(windowID: wid, keys: chord))
    case "scroll":
        var target: ClickTarget?
        if let index = option("--index", in: &args) {
            try await prepareIndex()
            target = .element(index: number(index, "--index N"))
        } else if let i = args.firstIndex(of: "--xy"), i + 2 < args.count {
            target = .point(x: number(args[i + 1], "X"), y: number(args[i + 2], "Y"))
            args.removeSubrange(i...(i + 2))
        }
        guard let direction = args.first.flatMap(ScrollDirection.init(rawValue:)) else { fail(usage) }
        let amount: Int = args.count > 1 ? number(args[1], "amount") : 3
        printJSON(try await desktop.scroll(windowID: wid, target: target, direction: direction, amount: amount))
    case "action":
        guard args.count >= 2 else { fail(usage) }
        try await prepareIndex()
        printJSON(try await desktop.performAction(windowID: wid, elementIndex: number(args[0], "<index>"), action: args[1]))
    case "shot":
        guard let path = args.first else { fail(usage) }
        let image = try await desktop.screenshot(windowID: wid)
        try image.pngData.write(to: URL(fileURLWithPath: path))
        print("\(image.width)x\(image.height) scale=\(image.scale) -> \(path)")
    case "sleep":
        try await Task.sleep(nanoseconds: UInt64((Double(args.first ?? "1") ?? 1) * 1e9))
    default:
        fail("unknown command \(command)\n\n\(usage)")
    }
}

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail(usage) }
args.removeFirst()

do {
    switch command {
    case "perms":
        printJSON(desktop.permissionStatus())
    case "list-apps":
        for app in await desktop.listApps(includeNotRunning: flag("--all", in: &args)) { printJSON(app) }
    case "list-windows":
        for window in await desktop.listWindows(appQuery: args.first) { printJSON(window) }
    case "launch":
        let newInstance = flag("--new", in: &args)
        guard let query = args.first else { fail(usage) }
        let urls = args.dropFirst().map { $0.contains("://") ? URL(string: $0)! : URL(fileURLWithPath: $0) }
        printJSON(try await desktop.launchApp(query, urls: urls, newInstance: newInstance))
    case "script":
        let wid: UInt32 = number(args.first, "<wid>")
        for step in args.dropFirst() {
            let parts = words(step)
            guard let name = parts.first else { continue }
            print("$ \(step)")
            do {
                try await run(name, wid: wid, Array(parts.dropFirst()), autoSnapshot: false)
            } catch {
                print("error: \(error.localizedDescription)")
            }
        }
    default:
        let wid: UInt32 = number(args.first, "<wid>")
        try await run(command, wid: wid, Array(args.dropFirst()), autoSnapshot: true)
    }
} catch {
    fail("error: \(error.localizedDescription)")
}
