import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    enum AuthStep: Equatable {
        case connecting
        case authenticated
    }

    @Published var authStep: AuthStep = .connecting
    @Published var account: AccountProfile?
    @Published var chats: [ChatSummary] = []
    @Published var selectedChat: ChatSummary?
    @Published var messages: [TelegramMessage] = []
    @Published var composerText = ""
    @Published var contactStatus = "LSR"
    @Published var contactStatusIsActive = false
    @Published var isBusy = false
    @Published private(set) var isLoadingOlderMessages = false
    @Published var connectionStatus = "CNT"
    @Published var errorMessage: String?

    private let api = APIClient()
    private let cacheStore = CacheStore()
    private var cachedMessages: [Int64: [TelegramMessage]] = [:]
    private var pollingTask: Task<Void, Never>?
    private var cacheSaveTask: Task<Void, Never>?
    private var olderHistoryTask: Task<Void, Never>?
    private var exhaustedHistoryChatIDs: Set<Int64> = []
    private var messageIDAliases: [Int64: Int64] = [:]
    private var didBootstrap = false
    private var nextLocalMessageID: Int64 = -1

    init() {
        if let cached = CacheStore.loadSnapshot() {
            restore(cached)
        }
    }

    func bootstrap() async {
        guard !didBootstrap else { return }
        didBootstrap = true
        await connectDevice()
    }

    func connectDevice() async {
        guard !isBusy else { return }
        isBusy = true
        errorMessage = nil
        connectionStatus = "UPD"
        defer { isBusy = false }

        guard let token = bundledDeviceToken else {
            connectionStatus = "CNT"
            errorMessage = "This build is missing its private device credential."
            return
        }

        await api.setBearerToken(token)
        do {
            try await loadAuthenticatedState()
        } catch {
            await handleSessionError(error)
        }
    }

    func refreshChats() async {
        guard authStep == .authenticated else { return }
        do {
            let latestChats = try await api.chats()
            chats = latestChats
            if let selectedID = selectedChat?.id,
               let refreshed = latestChats.first(where: { $0.id == selectedID }) {
                selectedChat = refreshed
            }
            connectionStatus = "RDY"
            persistCache()
        } catch {
            await handleSessionError(error)
        }
    }

    /// Cached history is installed before navigation. On a cold cache miss, the
    /// transition waits for the first 30 messages so the user never sees a lone
    /// top-aligned message followed by a corrective scroll.
    func open(_ chat: ChatSummary) async {
        if let history = cachedMessages[chat.id] {
            present(chat, history: history)
            Task { [weak self] in
                await self?.refreshOpenChat(chat)
            }
            return
        }

        connectionStatus = "UPD"
        do {
            let latest = try await api.messages(chatID: chat.id, limit: 30)
            let history = Array(latest.reversed())
            cachedMessages[chat.id] = history
            persistCache()
            present(chat, history: history)
            Task { [weak self] in
                await self?.refreshOpenChat(chat, fetchMessages: false)
            }
        } catch {
            await handleSessionError(error)
        }
    }

    func closeChat() {
        olderHistoryTask?.cancel()
        olderHistoryTask = nil
        isLoadingOlderMessages = false
        selectedChat = nil
        messages = []
        composerText = ""
        contactStatus = "LSR"
        contactStatusIsActive = false
    }

    /// Inserts a local negative-id message synchronously. The status label is
    /// intentionally absent until Telegram returns a real positive message id.
    func sendMessage() {
        guard let chat = selectedChat, !composerText.isEmpty else { return }

        let text = composerText
        composerText = ""
        let localID = nextLocalMessageID
        nextLocalMessageID -= 1
        let pending = TelegramMessage(
            id: localID,
            chatID: chat.id,
            isOutgoing: true,
            sentAt: ISO8601DateFormatter().string(from: Date()),
            kind: "text",
            text: text,
            isRead: false,
            sendingState: "pending"
        )

        mergeMessage(pending)

        Task { [weak self] in
            guard let self else { return }
            do {
                let sent = try await self.api.sendMessage(chatID: chat.id, text: text)
                withAnimation(.spring(duration: 0.3, bounce: 0)) {
                    self.mergeSendResponse(sent, replacingLocalID: localID)
                }
                await self.refreshChats()
            } catch {
                self.markLocalMessageFailed(localID, chatID: chat.id)
                await self.handleSessionError(error)
            }
        }
    }

    /// Starts fetching the next page once the user reaches the eleventh row
    /// from the oldest currently loaded message. Newly prepended rows keep
    /// stable message IDs, so SwiftUI preserves the visible scroll position.
    func loadOlderMessagesIfNeeded(visibleMessageID: Int64) {
        guard let chat = selectedChat,
              !isLoadingOlderMessages,
              !exhaustedHistoryChatIDs.contains(chat.id),
              let visibleIndex = messages.firstIndex(where: {
                  $0.id == visibleMessageID
              }),
              visibleIndex <= 10,
              let oldestMessageID = messages
                  .lazy
                  .filter({ $0.id > 0 })
                  .map(\.id)
                  .min()
        else { return }

        isLoadingOlderMessages = true
        olderHistoryTask = Task { [weak self] in
            guard let self else { return }
            defer {
                self.isLoadingOlderMessages = false
                self.olderHistoryTask = nil
            }

            do {
                let page = try await self.api.messages(
                    chatID: chat.id,
                    limit: 30,
                    beforeMessageID: oldestMessageID
                )
                guard !Task.isCancelled else { return }
                self.prependOlderHistory(
                    Array(page.reversed()),
                    chatID: chat.id
                )
                if page.isEmpty {
                    self.exhaustedHistoryChatIDs.insert(chat.id)
                }
            } catch is CancellationError {
                return
            } catch {
                self.connectionStatus = "CNT"
                self.errorMessage = error.localizedDescription
            }
        }
    }

    private func restore(_ cached: CachedAppState) {
        account = cached.account
        chats = cached.chats
        cachedMessages = cached.messagesByChat.reduce(into: [:]) { result, item in
            guard let chatID = Int64(item.key) else { return }
            result[chatID] = item.value
        }
        let smallestLocalID = cachedMessages.values
            .flatMap { $0 }
            .map(\.id)
            .filter { $0 < 0 }
            .min()
        nextLocalMessageID = min(-1, (smallestLocalID ?? 0) - 1)
        if account != nil || !chats.isEmpty {
            authStep = .authenticated
            connectionStatus = "UPD"
        }
    }

    private func loadAuthenticatedState() async throws {
        connectionStatus = "UPD"
        async let loadedAccount = api.account()
        async let loadedChats = api.chats()
        let (freshAccount, freshChats) = try await (loadedAccount, loadedChats)

        account = freshAccount
        chats = freshChats
        authStep = .authenticated
        persistCache()

        await preloadMessages(for: freshChats)
        connectionStatus = "RDY"
        persistCache()
        startPolling()
    }

    /// Four requests at a time keeps the one-gigabyte VPS responsive while the
    /// app builds a complete, locally reusable 30-message window for every chat.
    private func preloadMessages(for chats: [ChatSummary]) async {
        let batchSize = 4
        var start = 0

        while start < chats.count {
            let end = min(start + batchSize, chats.count)
            let batch = Array(chats[start..<end])

            await withTaskGroup(of: (Int64, [TelegramMessage]?).self) { group in
                for chat in batch {
                    group.addTask { [api] in
                        do {
                            let latest = try await api.messages(chatID: chat.id, limit: 30)
                            return (chat.id, Array(latest.reversed()))
                        } catch {
                            return (chat.id, nil)
                        }
                    }
                }

                for await (chatID, history) in group {
                    if let history {
                        let merged = mergeServerHistory(
                            history,
                            withLocalMessagesFor: chatID
                        )
                        cachedMessages[chatID] = merged
                        if selectedChat?.id == chatID {
                            messages = merged
                        }
                    }
                }
            }

            persistCache()
            start = end
        }
    }

    private func present(_ chat: ChatSummary, history: [TelegramMessage]) {
        messages = history
        contactStatus = chat.isSavedMessages ? "RDY" : "LSR"
        contactStatusIsActive = false
        selectedChat = chat
    }

    private func refreshOpenChat(
        _ chat: ChatSummary,
        fetchMessages: Bool = true
    ) async {
        do {
            async let latestRequest: [TelegramMessage]? = {
                guard fetchMessages else { return nil }
                return try await api.messages(chatID: chat.id, limit: 30)
            }()
            async let peerRequest: TelegramUserProfile? = {
                guard let userID = chat.peerUserID else { return nil }
                return try await api.user(userID: userID)
            }()

            if let latest = try await latestRequest {
                let history = mergeServerHistory(
                    Array(latest.reversed()),
                    withLocalMessagesFor: chat.id
                )
                cachedMessages[chat.id] = history
                if selectedChat?.id == chat.id {
                    messages = history
                }
            }

            if let peer = try await peerRequest, selectedChat?.id == chat.id {
                updatePresence(peer.presence)
            }

            let incoming = (cachedMessages[chat.id] ?? [])
                .filter { !$0.isOutgoing && $0.id > 0 }
                .map(\.id)
            try await api.markRead(chatID: chat.id, messageIDs: incoming)
            connectionStatus = "RDY"
            persistCache()
        } catch {
            await handleSessionError(error)
        }
    }

    private func startPolling() {
        pollingTask?.cancel()
        pollingTask = Task { [weak self] in
            await self?.pollEvents()
        }
    }

    private func pollEvents() async {
        while !Task.isCancelled, authStep == .authenticated {
            do {
                let event = try await api.nextEvent(chatID: nil)
                connectionStatus = "RDY"
                guard let event else { continue }
                handle(event)
            } catch is CancellationError {
                return
            } catch {
                connectionStatus = "CNT"
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func mergeMessage(
        _ message: TelegramMessage,
        replacingLocalID: Int64? = nil
    ) {
        var history = cachedMessages[message.chatID] ?? []

        if let replacingLocalID,
           let index = history.firstIndex(where: { $0.id == replacingLocalID }) {
            if let existingIndex = history.firstIndex(where: {
                $0.id == message.id
            }), existingIndex != index {
                history[existingIndex] = message
                history.remove(at: index)
            } else {
                history[index] = message
            }
        } else if let index = history.firstIndex(where: { $0.id == message.id }) {
            history[index] = message
        } else if message.isOutgoing,
                  message.id > 0,
                  let pendingIndex = history.firstIndex(where: {
                      $0.isOutgoing
                          && $0.id < 0
                          && $0.sendingState == "pending"
                          && $0.text == message.text
                  }) {
            history[pendingIndex] = message
        } else {
            history.append(message)
        }

        cachedMessages[message.chatID] = history
        if selectedChat?.id == message.chatID {
            messages = history
        }
        persistCache()
    }

    private func mergeSendResponse(
        _ message: TelegramMessage,
        replacingLocalID localID: Int64
    ) {
        if let canonicalID = messageIDAliases[message.id],
           var history = cachedMessages[message.chatID],
           history.contains(where: { $0.id == canonicalID }) {
            history.removeAll { $0.id == localID || $0.id == message.id }
            cachedMessages[message.chatID] = history
            if selectedChat?.id == message.chatID {
                messages = history
            }
            persistCache()
            return
        }
        mergeMessage(message, replacingLocalID: localID)
    }

    private func prependOlderHistory(
        _ olderHistory: [TelegramMessage],
        chatID: Int64
    ) {
        let current = cachedMessages[chatID] ?? []
        let currentIDs = Set(current.map(\.id))
        let newRows = olderHistory.filter { !currentIDs.contains($0.id) }
        guard !newRows.isEmpty else { return }

        let merged = newRows + current
        cachedMessages[chatID] = merged
        if selectedChat?.id == chatID {
            messages = merged
        }
        persistCache()
    }

    private func markLocalMessageFailed(_ id: Int64, chatID: Int64) {
        guard var history = cachedMessages[chatID],
              let index = history.firstIndex(where: { $0.id == id })
        else { return }

        history[index].sendingState = "failed"
        cachedMessages[chatID] = history
        if selectedChat?.id == chatID {
            messages = history
        }
        persistCache()
    }

    private func mergeServerHistory(
        _ serverHistory: [TelegramMessage],
        withLocalMessagesFor chatID: Int64
    ) -> [TelegramMessage] {
        let cachedHistory = cachedMessages[chatID] ?? []
        let oldestServerID = serverHistory
            .lazy
            .filter({ $0.id > 0 })
            .map(\.id)
            .min()
        let retainedOlderHistory = cachedHistory.filter { message in
            guard message.id > 0, let oldestServerID else { return false }
            return message.id < oldestServerID
        }
        let localOnly = cachedHistory.filter {
            $0.id < 0 || $0.sendingState != "sent"
        }
        let unresolved = localOnly.filter { local in
            !serverHistory.contains { server in
                server.isOutgoing
                    && server.text == local.text
                    && abs(
                        serverDate(server.sentAt)
                            .timeIntervalSince(serverDate(local.sentAt))
                    ) < 300
            }
        }
        var seenIDs = Set<Int64>()
        return (retainedOlderHistory + serverHistory + unresolved).filter {
            seenIDs.insert($0.id).inserted
        }
    }

    private func serverDate(_ value: String) -> Date {
        ISO8601DateFormatter().date(from: value) ?? .distantPast
    }

    private func handle(_ event: TelegramEvent) {
        if let message = event.message {
            withAnimation(.easeOut(duration: 0.25)) {
                if let oldMessageID = event.oldMessageID {
                    messageIDAliases[oldMessageID] = message.id
                    mergeMessage(message, replacingLocalID: oldMessageID)
                } else if messageIDAliases[message.id] == nil {
                    mergeMessage(message)
                }
            }
            if selectedChat?.id == message.chatID, !message.isOutgoing {
                Task {
                    try? await api.markRead(
                        chatID: message.chatID,
                        messageIDs: [message.id]
                    )
                }
            }
            Task { await refreshChats() }
        }

        if let receipt = event.receipt, receipt.direction == "outbox" {
            setOutgoingRead(
                chatID: receipt.chatID,
                through: receipt.lastReadMessageID
            )
        }

        if let presence = event.presence,
           presence.userID == selectedChat?.peerUserID {
            withAnimation(.easeInOut(duration: 0.3)) {
                updatePresence(presence)
            }
        }

        if let action = event.action,
           action.chatID == selectedChat?.id {
            withAnimation(.easeInOut(duration: 0.3)) {
                if action.action == "cancel" {
                    contactStatus = "ONL"
                    contactStatusIsActive = true
                } else {
                    contactStatus = "TYP"
                    contactStatusIsActive = true
                }
            }
        }
    }

    private func setOutgoingRead(chatID: Int64, through messageID: Int64) {
        guard var history = cachedMessages[chatID] else { return }
        withAnimation(.easeInOut(duration: 0.3)) {
            for index in history.indices
            where history[index].isOutgoing
                && history[index].id > 0
                && history[index].id <= messageID {
                history[index].isRead = true
            }
            cachedMessages[chatID] = history
            if selectedChat?.id == chatID {
                messages = history
            }
        }
        persistCache()
    }

    private func updatePresence(_ presence: TelegramUserPresence) {
        switch presence.state {
        case "online":
            contactStatus = "ONL"
            contactStatusIsActive = true
        case "recently":
            contactStatus = "LSR"
            contactStatusIsActive = false
        case "offline":
            if let value = presence.lastSeenAt,
               let date = ISO8601DateFormatter().date(from: value) {
                contactStatus = date.formatted(
                    Date.FormatStyle()
                        .hour(.twoDigits(amPM: .omitted))
                        .minute(.twoDigits)
                ).replacingOccurrences(of: ":", with: ".")
            } else {
                contactStatus = "LSR"
            }
            contactStatusIsActive = false
        default:
            contactStatus = "LSR"
            contactStatusIsActive = false
        }
    }

    private func persistCache() {
        let snapshot = CachedAppState(
            account: account,
            chats: chats,
            messagesByChat: cachedMessages.reduce(into: [:]) { result, item in
                result[String(item.key)] = item.value
            }
        )
        cacheSaveTask?.cancel()
        cacheSaveTask = Task { [cacheStore] in
            try? await Task.sleep(for: .milliseconds(80))
            guard !Task.isCancelled else { return }
            await cacheStore.save(snapshot)
        }
    }

    private func handleSessionError(_ error: Error) async {
        if let apiError = error as? APIClientError,
           apiError.statusCode == 401 {
            await api.setBearerToken(nil)
        }
        errorMessage = error.localizedDescription
        connectionStatus = "CNT"
    }

    private var bundledDeviceToken: String? {
        guard let raw = Bundle.main.object(
            forInfoDictionaryKey: "TGDeviceAccessToken"
        ) as? String else { return nil }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard token.count >= 32, !token.contains("$(") else { return nil }
        return token
    }
}
