import Foundation
import SCBrowser
import SCShared

let usage = """
usage: sc-browser-dev [command args...]     one command (waits up to 30s for the extension)
       sc-browser-dev < script              one command per line

commands:
  status | wait [seconds] | tabs | sleep <seconds>
  open <url> | nav <tab> <url> | close <tab>
  snapshot <tab> | click <tab> <ref|x,y> | type <tab> <ref|-> <text> [--submit]
  key <tab> <chord> | scroll <tab> <up|down|left|right> [amount] | shot <tab> [file.png]
  expect <text>          fail unless the last snapshot contains <text>

$tab is the last opened tab. "@role=name" is the ref of `[ref] role "name"` in the last snapshot.
"""

setvbuf(stdout, nil, _IOLBF, 0)

let bridge = BrowserBridge()
do {
    try bridge.start()
} catch {
    FileHandle.standardError.write("\(error.localizedDescription)\n".data(using: .utf8)!)
    exit(1)
}

var lastTab: Int?
var lastOutline = ""
var failures = 0

func tokenize(_ line: String) -> [String] {
    var tokens: [String] = []
    var current = ""
    var quoted = false
    var hasToken = false
    for char in line {
        if char == "\"" {
            quoted.toggle()
            hasToken = true
        } else if char.isWhitespace && !quoted {
            if hasToken { tokens.append(current) }
            current = ""
            hasToken = false
        } else {
            current.append(char)
            hasToken = true
        }
    }
    if hasToken { tokens.append(current) }
    return tokens
}

@MainActor
func resolve(_ token: String) throws -> String {
    if token == "$tab" {
        guard let lastTab else { throw SCError.invalidArgument("$tab used before open") }
        return String(lastTab)
    }
    guard token.hasPrefix("@"), let equals = token.firstIndex(of: "=") else { return token }
    let role = String(token[token.index(after: token.startIndex)..<equals])
    let name = String(token[token.index(after: equals)...])
    let pattern = "\\[(\\w+)\\] \(NSRegularExpression.escapedPattern(for: role)) \"\(NSRegularExpression.escapedPattern(for: name))\""
    let regex = try NSRegularExpression(pattern: pattern)
    let range = NSRange(lastOutline.startIndex..., in: lastOutline)
    guard let match = regex.firstMatch(in: lastOutline, range: range),
          let refRange = Range(match.range(at: 1), in: lastOutline) else {
        throw SCError.invalidArgument("no `\(role) \"\(name)\"` with a ref in the last snapshot")
    }
    return String(lastOutline[refRange])
}

@MainActor
func tabID(_ token: String?) throws -> Int {
    guard let token, let id = Int(try resolve(token)) else { throw SCError.invalidArgument("expected a tab id") }
    return id
}

func printJSON<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(value), let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}

func waitForExtension(_ seconds: Double) async -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if await bridge.status().connected { return true }
        try? await Task.sleep(nanoseconds: 200_000_000)
    }
    return await bridge.status().connected
}

@MainActor
func run(_ words: [String]) async throws {
    guard let command = words.first else { return }
    let args = try words.dropFirst().map(resolve)
    switch command {
    case "status":
        printJSON(await bridge.status())
    case "wait":
        let seconds = Double(args.first ?? "30") ?? 30
        guard await waitForExtension(seconds) else { throw SCError.browserUnavailable("extension did not connect in \(seconds)s") }
        printJSON(await bridge.status())
    case "sleep":
        try await Task.sleep(nanoseconds: UInt64((Double(args.first ?? "1") ?? 1) * 1_000_000_000))
    case "tabs":
        printJSON(try await bridge.listTabs())
    case "open":
        guard let url = args.first else { throw SCError.invalidArgument("open <url>") }
        let tab = try await bridge.openTab(url: url)
        lastTab = tab.id
        printJSON(tab)
    case "nav":
        guard args.count >= 2 else { throw SCError.invalidArgument("nav <tab> <url>") }
        printJSON(try await bridge.navigate(tabID: try tabID(args[0]), url: args[1]))
    case "close":
        try await bridge.closeTab(tabID: try tabID(args.first))
        print("closed")
    case "snapshot":
        let snapshot = try await bridge.snapshot(tabID: try tabID(args.first), includeScreenshot: false)
        lastOutline = snapshot.outline
        printJSON(snapshot.tab)
        print(snapshot.outline)
    case "click":
        guard args.count >= 2 else { throw SCError.invalidArgument("click <tab> <ref|x,y>") }
        let parts = args[1].split(separator: ",").compactMap { Double($0) }
        let target: BrowserTarget = parts.count == 2 ? .point(x: parts[0], y: parts[1]) : .ref(args[1])
        try await bridge.click(tabID: try tabID(args[0]), target: target)
        print("clicked \(args[1])")
    case "type":
        guard args.count >= 3 else { throw SCError.invalidArgument("type <tab> <ref|-> <text> [--submit]") }
        let submit = args.last == "--submit"
        let text = args[2..<(submit ? args.count - 1 : args.count)].joined(separator: " ")
        try await bridge.type(tabID: try tabID(args[0]), ref: args[1] == "-" ? nil : args[1], text: text, submit: submit)
        print("typed \(text.debugDescription)\(submit ? " + Enter" : "")")
    case "key":
        guard args.count >= 2 else { throw SCError.invalidArgument("key <tab> <chord>") }
        try await bridge.pressKey(tabID: try tabID(args[0]), keys: args[1])
        print("pressed \(args[1])")
    case "scroll":
        guard args.count >= 2, let direction = ScrollDirection(rawValue: args[1]) else {
            throw SCError.invalidArgument("scroll <tab> <up|down|left|right> [amount]")
        }
        try await bridge.scroll(tabID: try tabID(args[0]), direction: direction, amount: Int(args.count > 2 ? args[2] : "3") ?? 3)
        print("scrolled \(direction.rawValue)")
    case "shot":
        let id = try tabID(args.first)
        let shot = try await bridge.screenshot(tabID: id)
        let path = args.count > 1 ? args[1] : "/tmp/sc-browser/shot-\(id).png"
        try shot.pngData.write(to: URL(fileURLWithPath: path))
        print("screenshot \(shot.width)x\(shot.height) css px, scale \(shot.scale), \(shot.pngData.count) bytes -> \(path)")
    case "expect":
        let text = args.joined(separator: " ")
        if lastOutline.contains(text) {
            print("PASS outline contains \(text.debugDescription)")
        } else {
            failures += 1
            print("FAIL outline lacks \(text.debugDescription)")
        }
    case "help", "-h", "--help":
        print(usage)
    default:
        throw SCError.invalidArgument("unknown command \(command)\n\(usage)")
    }
}

@MainActor
func execute(_ words: [String]) async {
    let started = Date()
    do {
        try await run(words)
    } catch {
        failures += 1
        print("ERROR \(error.localizedDescription)")
    }
    let elapsed = Date().timeIntervalSince(started)
    if elapsed > 0.05 {
        print(String(format: "  (%.2fs)", elapsed))
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
if !arguments.isEmpty {
    if !["status", "help", "-h", "--help", "wait"].contains(arguments[0]), await !waitForExtension(30) {
        print("ERROR extension did not connect within 30s")
        exit(1)
    }
    await execute(arguments)
} else {
    while let line = readLine() {
        let words = tokenize(line)
        guard let first = words.first, !first.hasPrefix("#") else { continue }
        print("> \(line.count > 200 ? line.prefix(200) + "… (\(line.count) chars)" : line)")
        await execute(words)
    }
}
exit(failures == 0 ? 0 : 1)
