import Foundation
import Combine

/// The server owns the conversation. Polls replace cumulative streaming text;
/// mutations are sent once, after refreshing the authoritative server state.
@MainActor
final class AppStore: ObservableObject {
    @Published private(set) var connection: ConnectionState = .disconnected
    @Published var serverURL: String
    @Published private(set) var messages: [ChatMessage] = []
    @Published private(set) var streaming = ""
    @Published private(set) var busy = false
    @Published private(set) var stopping = false
    @Published private(set) var approval: ApprovalRequest?
    @Published private(set) var sessionName = "default"
    @Published private(set) var modelName = ""
    @Published private(set) var workspace = ""
    @Published private(set) var runMode = "act"
    @Published private(set) var commandOutput = ""
    @Published private(set) var serverDemo = false
    @Published private(set) var conversations: [ConversationSummary] = []
    @Published private(set) var modelProfiles: [ModelProfile] = []
    @Published private(set) var modelSettings: ModelConfiguration?
    @Published private(set) var knowledge: KnowledgeOverview?
    @Published private(set) var searchResults: [KnowledgeHit] = []
    @Published var errorMessage: String?
    @Published var noticeMessage: String?
    @Published private(set) var isWorking = false
    @Published private(set) var isForeground = true

    var demoMode: Bool { connection == .demo }
    var canSend: Bool { connection == .connected && isForeground && !busy && !isWorking }

    private let defaults: UserDefaults
    private let keyStore: any PairingKeyStore
    private let clientFactory: (URL, String) throws -> APIClient
    private var client: APIClient?
    private var pollTask: Task<Void, Never>?
    private var pollIdentity: UUID?
    private var generation = UUID()
    private var knowledgeSearchGeneration = UUID()
    private var historyGeneration = UUID()
    private static let endpointPreference = "herness.serverOrigin"

    init(defaults: UserDefaults = .standard, keyStore: any PairingKeyStore = KeychainStore(),
         clientFactory: @escaping (URL, String) throws -> APIClient = {
             try APIClient(baseURL: $0, pairingKey: $1)
         }) {
        self.defaults = defaults
        self.keyStore = keyStore
        self.clientFactory = clientFactory
        self.serverURL = defaults.string(forKey: Self.endpointPreference) ?? ""
    }

    func connect(serverURL: String, pairingKey: String) async {
        let attempt = UUID()
        generation = attempt
        stopPolling()
        client?.cancelRequests()
        client = nil
        isWorking = false
        clearSnapshots()
        connection = .connecting
        errorMessage = nil
        noticeMessage = nil
        do {
            let endpoint = try APIClient.validatedBaseURL(serverURL)
            let key = pairingKey.trimmingCharacters(in: .whitespacesAndNewlines)
            let candidate = try clientFactory(endpoint, key)
            client = candidate // Allows foreground lifecycle to cancel the handshake too.
            let info: JSONValue = try await candidate.request("/api/mobile/info")
            guard info["protocol"] == .string("herness-mobile"), info["version"] == .number(1),
                  let capabilityValue = info["capabilities"], case .array(let capabilities) = capabilityValue,
                  capabilities.contains(.string("expected_session")) else {
                throw APIError.invalidInput("This server does not support the required mobile session protection. Update Herness and start its mobile API server.")
            }
            guard attempt == generation, isForeground, !Task.isCancelled else {
                candidate.cancelRequests()
                if attempt == generation { connection = .disconnected; client = nil }
                return
            }
            let status: StatusResponse = try await candidate.request("/api/status")
            guard attempt == generation, isForeground, !Task.isCancelled else {
                candidate.cancelRequests()
                if attempt == generation { connection = .disconnected; client = nil }
                return
            }
            // Persist only after this exact server accepted the key.
            try keyStore.save(key, server: endpoint)
            self.serverURL = endpoint.absoluteString
            defaults.set(endpoint.absoluteString, forKey: Self.endpointPreference)
            client = candidate
            connection = .connected
            apply(status)
            await refreshHistory()
            guard attempt == generation else { return }
            await refreshProfiles()
            startPolling()
        } catch {
            guard attempt == generation else { return }
            client?.cancelRequests()
            client = nil
            connection = .disconnected
            report(error)
        }
    }

