import PhotosUI
import SwiftUI
import UniformTypeIdentifiers
import UIKit

struct ChatView: View {
    @ObservedObject var model: AppModel
    let chat: ChatSummary

    @FocusState private var composerFocused: Bool
    @State private var didEstablishInitialPosition = false
    @State private var isClosing = false
    @State private var isAtBottom = true
    @State private var isRecordingVideoNote = ProcessInfo.processInfo.arguments
        .contains("-ChatRecordingPreview")
    @State private var recordingStartedAt: Date? = ProcessInfo.processInfo
        .arguments.contains("-ChatRecordingPreview") ? Date() : nil
    @State private var recordingElapsed: TimeInterval = 0
    @State private var selectedMediaItem: PhotosPickerItem?
    @State private var isFinishingVideoNote = false
    @State private var keyboardIsVisible = false
    @State private var lastTypingSentAt = Date.distantPast
    @State private var typingCancelTask: Task<Void, Never>?
    @State private var edgeDragOffset: CGFloat = 0
    @StateObject private var videoRecorder = VideoNoteRecorder()
    @Namespace private var videoNoteNamespace

    private let bottomAnchorID = "chat-bottom-anchor"

    var body: some View {
        GeometryReader { geometry in
            let gridWidth = max(
                geometry.size.width - (PhotodeMetrics.messageInset * 2),
                0
            )
            let columnWidth = gridWidth / 8

            ZStack(alignment: .top) {
                messageList(
                    columnWidth: columnWidth,
                    viewportHeight: geometry.size.height
                )
                .opacity(isRecordingVideoNote ? 0.24 : 1)
                .animation(
                    .easeInOut(duration: 0.2),
                    value: isRecordingVideoNote
                )

                if isRecordingVideoNote {
                    recordingPreview(in: geometry.size)
                        .transition(.opacity)
                        .zIndex(2)
                }

                chatHeader
                    .padding(.horizontal, PhotodeMetrics.screenInset)
                    .padding(.top, 8)
                    .zIndex(3)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if isRecordingVideoNote {
                recordingToolbar
            } else {
                composer
            }
        }
        .background(Color.photodeBackground)
        .offset(x: edgeDragOffset)
        .contentShape(Rectangle())
        .simultaneousGesture(edgeBackGesture)
        .animation(
            .spring(duration: 0.3, bounce: 0),
            value: isRecordingVideoNote
        )
        .onChange(of: selectedMediaItem) { _, item in
            guard let item else { return }
            Task { await sendPickedMedia(item) }
        }
        .onChange(of: model.composerText) { _, text in
            updateTypingState(for: text)
        }
        .onReceive(
            NotificationCenter.default.publisher(
                for: UIResponder.keyboardWillShowNotification
            )
        ) { _ in
            keyboardIsVisible = true
        }
        .onReceive(
            NotificationCenter.default.publisher(
                for: UIResponder.keyboardWillHideNotification
            )
        ) { _ in
            keyboardIsVisible = false
        }
        .onDisappear {
            typingCancelTask?.cancel()
            model.sendChatAction("cancel")
        }
    }

    private var chatHeader: some View {
        PhotodeGlassGroup {
            HStack(spacing: PhotodeMetrics.glassSpacing) {
                Button(action: dismissKeyboardThenClose) {
                    HStack(spacing: 12) {
                        AccentBowl(seed: chat.id)

                        Text(chat.isSavedMessages ? "Saved Messages" : chat.title)
                            .lineLimit(1)
                    }
                    .photodeHeaderTypography()
                    .foregroundStyle(
                        !showsContactStatus || model.contactStatusIsActive
                            ? Color.photodeActive
                            : Color.photodeDisabled
                    )
                    .padding(.horizontal, PhotodeMetrics.glassContentInset)
                    .frame(height: PhotodeMetrics.glassControlHeight)
                    .contentShape(Capsule())
                    .photodeGlassCapsule(interactive: true)
                }
                .buttonStyle(PhotodePressButtonStyle())
                .frame(minHeight: PhotodeMetrics.minimumHitArea)
                .disabled(isClosing)
                .accessibilityLabel("Back to chats")

                Spacer(minLength: 12)

                if showsContactStatus {
                    Text(model.contactStatus)
                        .photodeHeaderTypography()
                        .multilineTextAlignment(.center)
                        .foregroundStyle(
                            model.contactStatusIsActive
                                ? Color.photodeActive
                                : Color.photodeDisabled
                        )
                        .contentTransition(.numericText())
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .monospacedDigit()
                        .padding(
                            .horizontal,
                            PhotodeMetrics.glassContentInset
                        )
                        .frame(height: PhotodeMetrics.glassControlHeight)
                        .photodeGlassCapsule()
                        .accessibilityLabel("Contact status")
                        .accessibilityValue(model.contactStatus)
                }
            }
        }
    }

    private var showsContactStatus: Bool {
        guard !chat.isSavedMessages else { return false }
        return chat.type == "private" || chat.type == "secret"
    }

    private func recordingPreview(in size: CGSize) -> some View {
        let diameter = min(size.width - 72, 330)

        return VStack(spacing: 0) {
            Spacer(minLength: 82)

            ZStack {
                Color.photodeDisabled.opacity(0.55)

                VideoNotePreview(
                    session: videoRecorder.session,
                    mirrorsVideo: videoRecorder.cameraPosition == .front
                )
                .opacity(videoRecorder.state == .recording ? 1 : 0)

                if videoRecorder.state == .preparing {
                    ProgressView()
                        .tint(Color.photodeActive)
                }
            }
                .frame(width: diameter, height: diameter)
                .clipShape(Circle())
                .overlay {
                    Circle()
                        .stroke(Color.white.opacity(0.1), lineWidth: 1)
                }
                .matchedGeometryEffect(
                    id: "video-note",
                    in: videoNoteNamespace
                )
                .accessibilityLabel("Video message preview")

            Spacer(minLength: 80)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func messageList(
        columnWidth: CGFloat,
        viewportHeight: CGFloat
    ) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(
                    alignment: .leading,
                    spacing: chat.type == "channel"
                        ? PhotodeMetrics.channelPostSpacing
                        : PhotodeMetrics.messageGroupSpacing
                ) {
                    if chat.type == "channel" {
                        ForEach(model.messages) { message in
                            HStack(alignment: .top, spacing: 0) {
                                Color.clear
                                    .frame(width: columnWidth)

                                messageBody(message)
                                    .frame(
                                        width: columnWidth * 7,
                                        alignment: .leading
                                    )
                            }
                        }
                    } else {
                        ForEach(messageGroups) { group in
                            HStack(alignment: .top, spacing: 0) {
                                Text(group.label)
                                    .photodeMessageTypography()
                                    .foregroundStyle(group.labelColor)
                                    .frame(
                                        width: columnWidth,
                                        alignment: .leading
                                    )
                                    .transition(
                                        .scale(scale: 0.25)
                                            .combined(with: .opacity)
                                    )

                                VStack(
                                    alignment: .leading,
                                    spacing: PhotodeMetrics.messageSpacing
                                ) {
                                    ForEach(group.messages) { message in
                                        messageBody(message)
                                    }
                                }
                                .frame(
                                    width: columnWidth * 7,
                                    alignment: .leading
                                )
                            }
                        }
                    }

                    Color.clear
                        .frame(height: 1)
                        .id(bottomAnchorID)
                        .onAppear { isAtBottom = true }
                        .onDisappear { isAtBottom = false }
                }
                .padding(.horizontal, PhotodeMetrics.messageInset)
                .padding(.top, 68)
                .padding(.bottom, 8)
            }
            .scrollIndicators(.hidden)
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .photodeChatScrollEdges()
            .onAppear {
                Task { @MainActor in
                    await Task.yield()
                    didEstablishInitialPosition = true
                }
            }
            .onChange(of: model.messages.last?.id) { oldID, newID in
                guard didEstablishInitialPosition,
                      let newID,
                      newID != oldID,
                      isAtBottom || model.messages.last?.isOutgoing == true
                else { return }
                scrollToBottom(using: proxy, animated: true)
            }
            .onChange(of: composerFocused) { _, focused in
                guard focused else { return }
                Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(220))
                    scrollToBottom(using: proxy, animated: true)
                }
            }
            .onChange(of: viewportHeight) { _, _ in
                guard composerFocused else { return }
                scrollToBottom(using: proxy, animated: true)
            }
        }
    }

    private func messageBody(_ message: TelegramMessage) -> some View {
        Text(messageText(message))
            .photodeMessageTypography()
            .foregroundStyle(Color.photodeActive)
            .frame(maxWidth: .infinity, alignment: .leading)
            .id(message.id)
            .transition(
                .opacity.combined(with: .move(edge: .bottom))
            )
            .onAppear {
                model.loadOlderMessagesIfNeeded(
                    visibleMessageID: message.id
                )
            }
    }

    private var composer: some View {
        let mediaIsDisabled = model.isSendingMedia

        return PhotodeGlassGroup {
            HStack(alignment: .bottom, spacing: PhotodeMetrics.glassSpacing) {
                PhotosPicker(
                    selection: $selectedMediaItem,
                    matching: .any(of: [.images, .videos]),
                    preferredItemEncoding: .automatic
                ) {
                    MediaPickerGlassLabel(isDisabled: mediaIsDisabled)
                }
                .buttonStyle(PhotodePressButtonStyle())
                .frame(
                    width: PhotodeMetrics.minimumHitArea,
                    height: PhotodeMetrics.minimumHitArea
                )
                .disabled(model.isSendingMedia)
                .accessibilityLabel("Choose media")

                TextField(
                    "",
                    text: $model.composerText,
                    prompt: Text("Message")
                        .foregroundStyle(Color.photodeDisabled),
                    axis: .vertical
                )
                .lineLimit(1...6)
                .focused($composerFocused)
                .photodeHeaderTypography()
                .foregroundStyle(Color.photodeActive)
                .tint(Color.photodeActive)
                .textFieldStyle(.plain)
                .padding(
                    .horizontal,
                    PhotodeMetrics.glassContentInset
                )
                .padding(.vertical, 9)
                .frame(minHeight: PhotodeMetrics.glassControlHeight)
                .fixedSize(horizontal: false, vertical: true)
                .photodeGlassCapsule(interactive: true)
                .frame(
                    minHeight: PhotodeMetrics.minimumHitArea,
                    alignment: .center
                )
                .accessibilityLabel("Message")

                Button(action: composerAction) {
                    ZStack {
                        if model.composerText.isEmpty {
                            Text("O")
                                .transition(
                                    .scale(scale: 0.25)
                                        .combined(with: .opacity)
                                )
                        } else {
                            Text("S")
                                .transition(
                                    .scale(scale: 0.25)
                                        .combined(with: .opacity)
                                )
                        }
                    }
                    .photodeHeaderTypography()
                    .multilineTextAlignment(.center)
                    .foregroundStyle(
                        model.composerText.isEmpty
                            ? Color.photodeDisabled
                            : Color.photodeActive
                    )
                    .frame(
                        width: PhotodeMetrics.glassControlHeight,
                        height: PhotodeMetrics.glassControlHeight,
                        alignment: .center
                    )
                    .contentShape(Circle())
                    .matchedGeometryEffect(
                        id: "video-note",
                        in: videoNoteNamespace,
                        isSource: !isRecordingVideoNote
                    )
                    .photodeGlassCircle(interactive: true)
                }
                .buttonStyle(PhotodePressButtonStyle())
                .frame(
                    width: PhotodeMetrics.minimumHitArea,
                    height: PhotodeMetrics.minimumHitArea,
                    alignment: .center
                )
                .accessibilityLabel(
                    model.composerText.isEmpty
                        ? "Hold to record a video message"
                        : "Send message"
                )
                .onLongPressGesture(minimumDuration: 0.35) {
                    guard model.composerText.isEmpty else { return }
                    beginVideoNoteRecording()
                }
            }
        }
        .padding(.horizontal, PhotodeMetrics.screenInset)
        .padding(.top, 8)
        .padding(.bottom, keyboardIsVisible ? 16 : 0)
        .animation(
            .easeInOut(duration: 0.3),
            value: keyboardIsVisible
        )
    }

    private var recordingToolbar: some View {
        PhotodeGlassGroup {
            HStack(spacing: PhotodeMetrics.glassSpacing) {
                HStack(spacing: 9) {
                    Circle()
                        .fill(Color.red)
                        .frame(width: 10, height: 10)

                    Text(recordingTime)
                        .monospacedDigit()
                }
                .photodeHeaderTypography()
                .padding(.horizontal, PhotodeMetrics.glassContentInset)
                .frame(height: PhotodeMetrics.glassControlHeight)
                .photodeGlassCapsule()

                Button("Cancel", action: cancelVideoNoteRecording)
                    .photodeHeaderTypography()
                    .foregroundStyle(Color.photodeActive)
                    .padding(
                        .horizontal,
                        PhotodeMetrics.glassContentInset
                    )
                    .frame(height: PhotodeMetrics.glassControlHeight)
                    .contentShape(Capsule())
                    .photodeGlassCapsule(interactive: true)
                    .buttonStyle(PhotodePressButtonStyle())
                    .frame(minHeight: PhotodeMetrics.minimumHitArea)
                    .disabled(isFinishingVideoNote)

                Spacer(minLength: 8)

                Button(action: finishVideoNoteRecording) {
                    Text("S")
                        .photodeHeaderTypography()
                        .foregroundStyle(Color.photodeActive)
                        .frame(
                            width: PhotodeMetrics.glassControlHeight,
                            height: PhotodeMetrics.glassControlHeight
                        )
                        .contentShape(Circle())
                        .photodeGlassCircle(interactive: true)
                }
                .buttonStyle(PhotodePressButtonStyle())
                .frame(
                    width: PhotodeMetrics.minimumHitArea,
                    height: PhotodeMetrics.minimumHitArea
                )
                .disabled(isFinishingVideoNote)
                .accessibilityLabel("Send video message")
            }
        }
        .padding(.horizontal, PhotodeMetrics.screenInset)
        .padding(.top, 8)
        .padding(.bottom, 0)
        .task(id: recordingStartedAt) {
            guard recordingStartedAt != nil else { return }
            while !Task.isCancelled, isRecordingVideoNote {
                if let recordingStartedAt {
                    recordingElapsed = Date().timeIntervalSince(
                        recordingStartedAt
                    )
                    if recordingElapsed >= 59.8 {
                        finishVideoNoteRecording()
                        break
                    }
                }
                try? await Task.sleep(for: .milliseconds(50))
            }
        }
    }

    private var recordingTime: String {
        String(format: "%.2f", min(recordingElapsed, 60))
    }

    private func composerAction() {
        guard !model.composerText.isEmpty else { return }
        withAnimation(.spring(duration: 0.3, bounce: 0)) {
            model.sendMessage()
        }
    }

    private func updateTypingState(for text: String) {
        typingCancelTask?.cancel()

        guard !text.isEmpty else {
            model.sendChatAction("cancel")
            return
        }

        let now = Date()
        if now.timeIntervalSince(lastTypingSentAt) >= 3 {
            lastTypingSentAt = now
            model.sendChatAction("typing")
        }

        typingCancelTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            model.sendChatAction("cancel")
        }
    }

    private func beginVideoNoteRecording() {
        composerFocused = false
        recordingElapsed = 0
        recordingStartedAt = Date()
        isFinishingVideoNote = false
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        withAnimation(.spring(duration: 0.3, bounce: 0)) {
            isRecordingVideoNote = true
        }
        Task {
            do {
                try await videoRecorder.start()
                model.sendChatAction("recording_video_note")
            } catch {
                model.errorMessage = error.localizedDescription
                cancelVideoNoteRecording()
            }
        }
    }

    private func cancelVideoNoteRecording() {
        guard !isFinishingVideoNote else { return }
        isFinishingVideoNote = true
        recordingStartedAt = nil
        recordingElapsed = 0
        withAnimation(.easeInOut(duration: 0.18)) {
            isRecordingVideoNote = false
        }
        Task {
            _ = try? await videoRecorder.stop(keepingRecording: false)
            model.sendChatAction("cancel")
            isFinishingVideoNote = false
        }
    }

    private func finishVideoNoteRecording() {
        guard !isFinishingVideoNote else { return }
        isFinishingVideoNote = true
        let duration = max(1, Int(recordingElapsed.rounded(.up)))
        recordingStartedAt = nil
        UIImpactFeedbackGenerator(style: .light).impactOccurred()

        Task {
            defer {
                model.sendChatAction("cancel")
                recordingElapsed = 0
                isFinishingVideoNote = false
                withAnimation(.easeInOut(duration: 0.18)) {
                    isRecordingVideoNote = false
                }
            }

            do {
                guard let url = try await videoRecorder.stop(
                    keepingRecording: true
                ) else { return }
                defer { try? FileManager.default.removeItem(at: url) }

                let data = try Data(contentsOf: url, options: .mappedIfSafe)
                model.sendMedia(
                    TelegramMediaUpload(
                        data: data,
                        kind: .videoNote,
                        fileName: "video-note.mov",
                        mimeType: "video/quicktime",
                        duration: duration,
                        width: 0,
                        height: 0
                    )
                )
            } catch {
                model.errorMessage = error.localizedDescription
            }
        }
    }

    @MainActor
    private func sendPickedMedia(_ item: PhotosPickerItem) async {
        defer { selectedMediaItem = nil }

        guard let type = item.supportedContentTypes.first(where: {
            $0.conforms(to: .movie)
        }) ?? item.supportedContentTypes.first(where: {
            $0.conforms(to: .image)
        }) else {
            model.errorMessage = "The selected item is not a photo or video."
            return
        }

        do {
            guard let data = try await item.loadTransferable(type: Data.self)
            else {
                throw CocoaError(.fileReadUnknown)
            }

            let isVideo = type.conforms(to: .movie)
            var width = 0
            var height = 0
            if !isVideo, let image = UIImage(data: data) {
                width = Int(image.size.width * image.scale)
                height = Int(image.size.height * image.scale)
            }

            let fileExtension = type.preferredFilenameExtension
                ?? (isVideo ? "mov" : "jpg")
            model.sendMedia(
                TelegramMediaUpload(
                    data: data,
                    kind: isVideo ? .video : .photo,
                    fileName: "telegram-upload.\(fileExtension)",
                    mimeType: type.preferredMIMEType
                        ?? (isVideo ? "video/quicktime" : "image/jpeg"),
                    duration: 0,
                    width: width,
                    height: height
                )
            )
        } catch {
            model.errorMessage = error.localizedDescription
        }
    }

    private func dismissKeyboardThenClose() {
        guard !isClosing else { return }
        isClosing = true
        let keyboardWasVisible = composerFocused
        composerFocused = false

        Task { @MainActor in
            if keyboardWasVisible {
                try? await Task.sleep(for: .milliseconds(320))
            }
            withAnimation(.easeInOut(duration: 0.3)) {
                model.closeChat()
            }
        }
    }

    private var edgeBackGesture: some Gesture {
        DragGesture(minimumDistance: 12, coordinateSpace: .local)
            .onChanged { value in
                guard !isClosing,
                      value.startLocation.x <= 28,
                      value.translation.width > 0,
                      abs(value.translation.width) > abs(value.translation.height)
                else { return }
                edgeDragOffset = min(value.translation.width, 180)
            }
            .onEnded { value in
                guard edgeDragOffset > 0 else { return }
                let shouldClose = edgeDragOffset >= 72
                    || value.predictedEndTranslation.width >= 140
                if shouldClose {
                    isClosing = true
                    composerFocused = false
                    withAnimation(.easeOut(duration: 0.22)) {
                        edgeDragOffset = 0
                        model.closeChat()
                    }
                } else {
                    withAnimation(.spring(duration: 0.25, bounce: 0)) {
                        edgeDragOffset = 0
                    }
                }
            }
    }

    private func scrollToBottom(
        using proxy: ScrollViewProxy,
        animated: Bool
    ) {
        Task { @MainActor in
            await Task.yield()
            if animated {
                withAnimation(.easeOut(duration: 0.25)) {
                    proxy.scrollTo(bottomAnchorID, anchor: .bottom)
                }
            } else {
                proxy.scrollTo(bottomAnchorID, anchor: .bottom)
            }
        }
    }

    private func messageText(_ message: TelegramMessage) -> String {
        if !message.text.isEmpty { return message.text }
        return switch message.kind {
        case "photo": "Photo"
        case "video": "Video"
        case "voice_note": "Voice message"
        case "video_note": "Video message"
        default: "Unsupported message"
        }
    }

    private var messageGroups: [MessageGroup] {
        model.messages.reduce(into: [MessageGroup]()) { groups, message in
            let delivery = MessageDelivery(message: message)
            if let last = groups.indices.last,
               groups[last].delivery == delivery {
                groups[last].messages.append(message)
            } else {
                groups.append(
                    MessageGroup(
                        id: message.id,
                        delivery: delivery,
                        messages: [message]
                    )
                )
            }
        }
    }
}

