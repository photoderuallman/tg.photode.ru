import SwiftUI

struct RootView: View {
    @ObservedObject var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack {
            Color.photodeBackground.ignoresSafeArea()

            // The last cached session is always the launch surface. UPD/CNT is
            // expressed in the header while the network hard-refreshes it.
            ChatListView(model: model)
                .ignoresSafeArea(.keyboard, edges: .bottom)
                .transition(.identity)

            if let chat = model.selectedChat {
                ChatView(model: model, chat: chat)
                    .background(Color.photodeBackground)
                    .transition(.move(edge: .trailing))
                    .zIndex(2)
            }
        }
        .animation(.easeInOut(duration: 0.3), value: model.selectedChat?.id)
        .task { await model.bootstrap() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                model.appDidBecomeActive()
            }
        }
        .alert(
            "TG PHOTODE",
            isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { if !$0 { model.errorMessage = nil } }
            )
        ) {
            Button("Retry") {
                model.errorMessage = nil
                Task { await model.connectDevice() }
            }
        } message: {
            Text(model.errorMessage ?? "")
        }
    }
}
