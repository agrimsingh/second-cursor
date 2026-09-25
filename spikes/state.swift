import CoreGraphics
@_silgen_name("CGSMainConnectionID") func CGSMainConnectionID() -> Int32
@_silgen_name("CGSGetActiveSpace") func CGSGetActiveSpace(_ cid: Int32) -> UInt64
let p = CGEvent(source: nil)!.location
print("space=\(CGSGetActiveSpace(CGSMainConnectionID())) cursor=(\(Int(p.x)),\(Int(p.y)))")
