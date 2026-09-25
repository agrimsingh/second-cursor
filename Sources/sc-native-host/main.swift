import Darwin
import Foundation

// Chrome native messaging host: relays Chrome frames (4-byte native-endian length + JSON)
// to newline-delimited JSON on the Second Cursor app's Unix socket, and back.

let maxFrameToChrome = 1 << 20
let maxFrameFromChrome = 64 << 20

signal(SIGPIPE, SIG_IGN)

func log(_ message: String) {
    FileHandle.standardError.write("sc-native-host: \(message)\n".data(using: .utf8)!)
}

func socketPath() -> String {
    let home = getpwuid(getuid()).map { String(cString: $0.pointee.pw_dir) } ?? NSHomeDirectory()
    return home + "/Library/Application Support/SecondCursor/browser.sock"
}

func writeAll(_ fd: Int32, _ data: Data) -> Bool {
    data.withUnsafeBytes { raw -> Bool in
        guard var pointer = raw.baseAddress else { return true }
        var remaining = raw.count
        while remaining > 0 {
            let written = write(fd, pointer, remaining)
            if written < 0 {
                if errno == EINTR { continue }
                return false
            }
            remaining -= written
            pointer += written
        }
        return true
    }
}

func readExact(_ fd: Int32, _ count: Int) -> Data? {
    var data = Data(count: count)
    var offset = 0
    let ok = data.withUnsafeMutableBytes { raw -> Bool in
        while offset < count {
            let n = read(fd, raw.baseAddress! + offset, count - offset)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { return false }
            offset += n
        }
        return true
    }
    return ok ? data : nil
}

let stdoutLock = NSLock()
let socketLock = NSLock()

@discardableResult
func sendToChrome(_ json: Data) -> Bool {
    var length = UInt32(json.count)
    var frame = Data(bytes: &length, count: 4)
    frame.append(json)
    stdoutLock.lock()
    defer { stdoutLock.unlock() }
    return writeAll(STDOUT_FILENO, frame)
}

func sendErrorToChrome(_ message: String) {
    let payload: [String: Any] = ["type": "error", "message": message]
    if let json = try? JSONSerialization.data(withJSONObject: payload) {
        sendToChrome(json)
    }
}

func sendLineToApp(_ fd: Int32, _ line: Data) -> Bool {
    socketLock.lock()
    defer { socketLock.unlock() }
    return writeAll(fd, line)
}

func connectToApp() -> Int32? {
    let path = socketPath()
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let capacity = MemoryLayout.size(ofValue: address.sun_path)
    guard path.utf8.count < capacity else {
        log("socket path too long: \(path)")
        return nil
    }
    withUnsafeMutableBytes(of: &address.sun_path) { raw in
        raw.copyBytes(from: path.utf8)
        raw[path.utf8.count] = 0
    }
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    var one: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
    let result = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard result == 0 else {
        close(fd)
        return nil
    }
    return fd
}

func forwardLineToChrome(_ line: Data, appSocket: Int32) {
    guard !line.allSatisfy({ $0 == 0x20 || $0 == 0x09 || $0 == 0x0D }) else { return }
    guard let object = try? JSONSerialization.jsonObject(with: line) else {
        log("dropping invalid JSON line from app (\(line.count) bytes)")
        return
    }
    guard line.count <= maxFrameToChrome else {
        log("dropping \(line.count)-byte message; Chrome accepts at most \(maxFrameToChrome)")
        if let id = (object as? [String: Any])?["id"],
           let reply = try? JSONSerialization.data(withJSONObject: [
               "id": id,
               "error": ["code": "invalid_argument", "message": "Request exceeds Chrome's 1 MB native messaging limit"],
           ]) {
            _ = sendLineToApp(appSocket, reply + [0x0A])
        }
        return
    }
    if !sendToChrome(line) {
        exit(0)
    }
}

func relayAppToChrome(_ fd: Int32) {
    var buffer = Data()
    var chunk = [UInt8](repeating: 0, count: 65536)
    while true {
        let n = read(fd, &chunk, chunk.count)
        if n < 0 && errno == EINTR { continue }
        if n <= 0 { break }
        buffer.append(contentsOf: chunk[0..<n])
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<newline]
            forwardLineToChrome(Data(line), appSocket: fd)
            buffer.removeSubrange(buffer.startIndex...newline)
        }
    }
    sendErrorToChrome("Second Cursor app disconnected")
    exit(0)
}

func relayChromeToApp(_ fd: Int32) -> Never {
    while true {
        guard let header = readExact(STDIN_FILENO, 4) else { exit(0) }
        let length = Int(header.withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) })
        if length == 0 { continue }
        guard length <= maxFrameFromChrome else {
            log("frame length \(length) exceeds \(maxFrameFromChrome); stream cannot be resynced")
            exit(1)
        }
        guard var body = readExact(STDIN_FILENO, length) else { exit(0) }
        // Valid JSON can only contain raw newlines as inter-token whitespace.
        for index in body.indices where body[index] == 0x0A {
            body[index] = 0x20
        }
        body.append(0x0A)
        guard sendLineToApp(fd, body) else {
            sendErrorToChrome("Second Cursor app disconnected")
            exit(0)
        }
    }
}

if let origin = CommandLine.arguments.dropFirst().first {
    log("started for \(origin)")
}

guard let appSocket = connectToApp() else {
    sendErrorToChrome("Second Cursor app not running")
    exit(1)
}

Thread { relayAppToChrome(appSocket) }.start()
relayChromeToApp(appSocket)
