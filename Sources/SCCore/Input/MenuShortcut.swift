import ApplicationServices
import CoreGraphics
import Foundation

/// Apps only match menu key equivalents (cmd+a, cmd+z, ...) while they are frontmost, so the same
/// chord posted as key events to a background app is ignored. Pressing the menu item bound to the
/// shortcut runs the same action without activating the app.
enum MenuShortcut {
    // Carbon kMenu*Modifier bits used by AXMenuItemCmdModifiers; command is implied unless noCommand is set.
    private static let shift = 1, option = 2, control = 4, noCommand = 8
    // Per element: a timeout set on the app element does not carry over to elements read from it, and the
    // default (about 6 s) would let one hung app stall every caller waiting on this lookup.
    private static let timeout: Float = 1

    /// The app's menu bar, or nil when the app does not answer in time. Reading the bar itself validates no menu.
    static func menuBar(pid: pid_t) -> AXUIElement? {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, timeout)
        guard let bar = app.element(kAXMenuBarAttribute) else { return nil }
        AXUIElementSetMessagingTimeout(bar, timeout)
        return bar
    }

    /// Only call while the app holds the focus handoff: AppKit validates a menu when it is read, and items read
    /// while the app is inactive stay disabled, so pressing them later does nothing. Gives up at `deadline`.
    /// The returned item keeps the short timeout, so pressing it cannot block longer either.
    static func find(_ chord: KeyChord, in bar: AXUIElement, deadline: Date) -> (item: AXUIElement, title: String?)? {
        guard chord.flags.contains(.maskCommand) else { return nil }
        var wanted = 0
        if chord.flags.contains(.maskShift) { wanted |= shift }
        if chord.flags.contains(.maskAlternate) { wanted |= option }
        if chord.flags.contains(.maskControl) { wanted |= control }
        return search(bar, chord: chord, modifiers: wanted, depth: 0, deadline: deadline)
    }

    private static func search(
        _ element: AXUIElement, chord: KeyChord, modifiers: Int, depth: Int, deadline: Date
    ) -> (item: AXUIElement, title: String?)? {
        guard depth < 6 else { return nil }
        for child in element.children {
            guard Date() < deadline else { return nil }
            AXUIElementSetMessagingTimeout(child, timeout)
            if child.role == kAXMenuItemRole, matches(child, chord: chord, modifiers: modifiers) {
                return (child, child.string(kAXTitleAttribute))
            }
            if let found = search(child, chord: chord, modifiers: modifiers, depth: depth + 1, deadline: deadline) {
                return found
            }
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
