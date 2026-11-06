import SwiftUI

/// Tell Claude what to put on the TV. Use the keyboard's mic key to speak.
struct AskView: View {
    @Environment(RemoteModel.self) private var model
    @State private var text = ""
    @State private var busy = false
    @State private var log: [Entry] = []
    @FocusState private var focused: Bool

    struct Entry: Identifiable {
        let id = UUID()
        let text: String
        let fromUser: Bool
        var failed = false
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 14) {
                HStack(spacing: 10) {
                    TextField("What should the TV do?", text: $text)
                        .focused($focused)
                        .submitLabel(.send)
                        .onSubmit(send)
                        .modifier(FieldStyle())
                    Button(action: send) {
                        Group {
                            if busy { ProgressView() } else { Image(systemName: "arrow.up") }
                        }
                        .font(.headline)
                        .frame(width: 50, height: 50)
                        .background(Theme.accent, in: Circle())
                        .foregroundStyle(.white)
                    }
                    .disabled(busy || text.trimmingCharacters(in: .whitespaces).isEmpty)
                    .accessibilityLabel("Send")
                }
                if log.isEmpty {
                    Text("Try \u{201C}open ohnepixel on Twitch\u{201D}, \u{201C}play the latest MKBHD video\u{201D} or \u{201C}volume down 5\u{201D}. Tap the mic on the keyboard to speak.")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(log) { e in
                            Text(e.text)
                                .padding(.horizontal, 12).padding(.vertical, 9)
                                .background(e.fromUser ? Theme.accent : Theme.key,
                                            in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                .foregroundStyle(e.fromUser ? .white : (e.failed ? Theme.danger : .primary))
                                .frame(maxWidth: .infinity, alignment: e.fromUser ? .trailing : .leading)
                        }
                    }
                }
            }
            .padding(20)
            .navigationTitle("Ask Claude")
            .onAppear { focused = true }
        }
    }

    private func send() {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !busy else { return }
        text = ""
        busy = true
        log.insert(Entry(text: t, fromUser: true), at: 0)
        model.tap()
        Task {
            let r = await model.ask(t)
            log.insert(Entry(text: r.reply, fromUser: false, failed: !r.ok), at: 0)
            if log.count > 30 { log.removeLast(log.count - 30) }
            busy = false
        }
    }
}
