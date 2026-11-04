import Foundation
import XCTest
@testable import Herness

private final class StubURLProtocol: URLProtocol {
    typealias Reply = (status: Int, headers: [String: String], data: Data)
    private static let lock = NSLock()
    private static var handler: ((URLRequest) throws -> Reply)?
    private static var requests: [URLRequest] = []

    static func configure(_ reply: @escaping (URLRequest) throws -> Reply) {
        lock.lock()
        handler = reply
        requests = []
        lock.unlock()
    }

    static func recordedRequests() -> [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        Self.requests.append(request)
        let responder = Self.handler
        Self.lock.unlock()
        do {
            guard let responder, let url = request.url else { throw URLError(.badServerResponse) }
            let reply = try responder(request)
            guard let response = HTTPURLResponse(url: url, statusCode: reply.status,
                                                  httpVersion: "HTTP/1.1", headerFields: reply.headers) else {
                throw URLError(.badServerResponse)
            }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: reply.data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

final class APIClientTests: XCTestCase {
    private let pairingKey = String(repeating: "K", count: 43)

    private func client() throws -> APIClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return try APIClient(baseURL: XCTUnwrap(URL(string: "https://herness.example")),
                             pairingKey: pairingKey, configuration: configuration)
    }

    private func respond(status: Int = 200, contentType: String = "application/json", body: String = "{}") {
        StubURLProtocol.configure { _ in
            (status, ["Content-Type": contentType], Data(body.utf8))
        }
    }

    func testOnlySecureServerOriginsAreAccepted() throws {
        let normalized = try APIClient.validatedBaseURL(" HTTPS://HERNess.example:443/ ")
        XCTAssertEqual(normalized.absoluteString, "https://herness.example")
        for endpoint in ["", "example.com", "http://public.example", "ftp://server.example",
                         "https://user:password@server.example", "https://server.example/api",
                         "https://server.example/?token=secret", "https://server.example/#fragment",
                         "https://server.example:0", "https://server.example:65536",
                         "https://server.example\\@other.example", "https://server.example/../"] {
            XCTAssertThrowsError(try APIClient.validatedBaseURL(endpoint), endpoint)
        }
        #if DEBUG
        XCTAssertEqual(try APIClient.validatedBaseURL("http://127.0.0.1:8765").host, "127.0.0.1")
        #else
        XCTAssertThrowsError(try APIClient.validatedBaseURL("http://127.0.0.1:8765"))
        #endif
    }

    func testPairingKeyRejectsHeaderInjectionAndEmptyValues() throws {
        let url = try XCTUnwrap(URL(string: "https://herness.example"))
        for key in ["", "contains spaces", "secret\r\nX-Injected: true", "\tsecret", "🔑",
                    String(repeating: "x", count: 42), String(repeating: "x", count: 44),
                    String(repeating: "+", count: 43), String(repeating: "x", count: 4097)] {
            XCTAssertThrowsError(try APIClient(baseURL: url, pairingKey: key)) { error in
                XCTAssertEqual(error as? APIError, .invalidPairingKey)
            }
        }
    }

    func testAuthorizationAndQueryAreSentOnlyToConfiguredOrigin() async throws {
        respond(body: #"{"busy":false,"messages":[]}"#)
        let api = try client()
        let result: StatusResponse = try await api.request("/api/status", query: [URLQueryItem(name: "q", value: "two words & symbols")])
        XCTAssertFalse(result.busy)
        let request = try XCTUnwrap(StubURLProtocol.recordedRequests().first)
        XCTAssertEqual(request.url?.host, "herness.example")
        XCTAssertEqual(request.url?.path, "/api/status")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer " + pairingKey)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cache-Control"), "no-store")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/json")
        let components = try XCTUnwrap(URLComponents(url: XCTUnwrap(request.url), resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.queryItems, [URLQueryItem(name: "q", value: "two words & symbols")])
        XCTAssertFalse(request.url?.absoluteString.contains(pairingKey) ?? true)
    }

    func testPostEncodesJSONWithoutAddingCredentialsToBody() async throws {
        respond(body: #"{"ok":true}"#)
        let api = try client()
        let result: JSONValue = try await api.request("/api/chat", method: .post,
                                                     body: .object(["prompt": .string("Hello \"Herness\""), "attachments": .array([]),
                                                                    "expected_session": .string("chat-1")]))
        XCTAssertEqual(result["ok"], .bool(true))
        let request = try XCTUnwrap(StubURLProtocol.recordedRequests().first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try bodyData(request)
        let decoded = try JSONDecoder().decode(JSONValue.self, from: body)
        XCTAssertEqual(decoded["prompt"], .string("Hello \"Herness\""))
        XCTAssertEqual(decoded["attachments"], .array([]))
        XCTAssertEqual(decoded["expected_session"], .string("chat-1"))
        XCTAssertFalse(String(decoding: body, as: UTF8.self).contains(pairingKey))
    }

    func testUnsafeAPIPathsFailBeforeTransport() async throws {
        respond()
        let api = try client()
        for path in ["https://other.example/api/status", "//other.example/api/status", "/api/../secrets",
                     "/api/%2e%2e/secrets", "/api/status?token=x", "/api/status#fragment", "/api/\\host"] {
            do {
                let _: JSONValue = try await api.request(path)
                XCTFail("Unsafe API path was accepted: \(path)")
            } catch let error as APIError {
                guard case .invalidEndpoint = error else { return XCTFail("Unexpected error: \(error)") }
            }
        }
        XCTAssertTrue(StubURLProtocol.recordedRequests().isEmpty)
    }

    func testAuthenticationFailureDoesNotExposeServerResponse() async throws {
        let api = try client()
        for status in [401, 403] {
            respond(status: status, body: "{\"error\":\"rejected \(pairingKey)\"}")
            do {
                let _: JSONValue = try await api.request("/api/status")
                XCTFail("Expected authentication rejection")
            } catch {
                XCTAssertEqual(error as? APIError, .unauthorized)
                XCTAssertFalse(error.localizedDescription.contains(pairingKey))
            }
        }
    }

    func testHTTPErrorRedactsPairingKeyAndPreservesStatus() async throws {
        respond(status: 409, body: "{\"error\":\"Busy; key \(pairingKey)\"}")
        let api = try client()
        do {
            let _: JSONValue = try await api.request("/api/chat", method: .post)
            XCTFail("Expected HTTP error")
        } catch {
            XCTAssertEqual(error as? APIError, .http(status: 409, message: "Busy; key [redacted]"))
            XCTAssertFalse(error.localizedDescription.contains(pairingKey))
        }
    }

    func testMalformedAndNonJSONResponsesFailClearly() async throws {
        let api = try client()
        respond(contentType: "text/html", body: "<html>Login portal</html>")
        do {
            let _: JSONValue = try await api.request("/api/status")
            XCTFail("HTML must not be accepted as a server payload")
        } catch { XCTAssertEqual(error as? APIError, .invalidResponse) }
        respond(body: "{broken")
        do {
            let _: JSONValue = try await api.request("/api/status")
            XCTFail("Malformed JSON must not be accepted")
        } catch { XCTAssertEqual(error as? APIError, .decoding) }
        respond(body: #"{"messages":[]}"#)
        do {
            let _: StatusResponse = try await api.request("/api/status")
            XCTFail("Missing required status fields must not imply a healthy connection")
        } catch { XCTAssertEqual(error as? APIError, .decoding) }
    }

    func testTransportErrorsDoNotExposeRequestDetails() async throws {
        StubURLProtocol.configure { _ in
            throw NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotConnectToHost,
                          userInfo: [NSLocalizedDescriptionKey: "Connection failed for secret key"])
        }
        let api = try client()
        do {
            let _: JSONValue = try await api.request("/api/status")
            XCTFail("Expected transport error")
        } catch { XCTAssertEqual(error as? APIError, .transport) }
    }

    func testRedirectDelegateNeverForwardsAuthorization() throws {
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let origin = try XCTUnwrap(URL(string: "https://herness.example/api/status"))
        let task = session.dataTask(with: origin)
        let response = try XCTUnwrap(HTTPURLResponse(url: origin, statusCode: 302, httpVersion: "HTTP/1.1", headerFields: nil))
        for destination in ["https://untrusted.example/api/status", "https://herness.example/redirected"] {
            var request = URLRequest(url: try XCTUnwrap(URL(string: destination)))
            request.setValue("Bearer " + pairingKey, forHTTPHeaderField: "Authorization")
            var invoked = false
            NoRedirectDelegate().urlSession(session, task: task, willPerformHTTPRedirection: response,
                                            newRequest: request) { forwarded in
                invoked = true
                XCTAssertNil(forwarded, "Redirects must never carry the pairing key to a new request")
            }
            XCTAssertTrue(invoked)
        }
    }

    private func bodyData(_ request: URLRequest) throws -> Data {
        if let data = request.httpBody { return data }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while true {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count == 0 { return data }
            if count < 0 { throw stream.streamError ?? URLError(.cannotDecodeRawData) }
            data.append(contentsOf: buffer.prefix(count))
        }
    }
}

private final class MemoryPairingKeys: PairingKeyStore {
    var saved: [String: String] = [:]
    var savedAfterStatus = false

    func load(server: URL) throws -> String? { saved[server.absoluteString] }
    func save(_ key: String, server: URL) throws {
        savedAfterStatus = StubURLProtocol.recordedRequests().contains { $0.url?.path == "/api/status" }
        saved[server.absoluteString] = key
    }
    func delete(server: URL) throws { saved.removeValue(forKey: server.absoluteString) }
}

final class AppStoreHandshakeTests: XCTestCase {
    @MainActor
    private func fixture() throws -> (AppStore, MemoryPairingKeys) {
        let suite = "app.herness.tests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        addTeardownBlock { UserDefaults.standard.removePersistentDomain(forName: suite) }
        let keys = MemoryPairingKeys()
        let store = AppStore(defaults: defaults, keyStore: keys, clientFactory: { endpoint, key in
            let config = URLSessionConfiguration.ephemeral
            config.protocolClasses = [StubURLProtocol.self]
            return try APIClient(baseURL: endpoint, pairingKey: key, configuration: config)
        })
        return (store, keys)
    }

    private func server(info: String, infoStatus: Int = 200, statusCode: Int = 200) {
        StubURLProtocol.configure { request in
            let payload: String
            let code: Int
            switch request.url?.path {
            case "/api/mobile/info": payload = info; code = infoStatus
            case "/api/status":
                payload = #"{"busy":false,"messages":[],"session":"chat-1","workspace":"project","model":"test-model"}"#
                code = statusCode
            case "/api/library": payload = #"{"sessions":[]}"#; code = 200
            case "/api/models/profiles": payload = #"{"profiles":[]}"#; code = 200
            case "/api/settings": payload = #"{"model":"test-model"}"#; code = 200
            default: payload = #"{"error":"Unexpected fixture request"}"#; code = 404
            }
            return (code, ["Content-Type": "application/json"], Data(payload.utf8))
        }
    }

    @MainActor
    func testSuccessfulHandshakeSavesPairingOnlyAfterStatusValidation() async throws {
        server(info: #"{"protocol":"herness-mobile","version":1,"capabilities":["chat","expected_session"]}"#)
        let (store, keys) = try fixture()
        defer { store.disconnect() }
        await store.connect(serverURL: "https://herness.example", pairingKey: String(repeating: "K", count: 43))
        XCTAssertEqual(store.connection, .connected)
        XCTAssertEqual(store.sessionName, "chat-1")
        XCTAssertEqual(keys.saved.count, 1)
        XCTAssertTrue(keys.savedAfterStatus)
        XCTAssertEqual(StubURLProtocol.recordedRequests().first?.url?.path, "/api/mobile/info")
    }

    @MainActor
    func testMissingHandshakeEndpointNeverFallsBackToStatus() async throws {
        server(info: #"{"error":"Not found"}"#, infoStatus: 404)
        let (store, keys) = try fixture()
        defer { store.disconnect() }
        await store.connect(serverURL: "https://herness.example", pairingKey: String(repeating: "K", count: 43))
        XCTAssertEqual(store.connection, .disconnected)
        XCTAssertTrue(keys.saved.isEmpty)
        XCTAssertFalse(StubURLProtocol.recordedRequests().contains { $0.url?.path == "/api/status" })
        XCTAssertNotNil(store.errorMessage)
    }

    @MainActor
    func testUnsupportedProtocolOrMissingSessionGuardRejectsPairing() async throws {
        for info in [#"{"protocol":"other","version":1,"capabilities":["expected_session"]}"#,
                     #"{"protocol":"herness-mobile","version":2,"capabilities":["expected_session"]}"#,
                     #"{"protocol":"herness-mobile","version":1,"capabilities":["chat"]}"#] {
            server(info: info)
            let (store, keys) = try fixture()
            await store.connect(serverURL: "https://herness.example", pairingKey: String(repeating: "K", count: 43))
            XCTAssertEqual(store.connection, .disconnected)
            XCTAssertTrue(keys.saved.isEmpty)
            XCTAssertFalse(StubURLProtocol.recordedRequests().contains { $0.url?.path == "/api/status" })
            store.disconnect()
        }
    }

    @MainActor
    func testRejectedStatusNeverPersistsPairingDespiteValidHandshake() async throws {
        server(info: #"{"protocol":"herness-mobile","version":1,"capabilities":["expected_session"]}"#, statusCode: 401)
        let (store, keys) = try fixture()
        defer { store.disconnect() }
        await store.connect(serverURL: "https://herness.example", pairingKey: String(repeating: "K", count: 43))
        XCTAssertEqual(store.connection, .disconnected)
        XCTAssertTrue(keys.saved.isEmpty)
        XCTAssertFalse(store.errorMessage?.contains(String(repeating: "K", count: 43)) ?? true)
    }
}
