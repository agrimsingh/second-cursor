import ApplicationServices
import CoreGraphics
import Foundation

/// Apps only match menu key equivalents (cmd+a, cmd+z, ...) while they are frontmost, so the same
/// chord posted as key events to a background app is ignored. Pressing the menu item bound to the
/// shortcut runs the same action without activating the app.
enum MenuShortcut {
    // Carbon kMenu*Modifier bits used by AXMenuItemCmdModifiers; command is implied unless noCommand is set.
    private static let shift = 1, option = 2, control = 4, noCommand = 8

    static func find(_ chord: KeyChord, pid: pid_t) -> AXUIElement? {
        guard chord.flags.contains(.maskCommand) else { return nil }
        var wanted = 0
        if chord.flags.contains(.maskShift) { wanted |= shift }
        if chord.flags.contains(.maskAlternate) { wanted |= option }
        if chord.flags.contains(.maskControl) { wanted |= control }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 1)
        guard let bar = app.element(kAXMenuBarAttribute) else { return nil }
        return search(bar, chord: chord, modifiers: wanted, depth: 0)
    }

    private static func search(_ element: AXUIElement, chord: KeyChord, modifiers: Int, depth: Int) -> AXUIElement? {
        guard depth < 6 else { return nil }
        for child in element.children {
            if child.role == kAXMenuItemRole, matches(child, chord: chord, modifiers: modifiers) { return child }
            if let found = search(child, chord: chord, modifiers: modifiers, depth: depth + 1) { return found }
        }
        return nil
    }

    private static func matches(_ item: AXUIElement, chord: KeyChord, modifiers: Int) -> Bool {
        // AXEnabled is ignored: AppKit only revalidates menus while the app is active, so a background
        // app reports stale "disabled" items; pressing one still revalidates and runs it.
        let values = item.attributes(["AXMenuItemCmdChar", "AXMenuItemCmdModifiers", "AXMenuItemCmdVirtualKey"])
        let itemModifiers = (values[1] as? NSNumber)?.intValue ?? 0
        guard itemModifiers & noCommand == 0, itemModifiers & (shift | option | control) == modifiers else { return false }
        if let character = values[0] as? String, !character.isEmpty {
            return chord.key.count == 1 && character.lowercased() == chord.key
        }
        if let virtualKey = (values[2] as? NSNumber)?.intValue { return virtualKey == Int(chord.keyCode) }
        return false
    }
}
