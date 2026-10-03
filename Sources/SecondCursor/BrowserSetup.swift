import Foundation

enum BrowserSetup {
    static let hostName = "dev.secondcursor.host"
    static let extensionID = "nfjjbnelcophiemlajgbmahaejppblog"
    static let installationURL = URL(string: "chrome://extensions")!
    static let browserPaths = ["Google/Chrome", "Google/Chrome for Testing", "Chromium", "BraveSoftware/Brave-Browser", "Microsoft Edge", "Microsoft Edge Beta"]

    static var applicationSupport: URL {
        InstallMCP.homeDirectory.appendingPathComponent("Library/Application Support", isDirectory: true)
    }

    static var extensionDirectory: URL {
        applicationSupport.appendingPathComponent("SecondCursor/extension", isDirectory: true)
    }

    static var packagedExtension: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("extension", isDirectory: true)
    }

    static var hostBinary: URL {
        URL(fileURLWithPath: InstallMCP.executablePath).deletingLastPathComponent().appendingPathComponent("sc-native-host")
    }

    private static var installedBrowsers: [URL] {
        browserPaths.map { applicationSupport.appendingPathComponent($0, isDirectory: true) }
            .filter { FileManager.default.fileExists(atPath: $0.path) }
    }

    static func diagnosticNotes() -> [String] {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: hostBinary.path) else {
            return ["Browser helper is missing from the app. Reinstall a complete Second Cursor app bundle."]
        }
        var notes: [String] = []
        if !fm.fileExists(atPath: extensionDirectory.appendingPathComponent("manifest.json").path) {
            notes.append("Browser extension has not been installed. Choose Install browser bridge in setup.")
        }
        let browsers = installedBrowsers
        if browsers.isEmpty { notes.append("No supported Chromium browser profile was found. Open Chrome, Edge, or Brave once, then install the bridge.") }
        for browser in browsers {
            let manifest = browser.appendingPathComponent("NativeMessagingHosts/\(hostName).json")
            guard let data = try? Data(contentsOf: manifest),
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  json["path"] as? String == hostBinary.path,
                  json["allowed_origins"] as? [String] == ["chrome-extension://\(extensionID)/"] else {
                notes.append("\(browser.lastPathComponent) browser bridge needs installation or repair.")
                continue
            }
        }
        return notes
    }

    /// Installs only this app's extension and native-host manifest in known, existing browser profiles.
    /// Marketplace publishing is separate; until then setup shows the manual Load unpacked step explicitly.
    static func install() throws -> [String] {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: hostBinary.path) else {
            throw CLI.ConfigProblem(errorDescription: "Browser helper is missing. Install a complete Second Cursor app bundle.")
        }
        guard let source = packagedExtension,
              fm.fileExists(atPath: source.appendingPathComponent("manifest.json").path) else {
            throw CLI.ConfigProblem(errorDescription: "This app has no bundled extension. Rebuild with scripts/build_and_run.sh or install a packaged app.")
        }
        let parent = extensionDirectory.deletingLastPathComponent()
        try fm.createDirectory(at: parent, withIntermediateDirectories: true)
        let temporary = parent.appendingPathComponent(".extension-install-\(UUID().uuidString)")
        let previous = parent.appendingPathComponent(".extension-previous-\(UUID().uuidString)")
        var preservePrevious = false
        defer {
            try? fm.removeItem(at: temporary)
            if !preservePrevious { try? fm.removeItem(at: previous) }
        }
        try fm.copyItem(at: source, to: temporary)
        let hadPrevious = fm.fileExists(atPath: extensionDirectory.path)
        if hadPrevious { try fm.moveItem(at: extensionDirectory, to: previous) }
        do { try fm.moveItem(at: temporary, to: extensionDirectory) }
        catch {
            let installError = error
            if hadPrevious {
                do { try fm.moveItem(at: previous, to: extensionDirectory) }
                catch {
                    preservePrevious = true
                    throw CLI.ConfigProblem(errorDescription: "Could not replace the extension or restore it. Your previous extension is preserved at \(previous.path).")
                }
            }
            throw installError
        }
        var messages = ["Installed the bundled extension at \(extensionDirectory.path)."]
        let manifest: [String: Any] = [
            "name": hostName, "description": "Second Cursor browser bridge", "path": hostBinary.path,
            "type": "stdio", "allowed_origins": ["chrome-extension://\(extensionID)/"],
        ]
        let encoded = try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        let browsers = installedBrowsers
        for browser in browsers {
            let directory = browser.appendingPathComponent("NativeMessagingHosts", isDirectory: true)
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            let path = directory.appendingPathComponent("\(hostName).json")
            let previous: Data?
            do { previous = try Data(contentsOf: path) }
            catch let error as CocoaError where error.code == .fileReadNoSuchFile { previous = nil }
            catch { throw CLI.ConfigProblem(errorDescription: "Could not read the browser registration at \(path.path); it was left unchanged.") }
            if let previous, previous != encoded {
                try previous.write(to: path.appendingPathExtension("bak"), options: .atomic)
            }
            try encoded.write(to: path, options: .atomic)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            messages.append("Registered the browser bridge for \(browser.lastPathComponent).")
        }
        if browsers.isEmpty { messages.append("Open Chrome, Edge, or Brave once, then run browser setup again to register its profile.") }
        messages.append("Manual extension installation: open your browser's extensions page, enable Developer mode, choose Load unpacked, and select the installed extension folder. Then keep the extension enabled.")
        return messages
    }

    static func run() -> Int32 {
        do {
            for message in try install() { print(message) }
            return 0
        } catch {
            CLI.printError("install-browser: \(error.localizedDescription)")
            return 1
        }
    }
}
