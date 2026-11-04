import Foundation
import XCTest
@testable import Herness

final class ModelsTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    func testStatusDecodesApprovalAttachmentsAndDistinctMessagePositions() throws {
        let status = try decode(StatusResponse.self, #"""
        {"busy":true,"messages":[
          {"role":"user","text":"Inspect","index":1,"attachments":[{"name":"notes.txt","size":12,"kind":"text"}]},
          {"role":"assistant","text":"Reviewing","index":2},
          {"role":"assistant","text":"Reviewing","index":3,"bookmarked":true}],
         "streaming":"In progress","session":"session-1","workspace":"project","model":"model-name","mode":"plan",
         "approval":{"id":"approval-1","kind":"write","description":"Update notes","path":"notes.txt",
                     "diff":"-old\n+new","arguments":{"path":"notes.txt","content":"new"}},
         "command_output":"2 tests passed","future_server_field":{"supported":true}}
        """#)
        XCTAssertTrue(status.busy)
        XCTAssertEqual(status.messages.count, 3)
        XCTAssertEqual(Set(status.messages.map(\.id)).count, 3)
        XCTAssertEqual(status.messages[0].attachments.first?.name, "notes.txt")
        XCTAssertTrue(status.messages[2].bookmarked)
        XCTAssertEqual(status.approval?.id, "approval-1")
        XCTAssertEqual(status.approval?.arguments?["content"], .string("new"))
        XCTAssertEqual(status.commandOutput, "2 tests passed")
        XCTAssertEqual(status.mode, "plan")
        XCTAssertFalse(status.demo)
    }

    func testStatusRejectsMissingOrWrongRequiredFields() {
        for json in ["{}", #"{"busy":"false","messages":[]}"#,
                     #"{"busy":false,"messages":{}}"#, #"{"busy":false,"messages":[{"role":"assistant"}]}"#] {
            XCTAssertThrowsError(try decode(StatusResponse.self, json))
        }
    }

    func testOlderStatusPayloadUsesSafeOptionalDefaults() throws {
        let status = try decode(StatusResponse.self, #"{"busy":false,"messages":[]}"#)
        XCTAssertEqual(status.streaming, "")
        XCTAssertNil(status.approval)
        XCTAssertEqual(status.session, "default")
        XCTAssertFalse(status.stopping)
    }

    func testConversationAndProviderProfilesMatchServerKeys() throws {
        let library = try decode(ConversationList.self, #"{"sessions":[{"name":"chat-1","title":"Review","updated":123,"pinned":true,"folder":"Work","tags":["code"],"trashed":false}]}"#)
        XCTAssertEqual(library.sessions.first?.id, "chat-1")
        XCTAssertEqual(library.sessions.first?.tags, ["code"])
        let profiles = try decode(ModelProfileList.self, #"{"profiles":[{"id":"profile-1","name":"Local model","config":{"protocol":"chat_completions","model":"local","base_url":"http://127.0.0.1:11434/v1","demo":false},"defaults":{"max_output_tokens":2048}}]}"#)
        XCTAssertEqual(profiles.profiles.first?.config.baseURL, "http://127.0.0.1:11434/v1")
        XCTAssertEqual(profiles.profiles.first?.defaults["max_output_tokens"], 2048)
        XCTAssertFalse(try XCTUnwrap(profiles.profiles.first).config.keyPresent)
    }

    func testKnowledgeDecodesIndexingAndSourceCitationFields() throws {
        let overview = try decode(KnowledgeOverview.self, #"""
        {"configured":true,"settings":{"protocol":"openai","base_url":"https://embeddings.example/v1","model":"embedding-model","enabled":true,"batch_size":16,"key_present":true,"dimensions":384},
         "stats":{"sources":2,"chunks":12,"dimensions":384},
         "available_sources":[{"id":"doc-1","kind":"document","title":"Handbook","characters":2000,"indexable":true,"indexed":true,"stale":false}],
         "indexing":{"status":"completed","completed":2,"total":2,"chunks":12,"error":null},"requires_rebuild":false}
        """#)
        XCTAssertTrue(overview.configured)
        XCTAssertEqual(overview.stats.chunks, 12)
        XCTAssertEqual(overview.settings.dimensions, 384)
        XCTAssertEqual(overview.availableSources.first?.title, "Handbook")
        let results = try decode(KnowledgeSearchResponse.self, #"{"results":[{"id":"chunk-1","source_id":"doc-1","title":"Handbook","location":"Page 3","text":"The indexed evidence","score":0.82}],"score_description":"Cosine similarity"}"#)
        XCTAssertEqual(results.results.first?.sourceID, "doc-1")
        XCTAssertEqual(results.results.first?.location, "Page 3")
        XCTAssertNil(results.results.first?.distance)
    }

    func testJSONBooleansRemainBooleansAndNestedValuesRoundTrip() throws {
        let original = try decode(JSONValue.self, #"{"allowed":false,"count":1,"values":[null,true,"quoted\"text"]}"#)
        XCTAssertEqual(original["allowed"], .bool(false))
        XCTAssertEqual(original["count"], .number(1))
        XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(original)), original)
    }

    func testAttachmentUsesServerWireFormatWithoutLocalPathOrMetadata() throws {
        let upload = try AttachmentUpload(name: "/private/local/notes.txt", data: Data("Notes".utf8))
        let encoded = try JSONEncoder().encode(upload)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: String])
        XCTAssertEqual(Set(object.keys), Set(["name", "data"]))
        XCTAssertEqual(object["name"], "notes.txt")
        XCTAssertEqual(object["data"], Data("Notes".utf8).base64EncodedString())
        XCTAssertEqual(upload.byteCount, 5)
    }

    func testAttachmentRejectsCredentialFilesEmptyAndOversizedData() {
        for name in [".env", ".env.production", "server.pem", "secret.key", ""] {
            XCTAssertThrowsError(try AttachmentUpload(name: name, data: Data("secret".utf8)))
        }
        XCTAssertThrowsError(try AttachmentUpload(name: "empty.txt", data: Data()))
        XCTAssertThrowsError(try AttachmentUpload(name: "large.txt", data: Data(repeating: 0, count: 4_000_001)))
    }
}
