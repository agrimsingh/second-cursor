import AppKit
import SCShared

// Commands run from shells, hooks and MCP hosts while the app runs; they must not start a second server or UI.
let arguments = CommandLine.arguments.dropFirst()
if arguments.contains("--help") || arguments.contains("-h") {
    print(CLI.usage)
    exit(0)
}
if arguments.contains("--pause") {
    do {
        try PauseState.pause()
        print("Second Cursor paused")
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("Could not write \(SCPaths.pauseFlagURL.path): \(error)\n".utf8))
        exit(1)
    }
}
if arguments.contains("--resume") {
    PauseState.resume()
    print("Second Cursor resumed")
    exit(0)
}
if arguments.contains("--turn-ended") {
    exit(TurnEnded.run(session: CLI.value(after: "--session", in: arguments)))
}
switch arguments.first {
case "mcp":
    exit(MCPProxy.run())
case "doctor":
    exit(Doctor.run(json: arguments.contains("--json")))
case "install-mcp":
    exit(InstallMCP.run(arguments.dropFirst()))
default:
    break
}

guard InstanceLock.acquire() else {
    if arguments.contains("--show-onboarding") {
        OnboardingPanel.requestFromRunningInstance()
        print("Asked the running Second Cursor to show its onboarding panel")
        exit(0)
    }
    FileHandle.standardError.write(Data("Second Cursor is already running\n".utf8))
    exit(0)
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
