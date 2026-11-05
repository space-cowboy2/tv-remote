import SwiftUI

// MARK: - Sign in

struct SignInView: View {
    @Environment(RemoteModel.self) private var model
    @State private var url = HubConfig.defaultURL
    @State private var key = ""
    @State private var error: String?
    @State private var working = false

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "appletvremote.gen4.fill")
                .font(.system(size: 64))
                .foregroundStyle(Theme.accent)
            Text("TV Remote").font(.largeTitle.bold())
            Text("Enter your hub's address and access key. You only do this once.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            VStack(spacing: 12) {
                TextField("Hub address", text: $url)
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .modifier(FieldStyle())
                SecureField("Access key", text: $key)
                    .textContentType(.password)
                    .submitLabel(.go)
                    .onSubmit(submit)
                    .modifier(FieldStyle())
            }
            Button(action: submit) {
                Group { if working { ProgressView() } else { Text("Sign in").bold() } }
                    .frame(maxWidth: .infinity, minHeight: 50)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.accent)
            .disabled(working || key.isEmpty)
            if let error {
                Text(error).foregroundStyle(Theme.danger).font(.callout).multilineTextAlignment(.center)
            }
            Spacer()
            Text("Find the key on the hub: ~/.config/tvremote/hub.key")
                .font(.footnote).foregroundStyle(.tertiary)
        }
        .padding(24)
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
    }

    private func submit() {
        guard !key.isEmpty, !working else { return }
        working = true
        error = nil
        Task {
            do { try await model.signIn(urlString: url, key: key) }
            catch { self.error = error.localizedDescription }
            working = false
        }
    }
}

struct FieldStyle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 14)
            .frame(height: 50)
            .background(Theme.key, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

// MARK: - Apps

struct AppsView: View {
    @Environment(RemoteModel.self) private var model
    private let palette: [Color] = [.red, .pink, .green, .cyan, .purple, .orange, .mint, .blue]

    var body: some View {
        NavigationStack {
            ScrollView {
                if let msg = model.appsState {
                    Text(msg).foregroundStyle(.secondary).padding(.top, 60)
                } else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 12)], spacing: 12) {
                        ForEach(model.apps) { app in
                            Button { Task { await model.launch(app) } } label: {
                                VStack(spacing: 10) {
                                    Text(String(app.title.prefix(1)).uppercased())
                                        .font(.title.bold())
                                        .frame(width: 56, height: 56)
                                        .background(color(for: app.title), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                    Text(app.title).font(.footnote).lineLimit(1)
                                }
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 14)
                                .background(Theme.key, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(16)
                }
            }
            .refreshable { await model.loadApps(refresh: true) }
            .navigationTitle("Apps")
            .toolbarBackground(Theme.bg, for: .navigationBar)
        }
        .task { if model.apps.isEmpty { await model.loadApps() } }
    }

    private func color(for name: String) -> Color {
        var h: UInt32 = 0
        for u in name.unicodeScalars { h = h &* 31 &+ u.value }
        return palette[Int(h % UInt32(palette.count))]
    }
}

// MARK: - Keyboard

struct KeyboardView: View {
    @Environment(RemoteModel.self) private var model
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                Text("Open a search box on the TV, then type here.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                TextField("Search…", text: $text)
                    .focused($focused)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.send)
                    .onSubmit { send(submit: true) }
                    .modifier(FieldStyle())
                HStack(spacing: 12) {
                    Button("Send") { send(submit: false) }
                        .buttonStyle(.borderedProminent).tint(Theme.accent)
                    Button("Send + Enter") { send(submit: true) }
                        .buttonStyle(.bordered)
                }
                .controlSize(.large)
                .disabled(text.isEmpty)
                HStack(spacing: 12) {
                    KeyButton("KEY_LEFT", symbol: "chevron.left", label: "Left", repeats: true)
                    KeyButton("KEY_ENTER", text: "OK")
                    KeyButton("KEY_RIGHT", symbol: "chevron.right", label: "Right", repeats: true)
                    KeyButton("KEY_RETURN", symbol: "arrow.uturn.backward", label: "Back")
                }
                .frame(height: 50)
                Spacer()
            }
            .padding(20)
            .navigationTitle("Keyboard")
            .onAppear { focused = true }
        }
    }

    private func send(submit: Bool) {
        let t = text
        Task { if await model.sendText(t, submit: submit) { text = "" } }
    }
}

// MARK: - More

struct MoreView: View {
    @Environment(RemoteModel.self) private var model
    @State private var confirmSignOut = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 22) {
                    section("Numbers") {
                        VStack(spacing: 12) {
                            ForEach([["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"]], id: \.self) { row in
                                HStack(spacing: 12) { ForEach(row, id: \.self) { n in KeyButton("KEY_\(n)", text: n) } }
                                    .frame(height: 54)
                            }
                            HStack(spacing: 12) {
                                KeyButton("KEY_INFO", text: "Info")
                                KeyButton("KEY_0", text: "0")
                                KeyButton("KEY_GUIDE", text: "Guide")
                            }
                            .frame(height: 54)
                        }
                        .font(.title3.weight(.medium))
                    }
                    section("Color keys") {
                        HStack(spacing: 12) {
                            KeyButton("KEY_RED", text: "A", label: "Red", fill: .red.opacity(0.75))
                            KeyButton("KEY_GREEN", text: "B", label: "Green", fill: .green.opacity(0.7))
                            KeyButton("KEY_YELLOW", text: "C", label: "Yellow", fill: .yellow.opacity(0.6))
                            KeyButton("KEY_CYAN", text: "D", label: "Blue", fill: .blue.opacity(0.75))
                        }
                        .frame(height: 48)
                    }
                    section("Inputs") {
                        HStack(spacing: 12) {
                            KeyButton("KEY_HDMI1", text: "HDMI 1")
                            KeyButton("KEY_HDMI2", text: "HDMI 2")
                            KeyButton("KEY_HDMI3", text: "HDMI 3")
                            KeyButton("KEY_TV", text: "TV")
                        }
                        .frame(height: 48)
                        .font(.footnote.weight(.medium))
                    }
                    section("Other") {
                        HStack(spacing: 12) {
                            KeyButton("KEY_CAPTION", text: "Captions")
                            KeyButton("KEY_TOOLS", text: "Tools")
                            KeyButton("KEY_AMBIENT", text: "Ambient")
                            KeyButton("KEY_EXIT", text: "Exit")
                        }
                        .frame(height: 48)
                        .font(.footnote.weight(.medium))
                    }
                    section("Hub") {
                        VStack(alignment: .leading, spacing: 12) {
                            Text(model.config?.baseURL.absoluteString ?? "").foregroundStyle(.secondary).textSelection(.enabled)
                            Button("Sign out of this device", role: .destructive) { confirmSignOut = true }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(20)
            }
            .navigationTitle("More")
            .confirmationDialog("Sign out? You'll need the access key again.", isPresented: $confirmSignOut, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) { model.signOut() }
            }
        }
    }

    private func section<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased()).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            content()
        }
    }
}
