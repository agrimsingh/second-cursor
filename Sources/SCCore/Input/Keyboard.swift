// Key posting follows cua-driver's keyboard.rs (MIT, github.com/trycua/cua); see LICENSES/cua-driver-MIT.txt.
import CoreGraphics
import Foundation
import SCShared

struct KeyChord: Equatable {
    var keyCode: CGKeyCode
    var flags: CGEventFlags
    /// The key as typed in the chord ("a", "?", "return"); matched against menu key equivalents.
    var key: String = ""
}

enum Keyboard {
    private static let modifierFlags: [String: CGEventFlags] = [
        "cmd": .maskCommand, "command": .maskCommand, "super": .maskCommand,
        "shift": .maskShift,
        "alt": .maskAlternate, "option": .maskAlternate, "opt": .maskAlternate,
        "ctrl": .maskControl, "control": .maskControl,
        "fn": .maskSecondaryFn,
    ]

    /// US ANSI virtual keycodes.
    private static let keyCodes: [String: CGKeyCode] = {
        var codes: [String: CGKeyCode] = [
            "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
            "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
            "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
            "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
            ".": 47, "`": 50,
            "kp0":82,"kp1":83,"kp2":84,"kp3":85,"kp4":86,"kp5":87,"kp6":88,"kp7":89,"kp8":91,"kp9":92,
            "kpenter":76,"kpadd":69,"kpsubtract":78,"kpmultiply":67,"kpdivide":75,"kpdecimal":65,
            "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
            "escape": 53, "esc": 53, "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
            "left": 123, "right": 124, "down": 125, "up": 126,
            "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
            "f9": 101, "f10": 109, "f11": 103, "f12": 111,
        ]
        codes["plus"] = codes["="]
        codes["minus"] = codes["-"]
        return codes
    }()

    private static let shiftedPunctuation: [String: String] = [
        "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
        "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`",
    ]

    /// X11 keysym spellings (xdotool, OpenAI computer-use) that differ from the names above. xdotool's
    /// "Delete" is forward delete, but "delete" keeps the macOS meaning (backspace) here.
    private static let aliases: [String: String] = ["prior": "pageup", "next": "pagedown"]

    /// Names are case-insensitive, and `_`/`-` inside a multi-character name are ignored ("Page_Down", "KP_Enter").
    private static func normalized(_ name: String) -> String {
        var name = name.trimmingCharacters(in: .whitespaces).lowercased()
        if name.count > 1 { name.removeAll { $0 == "_" || $0 == "-" } }
        return aliases[name] ?? name
    }

    static func flags(_ names: [String]) throws -> CGEventFlags {
        var flags: CGEventFlags = []
        for name in names {
            guard let flag = modifierFlags[normalized(name)] else {
                throw SCError.invalidArgument("Unknown modifier \"\(name)\"; use cmd, shift, alt, ctrl or fn")
            }
            flags.insert(flag)
        }
        return flags
    }

    /// Parses `+`-separated chords such as "cmd+shift+t", "return", "cmd++", "super+Page_Down".
    static func parse(_ keys: String) throws -> KeyChord {
        let trimmed = keys.trimmingCharacters(in: .whitespaces).lowercased()
        var tokens = trimmed.split(separator: "+", omittingEmptySubsequences: false).map { normalized(String($0)) }
        if trimmed == "+" || trimmed.hasSuffix("++") {
            tokens.removeLast(2)
            tokens.append("+")
        }
        guard let key = tokens.last, !key.isEmpty, !tokens.dropLast().contains("") else {
            throw SCError.invalidArgument("Cannot parse key chord \"\(keys)\"")
        }
        var flags: CGEventFlags = []
        for name in tokens.dropLast() {
            guard let flag = modifierFlags[name] else {
                throw SCError.invalidArgument("Unknown modifier \"\(name)\" in \"\(keys)\"; use cmd, shift, alt, ctrl or fn")
            }
            flags.insert(flag)
        }
        if let base = shiftedPunctuation[key], let code = keyCodes[base] {
            return KeyChord(keyCode: code, flags: flags.union(.maskShift), key: key)
        }
        guard let code = keyCodes[key] else { throw SCError.invalidArgument("Unknown key \"\(key)\" in \"\(keys)\"") }
        if ["up", "down", "left", "right", "home", "end", "pageup", "pagedown", "forwarddelete"].contains(key)
            || key.hasPrefix("f") && key.count > 1 {
            flags.insert(.maskSecondaryFn)
        }
        if key.hasPrefix("kp") { flags.insert(.maskNumericPad) }
        if ["up", "down", "left", "right"].contains(key) { flags.insert(.maskNumericPad) }
        return KeyChord(keyCode: code, flags: flags, key: key)
    }

