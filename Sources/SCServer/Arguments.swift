import Foundation
import MCP
import SCShared

/// Typed access to tool-call arguments. Numbers and booleans are accepted leniently
/// (e.g. `"12"` or `12.0` for an integer) because LLM clients are inconsistent about JSON types.
struct Arguments {
    let raw: [String: Value]

    init(_ raw: [String: Value]) {
        self.raw = raw
    }

    private func present(_ key: String) -> Value? {
        guard let value = raw[key], !value.isNull else { return nil }
        return value
    }

    func optionalInt(_ key: String) throws -> Int? {
        guard let value = present(key) else { return nil }
        guard let int = Int(value, strict: false) else { throw SCError.invalidArgument("\(key) must be an integer") }
        return int
    }

    func int(_ key: String) throws -> Int {
        guard let int = try optionalInt(key) else { throw SCError.invalidArgument(Self.required(key)) }
        return int
    }

    static func required(_ key: String) -> String {
        key == "keys" ? "keys is required: a key or +-separated chord, e.g. \"keys\": \"enter\", \"escape\" or \"cmd+a\""
            : "\(key) is required"
    }

    func int(_ key: String, default fallback: Int) throws -> Int {
        try optionalInt(key) ?? fallback
    }

    func optionalDouble(_ key: String) throws -> Double? {
        guard let value = present(key) else { return nil }
        guard let double = Double(value, strict: false) else { throw SCError.invalidArgument("\(key) must be a number") }
        return double
    }

    func optionalString(_ key: String) throws -> String? {
        guard let value = present(key) else { return nil }
        if case .data = value { return value.description }
        guard let string = String(value, strict: false) else { throw SCError.invalidArgument("\(key) must be a string") }
        return string
    }

    func string(_ key: String) throws -> String {
        guard let string = try optionalString(key) else { throw SCError.invalidArgument(Self.required(key)) }
        return string
    }

    func bool(_ key: String, default fallback: Bool) throws -> Bool {
        guard let value = present(key) else { return fallback }
        guard let bool = Bool(value, strict: false) else { throw SCError.invalidArgument("\(key) must be true or false") }
        return bool
    }

    func strings(_ key: String) throws -> [String] {
        guard let value = present(key) else { return [] }
        if let string = value.stringValue { return [string] }
        guard let array = value.arrayValue else { throw SCError.invalidArgument("\(key) must be an array of strings") }
        return try array.map {
            guard let string = $0.stringValue else { throw SCError.invalidArgument("\(key) must be an array of strings") }
            return string
        }
    }

    func objects(_ key: String) throws -> [[String: Value]] {
        guard let value = present(key) else { return [] }
        guard let array = value.arrayValue else { throw SCError.invalidArgument("\(key) must be an array of objects") }
        return try array.map {
            guard let object = $0.objectValue else { throw SCError.invalidArgument("\(key) must be an array of objects") }
            return object
        }
    }

    func windowID() throws -> UInt32 {
        guard let id = try optionalInt("window_id") else {
            throw SCError.invalidArgument("window_id is required, or pass app to use that app's main window")
        }
        guard let windowID = UInt32(exactly: id) else { throw SCError.invalidArgument("window_id must be a positive integer") }
        return windowID
    }

    /// Reads other clients' names for our arguments (Codex's `click_count` for `count`, …).
    /// The canonical name wins when both are present.
    func aliasing(_ aliases: [String: String]) -> Arguments {
        var merged = raw
        for (alias, canonical) in aliases where present(canonical) == nil {
            if let value = present(alias) { merged[canonical] = value }
        }
        return Arguments(merged)
    }

    func direction() throws -> ScrollDirection {
        let name = try string("direction")
        guard let direction = ScrollDirection(rawValue: name.lowercased()) else {
            throw SCError.invalidArgument("direction must be one of up, down, left, right")
        }
        return direction
    }

    /// `scope` is an element index when it reads as an integer (12 or "12"), otherwise a container label.
    func scope() throws -> (index: Int?, label: String?) {
        guard let value = present("scope") else { return (nil, nil) }
        if let index = Int(value, strict: false) { return (index, nil) }
        guard let label = String(value, strict: false)?.trimmingCharacters(in: .whitespaces), !label.isEmpty else {
            throw SCError.invalidArgument("scope must be an element index or a container label")
        }
        return (nil, label)
    }

    /// `element_index` wins when both it and x/y are given.
    func clickTarget() throws -> ClickTarget? {
        if let index = try optionalInt("element_index") { return .element(index: index) }
        let x = try optionalDouble("x"), y = try optionalDouble("y")
        switch (x, y) {
        case let (x?, y?): return .point(x: x, y: y)
        case (nil, nil): return nil
        default: throw SCError.invalidArgument("Pass both x and y")
        }
    }

    /// Short human-readable summary for the action log; long text values are truncated.
    var logDetail: String? {
        let object = raw.mapValues { value -> Any in
            if case .string(let s) = value, s.count > 200 { return String(s.prefix(200)) + "…" }
            return Self.jsonObject(value)
        }
        guard !object.isEmpty,
              let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    var logTarget: String? {
        for key in ["window_id", "tab_id", "app", "url"] {
            if let value = present(key) { return "\(key)=\(Self.jsonObject(value))" }
        }
        return nil
    }

    private static func jsonObject(_ value: Value) -> Any {
        switch value {
        case .null: return NSNull()
        case .bool(let b): return b
        case .int(let i): return i
        case .double(let d): return d
        case .string(let s): return s
        case .data(_, let data): return "<\(data.count) bytes>"
        case .array(let array): return array.map(jsonObject)
        case .object(let object): return object.mapValues(jsonObject)
        }
    }

    /// A nested object, or a JSON string holding one: some clients (Gemini through Cursor, for one)
    /// cannot send a free-form nested object and stringify it instead.
    static func nestedObject(_ value: Value) -> [String: Value]? {
        if let object = value.objectValue { return object }
        guard let text = value.stringValue else { return nil }
        return try? JSONDecoder().decode([String: Value].self, from: Data(text.utf8))
    }
}
