import Foundation
import MCP
import NIOCore
import SCShared

public final class SecondCursorServer: @unchecked Sendable {
    public enum StartError: Error, LocalizedError {
        case portInUse(Int)
        case bindFailed(port: Int, reason: String)

        public var errorDescription: String? {
            switch self {
            case .portInUse(let port): return "Port \(port) is already in use"
            case .bindFailed(let port, let reason): return "Could not listen on port \(port): \(reason)"
            }
        }
    }

    public static let host = "127.0.0.1"
    public static let path = "/mcp"
    static let version = "0.2.0"

    static let instructions = """
        Second Cursor operates macOS apps in the background without moving the user's cursor, \
        stealing focus, or switching Spaces.
        - Work in the background; never ask the user to click or switch apps for you.
        - At the start of each turn, call get_window_state before acting; the UI may have changed since your last turn.
        - The user approves each app the first time you use it. If approval is denied or expires, do not retry without asking.
        - Prefer element_index actions (from get_window_state) over x/y coordinates.
        - Coordinates are window-local points, which equal screenshot pixels.
        - Everything works in place: windows stay where they are, on any Space, behind any app. Never move the user's windows.
        - Keyboard shortcuts (press_key) are often the fastest, most reliable way to drive pro apps.
        - Custom-drawn apps may stop repainting while on a Space the user is not viewing; trust the accessibility tree over a stale screenshot.
        - Browser tools act on agent-created tabs. Personal tab discovery/claim, history, clipboard, uploads and raw CDP require local user approval.
        - Browser operation argument help is available through browser_command(operation=capabilities).
        - Calls may run concurrently; send order-dependent actions (typing, key sequences, button presses) one at a time.
        """

    private let desktop: DesktopControlling
    private let agentDisplay: AgentDisplayControlling
    private let browser: BrowserControlling
    private let listener = HTTPListener()

    public init(desktop: DesktopControlling, agentDisplay: AgentDisplayControlling, browser: BrowserControlling) {
        self.desktop = desktop
        self.agentDisplay = agentDisplay
        self.browser = browser
    }

    /// Binds 127.0.0.1:`config.port` and returns once the server is listening.
    public func start(config: SCConfig) async throws {
        let router = ToolRouter(desktop: desktop, agentDisplay: agentDisplay, browser: browser, config: config,
                                connectionCount: { [listener] in listener.activeConnectionCount })
        let tools = ToolCatalog.advertised(surface: config.toolSurface, condensing: config.condenseOutlines == "rules")
        // browser_repl's description is all its client needs; the desktop instructions would only mislead.
        let instructions = config.toolSurface == "repl" ? nil : Self.instructions
        let pipeline = StandardValidationPipeline(validators: [
            OriginValidator.localhost(),
            StaticBearerTokenValidator(token: config.token),
            AcceptHeaderValidator(mode: .jsonOnly),
            ContentTypeValidator(),
            ProtocolVersionValidator(),
        ])
        do {
            try await listener.start(host: Self.host, port: config.port, path: Self.path) { request in
                if request.path == "/turn-ended" {
                    return await Self.turnEnded(request, token: config.token, router: router)
                }
                if request.path == "/api" {
                    let context = HTTPValidationContext(httpMethod:request.method)
                    for validator: any HTTPRequestValidator in [OriginValidator.localhost(), StaticBearerTokenValidator(token:config.token), ContentTypeValidator()] {
                        if let refusal = validator.validate(request,context:context) { return refusal }
                    }
                    guard request.method == "POST", let body = request.body else { return .error(statusCode:400,.invalidRequest("POST JSON required")) }
                    do {
                        let payload = try JSONDecoder().decode(APIRequest.self,from:body)
                        if payload.tool == "tools/list" { return .data(try JSONEncoder().encode(tools),headers:["Content-Type":"application/json"]) }
                        let result = await router.call(name:payload.tool,arguments:payload.args ?? [:])
                        return .data(try JSONEncoder().encode(result),headers:["Content-Type":"application/json"])
                    } catch { return .error(statusCode:400,.invalidRequest("Malformed request: \(error.localizedDescription)")) }
                }
                Self.forwardCancellation(request, token: config.token, router: router)
                let response = await Self.handle(
                    request, pipeline: pipeline, router: router, tools: tools, instructions: instructions)
                return Self.identifyingClient(response, to: request)
            }
        } catch let error as IOError where error.errnoCode == EADDRINUSE {
            throw StartError.portInUse(config.port)
        } catch {
            throw StartError.bindFailed(port: config.port, reason: "\(error)")
        }
    }

