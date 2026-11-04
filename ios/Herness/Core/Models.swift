import Foundation

enum ConnectionState: String, Equatable {
    case disconnected, connecting, connected, demo

    var label: String {
        switch self {
        case .disconnected: return "Disconnected"
        case .connecting: return "Connecting…"
        case .connected: return "Connected"
        case .demo: return "Local demo"
        }
    }
}

/// A typed JSON value for tool arguments and small API request bodies.
enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let item = try? value.decode(Bool.self) { self = .bool(item) }
        else if let item = try? value.decode(Double.self) { self = .number(item) }
        else if let item = try? value.decode(String.self) { self = .string(item) }
        else if let item = try? value.decode([JSONValue].self) { self = .array(item) }
        else { self = .object(try value.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .object(let item): try value.encode(item)
        case .array(let item): try value.encode(item)
        case .string(let item): try value.encode(item)
        case .number(let item): try value.encode(item)
        case .bool(let item): try value.encode(item)
        case .null: try value.encodeNil()
        }
    }

    subscript(_ key: String) -> JSONValue? {
        guard case .object(let object) = self else { return nil }
        return object[key]
    }

    var stringValue: String? {
        guard case .string(let value) = self else { return nil }
        return value
    }

    var prettyPrinted: String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(self) else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
}

extension KeyedDecodingContainer {
    func fallback<T: Decodable>(_ key: Key, _ defaultValue: T) -> T {
        (try? decodeIfPresent(T.self, forKey: key)) ?? defaultValue
    }

    func optional<T: Decodable>(_ key: Key, as type: T.Type = T.self) -> T? {
        try? decodeIfPresent(type, forKey: key)
    }
}

struct AttachmentSummary: Decodable, Identifiable, Equatable {
    let name: String
    let size: Int
    let kind: String
    var id: String { name + ":" + String(size) }

    enum CodingKeys: String, CodingKey { case name, size, kind }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        name = try values.decode(String.self, forKey: .name)
        size = values.fallback(.size, 0)
        kind = values.fallback(.kind, "file")
    }
}

struct AttachmentUpload: Encodable, Identifiable, Equatable {
    let id = UUID()
    let name: String
    let base64: String
    let byteCount: Int

    enum CodingKeys: String, CodingKey { case name; case base64 = "data" }

    init(name: String, data: Data) throws {
        let clean = name.replacingOccurrences(of: "\\", with: "/").components(separatedBy: "/").last ?? ""
        guard !clean.isEmpty, clean.count <= 200, clean != ".env", !clean.hasPrefix(".env."),
              !clean.hasSuffix(".pem"), !clean.hasSuffix(".key") else {
            throw APIError.invalidInput("Choose a file with a valid name. Credential files cannot be attached.")
        }
        guard !data.isEmpty, data.count <= 4_000_000 else {
            throw APIError.invalidInput("Files must be nonempty and no larger than 4 MB each.")
        }
        self.name = clean
        self.base64 = data.base64EncodedString()
        self.byteCount = data.count
    }
}

struct ChatMessage: Decodable, Identifiable, Equatable {
    let role: String
    let text: String
    let index: Int?
    let attachments: [AttachmentSummary]
    let bookmarked: Bool
    var position = 0
    var id: String { "\(position)-\(role)" }

    enum CodingKeys: String, CodingKey { case role, text, index, attachments, bookmarked }

    init(role: String, text: String, position: Int = 0) {
        self.role = role
        self.text = text
        self.index = nil
        self.attachments = []
        self.bookmarked = false
        self.position = position
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        role = try values.decode(String.self, forKey: .role)
        text = try values.decode(String.self, forKey: .text)
        index = values.optional(.index)
        attachments = values.fallback(.attachments, [])
        bookmarked = values.fallback(.bookmarked, false)
    }
}

struct ApprovalRequest: Decodable, Identifiable, Equatable {
    let id: String
    let kind: String
    let description: String
    let path: String?
    let diff: String?
    let content: String?
    let arguments: JSONValue?

    enum CodingKeys: String, CodingKey { case id, kind, description, path, diff, content, arguments }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        kind = values.fallback(.kind, "action")
        description = values.fallback(.description, "Review this action before allowing it.")
        path = values.optional(.path)
        diff = values.optional(.diff)
        content = values.optional(.content)
        arguments = values.optional(.arguments)
    }
}

struct StatusResponse: Decodable {
    let busy: Bool
    let messages: [ChatMessage]
    let streaming: String
    let approval: ApprovalRequest?
    let workspace: String
    let session: String
    let model: String
    let mode: String
    let demo: Bool
    let stopping: Bool
    let commandOutput: String

    enum CodingKeys: String, CodingKey {
        case busy, messages, streaming, approval, workspace, session, model, mode, demo, stopping
        case commandOutput = "command_output"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        busy = try values.decode(Bool.self, forKey: .busy)
        messages = try values.decode([ChatMessage].self, forKey: .messages).enumerated().map { offset, message in
            var ordered = message
            ordered.position = offset
            return ordered
        }
        streaming = values.fallback(.streaming, "")
        approval = values.optional(.approval)
        workspace = values.fallback(.workspace, "")
        session = values.fallback(.session, "default")
        model = values.fallback(.model, "")
        mode = values.fallback(.mode, "act")
        demo = values.fallback(.demo, false)
        stopping = values.fallback(.stopping, false)
        commandOutput = values.fallback(.commandOutput, "")
    }
}

