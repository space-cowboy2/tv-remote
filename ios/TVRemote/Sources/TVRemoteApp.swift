import SwiftUI

@main
struct TVRemoteApp: App {
    @State private var model = RemoteModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .preferredColorScheme(.dark)
                .tint(Theme.accent)
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active: model.becameActive()
            case .background: model.wentToBackground()
            default: break
            }
        }
    }
}

struct RootView: View {
    @Environment(RemoteModel.self) private var model

    var body: some View {
        ZStack(alignment: .bottom) {
            Theme.bg.ignoresSafeArea()
            if model.config == nil {
                SignInView()
            } else {
                TabView {
                    RemoteView()
                        .tabItem { Label("Remote", systemImage: "appletvremote.gen4") }
                    AskView()
                        .tabItem { Label("Ask", systemImage: "sparkles") }
                    AppsView()
                        .tabItem { Label("Apps", systemImage: "square.grid.2x2.fill") }
                    KeyboardView()
                        .tabItem { Label("Keyboard", systemImage: "keyboard") }
                    MoreView()
                        .tabItem { Label("More", systemImage: "ellipsis.circle") }
                }
            }
            if let toast = model.toast {
                Toast(text: toast).padding(.bottom, 56)
            }
        }
        .animation(.spring(duration: 0.25), value: model.toast)
    }
}
