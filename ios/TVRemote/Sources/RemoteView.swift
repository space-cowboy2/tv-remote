import SwiftUI

struct StatusBar: View {
    @Environment(RemoteModel.self) private var model

    private var label: (String, Color) {
        switch model.power {
        case .on: return ("TV on", .green)
        case .standby: return ("TV in standby", .orange)
        case .off: return ("TV off", .gray)
        case .hubDown: return ("Hub unreachable", Theme.danger)
        case .unknown: return ("Connecting…", .gray)
        }
    }

    var body: some View {
        HStack {
            HStack(spacing: 8) {
                Circle().fill(label.1).frame(width: 8, height: 8)
                Text(label.0).font(.subheadline).foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)
            Spacer()
            Button {
                Task { await model.togglePower() }
            } label: {
                Image(systemName: "power")
                    .font(.title2.weight(.semibold))
                    .frame(width: 52, height: 52)
                    .background(Theme.danger.opacity(0.18), in: Circle())
                    .foregroundStyle(Theme.danger)
            }
            .disabled(model.busyPower)
            .accessibilityLabel("Power")
        }
    }
}

struct RemoteView: View {
    var body: some View {
        VStack(spacing: 18) {
            StatusBar()

            HStack(spacing: 12) {
                KeyButton("KEY_RETURN", symbol: "arrow.uturn.backward", label: "Back")
                KeyButton("KEY_HOME", symbol: "house.fill", label: "Home")
                KeyButton("KEY_MENU", symbol: "gearshape.fill", label: "Settings")
            }
            .frame(height: 54)
            .font(.title3)

            DPad()
                .frame(maxWidth: 300)
                .aspectRatio(1, contentMode: .fit)

            HStack(spacing: 12) {
                Rocker(title: "VOL", up: ("KEY_VOLUP", "plus", "Volume up"), down: ("KEY_VOLDOWN", "minus", "Volume down"), repeats: true)
                VStack(spacing: 12) {
                    KeyButton("KEY_MUTE", symbol: "speaker.slash.fill", label: "Mute")
                    KeyButton("KEY_SOURCE", symbol: "rectangle.portrait.and.arrow.right", label: "Source")
                }
                Rocker(title: "CH", up: ("KEY_CHUP", "chevron.up", "Channel up"), down: ("KEY_CHDOWN", "chevron.down", "Channel down"), repeats: false)
            }
            .frame(height: 124)
            .font(.title3)

            HStack(spacing: 12) {
                KeyButton("KEY_REWIND", symbol: "backward.fill", label: "Rewind")
                KeyButton("KEY_PLAY", symbol: "play.fill", label: "Play")
                KeyButton("KEY_PAUSE", symbol: "pause.fill", label: "Pause")
                KeyButton("KEY_FF", symbol: "forward.fill", label: "Fast forward")
            }
            .frame(height: 50)
        }
        .padding(.horizontal, 20)
        .padding(.top, 8)
        .frame(maxHeight: .infinity, alignment: .top)
    }
}

struct DPad: View {
    var body: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height)
            let arm = s * 0.3
            ZStack {
                Circle().fill(Theme.key)
                VStack(spacing: 0) {
                    arrow("KEY_UP", "chevron.up", "Up").frame(width: arm * 1.6, height: arm)
                    HStack(spacing: 0) {
                        arrow("KEY_LEFT", "chevron.left", "Left").frame(width: arm, height: arm * 1.6)
                        KeyButton(key: "KEY_ENTER", fill: Theme.keyPressed.opacity(0.7),
                                  shape: AnyShape(Circle()), accessibility: "OK") {
                            Text("OK").font(.title3.weight(.bold))
                        }
                        .frame(width: s * 0.36, height: s * 0.36)
                        .padding(s * 0.02)
                        arrow("KEY_RIGHT", "chevron.right", "Right").frame(width: arm, height: arm * 1.6)
                    }
                    arrow("KEY_DOWN", "chevron.down", "Down").frame(width: arm * 1.6, height: arm)
                }
            }
            .frame(width: s, height: s)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func arrow(_ key: String, _ symbol: String, _ label: String) -> some View {
        KeyButton(key: key, repeats: true, fill: .clear, shape: AnyShape(Circle()), accessibility: label) {
            Image(systemName: symbol).font(.title2.weight(.semibold))
        }
    }
}

struct Rocker: View {
    let title: String
    let up: (String, String, String)
    let down: (String, String, String)
    let repeats: Bool

    var body: some View {
        VStack(spacing: 0) {
            KeyButton(up.0, symbol: up.1, label: up.2, repeats: repeats)
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.vertical, 4)
            KeyButton(down.0, symbol: down.1, label: down.2, repeats: repeats)
        }
        .background(Theme.key, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}
