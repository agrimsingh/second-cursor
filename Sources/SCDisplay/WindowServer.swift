import ApplicationServices
import CoreGraphics
import Foundation

@_silgen_name("_AXUIElementGetWindow")
private func _AXUIElementGetWindow(_ element: AXUIElement, _ wid: UnsafeMutablePointer<CGWindowID>) -> AXError

@_silgen_name("_AXUIElementCreateWithRemoteToken")
private func _AXUIElementCreateWithRemoteToken(_ token: CFData) -> Unmanaged<AXUIElement>?

@_silgen_name("CGSMainConnectionID")
private func CGSMainConnectionID() -> Int32

@_silgen_name("CGSCopyManagedDisplaySpaces")
private func CGSCopyManagedDisplaySpaces(_ cid: Int32) -> Unmanaged<CFArray>?

@_silgen_name("CGSCopySpacesForWindows")
private func CGSCopySpacesForWindows(_ cid: Int32, _ mask: Int32, _ wids: CFArray) -> Unmanaged<CFArray>?

/// A window as the window server reports it. `frame` is in global points, top-left origin.
struct ServerWindow {
    let id: CGWindowID
    let pid: pid_t
    let frame: CGRect
    let isOnScreen: Bool
}

enum WindowServer {
    /// `.optionIncludingWindow` returns nothing for minimized windows and some off-Space ones, so fall back to the full list.
    static func window(_ id: CGWindowID) -> ServerWindow? {
        func find(_ list: [[String: Any]]?) -> [String: Any]? {
            list?.first { ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == id }
        }
        guard let info = find(CGWindowListCopyWindowInfo([.optionIncludingWindow], id) as? [[String: Any]])
                ?? find(CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]]),
              let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
              let bounds = info[kCGWindowBounds as String] as? NSDictionary,
              let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary)
        else { return nil }
        let onScreen = (info[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue ?? false
        return ServerWindow(id: id, pid: pid, frame: frame, isOnScreen: onScreen)
    }

    /// Displays that exist right now (awake or asleep), excluding mirrors of another display.
    static func onlineDisplays() -> [CGDirectDisplayID] {
        var ids = [CGDirectDisplayID](repeating: 0, count: 32)
        var count: UInt32 = 0
        guard CGGetOnlineDisplayList(UInt32(ids.count), &ids, &count) == .success else { return [] }
        return ids.prefix(Int(count)).filter { CGDisplayMirrorsDisplay($0) == kCGNullDirectDisplay }
    }

    static func activeDisplays() -> [CGDirectDisplayID] {
        var ids = [CGDirectDisplayID](repeating: 0, count: 32)
        var count: UInt32 = 0
        guard CGGetActiveDisplayList(UInt32(ids.count), &ids, &count) == .success else { return [] }
        return Array(ids.prefix(Int(count)))
    }

    static func spaces(of window: CGWindowID) -> [UInt64] {
        let spaces = CGSCopySpacesForWindows(CGSMainConnectionID(), 7, [window] as CFArray)?.takeRetainedValue()
        return (spaces as? [NSNumber])?.map(\.uint64Value) ?? []
    }

    /// On Spaces, none of which any display is showing right now.
    static func isOnHiddenSpace(_ window: CGWindowID) -> Bool {
        let spaces = spaces(of: window).filter { $0 != 0 }
        guard !spaces.isEmpty,
              let managed = CGSCopyManagedDisplaySpaces(CGSMainConnectionID())?.takeRetainedValue() as? [[String: Any]]
        else { return false }
        let showing = Set(managed.compactMap {
            (($0["Current Space"] as? [String: Any])?["ManagedSpaceID"] as? NSNumber)?.uint64Value
        })
        return showing.isDisjoint(with: spaces)
    }

    /// The current Space of `display`, once the window server has given it one.
    static func currentSpace(of display: CGDirectDisplayID) -> UInt64? {
        guard let uuid = CGDisplayCreateUUIDFromDisplayID(display)?.takeRetainedValue(),
              let identifier = CFUUIDCreateString(nil, uuid) as String?,
              let managed = CGSCopyManagedDisplaySpaces(CGSMainConnectionID())?.takeRetainedValue() as? [[String: Any]],
              let entry = managed.first(where: { ($0["Display Identifier"] as? String)?.caseInsensitiveCompare(identifier) == .orderedSame }),
              let current = entry["Current Space"] as? [String: Any]
        else { return nil }
        return (current["ManagedSpaceID"] as? NSNumber)?.uint64Value
    }
}

enum AXWindow {
    /// Finds the AX element for `windowID`, including windows on inactive Spaces, which
    /// kAXWindowsAttribute omits.
    static func find(pid: pid_t, windowID: CGWindowID) -> AXUIElement? {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 2)
        var found: AXUIElement?
        // A just-launched app publishes its main window a moment after the window appears.
        if poll(timeout: 2, { found = cheapLookup(app, windowID); return found != nil }) { return found }
        return scanRemoteTokens(pid: pid, windowID: windowID)
    }

    private static func cheapLookup(_ app: AXUIElement, _ windowID: CGWindowID) -> AXUIElement? {
        let windows = copy(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        if let match = windows.first(where: { id(of: $0) == windowID }) { return match }
        for attribute in [kAXMainWindowAttribute, kAXFocusedWindowAttribute] {
            if let value = copy(app, attribute), CFGetTypeID(value) == AXUIElementGetTypeID() {
                let window = value as! AXUIElement
                if id(of: window) == windowID { return window }
            }
        }
        return nil
    }

    static func setPosition(_ window: AXUIElement, _ point: CGPoint) -> AXError {
        var point = point
        return AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &point)!)
    }

    static func setSize(_ window: AXUIElement, _ size: CGSize) -> AXError {
        var size = size
        return AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, AXValueCreate(.cgSize, &size)!)
    }

    static func flag(_ element: AXUIElement, _ attribute: String) -> Bool {
        (copy(element, attribute) as? NSNumber)?.boolValue ?? false
    }

    private static func copy(_ element: AXUIElement, _ attribute: String) -> AnyObject? {
        var value: AnyObject?
        return AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success ? value : nil
    }

    private static func id(of window: AXUIElement) -> CGWindowID? {
        var id: CGWindowID = 0
        return _AXUIElementGetWindow(window, &id) == .success ? id : nil
    }

    /// Remote tokens are `pid | 0 | 'coco' | elementID`. Element IDs are assigned lazily, so this
    /// only reaches windows some AX client has already touched.
    private static func scanRemoteTokens(pid: pid_t, windowID: CGWindowID) -> AXUIElement? {
        var token = Data(count: 20)
        token.replaceSubrange(0..<4, with: withUnsafeBytes(of: pid) { Data($0) })
        token.replaceSubrange(8..<12, with: withUnsafeBytes(of: Int32(0x636f_636f)) { Data($0) })
        for elementID: UInt64 in 0..<50_000 {
            token.replaceSubrange(12..<20, with: withUnsafeBytes(of: elementID) { Data($0) })
            guard let element = _AXUIElementCreateWithRemoteToken(token as CFData)?.takeRetainedValue() else { continue }
            AXUIElementSetMessagingTimeout(element, 0.2)
            if (copy(element, kAXRoleAttribute) as? String) == kAXWindowRole, id(of: element) == windowID {
                return element
            }
        }
        return nil
    }
}

/// Polls `condition` every 50 ms until it holds or `timeout` elapses.
func poll(timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while true {
        if condition() { return true }
        if Date() >= deadline { return false }
        Thread.sleep(forTimeInterval: 0.05)
    }
}
