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
    @Published private(set) var isSendingMedia = false
    @Published private(set) var isLoadingOlderMessages = false
    @Published var connectionStatus = "CNT"
    @Published var errorMessage: String?

    private let api = APIClient()
    private let cacheStore = CacheStore()
    private var cachedMessages: [Int64: [TelegramMessage]] = [:]
    private var pollingTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var cacheSaveTask: Task<Void, Never>?
    private var olderHistoryTask: Task<Void, Never>?
    private var contactActionResetTask: Task<Void, Never>?
    private var exhaustedHistoryChatIDs: Set<Int64> = []
    private var messageIDAliases: [Int64: Int64] = [:]
    private var didBootstrap = false
    private var needsReconciliation = false
    private var lastReconciliationAt = Date.distantPast
    private var lastForegroundCheckAt = Date.distantPast
    private var lastEventID: Int64?
    private var nextLocalMessageID: Int64 = -1

    init() {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ChatDesignPreview") {
            installChatDesignPreview()
            return
        }
        #endif

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
        if lastForegroundCheckAt == .distantPast {
            lastForegroundCheckAt = Date()
            Task { [api] in
                try? await api.triggerTransportCheck()
            }
        }
        do {
            try await loadAuthenticatedState()
            reconnectTask?.cancel()
            reconnectTask = nil
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
            await handleBackgroundError(error)
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
        contactActionResetTask?.cancel()
        contactActionResetTask = nil
        startPolling()
    }

    func appDidBecomeActive() {
        guard didBootstrap,
              Date().timeIntervalSince(lastForegroundCheckAt) >= 10
        else { return }

        lastForegroundCheckAt = Date()
        Task { [weak self] in
            guard let self else { return }
            try? await self.api.triggerTransportCheck()
            if self.authStep == .authenticated {
                self.startPolling()
                await self.reconcileLiveState()
                if let chat = self.selectedChat {
                    await self.markVisibleIncomingRead(chatID: chat.id)
                }
            }
        }
    }

    /// Inserts a local negative-id message synchronously. The status label is
    /// intentionally absent until Telegram returns a real positive message id.
    func sendMessage() {
        guard let chat = selectedChat, !composerText.isEmpty else { return }

        let text = composerText
        let clientRequestID = UUID().uuidString
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
            sendingState: "pending",
            clientRequestID: clientRequestID
        )

        mergeMessage(pending)

        Task { [weak self] in
            guard let self else { return }
            do {
                let sent = try await self.api.sendMessage(
                    chatID: chat.id,
                    text: text,
                    clientRequestID: clientRequestID
                )
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

    func sendMedia(_ upload: TelegramMediaUpload) {
        guard let chat = selectedChat, !isSendingMedia else { return }

        let localID = nextLocalMessageID
        let clientRequestID = UUID().uuidString
        nextLocalMessageID -= 1
        let pending = TelegramMessage(
            id: localID,
            chatID: chat.id,
            isOutgoing: true,
            sentAt: ISO8601DateFormatter().string(from: Date()),
            kind: upload.kind.rawValue,
            text: "",
            media: TelegramMedia(
                kind: upload.kind.rawValue,
                fileID: localID,
                downloadURL: "",
                fileName: upload.fileName,
                mimeType: upload.mimeType,
                size: upload.data.count,
                width: upload.width > 0 ? upload.width : nil,
                height: upload.height > 0 ? upload.height : nil,
                duration: upload.duration > 0 ? upload.duration : nil,
                thumbnailFileID: nil,
                isOpened: true
            ),
            isRead: false,
            sendingState: "pending",
            clientRequestID: clientRequestID
        )

        isSendingMedia = true
        mergeMessage(pending)

        Task { [weak self] in
            guard let self else { return }
            defer { self.isSendingMedia = false }
            do {
                let sent = try await self.api.sendMedia(
                    chatID: chat.id,
                    upload: upload,
                    clientRequestID: clientRequestID
                )
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

    func sendChatAction(_ action: String, progress: Int = 0) {
        guard let chatID = selectedChat?.id else { return }
        Task { [api] in
            try? await api.sendChatAction(
                chatID: chatID,
                action: action,
                progress: progress
            )
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
                await self.handleBackgroundError(error)
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
        needsReconciliation = false
        lastReconciliationAt = Date()
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
        startPolling()
    }

    private func refreshOpenChat(
        _ chat: ChatSummary,
        fetchMessages: Bool = true
    ) async {
        if fetchMessages {
            do {
                let latest = try await api.messages(chatID: chat.id, limit: 30)
                let history = mergeServerHistory(
                    Array(latest.reversed()),
                    withLocalMessagesFor: chat.id
                )
                cachedMessages[chat.id] = history
                if selectedChat?.id == chat.id {
                    messages = history
                }
            } catch {
                await handleBackgroundError(error)
            }
        }

        if let userID = chat.peerUserID {
            do {
                let peer = try await api.user(userID: userID)
                guard selectedChat?.id == chat.id else { return }
                updatePresence(peer.presence)
            } catch {
                await handleBackgroundError(error)
            }
        }

        await markVisibleIncomingRead(chatID: chat.id)
        persistCache()
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
                let event = try await api.nextEvent(
                    activeChatID: selectedChat?.id,
                    afterEventID: lastEventID
                )
                connectionStatus = "RDY"
                if let event {
                    if let eventID = event.eventID {
                        lastEventID = max(lastEventID ?? 0, eventID)
                    }
                    handle(event)
                }
                let reconciliationInterval: TimeInterval = selectedChat == nil
                    ? 30
                    : 15
                if needsReconciliation
                    || Date().timeIntervalSince(lastReconciliationAt)
                        >= reconciliationInterval {
                    await reconcileLiveState()
                }
            } catch is CancellationError {
                return
            } catch {
                connectionStatus = "CNT"
                needsReconciliation = true
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    /// The event cursor replays short network gaps. Reconciliation is the slower
    /// safety net for process restarts and events older than the replay window.
    private func reconcileLiveState() async {
        do {
            let latestChats = try await api.chats()
            chats = latestChats

            if let selectedID = selectedChat?.id,
               let refreshed = latestChats.first(where: { $0.id == selectedID }) {
                selectedChat = refreshed
                let latest = try await api.messages(
                    chatID: refreshed.id,
                    limit: 30
                )
                let history = mergeServerHistory(
                    Array(latest.reversed()),
                    withLocalMessagesFor: refreshed.id
                )
                cachedMessages[refreshed.id] = history
                messages = history
                await markVisibleIncomingRead(chatID: refreshed.id)
            }

            needsReconciliation = false
            lastReconciliationAt = Date()
            connectionStatus = "RDY"
            persistCache()
        } catch is CancellationError {
            return
        } catch {
            needsReconciliation = true
            connectionStatus = "CNT"
        }
    }

    private func mergeMessage(
        _ message: TelegramMessage,
        replacingLocalID: Int64? = nil
    ) {
        var history = cachedMessages[message.chatID] ?? []

        let replacementIndex = replacingLocalID.flatMap { localID in
            history.firstIndex(where: { $0.id == localID })
        } ?? history.firstIndex(where: { $0.id == message.id })
            ?? message.clientRequestID.flatMap { clientRequestID in
                history.firstIndex(where: {
                    $0.clientRequestID == clientRequestID
                })
            }
            ?? compatiblePendingIndex(for: message, in: history)

        if let replacementIndex {
            history[replacementIndex] = message
            history = history.enumerated().compactMap { index, candidate in
                guard index != replacementIndex,
                      isSameLogicalMessage(candidate, as: message)
                else { return candidate }
                return nil
            }
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
                isSameLogicalMessage(server, as: local)
                    || isCompatibleOutgoing(server, with: local)
            }
        }
        var seenIDs = Set<Int64>()
        var seenClientRequestIDs = Set<String>()
        return (retainedOlderHistory + serverHistory + unresolved).filter {
            guard seenIDs.insert($0.id).inserted else { return false }
            guard let clientRequestID = $0.clientRequestID else { return true }
            return seenClientRequestIDs.insert(clientRequestID).inserted
        }
    }

    private func compatiblePendingIndex(
        for message: TelegramMessage,
        in history: [TelegramMessage]
    ) -> Int? {
        guard message.isOutgoing else { return nil }
        return history.indices
            .filter { history[$0].sendingState != "sent" }
            .filter { isCompatibleOutgoing(history[$0], with: message) }
            .min { left, right in
                let messageDate = serverDate(message.sentAt)
                let leftDistance = abs(
                    serverDate(history[left].sentAt).timeIntervalSince(messageDate)
                )
                let rightDistance = abs(
                    serverDate(history[right].sentAt).timeIntervalSince(messageDate)
                )
                return leftDistance < rightDistance
            }
    }

    private func isSameLogicalMessage(
        _ lhs: TelegramMessage,
        as rhs: TelegramMessage
    ) -> Bool {
        if lhs.id == rhs.id { return true }
        guard let leftRequestID = lhs.clientRequestID,
              let rightRequestID = rhs.clientRequestID
        else { return false }
        return leftRequestID == rightRequestID
    }

    private func isCompatibleOutgoing(
        _ lhs: TelegramMessage,
        with rhs: TelegramMessage
    ) -> Bool {
        lhs.isOutgoing
            && rhs.isOutgoing
            && lhs.kind == rhs.kind
            && (lhs.kind != "text" || lhs.text == rhs.text)
            && abs(
                serverDate(lhs.sentAt).timeIntervalSince(serverDate(rhs.sentAt))
            ) < 300
    }

    private func serverDate(_ value: String) -> Date {
        ISO8601DateFormatter().date(from: value) ?? .distantPast
    }

    private func markVisibleIncomingRead(chatID: Int64) async {
        guard selectedChat?.id == chatID,
              let newestIncomingID = (cachedMessages[chatID] ?? [])
                .reversed()
                .first(where: { !$0.isOutgoing && $0.id > 0 })?.id
        else { return }

        do {
            // TDLib advances the inbox read marker through this newest visible
            // message. Sending one ID avoids the API's 100-ID ceiling on long chats.
            try await api.markRead(
                chatID: chatID,
                messageIDs: [newestIncomingID]
            )
        } catch {
            await handleBackgroundError(error)
        }
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
           action.chatID == selectedChat?.id,
           action.senderID != account?.id {
            contactActionResetTask?.cancel()
            withAnimation(.easeInOut(duration: 0.3)) {
                if action.action == "cancel" {
                    restoreSelectedContactPresence()
                } else {
                    contactStatus = "TYP"
                    contactStatusIsActive = true
                    contactActionResetTask = Task { [weak self] in
                        try? await Task.sleep(for: .seconds(6))
                        guard !Task.isCancelled, let self else { return }
                        self.restoreSelectedContactPresence()
                    }
                }
            }
        }
    }

    private func restoreSelectedContactPresence() {
        guard let userID = selectedChat?.peerUserID else {
            contactStatus = selectedChat?.isSavedMessages == true ? "RDY" : "LSR"
            contactStatusIsActive = selectedChat?.isSavedMessages == true
            return
        }
        contactStatus = "ONL"
        contactStatusIsActive = true
        Task { [weak self] in
            guard let self else { return }
            if let peer = try? await self.api.user(userID: userID),
               self.selectedChat?.peerUserID == userID {
                self.updatePresence(peer.presence)
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
        connectionStatus = "CNT"
        needsReconciliation = true
        if isTransientConnectionError(error) {
            scheduleReconnect()
        } else {
            errorMessage = error.localizedDescription
        }
    }

    private func handleBackgroundError(_ error: Error) async {
        if let apiError = error as? APIClientError,
           apiError.statusCode == 401 {
            await handleSessionError(error)
            return
        }
        if isTransientConnectionError(error) {
            connectionStatus = "CNT"
            needsReconciliation = true
            scheduleReconnect()
        }
    }

    private func isTransientConnectionError(_ error: Error) -> Bool {
        if error is URLError { return true }
        guard let apiError = error as? APIClientError,
              let status = apiError.statusCode
        else { return false }
        return status == 408 || status == 429 || status >= 500
    }

    private func scheduleReconnect() {
        guard reconnectTask == nil else { return }
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, let self else { return }
            self.reconnectTask = nil
            await self.connectDevice()
        }
    }

    private var bundledDeviceToken: String? {
        guard let raw = Bundle.main.object(
            forInfoDictionaryKey: "TGDeviceAccessToken"
        ) as? String else { return nil }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard token.count >= 32, !token.contains("$(") else { return nil }
        return token
    }

    #if DEBUG
    private func installChatDesignPreview() {
        let previewChat = ChatSummary(
            id: 42,
            title: "Андрей Петров",
            type: "private",
            unreadCount: 0,
            lastMessage: "Да, это действительно работает",
            lastMessageID: 12,
            lastMessageIsOutgoing: true,
            peerUserID: 77,
            isSavedMessages: false,
            profilePhotoURL: nil,
            lastReadOutboxMessageID: 10
        )
        let rows: [(Bool, String, Bool)] = [
            (false, "А в теории через термукс можно?", true),
            (true, "Потому что это действительно работает", true),
            (true, "Магическим абсолютно образом", true),
            (false, "Го", true),
            (true, "Да, можно", true),
            (true, "Если хочешь, поставим без интерфейса", true),
            (true, "В целом тоже вайб", true),
            (false, "Что за постпанк", true),
            (true, "Новая стадия проекта", true),
            (false, "Я проверил сетевое ограничение", true),
            (true, "Теперь чат работает через VPS", false),
            (true, "И сохраняет позицию при обновлении", false),
        ]
        let previewMessages = rows.enumerated().map { index, row in
            TelegramMessage(
                id: Int64(index + 1),
                chatID: previewChat.id,
                isOutgoing: row.0,
                sentAt: ISO8601DateFormatter().string(from: Date()),
                kind: "text",
                text: row.1,
                isRead: row.2,
                sendingState: "sent"
            )
        }

        account = AccountProfile(
            id: 1,
            displayName: "Lucius P.",
            username: "lucius",
            profilePhotoURL: nil
        )
        chats = [previewChat]
        selectedChat = previewChat
        messages = previewMessages
        cachedMessages[previewChat.id] = previewMessages
        contactStatus = "ONL"
        contactStatusIsActive = true
        connectionStatus = "RDY"
        authStep = .authenticated
        didBootstrap = true
    }
    #endif
}
