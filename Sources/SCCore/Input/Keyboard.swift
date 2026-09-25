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
            "kp_0":82,"kp_1":83,"kp_2":84,"kp_3":85,"kp_4":86,"kp_5":87,"kp_6":88,"kp_7":89,"kp_8":91,"kp_9":92,
            "kp_enter":76,"kp_add":69,"kp_subtract":78,"kp_multiply":67,"kp_divide":75,"kp_decimal":65,
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

    static func flags(_ names: [String]) throws -> CGEventFlags {
        var flags: CGEventFlags = []
        for name in names {
            guard let flag = modifierFlags[name.trimmingCharacters(in: .whitespaces).lowercased()] else {
                throw SCError.invalidArgument("Unknown modifier \"\(name)\"; use cmd, shift, alt, ctrl or fn")
            }
            flags.insert(flag)
        }
        return flags
    }

    /// Parses `+`-separated chords such as "cmd+shift+t", "return", "cmd++".
    static func parse(_ keys: String) throws -> KeyChord {
        let trimmed = keys.trimmingCharacters(in: .whitespaces).lowercased()
        var tokens = trimmed.split(separator: "+", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
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
        if key.hasPrefix("kp_") { flags.insert(.maskNumericPad) }
        if ["up", "down", "left", "right"].contains(key) { flags.insert(.maskNumericPad) }
        return KeyChord(keyCode: code, flags: flags, key: key)
    }

    static func press(_ chord: KeyChord, pid: pid_t) {
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: chord.keyCode, keyDown: down)
            else { continue }
            event.flags = chord.flags
            SkyLight.post(event, to: pid, authenticated: true)
            usleep(8_000)
        }
    }

    /// Types each character as a Unicode key event; newlines and tabs become real Return/Tab presses
    /// so web forms see them as keys.
    static func type(_ text: String, pid: pid_t) {
        let source = CGEventSource(stateID: .hidSystemState)
        for character in text {
            if character == "\n" || character == "\r\n" || character == "\r" {
                press(KeyChord(keyCode: 36, flags: []), pid: pid)
                continue
            }
            if character == "\t" {
                press(KeyChord(keyCode: 48, flags: []), pid: pid)
                continue
            }
            var units = Array(String(character).utf16)
            for down in [true, false] {
                guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else { continue }
                event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
                event.flags = []
                SkyLight.post(event, to: pid, authenticated: true)
                usleep(8_000)
            }
        }
    }
}
