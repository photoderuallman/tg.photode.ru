import Foundation

struct AccountProfile: Codable, Equatable, Sendable {
    let id: Int64
    let displayName: String
    let username: String?
    let profilePhotoURL: String?

    enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
        case username
        case profilePhotoURL = "profile_photo_url"
    }
}

struct ChatSummary: Codable, Identifiable, Hashable, Sendable {
    let id: Int64
    let title: String
    let type: String
    let unreadCount: Int
    let lastMessage: String?
    let lastMessageID: Int64
    let lastMessageIsOutgoing: Bool
    let peerUserID: Int64?
    let isSavedMessages: Bool
    let profilePhotoURL: String?
    let lastReadOutboxMessageID: Int64

    enum CodingKeys: String, CodingKey {
        case id, title, type
        case unreadCount = "unread_count"
        case lastMessage = "last_message"
        case lastMessageID = "last_message_id"
        case lastMessageIsOutgoing = "last_message_is_outgoing"
        case peerUserID = "peer_user_id"
        case isSavedMessages = "is_saved_messages"
        case profilePhotoURL = "profile_photo_url"
        case lastReadOutboxMessageID = "last_read_outbox_message_id"
    }
}

struct TelegramMessage: Codable, Identifiable, Equatable, Sendable {
    let id: Int64
    let chatID: Int64
    let isOutgoing: Bool
    let sentAt: String
    let kind: String
    let text: String
    var media: TelegramMedia? = nil
    var isRead: Bool
    var sendingState: String
    let clientRequestID: String?

    init(
        id: Int64,
        chatID: Int64,
        isOutgoing: Bool,
        sentAt: String,
        kind: String,
        text: String,
        media: TelegramMedia? = nil,
        isRead: Bool,
        sendingState: String,
        clientRequestID: String? = nil
    ) {
        self.id = id
        self.chatID = chatID
        self.isOutgoing = isOutgoing
        self.sentAt = sentAt
        self.kind = kind
        self.text = text
        self.media = media
        self.isRead = isRead
        self.sendingState = sendingState
        self.clientRequestID = clientRequestID
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, text, media
        case chatID = "chat_id"
        case isOutgoing = "is_outgoing"
        case sentAt = "sent_at"
        case isRead = "is_read"
        case sendingState = "sending_state"
        case clientRequestID = "client_request_id"
    }
}

struct TelegramMedia: Codable, Equatable, Sendable {
    let kind: String
    let fileID: Int64
    let downloadURL: String
    let fileName: String?
    let mimeType: String?
    let size: Int
    let width: Int?
    let height: Int?
    let duration: Int?
    let thumbnailFileID: Int64?
    let isOpened: Bool

    enum CodingKeys: String, CodingKey {
        case kind, size, width, height, duration
        case fileID = "file_id"
        case downloadURL = "download_url"
        case fileName = "file_name"
        case mimeType = "mime_type"
        case thumbnailFileID = "thumbnail_file_id"
        case isOpened = "is_opened"
    }
}

enum TelegramMediaKind: String, Sendable {
    case photo
    case video
    case voiceNote = "voice_note"
    case videoNote = "video_note"
}

struct TelegramMediaUpload: Sendable {
    let data: Data
    let kind: TelegramMediaKind
    let fileName: String
    let mimeType: String
    let duration: Int
    let width: Int
    let height: Int
}

struct TelegramEvent: Codable, Sendable {
    let eventID: Int64?
    let type: String
    let chatID: Int64?
    let message: TelegramMessage?
    let oldMessageID: Int64?
    let presence: TelegramUserPresence?
    let action: TelegramChatActionState?
    let receipt: TelegramReadReceipt?

    enum CodingKeys: String, CodingKey {
        case type, message, presence, action, receipt
        case eventID = "event_id"
        case chatID = "chat_id"
        case oldMessageID = "old_message_id"
    }
}

struct TelegramUserPresence: Codable, Sendable {
    let userID: Int64
    let state: String
    let lastSeenAt: String?

    enum CodingKeys: String, CodingKey {
        case state
        case userID = "user_id"
        case lastSeenAt = "last_seen_at"
    }
}

struct TelegramUserProfile: Codable, Sendable {
    let id: Int64
    let displayName: String
    let username: String?
    let presence: TelegramUserPresence

    enum CodingKeys: String, CodingKey {
        case id, username, presence
        case displayName = "display_name"
    }
}

struct TelegramChatActionState: Codable, Sendable {
    let chatID: Int64
    let senderID: Int64?
    let action: String

    enum CodingKeys: String, CodingKey {
        case action
        case chatID = "chat_id"
        case senderID = "sender_id"
    }
}

struct TelegramReadReceipt: Codable, Sendable {
    let chatID: Int64
    let direction: String
    let lastReadMessageID: Int64

    enum CodingKeys: String, CodingKey {
        case direction
        case chatID = "chat_id"
        case lastReadMessageID = "last_read_message_id"
    }
}

struct APIErrorDetail: Codable, Sendable {
    let code: String
    let message: String
}

struct APIErrorEnvelope: Codable, Sendable {
    let detail: APIErrorDetail
}

struct SendMessageRequest: Codable, Sendable {
    let text: String
    let clientRequestID: String

    enum CodingKeys: String, CodingKey {
        case text
        case clientRequestID = "client_request_id"
    }
}

struct ReadMessagesRequest: Codable, Sendable {
    let messageIDs: [Int64]

    enum CodingKeys: String, CodingKey {
        case messageIDs = "message_ids"
    }
}

struct ChatActionRequest: Codable, Sendable {
    let action: String
    let progress: Int
}
