import AppKit
import SCShared

// The e2e suite and scripts flip the pause flag through the installed binary while the app runs;
// these must not start a second server or UI.
let arguments = CommandLine.arguments.dropFirst()
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

guard InstanceLock.acquire() else {
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
