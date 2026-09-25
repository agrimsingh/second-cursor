import Foundation
import MCP

/// Rejects requests whose `Authorization` header is not exactly `Bearer <token>`.
///
/// The SDK's `BearerTokenValidator` is built for OAuth resource servers and advertises a
/// metadata URL in its challenge; this server uses one static local token instead.
struct StaticBearerTokenValidator: HTTPRequestValidator {
    let token: String

    func validate(_ request: HTTPRequest, context: HTTPValidationContext) -> HTTPResponse? {
        let header = request.header(HTTPHeaderName.authorization) ?? ""
        let parts = header.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true)
        if parts.count == 2,
           parts[0].caseInsensitiveCompare("Bearer") == .orderedSame,
           constantTimeEquals(parts[1].trimmingCharacters(in: .whitespaces), token) {
            return nil
        }
        return .error(
            statusCode: 401,
            .invalidRequest("Unauthorized: missing or wrong bearer token"),
            extraHeaders: [HTTPHeaderName.wwwAuthenticate: "Bearer"]
        )
    }

    private func constantTimeEquals(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        return zip(x, y).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }
}
