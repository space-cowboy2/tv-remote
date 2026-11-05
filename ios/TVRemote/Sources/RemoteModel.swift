import Foundation
import Observation
import UIKit

@MainActor
@Observable
final class RemoteModel {
    enum Power: Equatable { case unknown, on, standby, off, hubDown }

    private(set) var config: HubConfig?
    var power: Power = .unknown
    var apps: [TVApp] = []
    var appsState: String? = nil       // nil = loaded; otherwise a message
    var toast: String?
    var busyPower = false

    @ObservationIgnored private var client: HubClient?
    @ObservationIgnored private var socket: URLSessionWebSocketTask?
    @ObservationIgnored private var socketReady = false
    @ObservationIgnored private var retry = 0
    @ObservationIgnored private var statusTimer: Timer?
    @ObservationIgnored private var toastTask: Task<Void, Never>?
    @ObservationIgnored private let haptic = UIImpactFeedbackGenerator(style: .light)

    init() {
        if let saved = HubConfig.load() { use(saved) }
    }

    // MARK: sign in / out

    /// Verifies the URL + key against the hub, then saves them.
    func signIn(urlString: String, key: String) async throws {
        guard let url = HubConfig.parseURL(urlString) else { throw HubError.server("Enter the hub address, like dannys-mac-mini.tail24f732.ts.net") }
        let cfg = HubConfig(baseURL: url, key: key.trimmingCharacters(in: .whitespacesAndNewlines))
        let probe = HubClient(config: cfg)
        do {
            _ = try await probe.status()
        } catch HubError.tvUnreachable {
            // Signed in fine; the TV is just off.
        }
        cfg.save()
        use(cfg)
    }

    func signOut() {
        HubConfig.clear()
        closeSocket()
        statusTimer?.invalidate()
        client = nil
        config = nil
        apps = []
        power = .unknown
    }

    private func use(_ cfg: HubConfig) {
        config = cfg
        client = HubClient(config: cfg)
        apps = []
        appsState = "Loading apps…"
        becameActive()
    }

    // MARK: lifecycle

    func becameActive() {
        guard client != nil else { return }
        openSocket()
        Task { await refreshStatus() }
        statusTimer?.invalidate()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refreshStatus() }
        }
    }

    func wentToBackground() {
        statusTimer?.invalidate()
        closeSocket()
    }

    func refreshStatus() async {
        guard let client else { return }
        do {
            let s = try await client.status()
            power = s.power == "on" ? .on : s.power == "standby" ? .standby : .off
        } catch HubError.unauthorized {
            show("Access key no longer valid. Sign in again.")
            signOut()
        } catch HubError.tvUnreachable {
            power = .off
        } catch {
            power = .hubDown
        }
    }

    // MARK: keys

    func tap() { haptic.impactOccurred() }

    func send(_ key: String) {
        guard let client else { return }
        if socketReady, let socket {
            let msg = "{\"key\":\"\(key)\"}"
            socket.send(.string(msg)) { [weak self] err in
                guard err != nil else { return }
                Task { @MainActor in
                    self?.socketReady = false
                    self?.restSend(key, client)
                }
            }
        } else {
            restSend(key, client)
        }
    }

    private func restSend(_ key: String, _ client: HubClient) {
        Task {
            do { try await client.key(key) } catch { fail(error) }
        }
    }

    // MARK: actions

    func togglePower() async {
        guard let client else { return }
        tap()
        busyPower = true
        defer { busyPower = false }
        await refreshStatus()
        let turnOn = power != .on
        show(turnOn ? "Turning TV on…" : "Turning TV off…")
        do {
            try await client.power(turnOn)
            power = turnOn ? .on : .standby
        } catch { fail(error) }
        try? await Task.sleep(for: .seconds(1.5))
        await refreshStatus()
    }

    func loadApps(refresh: Bool = false) async {
        guard let client else { return }
        if apps.isEmpty { appsState = "Loading apps…" }
        do {
            apps = try await client.apps(refresh: refresh)
            appsState = apps.isEmpty ? "No apps found." : nil
        } catch HubError.tvUnreachable {
            appsState = "Turn the TV on to see apps."
        } catch {
            appsState = error.localizedDescription
        }
    }

    func launch(_ app: TVApp) async {
        guard let client else { return }
        tap()
        do {
            try await client.launch(app.id)
            show("Opening \(app.title)")
        } catch { fail(error) }
    }

    func sendText(_ text: String, submit: Bool) async -> Bool {
        guard let client, !text.isEmpty else { return false }
        do {
            try await client.text(text, submit: submit)
            show("Sent")
            return true
        } catch {
            fail(error)
            return false
        }
    }

    // MARK: websocket

    private func openSocket() {
        guard let client, socket == nil else { return }
        let ws = client.makeSocket()
        socket = ws
        ws.resume()
        receive(ws)
        // URLSessionWebSocketTask has no open callback; a ping confirms the handshake.
        ws.sendPing { [weak self] err in
            Task { @MainActor in
                guard let self, self.socket === ws else { return }
                if err == nil { self.socketReady = true; self.retry = 0 } else { self.socketDropped(ws) }
            }
        }
    }

    private func receive(_ ws: URLSessionWebSocketTask) {
        ws.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.socket === ws else { return }
                switch result {
                case .success(.string(let s)):
                    if let d = s.data(using: .utf8),
                       let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                       obj["ok"] as? Bool == false {
                        let err = obj["error"] as? String ?? "TV error"
                        self.show(err.lowercased().contains("unreachable") ? "TV is off or unreachable" : err)
                        await self.refreshStatus()
                    }
                    self.receive(ws)
                case .success:
                    self.receive(ws)
                case .failure:
                    self.socketDropped(ws)
                }
            }
        }
    }

    private func socketDropped(_ ws: URLSessionWebSocketTask) {
        guard socket === ws else { return }
        socket = nil
        socketReady = false
        guard UIApplication.shared.applicationState == .active else { return }
        let delay = min(pow(2.0, Double(retry)), 15)
        retry += 1
        Task {
            try? await Task.sleep(for: .seconds(delay))
            self.openSocket()
        }
    }

    private func closeSocket() {
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        socketReady = false
    }

    // MARK: messages

    private func fail(_ error: Error) {
        if case HubError.unauthorized = error {
            show("Access key no longer valid. Sign in again.")
            signOut()
            return
        }
        show(error.localizedDescription)
        Task { await refreshStatus() }
    }

    func show(_ message: String) {
        toast = message
        toastTask?.cancel()
        toastTask = Task {
            try? await Task.sleep(for: .seconds(2.5))
            if !Task.isCancelled { toast = nil }
        }
    }
}