private enum MessageDelivery: Equatable {
    case incoming
    case pending
    case outgoingUnread
    case outgoingRead

    init(message: TelegramMessage) {
        if !message.isOutgoing {
            self = .incoming
        } else if message.id < 0 || message.sendingState != "sent" {
            self = .pending
        } else if message.isRead {
            self = .outgoingRead
        } else {
            self = .outgoingUnread
        }
    }
}

private struct MessageGroup: Identifiable {
    let id: Int64
    let delivery: MessageDelivery
    var messages: [TelegramMessage]

    var label: String {
        switch delivery {
        case .incoming: "T."
        case .pending: ""
        case .outgoingUnread, .outgoingRead: "Y."
        }
    }

    var labelColor: Color {
        delivery == .outgoingUnread ? .photodeDisabled : .photodeActive
    }
}

private struct MediaPickerGlassLabel: View {
    let isDisabled: Bool

    var body: some View {
        Text("M")
            .photodeHeaderTypography()
            .foregroundStyle(
                isDisabled ? Color.photodeDisabled : Color.photodeActive
            )
            .frame(
                width: PhotodeMetrics.glassControlHeight,
                height: PhotodeMetrics.glassControlHeight
            )
            .contentShape(Circle())
            .photodeGlassCircle(interactive: true)
    }
}
