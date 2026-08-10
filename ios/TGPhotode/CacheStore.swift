import Foundation

struct CachedAppState: Codable, Sendable {
    var account: AccountProfile?
    var chats: [ChatSummary]
    var messagesByChat: [String: [TelegramMessage]]

    static let empty = CachedAppState(
        account: nil,
        chats: [],
        messagesByChat: [:]
    )
}

actor CacheStore {
    private let fileURL: URL
    private let encoder = JSONEncoder()

    init() {
        fileURL = Self.cacheFileURL()
    }

    static func loadSnapshot() -> CachedAppState? {
        guard let data = try? Data(contentsOf: cacheFileURL()) else { return nil }
        return try? JSONDecoder().decode(CachedAppState.self, from: data)
    }

    private static func cacheFileURL() -> URL {
        let baseURL = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!
        let directoryURL = baseURL.appendingPathComponent(
            "TGPhotode",
            isDirectory: true
        )
        try? FileManager.default.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true
        )
        return directoryURL.appendingPathComponent("session-cache.json")
    }

    func save(_ state: CachedAppState) {
        guard let data = try? encoder.encode(state) else { return }
        do {
            try data.write(to: fileURL, options: .atomic)
            try? FileManager.default.setAttributes(
                [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
                ofItemAtPath: fileURL.path
            )
        } catch {
            // A cache failure must never block Telegram messaging.
        }
    }
}
