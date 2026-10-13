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
    static let version = "0.3.0"

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
        - For a web page, start with browser_open: it picks the Chrome profile the user is in, so status and browser_instances are not needed first.
        - Every turn costs seconds, so make fewer calls: browser_open and browser actions return the tab's outline or a diff, so read it instead of calling browser_snapshot; read text and prices from it, not from screenshots; put steps you already know in one batch with tab_id; run a multi-step web flow (a form, a search with filters, collecting results) as one browser_repl call, passing it the tab_id you already have.
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
                return await Self.handleMCP(request, token: config.token, pipeline: pipeline,
                    router: router, tools: tools, instructions: instructions)
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
        var session = ClientRequest.client(request.headers) ?? "legacy"
        if session != "legacy" {
            do { try router.sessions.validateTransport(session) }
            catch Sessions.TransportValidationError.unknownOrClosed {
                return .error(statusCode: 404, .invalidRequest("MCP session is unknown or closed"))
            } catch {
                return .error(statusCode: 500, .internalError("Could not renew MCP session; keep its ID and restore journal access"))
            }
        }
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
        do { try router.sessions.validate(session) }
        catch { return .error(statusCode: 404, .invalidRequest("Session is unknown or closed")) }
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

    /// HTTP connections are short-lived; the MCP header is the durable runtime ownership boundary.
    /// An explicit tool session is a deliberate host override authorized by the local bearer token and
    /// the opaque session ID. It is never substituted for or automatically closed with the transport.
    private static func handleMCP(_ request: HTTPRequest, token: String,
                                  pipeline: StandardValidationPipeline, router: ToolRouter,
                                  tools: [Tool], instructions: String?) async -> HTTPResponse {
        let context = HTTPValidationContext(httpMethod: request.method)
        for validator: any HTTPRequestValidator in [OriginValidator.localhost(), StaticBearerTokenValidator(token: token)] {
            if let refusal = validator.validate(request, context: context) { return refusal }
        }
        let body = request.body.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
        let initializing = body?["method"] as? String == "initialize" && request.method == "POST"
        let existing = ClientRequest.client(request.headers)
        if let existing {
            do { try router.sessions.validateTransport(existing) }
            catch Sessions.TransportValidationError.unknownOrClosed {
                return .error(statusCode: 404, .invalidRequest(
                    "MCP session is unknown or closed; initialize a new session explicitly. Do not retry an uncertain mutation."))
            } catch {
                return .error(statusCode: 500, .internalError("Could not renew MCP session; keep its ID and restore journal access"))
            }
        } else if !initializing {
            return .error(statusCode: 400, .invalidRequest("Mcp-Session-Id is required; initialize first"))
        }
        if request.method == "DELETE", let existing {
            do {
                try await router.closeSession(session: existing)
                return .ok()
            } catch {
                return .error(statusCode: 500, .internalError("Could not close MCP session"))
            }
        }
        // Let MCP validate initialization before allocating a durable session. A malformed or unsupported
        // initialize must not leak an unreachable session on disk.
        Self.forwardCancellation(request, token: token, router: router)
        let response = await handle(request, pipeline: pipeline, router: router, tools: tools,
                                    instructions: instructions, session: existing)
        guard initializing, case .data(let data, var headers) = response,
              let reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], reply["result"] != nil
        else { return response }
        do {
            headers[HTTPHeaderName.sessionID] = try existing ?? router.sessions.create(transport: true)
            return .data(data, headers: headers)
        } catch {
            return .error(statusCode: 500, .internalError("Could not persist MCP session; no session was created"))
        }
    }

    /// One MCP `Server` per HTTP request: the stateless transport keys in-flight requests by
    /// JSON-RPC id and a long-lived `Server` refuses a second `initialize`, so sharing one
    /// instance breaks reconnecting clients and collides ids across concurrent clients.
    private static func handle(_ request: HTTPRequest, pipeline: StandardValidationPipeline, router: ToolRouter,
                               tools: [Tool], instructions: String?, session: String?) async -> HTTPResponse {
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
            guard let session else {
                return CallTool.Result(content: [.text(text: "Initialize an MCP session before calling tools", annotations: nil, _meta: nil)], isError: true)
            }
            var arguments = params.arguments ?? [:]
            if let explicit = arguments["session_id"], !explicit.isNull {
                guard let id = explicit.stringValue, !id.isEmpty, id != "legacy" else {
                    return CallTool.Result(content: [.text(text: "session_id must identify an explicit task session; legacy is reserved for /api", annotations: nil, _meta: nil)], isError: true)
                }
            } else {
                arguments["session_id"] = .string(session)
            }
            return await router.call(name: params.name, arguments: arguments)
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
