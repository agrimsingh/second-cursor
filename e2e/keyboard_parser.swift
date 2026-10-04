// Offline check of the production parser; no input APIs can run in this harness.
// After the normal app build:
// swiftc -I .build/debug Sources/SCCore/Input/Keyboard.swift e2e/keyboard_parser.swift \
//   .build/debug/SCShared.o -o build/keyboard-parser-tests
// build/keyboard-parser-tests
import CoreGraphics
import Foundation

// Keyboard.swift includes dispatch code. Fail immediately if a parser test ever reaches it.
enum SkyLight {
    static func requireKeyboardSupport() throws { fatalError("Input is disabled in parser checks") }
    static func keyWindow(pid: pid_t) -> CGWindowID? { fatalError("Input is disabled in parser checks") }
    static func post(_ event: CGEvent, to pid: pid_t, authenticated: Bool) -> Bool {
        fatalError("Input is disabled in parser checks")
    }
}
enum NativeSafety {
    static func check(pid: pid_t, wid: CGWindowID, input: Bool) throws {
        fatalError("Input is disabled in parser checks")
    }
}
enum HumanInputMonitor {
    static func stamp(_ event: CGEvent) { fatalError("Input is disabled in parser checks") }
}

enum ParserCheckError: Error { case failed(String) }

@main
struct KeyboardParserChecks {
    static func check(_ condition: Bool, _ message: String) throws {
        guard condition else { throw ParserCheckError.failed(message) }
        print("PASS \(message)")
    }

    static func main() throws {
        for (browser, native, code) in [("ArrowUp", "up", 126), ("ArrowDown", "down", 125),
                                        ("ArrowLeft", "left", 123), ("ArrowRight", "right", 124)] {
            let parsed = try Keyboard.parse(browser)
            try check(parsed == Keyboard.parse(native), "\(browser) preserves the canonical arrow chord")
            try check(parsed.keyCode == CGKeyCode(code)
                      && parsed.flags == [.maskSecondaryFn, .maskNumericPad],
                      "\(browser) retains the native key code and arrow flags")
            for prefix in ["shift+", "cmd+", "ctrl+alt+"] {
                try check(Keyboard.parse(prefix + browser) == Keyboard.parse(prefix + native),
                          "\(prefix)\(browser) preserves modifier semantics")
            }
        }
        try check(Keyboard.parse(" Shift+Arrow_Left ") == Keyboard.parse("shift+left"),
                  "browser aliases retain case and separator normalization")
        for bad in ["ArrowDiagonal", "Arrow", "cmd++ArrowDown"] {
            do {
                _ = try Keyboard.parse(bad)
            } catch {
                print("PASS unsupported chord \(bad) is refused")
                continue
            }
            throw ParserCheckError.failed("Unsupported chord \(bad) was accepted")
        }
    }
}
