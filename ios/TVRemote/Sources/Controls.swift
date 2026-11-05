import SwiftUI

// Shared look
enum Theme {
    static let bg = Color(red: 0.055, green: 0.059, blue: 0.071)
    static let key = Color(red: 0.14, green: 0.15, blue: 0.18)
    static let keyPressed = Color(red: 0.22, green: 0.24, blue: 0.29)
    static let accent = Color(red: 0.36, green: 0.55, blue: 1.0)
    static let danger = Color(red: 1.0, green: 0.36, blue: 0.36)
}

/// Fires on touch-down (not release) like a physical remote. With `repeats`,
/// holding keeps sending (volume, arrows).
final class Repeater {
    private var start: DispatchWorkItem?
    private var timer: Timer?

    func begin(repeats: Bool, fire: @escaping () -> Void) {
        end()
        fire()
        guard repeats else { return }
        let work = DispatchWorkItem { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 0.14, repeats: true) { _ in fire() }
        }
        start = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: work)
    }

    func end() {
        start?.cancel()
        start = nil
        timer?.invalidate()
        timer = nil
    }
}

struct PressStyle: ButtonStyle {
    var shape: AnyShape = AnyShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    var fill: Color = Theme.key
    let onPressChange: (Bool) -> Void

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(configuration.isPressed ? Theme.keyPressed : fill, in: shape)
            .contentShape(shape)
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
            .animation(.easeOut(duration: 0.08), value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, pressed in onPressChange(pressed) }
    }
}

/// A button that sends one TV key.
struct KeyButton<Label: View>: View {
    @Environment(RemoteModel.self) private var model
    let key: String
    var repeats = false
    var fill: Color = Theme.key
    var shape: AnyShape = AnyShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    var accessibility: String
    var label: () -> Label
    @State private var repeater = Repeater()

    init(key: String, repeats: Bool = false, fill: Color = Theme.key,
         shape: AnyShape = AnyShape(RoundedRectangle(cornerRadius: 18, style: .continuous)),
         accessibility: String, @ViewBuilder label: @escaping () -> Label) {
        self.key = key
        self.repeats = repeats
        self.fill = fill
        self.shape = shape
        self.accessibility = accessibility
        self.label = label
    }

    var body: some View {
        Button {
            // Touch-down already sent it; VoiceOver/Switch Control only trigger the action.
            if UIAccessibility.isVoiceOverRunning || UIAccessibility.isSwitchControlRunning { model.send(key) }
        } label: {
            label()
        }
        .buttonStyle(PressStyle(shape: shape, fill: fill) { pressed in
            if pressed {
                model.tap()
                repeater.begin(repeats: repeats) { model.send(key) }
            } else {
                repeater.end()
            }
        })
        .accessibilityLabel(accessibility)
        .onDisappear { repeater.end() }
    }
}

extension KeyButton where Label == Image {
    init(_ key: String, symbol: String, label: String, repeats: Bool = false) {
        self.init(key: key, repeats: repeats, accessibility: label) { Image(systemName: symbol) }
    }
}

extension KeyButton where Label == Text {
    init(_ key: String, text: String, label: String? = nil, fill: Color = Theme.key) {
        self.init(key: key, fill: fill, accessibility: label ?? text) { Text(text) }
    }
}

struct Toast: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.subheadline.weight(.medium))
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.bottom, 8)
            .transition(.move(edge: .bottom).combined(with: .opacity))
            .accessibilityAddTraits(.updatesFrequently)
    }
}