    func reconnect() async {
        // XCTest's app host must never reconnect to a developer's real server.
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return }
        guard connection != .connecting, connection != .connected, connection != .demo,
              !serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, isForeground else { return }
        do {
            let endpoint = try APIClient.validatedBaseURL(serverURL)
            guard let key = try keyStore.load(server: endpoint) else {
                noticeMessage = "Enter your server’s pairing key to connect this iPhone."
                return
            }
            await connect(serverURL: endpoint.absoluteString, pairingKey: key)
        } catch { report(error) }
    }

    func disconnect(forgetPairing: Bool = false) {
        generation = UUID()
        stopPolling()
        client?.cancelRequests()
        client = nil
        connection = .disconnected
        isWorking = false
        errorMessage = nil
        noticeMessage = nil
        clearSnapshots()
        if forgetPairing {
            do {
                if !serverURL.isEmpty {
                    try keyStore.delete(server: APIClient.validatedBaseURL(serverURL))
                }
                defaults.removeObject(forKey: Self.endpointPreference)
                serverURL = ""
                noticeMessage = "This iPhone’s saved pairing has been removed."
            } catch { report(error) }
        }
    }

    func setSceneActive(_ active: Bool) {
        isForeground = active
        approval = nil
        if active {
            startPolling()
        } else {
            stopPolling()
            client?.cancelRequests()
            // No operation is replayed on return. A fresh status poll reconciles
            // a mutation whose HTTP response was interrupted by backgrounding.
            if isWorking { noticeMessage = "Connection paused. Review the current server state before retrying an interrupted action." }
        }
    }

    func dismissError() { errorMessage = nil }

    func loadDemo() {
        disconnect()
        connection = .demo
        modelName = "Local demo · no model connected"
        sessionName = "local-demo"
        messages = [
            ChatMessage(role: "user", text: "What can I do with Herness?", position: 0),
            ChatMessage(role: "assistant", text: "This is a local sample conversation. Connect your Herness server to chat with your models, review tool approvals, switch conversations, and search your indexed knowledge.", position: 1),
            ChatMessage(role: "assistant", text: "Your server owns the workspace and runs the agent. This native iPhone app securely connects to it. No model requests are made in this demo.", position: 2)
        ]
        noticeMessage = "Local demo — sample content only. Connect a server to use live features."
    }

    @discardableResult
    func send(prompt: String, attachments: [AttachmentUpload] = [], expectedSession: String) async -> Bool {
        let clean = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty, clean.count <= 32_000 else {
            errorMessage = "Enter a message of 1–32,000 characters."
            return false
        }
        guard attachments.count <= 4, attachments.reduce(0, { $0 + $1.byteCount }) <= 8_000_000 else {
            errorMessage = "Attach up to 4 files with a combined size of at most 8 MB."
            return false
        }
        return await mutate { api, status in
            guard !status.busy else { throw APIError.invalidInput("A turn is already running. Stop it or wait for it to finish.") }
            guard status.session == expectedSession else {
                throw APIError.invalidInput("Another client switched the conversation. Review the current conversation, then send again.")
            }
            let files = attachments.map { JSONValue.object(["name": .string($0.name), "data": .string($0.base64)]) }
            let _: JSONValue = try await api.request("/api/chat", method: .post,
                body: .object(["prompt": .string(clean), "attachments": .array(files),
                               "expected_session": .string(expectedSession)]))
        }
    }

    func stop(expectedSession: String) async {
        await mutate { api, status in
            guard status.session == expectedSession else {
                throw APIError.invalidInput("Another client switched the conversation. Review the current turn before stopping it.")
            }
            guard status.busy else { return }
            let _: JSONValue = try await api.request("/api/stop", method: .post,
                body: .object(["expected_session": .string(expectedSession)]))
        }
    }

    func approve(allow: Bool, reviewedID: String) async {
        guard approval?.id == reviewedID else {
            errorMessage = "The reviewed approval changed or expired. Review the current request."
            return
        }
        let expectedSession = sessionName
        await mutate { api, status in
            guard status.session == expectedSession, status.approval?.id == reviewedID else {
                throw APIError.invalidInput("This approval expired or changed. Review the current request.")
            }
            let _: JSONValue = try await api.request("/api/approve", method: .post,
                body: .object(["id": .string(reviewedID), "allow": .bool(allow),
                               "expected_session": .string(expectedSession)]))
        }
    }

    func loadConversation(name: String) async {
        let expectedSession = sessionName
        await mutate { api, status in
            guard status.session == expectedSession else {
                throw APIError.invalidInput("Another client changed the active conversation. Review it, then choose a conversation again.")
            }
            guard !status.busy else { throw APIError.invalidInput("Finish or stop the current turn before changing conversations.") }
            let _: JSONValue = try await api.request("/api/session", method: .post,
                body: .object(["name": .string(name), "expected_session": .string(expectedSession)]))
        }
        await refreshHistory()
    }

    func newConversation() async {
        await loadConversation(name: "chat-" + UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: ""))
    }

    func refreshHistory(query: String = "") async {
        guard let api = readableClient() else { return }
        let current = generation
        let search = UUID()
        historyGeneration = search
        do {
            let result: ConversationList = try await api.request("/api/library", query: [URLQueryItem(name: "q", value: query)])
            guard current == generation, search == historyGeneration, isForeground else { return }
            conversations = result.sessions
        } catch { if current == generation { report(error) } }
    }

    func refreshProfiles() async {
        guard let api = readableClient() else { return }
        let current = generation
        do {
            let profiles: ModelProfileList = try await api.request("/api/models/profiles")
            guard current == generation, isForeground else { return }
            modelProfiles = profiles.profiles
            let settings: ModelConfiguration = try await api.request("/api/settings")
            guard current == generation, isForeground else { return }
            modelSettings = settings
        } catch { if current == generation { report(error) } }
    }

    func selectProfile(id: String) async {
        await mutate { api, status in
            guard !status.busy else { throw APIError.invalidInput("Finish or stop the current turn before changing models.") }
            let _: JSONValue = try await api.request("/api/models/profiles", method: .post,
                body: .object(["action": .string("select"), "id": .string(id)]))
        }
        await refreshProfiles()
    }

    func setRunMode(_ mode: String) async {
        guard ["act", "plan"].contains(mode) else { return }
        await mutate { api, status in
            guard !status.busy else { throw APIError.invalidInput("Finish or stop the current turn before changing modes.") }
            let _: JSONValue = try await api.request("/api/run/options", method: .post, body: .object(["mode": .string(mode)]))
        }
    }

    func refreshKnowledge() async {
        guard let api = readableClient() else { return }
        let current = generation
        let requestedWorkspace = workspace
        do {
            let result: KnowledgeOverview = try await api.request("/api/vectors")
            guard current == generation, requestedWorkspace == workspace, isForeground else { return }
            knowledge = result
        } catch { if current == generation { report(error) } }
    }

    func searchKnowledge(query: String, kinds: [String]? = nil) async {
        searchResults = []
        noticeMessage = nil
        let search = UUID()
        knowledgeSearchGeneration = search
        guard let api = readableClient() else { return }
        let clean = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty, clean.count <= 4000 else {
            errorMessage = "Enter a knowledge query of 1–4,000 characters."
            return
        }
        let current = generation
        var body: [String: JSONValue] = ["query": .string(clean), "limit": .number(10)]
        if let kinds, !kinds.isEmpty { body["kinds"] = .array(kinds.map(JSONValue.string)) }
        do {
            let result: KnowledgeSearchResponse = try await api.request("/api/vectors/search", method: .post, body: .object(body))
            guard current == generation, search == knowledgeSearchGeneration, isForeground else { return }
            searchResults = result.results
            noticeMessage = result.message ?? result.scoreDescription
        } catch {
            if current == generation, search == knowledgeSearchGeneration {
                searchResults = []
                report(error)
            }
        }
    }

    func indexKnowledge(sourceIDs: [String], force: Bool = false) async {
        guard !sourceIDs.isEmpty, sourceIDs.count <= 100 else {
            errorMessage = "Select 1–100 available sources to index."
            return
        }
        await mutate { api, _ in
            let _: JSONValue = try await api.request("/api/vectors/index", method: .post,
                body: .object(["source_ids": .array(sourceIDs.map(JSONValue.string)), "force": .bool(force)]))
        }
        await refreshKnowledge()
    }

    @discardableResult
    func uploadKnowledgeDocument(name: String, data: Data) async -> Bool {
        do {
            let upload = try AttachmentUpload(name: name, data: data)
            let expectedWorkspace = workspace
            let accepted = await mutate { api, status in
                guard status.workspace == expectedWorkspace else {
                    throw APIError.invalidInput("Another client changed the project. Review the current project before uploading this document.")
                }
                let _: JSONValue = try await api.request("/api/documents/upload", method: .post,
                    body: .object(["name": .string(upload.name), "data": .string(upload.base64)]))
            }
            if accepted { await refreshKnowledge() }
            return accepted
        } catch {
            report(error)
            return false
        }
    }

    func stopIndexing() async {
        await mutate { api, _ in
            let _: JSONValue = try await api.request("/api/vectors/stop", method: .post)
        }
        await refreshKnowledge()
    }

    /// The caller presents a confirmation dialog before invoking this method.
    func clearKnowledge() async {
        await mutate { api, _ in
            let _: JSONValue = try await api.request("/api/vectors/clear", method: .post,
                body: .object(["confirmed": .bool(true)]))
        }
        searchResults = []
        await refreshKnowledge()
    }

    func configureEmbeddings(protocolName: String, baseURL: String, model: String,
                             apiKey: String, enabled: Bool = true) async {
        var body: [String: JSONValue] = ["protocol": .string(protocolName), "base_url": .string(baseURL),
            "model": .string(model), "enabled": .bool(enabled), "batch_size": .number(16)]
        if !apiKey.isEmpty { body["api_key"] = .string(apiKey) }
        await mutate { api, _ in
            let _: JSONValue = try await api.request("/api/vectors/settings", method: .post, body: .object(body))
        }
        searchResults = []
        await refreshKnowledge()
    }

    func testEmbeddings() async {
        await mutate { api, _ in
            let result: JSONValue = try await api.request("/api/vectors/test", method: .post)
            self.noticeMessage = result["message"]?.stringValue ?? "Embedding connection successful."
        }
    }

    private func readableClient() -> APIClient? {
        guard connection == .connected, isForeground, let client else {
            if connection == .demo { noticeMessage = "Connect a server to use live features. This local demo does not make API requests." }
            return nil
        }
        return client
    }

    /// Returns true once the mutation was accepted, even if its follow-up status
    /// read failed. A chat composer must not invite a duplicate send in that case.
    @discardableResult
    private func mutate(_ operation: (APIClient, StatusResponse) async throws -> Void) async -> Bool {
        guard let api = readableClient() else {
            if !demoMode { errorMessage = "Connect to your Herness server first." }
            return false
        }
        guard !isWorking else {
            errorMessage = "Wait for the current action to finish."
            return false
        }
        isWorking = true
        errorMessage = nil
        let current = generation
        stopPolling()
        var accepted = false
        var mutationBegan = false
        defer {
            if current == generation {
                isWorking = false
                startPolling()
            }
        }
        do {
            let status: StatusResponse = try await api.request("/api/status")
            guard current == generation, isForeground else { return false }
            apply(status)
            mutationBegan = true
            try await operation(api, status)
            accepted = true
            guard current == generation, isForeground else { return accepted }
            let updated: StatusResponse = try await api.request("/api/status")
            guard current == generation, isForeground else { return accepted }
            apply(updated)
        } catch {
            if current == generation {
                report(error)
                if mutationBegan && ((error is CancellationError) || (error as? APIError) == .transport) {
                    noticeMessage = "The action’s outcome could not be confirmed. Review the server state before retrying; the app will not resend it."
                }
            }
        }
        return accepted
    }

    private func apply(_ status: StatusResponse) {
        if workspace != status.workspace {
            knowledge = nil
            searchResults = []
            knowledgeSearchGeneration = UUID()
        }
        messages = status.messages
        streaming = status.streaming
        busy = status.busy
        stopping = status.stopping
        approval = isForeground ? status.approval : nil
        sessionName = status.session
        modelName = status.model
        workspace = status.workspace
        runMode = status.mode
        serverDemo = status.demo
        commandOutput = status.commandOutput
    }

    private func startPolling() {
        guard pollTask == nil, !isWorking, isForeground, connection == .connected, let api = client else { return }
        let current = generation
        let identity = UUID()
        pollIdentity = identity
        pollTask = Task { [weak self] in
            defer {
                if self?.pollIdentity == identity {
                    self?.pollTask = nil
                    self?.pollIdentity = nil
                }
            }
            while !Task.isCancelled {
                do {
                    let status: StatusResponse = try await api.request("/api/status")
                    guard let self, current == self.generation, self.isForeground, !Task.isCancelled else { return }
                    let finished = self.busy && !status.busy
                    self.apply(status)
                    if finished { await self.refreshHistory() }
                    if self.knowledge?.indexing.status == "running" { await self.refreshKnowledge() }
                    try await Task.sleep(nanoseconds: status.busy ? 900_000_000 : 2_500_000_000)
                } catch is CancellationError {
                    // A URLSession task can be cancelled while the poll loop
                    // itself remains active during a rapid scene transition.
                    guard !Task.isCancelled, let self, current == self.generation, self.isForeground else { return }
                    continue
                }
                catch {
                    guard let self, current == self.generation, !Task.isCancelled else { return }
                    self.report(error, lostConnection: true)
                    return
                }
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
        pollIdentity = nil
    }

    private func report(_ error: Error, lostConnection: Bool = false) {
        guard !(error is CancellationError) else { return }
        errorMessage = (error as? LocalizedError)?.errorDescription ?? "The request could not be completed."
        let unauthorized = (error as? APIError) == .unauthorized
        if unauthorized || lostConnection {
            generation = UUID()
            stopPolling()
            client?.cancelRequests()
            client = nil
            connection = .disconnected
            approval = nil
            busy = false
            stopping = false
            isWorking = false
        }
    }

    private func clearSnapshots() {
        messages = []
        streaming = ""
        busy = false
        stopping = false
        approval = nil
        sessionName = "default"
        modelName = ""
        workspace = ""
        runMode = "act"
        commandOutput = ""
        serverDemo = false
        conversations = []
        modelProfiles = []
        modelSettings = nil
        knowledge = nil
        searchResults = []
    }
}
