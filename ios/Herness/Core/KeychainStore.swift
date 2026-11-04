import Foundation
import Security

protocol PairingKeyStore {
    func load(server: URL) throws -> String?
    func save(_ key: String, server: URL) throws
    func delete(server: URL) throws
}

enum PairingStoreError: Error, LocalizedError {
    case unavailable
    var errorDescription: String? { "The pairing key could not be accessed securely. Unlock this iPhone and try again." }
}

struct KeychainStore: PairingKeyStore {
    private let service = "app.herness.mobile.pairing"

    private func query(server: URL) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: server.absoluteString,
         kSecAttrSynchronizable as String: false]
    }

    func load(server: URL) throws -> String? {
        var request = query(server: server)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data,
              let value = String(data: data, encoding: .utf8) else { throw PairingStoreError.unavailable }
        return value
    }

    func save(_ key: String, server: URL) throws {
        let attributes: [String: Any] = [
            kSecValueData as String: Data(key.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        ]
        let base = query(server: server)
        let status = SecItemUpdate(base as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            let inserted = SecItemAdd(base.merging(attributes) { _, new in new } as CFDictionary, nil)
            guard inserted == errSecSuccess else { throw PairingStoreError.unavailable }
        } else if status != errSecSuccess { throw PairingStoreError.unavailable }
    }

    func delete(server: URL) throws {
        let status = SecItemDelete(query(server: server) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw PairingStoreError.unavailable }
    }
}
