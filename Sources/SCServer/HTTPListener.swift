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

    func start(host: String, port: Int, path: String, route: @escaping Route) async throws {
        let bootstrap = ServerBootstrap(group: group)
            .serverChannelOption(ChannelOptions.backlog, value: 64)
            // Lets a relaunch bind while connections from the previous run sit in TIME_WAIT.
            .serverChannelOption(ChannelOptions.socketOption(.so_reuseaddr), value: 1)
            .childChannelOption(ChannelOptions.socketOption(.tcp_nodelay), value: 1)
            .childChannelInitializer { channel in
                channel.pipeline.configureHTTPServerPipeline().flatMap {
                    channel.pipeline.addHandler(MCPHTTPHandler(path: path, route: route))
                }
            }
        channel = try await bootstrap.bind(host: host, port: port).get()
    }

    func stop() async {
        try? await channel?.close()
        channel = nil
        try? await group.shutdownGracefully()
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

    private func respond(to head: HTTPRequestHead, context: ChannelHandlerContext) {
        let requestPath = String(head.uri.split(separator: "?", maxSplits: 1).first ?? "")
        let loop = context.eventLoop
        let boundContext = NIOLoopBound(context, eventLoop: loop)

        guard requestPath == path else {
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
        Task {
            let response = await route(request)
            loop.execute { Self.write(response, for: head, to: boundContext.value) }
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
