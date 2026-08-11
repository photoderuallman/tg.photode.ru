import SwiftUI

struct ChatListView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        GeometryReader { geometry in
            let contentHeight = max(geometry.size.height - 72, 0)
            let rowHeight = contentHeight / 12
            let columnWidth = max(geometry.size.width - 72, 0) / 8

            VStack(spacing: 0) {
                mainHeader(rowHeight: rowHeight, columnWidth: columnWidth)

                if model.connectionStatus == "RDY" {
                    VStack(spacing: 0) {
                        ForEach(
                            Array(model.chats.prefix(11).enumerated()),
                            id: \.element.id
                        ) { _, chat in
                            chatRow(
                                chat,
                                rowHeight: rowHeight,
                                columnWidth: columnWidth
                            )
                        }
                    }
                    .transition(.opacity)
                }

                Spacer(minLength: 0)
            }
            .frame(height: contentHeight, alignment: .top)
            .padding(.horizontal, 36)
            .padding(.vertical, 36)
            .animation(
                .easeInOut(duration: 0.3),
                value: model.connectionStatus
            )
        }
        .font(.system(size: 18, weight: .medium))
        .lineSpacing(2)
        .refreshable { await model.refreshChats() }
    }

    private func mainHeader(rowHeight: CGFloat, columnWidth: CGFloat) -> some View {
        HStack(alignment: .top, spacing: 0) {
            ZStack(alignment: .topLeading) {
                AccentBowl(seed: model.account?.id ?? 0)
                    .padding(.leading, 1)
                    .padding(.top, 5)
            }
            .frame(width: columnWidth, height: rowHeight, alignment: .topLeading)

            Text(model.account?.displayName ?? "Telegram")
                .foregroundStyle(Color.photodeDisabled)
                .frame(width: columnWidth * 6, alignment: .leading)

            Text(model.connectionStatus)
                .foregroundStyle(Color.photodeDisabled)
                .contentTransition(.numericText())
                .frame(width: columnWidth, alignment: .trailing)
        }
        .frame(height: rowHeight)
    }

    private func chatRow(
        _ chat: ChatSummary,
        rowHeight: CGFloat,
        columnWidth: CGFloat
    ) -> some View {
        Button {
            Task { await model.open(chat) }
        } label: {
            HStack(alignment: .top, spacing: 0) {
                ZStack(alignment: .topLeading) {
                    AccentBowl(seed: chat.id)
                        .padding(.leading, 1)
                        .padding(.top, 5)

                    Text(chat.lastMessageIsOutgoing ? "Y." : "T.")
                        .foregroundStyle(directionColor(chat))
                        .padding(.top, 20)
                }
                .frame(width: columnWidth, height: rowHeight, alignment: .topLeading)

                VStack(alignment: .leading, spacing: 0) {
                    Text(chat.isSavedMessages ? "Saved Messages" : chat.title)
                        .foregroundStyle(Color.photodeActive)
                        .lineLimit(1)
                    Text(chat.lastMessage ?? "")
                        .foregroundStyle(Color.photodeDisabled)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                .frame(width: columnWidth * 6, alignment: .leading)

                Color.clear
                    .frame(width: columnWidth)
            }
            .frame(height: rowHeight)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func directionColor(_ chat: ChatSummary) -> Color {
        guard chat.lastMessageIsOutgoing else { return .photodeDisabled }
        return chat.lastMessageID <= chat.lastReadOutboxMessageID
            ? .photodeActive
            : .photodeDisabled
    }
}
