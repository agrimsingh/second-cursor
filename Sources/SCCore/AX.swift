import ApplicationServices
import CoreGraphics
import Foundation

extension AXUIElement {
    func attribute(_ name: String) -> AnyObject? {
        var value: AnyObject?
        return AXUIElementCopyAttributeValue(self, name as CFString, &value) == .success ? value : nil
    }

    /// Fetches several attributes in one IPC round trip; missing attributes come back as nil.
    func attributes(_ names: [String]) -> [AnyObject?] {
        var values: CFArray?
        guard AXUIElementCopyMultipleAttributeValues(self, names as CFArray, [], &values) == .success,
              let array = values as? [AnyObject], array.count == names.count
        else { return names.map { attribute($0) } }
        return array.map { value in
            if CFGetTypeID(value) == AXValueGetTypeID(), AXValueGetType(value as! AXValue) == .axError { return nil }
            return value
        }
    }

    func string(_ name: String) -> String? { attribute(name) as? String }

    func bool(_ name: String) -> Bool? { (attribute(name) as? NSNumber)?.boolValue }

    func element(_ name: String) -> AXUIElement? {
        guard let value = attribute(name), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return (value as! AXUIElement)
    }

    func elements(_ name: String) -> [AXUIElement] { attribute(name) as? [AXUIElement] ?? [] }

    var role: String? { string(kAXRoleAttribute) }

    var children: [AXUIElement] { elements(kAXChildrenAttribute) }

    var actions: [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(self, &names) == .success else { return [] }
        return names as? [String] ?? []
    }

    var windowID: CGWindowID? {
        var wid: CGWindowID = 0
        return _AXUIElementGetWindow(self, &wid) == .success && wid != 0 ? wid : nil
    }

    var pid: pid_t? {
        var pid: pid_t = 0
        return AXUIElementGetPid(self, &pid) == .success ? pid : nil
    }

    var frame: CGRect? {
        let values = attributes([kAXPositionAttribute, kAXSizeAttribute])
        guard let origin = AX.point(values[0]), let size = AX.size(values[1]) else { return nil }
        return CGRect(origin: origin, size: size)
    }

    @discardableResult
    func set(_ name: String, _ value: AnyObject) -> AXError {
        AXUIElementSetAttributeValue(self, name as CFString, value)
    }

    func isSettable(_ name: String) -> Bool {
        var settable = DarwinBoolean(false)
        return AXUIElementIsAttributeSettable(self, name as CFString, &settable) == .success && settable.boolValue
    }

    func perform(_ action: String) -> AXError {
        AXUIElementPerformAction(self, action as CFString)
    }

    /// True for a live element; false once the app has destroyed it.
    var isAlive: Bool {
        var value: AnyObject?
        let error = AXUIElementCopyAttributeValue(self, kAXRoleAttribute as CFString, &value)
        return error != .invalidUIElement && error != .cannotComplete
    }
}

enum AX {
    static func point(_ value: AnyObject?) -> CGPoint? {
        guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        var point = CGPoint.zero
        return AXValueGetValue(value as! AXValue, .cgPoint, &point) ? point : nil
    }

    static func size(_ value: AnyObject?) -> CGSize? {
        guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        var size = CGSize.zero
        return AXValueGetValue(value as! AXValue, .cgSize, &size) ? size : nil
    }

    static func range(_ value: AnyObject?) -> CFRange? {
        guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        var range = CFRange()
        return AXValueGetValue(value as! AXValue, .cfRange, &range) ? range : nil
    }

    static func rangeValue(_ range: CFRange) -> AXValue {
        var range = range
        return AXValueCreate(.cfRange, &range)!
    }

    /// Text for AXValue when it is a string or number; nil for element references, ranges, etc.
    static func text(_ value: AnyObject?) -> String? {
        switch value {
        case let string as String: return string
        case let attributed as NSAttributedString: return attributed.string
        case let number as NSNumber: return number.stringValue
        default: return nil
        }
    }
}