    public func stop() async {
        await listener.stop()
    }

    /// `POST /turn-ended`, body optionally `{"session_id": "..."}`; agent hosts call it after every
    /// assistant turn (`SecondCursor --turn-ended`). Authenticated like `/api`.
    private static func turnEnded(_ request: HTTPRequest, token: String, router: ToolRouter) async -> HTTPResponse {
        let context = HTTPValidationContext(httpMethod: request.method)
        let validators: [any HTTPRequestValidator] = [
            OriginValidator.localhost(), StaticBearerTokenValidator(token: token), ContentTypeValidator(),
        ]
        for validator in validators {
            if let refusal = validator.validate(request, context: context) { return refusal }
        }
        guard request.method == "POST" else { return .error(statusCode: 405, .invalidRequest("POST required")) }
        var session = "legacy"
        if let body = request.body, !body.isEmpty {
            guard let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else {
                return .error(statusCode: 400, .invalidRequest("Body must be a JSON object"))
            }
            switch object["session_id"] {
            case nil, is NSNull:
                break
            case let id as String where !id.isEmpty:
                session = id
            default:
                return .error(statusCode: 400, .invalidRequest("session_id must be a non-empty string"))
            }
        }
        let result = await router.endTurn(session: session)
        guard let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) else {
            return .error(statusCode: 500, .internalError("Could not encode the turn-ended result"))
        }
        return .data(data, headers: ["Content-Type": "application/json"])
    }

    /// A client's `notifications/cancelled` arrives in its own HTTP request, so the per-request `Server` that would
    /// act on it never sees the call; it goes to the running browser_repl call with that request id instead.
    private static func forwardCancellation(_ request: HTTPRequest, token: String, router: ToolRouter) {
        let context = HTTPValidationContext(httpMethod: request.method)
        let validators: [any HTTPRequestValidator] = [OriginValidator.localhost(), StaticBearerTokenValidator(token: token)]
        guard validators.allSatisfy({ $0.validate(request, context: context) == nil }), let body = request.body,
            let message = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any],
            message["method"] as? String == "notifications/cancelled",
            let id = (message["params"] as? [String: Any])?["requestId"]
        else { return }
        if let key = ClientRequest.key(client: ClientRequest.client(request.headers), id: id) {
            router.repl.cancel(requestID: key)
        }
    }

    /// The transport is stateless, but an id a client echoes on every request tells its cancellations from another's.
    private static func identifyingClient(_ response: HTTPResponse, to request: HTTPRequest) -> HTTPResponse {
        guard case .data(let data, var headers) = response, let body = request.body,
            (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["method"] as? String == "initialize"
        else { return response }
        headers[HTTPHeaderName.sessionID] = UUID().uuidString
        return .data(data, headers: headers)
    }

    /// One MCP `Server` per HTTP request: the stateless transport keys in-flight requests by
    /// JSON-RPC id and a long-lived `Server` refuses a second `initialize`, so sharing one
    /// instance breaks reconnecting clients and collides ids across concurrent clients.
    private static func handle(_ request: HTTPRequest, pipeline: StandardValidationPipeline, router: ToolRouter,
                               tools: [Tool], instructions: String?) async -> HTTPResponse {
        let transport = StatelessHTTPServerTransport(validationPipeline: pipeline)
        let server = Server(
            name: "second-cursor",
            version: version,
            instructions: instructions,
            capabilities: .init(tools: .init(listChanged: false))
        )
        await server.withMethodHandler(ListTools.self) { _ in
            ListTools.Result(tools: tools)
        }
        await server.withMethodHandler(CallTool.self) { params in
            await router.call(name: params.name, arguments: params.arguments ?? [:])
        }
        do {
            try await server.start(transport: transport)
        } catch {
            return .error(statusCode: 500, .internalError("MCP server failed to start: \(error.localizedDescription)"))
        }
        let response = await transport.handleRequest(request)
        await server.stop()
        return response
    }
}

private struct APIRequest: Decodable { let tool: String; let args: [String: Value]? }