    static func press(_ chord: KeyChord, pid: pid_t, wid: CGWindowID) throws {
        try SkyLight.requireKeyboardSupport()
        guard let source = CGEventSource(stateID: .hidSystemState) else {
            throw SCError.actionFailed("Unable to create a keyboard event source; no key was sent.")
        }
        // Allocate the release before sending the press, so allocation failure cannot leave a key held down.
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: chord.keyCode, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: chord.keyCode, keyDown: false) else {
            throw SCError.actionFailed("Unable to create the keyboard event pair; no key was sent.")
        }
        down.flags = chord.flags
        up.flags = chord.flags
        try postPair(down: down, up: up, pid: pid, wid: wid)
    }

    /// Types each character as a Unicode key event; newlines and tabs become real Return/Tab presses
    /// so web forms see them as keys.
    static func type(_ text: String, pid: pid_t, wid: CGWindowID) throws {
        try SkyLight.requireKeyboardSupport()
        guard let source = CGEventSource(stateID: .hidSystemState) else {
            throw SCError.actionFailed("Unable to create a keyboard event source; no text was sent.")
        }
        for character in text {
            if character == "\n" || character == "\r\n" || character == "\r" {
                try press(KeyChord(keyCode: 36, flags: []), pid: pid, wid: wid)
                continue
            }
            if character == "\t" {
                try press(KeyChord(keyCode: 48, flags: []), pid: pid, wid: wid)
                continue
            }
            var units = Array(String(character).utf16)
            guard let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
                  let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
                throw SCError.actionFailed("Unable to create the next keyboard event pair; text entry stopped and may be partially complete.")
            }
            for event in [down, up] {
                event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
                event.flags = []
            }
            try postPair(down: down, up: up, pid: pid, wid: wid)
        }
    }

    /// Check immediately before each press, including every Unicode character. Once a press is dispatched its
    /// release must run even if the task is canceled or the human intervenes; only subsequent presses are blocked.
    private static func postPair(down: CGEvent, up: CGEvent, pid: pid_t, wid: CGWindowID) throws {
        HumanInputMonitor.stamp(down)
        HumanInputMonitor.stamp(up)
        try NativeSafety.check(pid: pid, wid: wid, input: true)
        guard SkyLight.keyWindow(pid: pid) == wid else {
            throw SCError.notAllowed("The target stopped being the app's key window; keyboard input stopped before the next key. Observe before continuing.")
        }
        guard SkyLight.post(down, to: pid, authenticated: true) else {
            throw SCError.actionFailed("Authenticated keyboard dispatch failed before key-down; this key was not sent.")
        }
        var released = false
        defer {
            // A public release is the last cleanup available if the authenticated route fails after key-down.
            // It does not prove that the target accepted the release, so the caller still receives an error.
            if !released { up.postToPid(pid) }
            usleep(8_000)
        }
        usleep(8_000)
        released = SkyLight.post(up, to: pid, authenticated: true)
        guard released else {
            throw SCError.actionFailed("Key-down was sent, but authenticated key-up failed. A fallback release was sent; the key state is uncertain. Inspect the app before retrying.")
        }
    }
}
