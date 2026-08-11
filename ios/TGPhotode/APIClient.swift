import Foundation

final class DomainOnlySessionDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private static let allowedHost = "photode.ru"

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard request.url?.scheme == "https",
              request.url?.host?.lowercased() == Self.allowedHost
        else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

actor APIClient {
    private let relayURL = URL(string: "https://photode.ru/tg/api/index.php")!
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let session: URLSession
    private var bearerToken: String?

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 30
        // Media crosses a shared-host relay before reaching the private VPS.
        // Give a 100 MB upload enough time on constrained mobile networks.
        configuration.timeoutIntervalForResource = 300
        configuration.httpAdditionalHeaders = ["Accept": "application/json"]
        session = URLSession(
            configuration: configuration,
            delegate: DomainOnlySessionDelegate(),
            delegateQueue: nil
        )
    }

    func setBearerToken(_ token: String?) {
        bearerToken = token
    }

    func account() async throws -> AccountProfile {
        try await request(path: "/api/telegram/me")
    }

    func chats(limit: Int = 100) async throws -> [ChatSummary] {
        try await request(
            path: "/api/chats",
            query: [URLQueryItem(name: "limit", value: String(limit))]
        )
    }

    func messages(
        chatID: Int64,
        limit: Int = 50,
        beforeMessageID: Int64? = nil
    ) async throws -> [TelegramMessage] {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let beforeMessageID {
            query.append(
                URLQueryItem(
                    name: "before_message_id",
                    value: String(beforeMessageID)
                )
            )
        }
        return try await request(
            path: "/api/chats/\(chatID)/messages",
            query: query
        )
    }

    func user(userID: Int64) async throws -> TelegramUserProfile {
        try await request(path: "/api/users/\(userID)")
    }

    func sendMessage(
        chatID: Int64,
        text: String,
        clientRequestID: String
    ) async throws -> TelegramMessage {
        try await request(
            path: "/api/chats/\(chatID)/messages",
            method: "POST",
            body: SendMessageRequest(
                text: text,
                clientRequestID: clientRequestID
            )
        )
    }

    func sendMedia(
        chatID: Int64,
        upload: TelegramMediaUpload,
        clientRequestID: String
    ) async throws -> TelegramMessage {
        let boundary = "TGPhotode-\(UUID().uuidString)"
        var body = Data()

        body.appendMultipartField(
            named: "client_request_id",
            value: clientRequestID,
            boundary: boundary
        )
        body.appendMultipartField(
            named: "kind",
            value: upload.kind.rawValue,
            boundary: boundary
        )
        body.appendMultipartField(
            named: "duration",
            value: String(upload.duration),
            boundary: boundary
        )
        body.appendMultipartField(
            named: "width",
            value: String(upload.width),
            boundary: boundary
        )
        body.appendMultipartField(
            named: "height",
            value: String(upload.height),
            boundary: boundary
        )
        body.appendMultipartFile(
            named: "file",
            fileName: upload.fileName,
            mimeType: upload.mimeType,
            data: upload.data,
            boundary: boundary
        )
        body.append("--\(boundary)--\r\n".utf8Data)

        let (data, _) = try await responseData(
            path: "/api/chats/\(chatID)/media",
            method: "POST",
            body: body,
            contentType: "multipart/form-data; boundary=\(boundary)",
            authenticated: true
        )
        return try decoder.decode(TelegramMessage.self, from: data)
    }

    func sendChatAction(
        chatID: Int64,
        action: String,
        progress: Int = 0
    ) async throws {
        let _: TelegramChatActionState = try await request(
            path: "/api/chats/\(chatID)/actions",
            method: "POST",
            body: ChatActionRequest(action: action, progress: progress)
        )
    }

    func markRead(chatID: Int64, messageIDs: [Int64]) async throws {
        guard !messageIDs.isEmpty else { return }
        let _: EmptyReadResponse = try await request(
            path: "/api/chats/\(chatID)/read",
            method: "POST",
            body: ReadMessagesRequest(messageIDs: messageIDs)
        )
    }

    func nextEvent(
        activeChatID: Int64?,
        afterEventID: Int64?
    ) async throws -> TelegramEvent? {
        var query = [URLQueryItem(name: "timeout_seconds", value: "8")]
        if let activeChatID {
            query.append(
                URLQueryItem(
                    name: "active_chat_id",
                    value: String(activeChatID)
                )
            )
        }
        if let afterEventID {
            query.append(
                URLQueryItem(
                    name: "after_event_id",
                    value: String(afterEventID)
                )
            )
        }
        let (data, response) = try await responseData(
            path: "/api/events/next",
            query: query,
            authenticated: true
        )
        if response.statusCode == 204 { return nil }
        return try decoder.decode(TelegramEvent.self, from: data)
    }

    func triggerTransportCheck() async throws {
        let _: TransportCheckResponse = try await request(
            path: "/api/transport/check",
            method: "POST",
            body: EmptyTransportCheckRequest()
        )
    }

    private func request<Response: Decodable & Sendable>(
        path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        authenticated: Bool = true
    ) async throws -> Response {
        let (data, _) = try await responseData(
            path: path,
            method: method,
            query: query,
            authenticated: authenticated
        )
        return try decoder.decode(Response.self, from: data)
    }

    private func request<Response: Decodable & Sendable, Body: Encodable>(
        path: String,
        method: String,
        query: [URLQueryItem] = [],
        body: Body,
        authenticated: Bool = true
    ) async throws -> Response {
        let payload = try encoder.encode(body)
        let (data, _) = try await responseData(
            path: path,
            method: method,
            query: query,
            body: payload,
            authenticated: authenticated
        )
        return try decoder.decode(Response.self, from: data)
    }

    private func responseData(
        path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: Data? = nil,
        contentType: String? = nil,
        authenticated: Bool
    ) async throws -> (Data, HTTPURLResponse) {
        var components = URLComponents(url: relayURL, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "_path", value: path)] + query
        guard let url = components.url,
              url.scheme == "https",
              url.host?.lowercased() == "photode.ru"
        else {
            throw APIClientError.blockedOrigin
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil {
            request.setValue(
                contentType ?? "application/json",
                forHTTPHeaderField: "Content-Type"
            )
        }
        if authenticated, let bearerToken {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }

        let (data, rawResponse) = try await session.data(for: request)
        guard let response = rawResponse as? HTTPURLResponse else {
            throw APIClientError.invalidResponse
        }
        guard 200..<300 ~= response.statusCode else {
            let envelope = try? decoder.decode(APIErrorEnvelope.self, from: data)
            throw APIClientError.server(
                envelope?.detail.message ?? "Request failed.",
                response.statusCode,
                code: envelope?.detail.code
            )
        }
        return (data, response)
    }
}

