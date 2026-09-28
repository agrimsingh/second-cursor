import Foundation
import MCP
import NIOCore
import NIOHTTP1
import NIOPosix

/// Minimal HTTP/1.1 listener that hands requests for one path to an async MCP handler.
final class HTTPListener: @unchecked Sendable {
    typealias Route = @Sendable (HTTPRequest) async -> HTTPResponse

    private let group = MultiThreadedEventLoopGroup(numberOfThreads: 2)
    private var channel: Channel?
    private let childrenLock = NSLock()
    private var children: [ObjectIdentifier: Channel] = [:]
    var activeConnectionCount: Int {
        childrenLock.lock()
        defer { childrenLock.unlock() }
        return children.count
    }

    private func remember(_ channel: Channel) {
        let id = ObjectIdentifier(channel)
        childrenLock.lock()
        children[id] = channel
        childrenLock.unlock()
        channel.closeFuture.whenComplete { [weak self] _ in
            guard let self else { return }
            self.childrenLock.lock()
            self.children[id] = nil
            self.childrenLock.unlock()
        }
    }

    private func drain() -> [Channel] {
        childrenLock.lock()
        defer { childrenLock.unlock() }
        let all = Array(children.values)
        children.removeAll()
        return all
    }

    func start(host: String, port: Int, path: String, route: @escaping Route) async throws {
        let bootstrap = ServerBootstrap(group: group)
            .serverChannelOption(ChannelOptions.backlog, value: 64)
            // Lets a relaunch bind while connections from the previous run sit in TIME_WAIT.
            .serverChannelOption(ChannelOptions.socketOption(.so_reuseaddr), value: 1)
            .childChannelOption(ChannelOptions.socketOption(.tcp_nodelay), value: 1)
            .childChannelInitializer { channel in
                self.remember(channel)
                // Pipelining assistance stops reading while a response is pending, which hides a client that hung up
                // (ClientRequest). Clients send one request per connection at a time.
                return channel.pipeline.configureHTTPServerPipeline(withPipeliningAssistance: false).flatMap {
                    channel.pipeline.addHandler(MCPHTTPHandler(path: path, route: route))
                }
            }
        channel = try await bootstrap.bind(host: host, port: port).get()
    }

    func stop() async {
        try? await channel?.close()
        channel = nil
        for child in drain() { try? await child.close() }
        try? await group.shutdownGracefully()
    }
}

/// The HTTP request a tool call arrived in: which client sent it and its JSON-RPC id, and whether the client is still
/// waiting for the answer. A client that gives up closes the connection before the response is written.
final class ClientRequest: @unchecked Sendable {
    @TaskLocal static var current: ClientRequest?

    /// The client's `Mcp-Session-Id` (issued on initialize) with the JSON-RPC id: request ids are unique only
    /// within one client, and a client without the header shares a scope with every other such client.
    let id: String?
    private let lock = NSLock()
    private var gone = false
    private var handlers: [@Sendable () -> Void] = []

    init(id: String?) {
        self.id = id
    }

    static func key(client: String?, id: Any?) -> String? {
        id.map { "\(client ?? "")\u{0}\($0)" }
    }

    static func client(_ headers: [String: String]) -> String? {
        headers.first { $0.key.caseInsensitiveCompare("Mcp-Session-Id") == .orderedSame }?.value
    }

    /// Runs `handler` once the client has gone, at once if it already has.
    func onGone(_ handler: @escaping @Sendable () -> Void) {
        let now = lock.withLock {
            if !gone { handlers.append(handler) }
            return gone
        }
        if now { handler() }
    }

    fileprivate func clientLeft() {
        let pending = lock.withLock {
            defer { handlers = [] }
            gone = true
            return handlers
        }
        pending.forEach { $0() }
    }
}

private final class MCPHTTPHandler: ChannelInboundHandler {
    typealias InboundIn = HTTPServerRequestPart
    typealias OutboundOut = HTTPServerResponsePart

    private static let maxBodyBytes = 16 * 1024 * 1024

    private let path: String
    private let route: HTTPListener.Route
    private var head: HTTPRequestHead?
    private var body = ByteBuffer()
    private var bodyTooLarge = false
    private var inFlight: ClientRequest?

    init(path: String, route: @escaping HTTPListener.Route) {
        self.path = path
        self.route = route
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        switch unwrapInboundIn(data) {
        case .head(let requestHead):
            head = requestHead
            body.clear()
            bodyTooLarge = false
        case .body(var chunk):
            if body.readableBytes + chunk.readableBytes > Self.maxBodyBytes {
                bodyTooLarge = true
            } else {
                body.writeBuffer(&chunk)
            }
        case .end:
            guard let head else { return }
            self.head = nil
            respond(to: head, context: context)
        }
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        context.close(promise: nil)
    }

    func channelInactive(context: ChannelHandlerContext) {
        inFlight?.clientLeft()
        inFlight = nil
        context.fireChannelInactive()
    }

    private func respond(to head: HTTPRequestHead, context: ChannelHandlerContext) {
        let requestPath = String(head.uri.split(separator: "?", maxSplits: 1).first ?? "")
        let loop = context.eventLoop
        let boundContext = NIOLoopBound(context, eventLoop: loop)

        guard requestPath == path || requestPath == "/api" || requestPath == "/turn-ended" else {
            Self.write(.error(statusCode: 404, .invalidRequest("Not Found")), for: head, to: context)
            return
        }
        guard !bodyTooLarge else {
            Self.write(.error(statusCode: 413, .invalidRequest("Request body too large")), for: head, to: context)
            return
        }

        var headers: [String: String] = [:]
        for (name, value) in head.headers {
            headers[name] = headers[name].map { "\($0), \(value)" } ?? value
        }
        let request = HTTPRequest(
            method: head.method.rawValue,
            headers: headers,
            body: body.readableBytes > 0 ? Data(body.readableBytesView) : nil,
            path: requestPath
        )
        let route = self.route
        let id = request.body.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }?["id"]
        let client = ClientRequest(id: ClientRequest.key(client: ClientRequest.client(headers), id: id))
        inFlight = client
        let boundSelf = NIOLoopBound(self, eventLoop: loop)
        Task {
            let response = await ClientRequest.$current.withValue(client) { await route(request) }
            loop.execute {
                if boundSelf.value.inFlight === client { boundSelf.value.inFlight = nil }
                Self.write(response, for: head, to: boundContext.value)
            }
        }
    }

    private static func write(_ response: HTTPResponse, for request: HTTPRequestHead, to context: ChannelHandlerContext) {
        let body = response.bodyData ?? Data()
        var head = HTTPResponseHead(version: request.version, status: HTTPResponseStatus(statusCode: response.statusCode))
        for (name, value) in response.headers {
            head.headers.replaceOrAdd(name: name, value: value)
        }
        head.headers.replaceOrAdd(name: "Content-Length", value: String(body.count))
        if !request.isKeepAlive {
            head.headers.replaceOrAdd(name: "Connection", value: "close")
        }

        context.write(NIOAny(HTTPServerResponsePart.head(head)), promise: nil)
        if !body.isEmpty {
            context.write(NIOAny(HTTPServerResponsePart.body(.byteBuffer(ByteBuffer(bytes: body)))), promise: nil)
        }
        let done = context.writeAndFlush(NIOAny(HTTPServerResponsePart.end(nil)))
        if !request.isKeepAlive {
            let channel = context.channel
            done.whenComplete { _ in channel.close(promise: nil) }
        }
    }
}
