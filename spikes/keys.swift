import ApplicationServices
import Foundation

let pid = pid_t(CommandLine.arguments[1])!
let text = CommandLine.arguments[2]
let source = CGEventSource(stateID: .privateState)

for scalar in text.utf16 {
    var ch = scalar
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down)!
        e.keyboardSetUnicodeString(stringLength: 1, unicodeString: &ch)
        e.postToPid(pid)
    }
    usleep(8_000)
}
