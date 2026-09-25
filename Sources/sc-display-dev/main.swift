// Development driver for the agent display. The display dies with this process, so commands
// that need it stay alive until they tear it down.
import AppKit
import SCDisplay
import SCShared

let usage = """
    usage: sc-display-dev <command>
      status               agent display status in this process, plus displays, Spaces and cursor
      create-hold <sec>    create the agent display, hold it, tear it down
      demo [sec]           host a scratch TextEdit on the agent display, hold (default 10s, or Enter), return it, tear down
      check                full lifecycle self-check against a scratch TextEdit window
    """

final class StopFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.withLock { value = true } }
    var isSet: Bool { lock.withLock { value } }
}

setvbuf(stdout, nil, _IOLBF, 0)
let stop = StopFlag()
signal(SIGINT, SIG_IGN)
let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
sigint.setEventHandler { stop.set() }
sigint.resume()
if isatty(STDIN_FILENO) != 0 {
    Thread { _ = readLine(); stop.set() }.start()
}

/// Sleeps for `seconds`, or until Enter / Ctrl-C, so teardown still runs.
func hold(_ seconds: Double) async {
    print("holding for \(Int(seconds))s (Enter or Ctrl-C to continue early)")
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline, !stop.isSet {
        try? await Task.sleep(nanoseconds: 100_000_000)
    }
}

func json<T: Encodable>(_ value: T) -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    return (try? encoder.encode(value)).map { String(decoding: $0, as: UTF8.self) } ?? "\(value)"
}

func createHold(seconds: Double) async throws {
    let agent = AgentDisplay()
    let started = Date()
    let status = try await agent.ensureDisplay()
    print(String(format: "created in %.2fs", Date().timeIntervalSince(started)))
    print(json(status))
    printDisplays()
    await hold(seconds)
    await agent.teardown()
    printStatus(await agent.status())
    printDisplays()
}

struct DemoOutput: Encodable {
    var status: AgentDisplayStatus
    var placement: WindowPlacement
}

func demo(seconds: Double) async throws {
    let agent = AgentDisplay()
    _ = try await agent.ensureDisplay()
    let (pid, wid) = try await launchScratchTextEdit("demo.txt", text: "Second Cursor agent display demo")
    defer { kill(pid, SIGKILL) }
    _ = await settledWindow(wid)
    print("scratch TextEdit pid \(pid) window \(wid): \(describe(windowFacts(wid)))")
    let placement = try await agent.moveToAgentDisplay(windowID: wid, pid: pid)
    let status = await agent.status()
    print(json(DemoOutput(status: status, placement: placement)))
    print("hosted: \(describe(windowFacts(wid))) agentSpace=\(status.displayID.flatMap(currentSpace(of:)).map(String.init) ?? "-")")
    await hold(seconds)
    try await agent.returnWindow(windowID: wid)
    print("returned: \(describe(windowFacts(wid)))")
    await agent.teardown()
    printStatus(await agent.status())
    printDisplays()
}

let args = Array(CommandLine.arguments.dropFirst())
do {
    switch args.first {
    case "status":
        printStatus(await AgentDisplay().status())
        printDisplays()
    case "create-hold" where args.count == 2:
        guard let seconds = Double(args[1]) else { print(usage); exit(2) }
        try await createHold(seconds: seconds)
    case "demo":
        try await demo(seconds: args.count > 1 ? Double(args[1]) ?? 10 : 10)
    case "check":
        exit(await check() ? 0 : 1)
    default:
        print(usage)
        exit(2)
    }
} catch {
    print("error: \(error.localizedDescription)")
    exit(1)
}
