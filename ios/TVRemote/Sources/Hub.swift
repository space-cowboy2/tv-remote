import Foundation
import Security

// MARK: - Saved connection (hub URL + access key, kept in the Keychain)

struct HubConfig: Codable, Equatable {
    var baseURL: URL
    var key: String

    static let defaultURL = ""
    private static let account = "hub"
    private static let service = "com.tvremote.app"

    static func load() -> HubConfig? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(HubConfig.self, from: data)
    }

    func save() {
        guard let data = try? JSONEncoder().encode(self) else { return }
        HubConfig.clear()
        let item: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: HubConfig.service,
            kSecAttrAccount as String: HubConfig.account,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data,
        ]
        SecItemAdd(item as CFDictionary, nil)
    }

    static func clear() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }

    /// Accepts "your-hub.your-tailnet.ts.net" (HTTPS assumed) or a full URL.
    static func parseURL(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return nil }
        if !s.contains("://") { s = "https://" + s }
        guard var c = URLComponents(string: s), let host = c.host, !host.isEmpty,
              c.scheme == "http" || c.scheme == "https" else { return nil }
        if c.port == nil && c.scheme == "http" { c.port = 8080 }
        c.path = ""
        c.query = nil
        return c.url
    }
}

// MARK: - API types

struct TVStatus: Decodable {
    let power: String          // "on" | "standby" | "off"
    let connected: Bool
}

struct TVApp: Decodable, Identifiable, Hashable {
    let id: String
    let name: String?
    var title: String { name ?? id }
}

enum HubError: LocalizedError {
    case unauthorized
    case tvUnreachable
    case server(String)
    case network(String)

    var errorDescription: String? {
        switch self {
        case .unauthorized: return "Wrong access key"
        case .tvUnreachable: return "TV is off or unreachable"
        case .server(let m): return m
        case .network(let m): return m
        }
    }
}

// MARK: - REST client

final class HubClient: @unchecked Sendable {
    let config: HubConfig
    let session: URLSession

    init(config: HubConfig) {
        self.config = config
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 8
        c.waitsForConnectivity = false
        c.httpCookieStorage = nil
        session = URLSession(configuration: c)
    }

    private func url(_ path: String) -> URL {
        URL(string: "/api/\(path)", relativeTo: config.baseURL)!.absoluteURL
    }

    private func authorized(_ url: URL) -> URLRequest {
        var r = URLRequest(url: url)
        r.setValue("Bearer \(config.key)", forHTTPHeaderField: "Authorization")
        return r
    }

    @discardableResult
    func call(_ path: String, method: String = "GET", body: [String: Any]? = nil,
              timeout: TimeInterval = 8) async throws -> Data {
        var req = authorized(url(path))
        req.httpMethod = method
        req.timeoutInterval = timeout
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch let e as URLError {
            switch e.code {
            case .timedOut: throw HubError.network("Hub didn't answer. Is Tailscale on?")
            case .cannotConnectToHost, .cannotFindHost, .networkConnectionLost, .notConnectedToInternet:
                throw HubError.network("Can't reach the hub. Is Tailscale on?")
            default: throw HubError.network(e.localizedDescription)
            }
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if (200..<300).contains(code) { return data }
        if code == 401 { throw HubError.unauthorized }
        if code == 503 { throw HubError.tvUnreachable }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw HubError.server(detail ?? "Hub error \(code)")
    }

    func status() async throws -> TVStatus {
        try JSONDecoder().decode(TVStatus.self, from: try await call("status"))
    }

    func apps(refresh: Bool = false) async throws -> [TVApp] {
        let data = try await call(refresh ? "apps?refresh=true" : "apps", timeout: 30)
        return try JSONDecoder().decode([TVApp].self, from: data)
    }

    func key(_ key: String) async throws {
        try await call("key", method: "POST", body: ["key": key, "cmd": "Click"])
    }

    func text(_ text: String, submit: Bool) async throws {
        try await call("text", method: "POST", body: ["text": text, "submit": submit])
    }

    func launch(_ appID: String) async throws {
        try await call("launch", method: "POST", body: ["app_id": appID])
    }

    func power(_ on: Bool) async throws {
        try await call("power", method: "POST", body: ["state": on ? "on" : "off"], timeout: 30)
    }

    /// WebSocket for low-latency key presses. Auth via the same bearer header.
    func makeSocket() -> URLSessionWebSocketTask {
        var c = URLComponents(url: url("ws"), resolvingAgainstBaseURL: false)!
        c.scheme = c.scheme == "https" ? "wss" : "ws"
        return session.webSocketTask(with: authorized(c.url!))
    }
}
