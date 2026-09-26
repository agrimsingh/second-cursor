import ApplicationServices
import CoreGraphics
import Foundation

@_silgen_name("_AXUIElementGetWindow")
func _AXUIElementGetWindow(_ element: AXUIElement, _ wid: UnsafeMutablePointer<CGWindowID>) -> AXError

@_silgen_name("_AXUIElementCreateWithRemoteToken")
func _AXUIElementCreateWithRemoteToken(_ token: CFData) -> Unmanaged<AXUIElement>?

@_silgen_name("CGSMainConnectionID")
func CGSMainConnectionID() -> Int32

@_silgen_name("CGSCopySpacesForWindows")
func CGSCopySpacesForWindows(_ cid: Int32, _ mask: Int32, _ wids: CFArray) -> Unmanaged<CFArray>?

@_silgen_name("CGSGetActiveSpace")
func CGSGetActiveSpace(_ cid: Int32) -> UInt64

@_silgen_name("CGSCopyManagedDisplaySpaces")
func CGSCopyManagedDisplaySpaces(_ cid: Int32) -> Unmanaged<CFArray>?

enum Spaces {
    /// Space membership must be queried one window at a time: the call returns the union for its input list.
    static func ids(for wid: CGWindowID) -> [UInt64] {
        let spaces = CGSCopySpacesForWindows(CGSMainConnectionID(), 7, [wid] as CFArray)?.takeRetainedValue() as? [UInt64]
        return (spaces ?? []).filter { $0 != 0 }
    }

    static var active: UInt64 { CGSGetActiveSpace(CGSMainConnectionID()) }

    /// On Spaces, none of which any display is showing right now.
    static func isHidden(_ wid: CGWindowID) -> Bool {
        let spaces = ids(for: wid)
        guard !spaces.isEmpty,
              let managed = CGSCopyManagedDisplaySpaces(CGSMainConnectionID())?.takeRetainedValue() as? [[String: Any]]
        else { return false }
        let showing = Set(managed.compactMap {
            (($0["Current Space"] as? [String: Any])?["ManagedSpaceID"] as? NSNumber)?.uint64Value
        })
        return showing.isDisjoint(with: spaces)
    }
}