struct ConversationSummary: Decodable, Identifiable, Equatable {
    let name: String
    let title: String
    let updated: Double
    let pinned: Bool
    let folder: String
    let tags: [String]
    let trashed: Bool
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, title, updated, pinned, folder, tags, trashed }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        name = try values.decode(String.self, forKey: .name)
        title = values.fallback(.title, name)
        updated = values.fallback(.updated, 0)
        pinned = values.fallback(.pinned, false)
        folder = values.fallback(.folder, "")
        tags = values.fallback(.tags, [])
        trashed = values.fallback(.trashed, false)
    }
}

struct ConversationList: Decodable { let sessions: [ConversationSummary] }

struct ModelConfiguration: Decodable, Equatable {
    let protocolName: String
    let model: String
    let baseURL: String
    let demo: Bool
    let keyPresent: Bool

    enum CodingKeys: String, CodingKey {
        case protocolName = "protocol", model, baseURL = "base_url", demo, keyPresent = "key_present"
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        protocolName = values.fallback(.protocolName, "chat_completions")
        model = values.fallback(.model, "")
        baseURL = values.fallback(.baseURL, "")
        demo = values.fallback(.demo, false)
        keyPresent = values.fallback(.keyPresent, false)
    }
}

struct ModelProfile: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let config: ModelConfiguration
    let defaults: [String: Double]

    enum CodingKeys: String, CodingKey { case id, name, config, defaults }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        name = try values.decode(String.self, forKey: .name)
        config = try values.decode(ModelConfiguration.self, forKey: .config)
        defaults = values.fallback(.defaults, [:])
    }
}

struct ModelProfileList: Decodable { let profiles: [ModelProfile] }

struct EmbeddingSettings: Decodable, Equatable {
    let protocolName: String
    let baseURL: String
    let model: String
    let enabled: Bool
    let batchSize: Int
    let keyPresent: Bool
    let dimensions: Int?

    enum CodingKeys: String, CodingKey {
        case protocolName = "protocol", baseURL = "base_url", model, enabled
        case batchSize = "batch_size", keyPresent = "key_present", dimensions
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        protocolName = values.fallback(.protocolName, "openai")
        baseURL = values.fallback(.baseURL, "")
        model = values.fallback(.model, "")
        enabled = values.fallback(.enabled, false)
        batchSize = values.fallback(.batchSize, 16)
        keyPresent = values.fallback(.keyPresent, false)
        dimensions = values.optional(.dimensions)
    }
}

struct KnowledgeStats: Decodable, Equatable {
    let sources: Int
    let chunks: Int
    let dimensions: Int?
    enum CodingKeys: String, CodingKey { case sources, chunks, dimensions }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sources = values.fallback(.sources, 0)
        chunks = values.fallback(.chunks, 0)
        dimensions = values.optional(.dimensions)
    }
}

struct KnowledgeSource: Decodable, Identifiable, Equatable {
    let id: String
    let kind: String
    let title: String
    let characters: Int
    let indexable: Bool
    let indexed: Bool
    let stale: Bool
    enum CodingKeys: String, CodingKey { case id, kind, title, characters, indexable, indexed, stale }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        kind = values.fallback(.kind, "document")
        title = values.fallback(.title, id)
        characters = values.fallback(.characters, 0)
        indexable = values.fallback(.indexable, false)
        indexed = values.fallback(.indexed, false)
        stale = values.fallback(.stale, false)
    }
}

struct IndexingJob: Decodable, Equatable {
    let status: String
    let completed: Int
    let total: Int
    let chunks: Int
    let current: String?
    let error: String?
    enum CodingKeys: String, CodingKey { case status, completed, total, chunks, current, error }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        status = values.fallback(.status, "idle")
        completed = values.fallback(.completed, 0)
        total = values.fallback(.total, 0)
        chunks = values.fallback(.chunks, 0)
        current = values.optional(.current)
        error = values.optional(.error)
    }
}

struct KnowledgeOverview: Decodable, Equatable {
    let configured: Bool
    let settings: EmbeddingSettings
    let stats: KnowledgeStats
    let availableSources: [KnowledgeSource]
    let indexing: IndexingJob
    let requiresRebuild: Bool
    let error: String?
    enum CodingKeys: String, CodingKey {
        case configured, settings, stats, availableSources = "available_sources", indexing
        case requiresRebuild = "requires_rebuild", error
    }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        configured = try values.decode(Bool.self, forKey: .configured)
        settings = try values.decode(EmbeddingSettings.self, forKey: .settings)
        stats = try values.decode(KnowledgeStats.self, forKey: .stats)
        availableSources = values.fallback(.availableSources, [])
        indexing = try values.decode(IndexingJob.self, forKey: .indexing)
        requiresRebuild = values.fallback(.requiresRebuild, false)
        error = values.optional(.error)
    }
}

struct KnowledgeHit: Decodable, Identifiable, Equatable {
    let id: String
    let sourceID: String
    let kind: String
    let title: String
    let location: String
    let text: String
    let score: Double?
    let distance: Double?
    enum CodingKeys: String, CodingKey { case id, sourceID = "source_id", kind, title, location, text, score, distance }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        sourceID = try values.decode(String.self, forKey: .sourceID)
        kind = values.fallback(.kind, "document")
        title = values.fallback(.title, sourceID)
        location = values.fallback(.location, "Source excerpt")
        text = try values.decode(String.self, forKey: .text)
        score = values.optional(.score)
        distance = values.optional(.distance)
    }
}

struct KnowledgeSearchResponse: Decodable {
    let results: [KnowledgeHit]
    let message: String?
    let scoreDescription: String?
    enum CodingKeys: String, CodingKey { case results, message, scoreDescription = "score_description" }
}