private extension String {
    var utf8Data: Data { Data(utf8) }
}

private extension Data {
    mutating func appendMultipartField(
        named name: String,
        value: String,
        boundary: String
    ) {
        append("--\(boundary)\r\n".utf8Data)
        append(
            "Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n"
                .utf8Data
        )
        append(value.utf8Data)
        append("\r\n".utf8Data)
    }

    mutating func appendMultipartFile(
        named name: String,
        fileName: String,
        mimeType: String,
        data: Data,
        boundary: String
    ) {
        append("--\(boundary)\r\n".utf8Data)
        append(
            "Content-Disposition: form-data; name=\"\(name)\"; "
                .utf8Data
        )
        append("filename=\"\(fileName)\"\r\n".utf8Data)
        append("Content-Type: \(mimeType)\r\n\r\n".utf8Data)
        append(data)
        append("\r\n".utf8Data)
    }
}

struct EmptyReadResponse: Codable, Sendable {}
struct EmptyTransportCheckRequest: Codable, Sendable {}
struct TransportCheckResponse: Codable, Sendable {
    let accepted: Bool
}

enum APIClientError: LocalizedError, Sendable {
    case blockedOrigin
    case invalidResponse
    case server(String, Int, code: String? = nil)

    var errorDescription: String? {
        switch self {
        case .blockedOrigin:
            "The app blocked a connection outside photode.ru."
        case .invalidResponse:
            "photode.ru returned an invalid response."
        case let .server(message, _, _):
            message
        }
    }

    var statusCode: Int? {
        if case let .server(_, status, _) = self { return status }
        return nil
    }
}
