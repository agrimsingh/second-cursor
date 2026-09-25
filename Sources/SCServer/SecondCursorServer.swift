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
    static let version = "0.1.0"

    static let instructions = """
        Second Cursor operates macOS apps in the background without moving the user's cursor, \
        stealing focus, or switching Spaces.
        - Work in the background; never ask the user to click or switch apps for you.
        - Prefer element_index actions (from get_window_state) over x/y coordinates.
        - Coordinates are window-local points, which equal screenshot pixels.
        - Everything works in place: windows stay where they are, on any Space, behind any app. Never move the user's windows.
        - Keyboard shortcuts (press_key) are often the fastest, most reliable way to drive pro apps.
        - Custom-drawn apps may stop repainting while on a Space the user is not viewing; trust the accessibility tree over a stale screenshot.
        - Browser tools act only on tabs the agent opened with browser_open.
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
        let router = ToolRouter(desktop: desktop, agentDisplay: agentDisplay, browser: browser, config: config)
        let pipeline = StandardValidationPipeline(validators: [
            OriginValidator.localhost(),
            StaticBearerTokenValidator(token: config.token),
            AcceptHeaderValidator(mode: .jsonOnly),
            ContentTypeValidator(),
            ProtocolVersionValidator(),
        ])
        do {
            try await listener.start(host: Self.host, port: config.port, path: Self.path) { request in
                await Self.handle(request, pipeline: pipeline, router: router)
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

    /// One MCP `Server` per HTTP request: the stateless transport keys in-flight requests by
    /// JSON-RPC id and a long-lived `Server` refuses a second `initialize`, so sharing one
    /// instance breaks reconnecting clients and collides ids across concurrent clients.
    private static func handle(_ request: HTTPRequest, pipeline: StandardValidationPipeline, router: ToolRouter) async -> HTTPResponse {
        let transport = StatelessHTTPServerTransport(validationPipeline: pipeline)
        let server = Server(
            name: "second-cursor",
            version: version,
            instructions: instructions,
            capabilities: .init(tools: .init(listChanged: false))
        )
        await server.withMethodHandler(ListTools.self) { _ in
            ListTools.Result(tools: ToolCatalog.tools)
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
