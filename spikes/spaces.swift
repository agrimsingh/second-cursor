import CoreGraphics
import Foundation

@_silgen_name("CGSMainConnectionID") func CGSMainConnectionID() -> Int32
@_silgen_name("CGSCopyManagedDisplaySpaces") func CGSCopyManagedDisplaySpaces(_ cid: Int32) -> Unmanaged<CFArray>?
@_silgen_name("CGSCopySpacesForWindows") func CGSCopySpacesForWindows(_ cid: Int32, _ mask: Int32, _ wids: CFArray) -> Unmanaged<CFArray>?

let cid = CGSMainConnectionID()
for d in (CGSCopyManagedDisplaySpaces(cid)?.takeRetainedValue() as? [[String: Any]]) ?? [] {
    let current = (d["Current Space"] as? [String: Any])?["ManagedSpaceID"] ?? "?"
    let all = ((d["Spaces"] as? [[String: Any]]) ?? []).compactMap { $0["ManagedSpaceID"] }
    print("display \(d["Display Identifier"] ?? "?"): current space \(current), spaces \(all)")
}
let onScreen = Set(((CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]]) ?? []).compactMap { $0[kCGWindowNumber as String] as? Int })
for arg in CommandLine.arguments.dropFirst() {
    let wid = CGWindowID(arg)!
    let spaces = (CGSCopySpacesForWindows(cid, 7, [wid] as CFArray)?.takeRetainedValue() as? [UInt64]) ?? []
    let b = ((CGWindowListCopyWindowInfo([.optionIncludingWindow], wid) as? [[String: Any]])?.first?[kCGWindowBounds as String]) ?? "?"
    print("window \(wid): spaces \(spaces) onscreen=\(onScreen.contains(Int(wid))) bounds=\(b)")
}
