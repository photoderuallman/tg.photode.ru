import SwiftUI

struct ChatView: View {
    @ObservedObject var model: AppModel
    let chat: ChatSummary

    @FocusState private var composerFocused: Bool
    @State private var didEstablishInitialPosition = false
    @State private var isClosing = false

    private let bottomAnchorID = "chat-bottom-anchor"

    var body: some View {
        GeometryReader { geometry in
            let gridWidth = max(geometry.size.width - 72, 0)
            let columnWidth = gridWidth / 8

            VStack(spacing: 0) {
                chatHeader(columnWidth: columnWidth)
                    .frame(height: 52)

                messageList(
                    columnWidth: columnWidth,
                    viewportHeight: geometry.size.height
                )
                    .layoutPriority(1)

                composer(columnWidth: columnWidth)
                    .padding(.top, 20)
            }
            .frame(width: gridWidth)
            .padding(.horizontal, 36)
            .padding(.top, 20)
            .padding(.bottom, 16)
        }
        .font(.system(size: 18, weight: .medium))
        .lineSpacing(2)
        .background(Color.photodeBackground)
    }

    private func chatHeader(columnWidth: CGFloat) -> some View {
        Button(action: dismissKeyboardThenClose) {
            HStack(alignment: .top, spacing: 0) {
                ZStack(alignment: .topLeading) {
                    AccentBowl(seed: chat.id)
                        .padding(.leading, 1)
                        .padding(.top, 5)
                }
                .frame(width: columnWidth, height: 52, alignment: .topLeading)

                Text(chat.isSavedMessages ? "Saved Messages" : chat.title)
                    .foregroundStyle(
                        model.contactStatusIsActive
                            ? Color.photodeActive
                            : Color.photodeDisabled
                    )
                    .lineLimit(1)
                    .frame(width: columnWidth * 6, alignment: .leading)

                Text(model.contactStatus)
                    .foregroundStyle(
                        model.contactStatusIsActive
                            ? Color.photodeActive
                            : Color.photodeDisabled
                    )
                    .contentTransition(.numericText())
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                    .monospacedDigit()
                    .frame(width: columnWidth, alignment: .trailing)
            }
            .frame(minHeight: 44, alignment: .top)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isClosing)
    }

    private func messageList(
        columnWidth: CGFloat,
        viewportHeight: CGFloat
    ) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    ForEach(messageGroups) { group in
                        HStack(alignment: .top, spacing: 0) {
                            Text(group.label)
                                .foregroundStyle(group.labelColor)
                                .frame(width: columnWidth, alignment: .leading)
                                .transition(
                                    .scale(scale: 0.25)
                                        .combined(with: .opacity)
                                )

                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(group.messages) { message in
                                    Text(messageText(message))
                                        .foregroundStyle(Color.photodeActive)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .id(message.id)
                                        .transition(
                                            .opacity.combined(
                                                with: .move(edge: .bottom)
                                            )
                                        )
                                        .onAppear {
                                            model.loadOlderMessagesIfNeeded(
                                                visibleMessageID: message.id
                                            )
                                        }
                                }
                            }
                            .frame(width: columnWidth * 7, alignment: .leading)
                        }
                    }

                    Color.clear
                        .frame(height: 1)
                        .id(bottomAnchorID)
                }
                .padding(.top, 8)
            }
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .onAppear {
                // defaultScrollAnchor lays out at the bottom before the first
                // frame. The flag only enables later, intentional animations.
                Task { @MainActor in
                    await Task.yield()
                    didEstablishInitialPosition = true
                }
            }
            .onChange(of: model.messages.last?.id) { oldID, newID in
                guard didEstablishInitialPosition,
                      let newID,
                      newID != oldID
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

    private func composer(columnWidth: CGFloat) -> some View {
        HStack(alignment: .top, spacing: 0) {
            Button("M.") { }
                .foregroundStyle(Color.photodeDisabled)
                .frame(width: columnWidth, alignment: .leading)
                .disabled(true)

            TextField(
                "",
                text: $model.composerText,
                prompt: Text("Message")
                    .foregroundStyle(Color.photodeDisabled),
                axis: .vertical
            )
                .lineLimit(1...6)
                .focused($composerFocused)
                .foregroundStyle(Color.photodeActive)
                .tint(Color.photodeActive)
                .textFieldStyle(.plain)
                .frame(width: columnWidth * 6, alignment: .topLeading)

            Button(model.composerText.isEmpty ? "O." : "S.") {
                if !model.composerText.isEmpty {
                    model.sendMessage()
                }
            }
            .foregroundStyle(
                model.composerText.isEmpty
                    ? Color.photodeDisabled
                    : Color.photodeActive
            )
            .frame(width: columnWidth, alignment: .trailing)
        }
        .frame(minHeight: 20, alignment: .top)
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
