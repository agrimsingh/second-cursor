import AppKit
import CoreGraphics
import Foundation

/// A window as the WindowServer reports it. `bounds` is in global points, top-left origin.
struct CGWindowRecord {
    var id: CGWindowID
    var pid: pid_t
    var ownerName: String
    var title: String
    var bounds: CGRect
    var layer: Int
    var isOnScreen: Bool

    init?(_ info: [String: Any]) {
        guard let number = info[kCGWindowNumber as String] as? Int,
              let pid = info[kCGWindowOwnerPID as String] as? Int32,
              let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: boundsDict)
        else { return nil }
        self.id = CGWindowID(number)
        self.pid = pid
        self.ownerName = info[kCGWindowOwnerName as String] as? String ?? ""
        self.title = info[kCGWindowName as String] as? String ?? ""
        self.bounds = bounds
        self.layer = info[kCGWindowLayer as String] as? Int ?? 0
        self.isOnScreen = info[kCGWindowIsOnscreen as String] as? Bool ?? false
    }
}

enum WindowList {
    static func all() -> [CGWindowRecord] {
        let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        return list.compactMap(CGWindowRecord.init)
    }

    static func onScreenIDs() -> Set<CGWindowID> {
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
        return Set(list.compactMap { ($0[kCGWindowNumber as String] as? Int).map(CGWindowID.init) })
    }

    /// `.optionIncludingWindow` returns nothing for windows on inactive Spaces or minimized, so fall back to the full list.
    static func record(_ wid: CGWindowID) -> CGWindowRecord? {
        let single = CGWindowListCopyWindowInfo([.optionIncludingWindow], wid) as? [[String: Any]] ?? []
        if let record = single.compactMap(CGWindowRecord.init).first(where: { $0.id == wid }) { return record }
        let all = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
        return all.lazy.compactMap(CGWindowRecord.init).first { $0.id == wid }
    }

    /// Top-level app windows: layer 0 and big enough to be a real window rather than a helper surface.
    static func isAppWindow(_ record: CGWindowRecord) -> Bool {
        record.layer == 0 && record.bounds.width > 50 && record.bounds.height > 50
    }
}
