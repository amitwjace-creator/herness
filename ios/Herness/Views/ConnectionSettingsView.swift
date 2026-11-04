import SwiftUI

@MainActor
struct ConnectionSettingsView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var serverURL = ""
    @State private var pairingKey = ""
    @State private var connecting = false
    @State private var confirmForget = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Connect to the Herness server running on your computer or cloud workspace.")
                        .foregroundStyle(HernessTheme.secondaryText)
                }
                Section {
                    TextField("https://your-herness.example.com", text: $serverURL)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityLabel("Server HTTPS URL")
                    SecureField("Pairing key", text: $pairingKey)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().privacySensitive()
                        .accessibilityLabel("Server pairing key")
                    Button {
                        connecting = true
                        Task {
                            await store.connect(serverURL: serverURL, pairingKey: pairingKey)
                            connecting = false
                            if store.connection == .connected { pairingKey = ""; dismiss() }
                        }
                    } label: {
                        HStack {
                            Text(connecting ? "Connecting…" : "Connect securely")
                            Spacer()
                            if connecting { ProgressView() }
                        }
                    }
                    .disabled(connecting || serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pairingKey.isEmpty)
                } header: {
                    Text("Pair your server")
                } footer: {
                    Text("Use HTTPS with a trusted certificate. The pairing key is saved in the iOS Keychain. Your model API keys remain on the server.")
                }
                if !store.serverURL.isEmpty {
                    Section("Saved connection") {
                        Text(store.serverURL).font(.footnote).textSelection(.enabled)
                        LabeledContent("Status", value: store.connection.label)
                        Button("Reconnect") { Task { await store.reconnect(); if store.connection == .connected { dismiss() } } }
                            .disabled(connecting || store.isWorking)
                        Button("Disconnect", role: .destructive) { store.disconnect(); dismiss() }
                        Button("Forget saved pairing", role: .destructive) { confirmForget = true }
                    }
                }
                Section {
                    Button("Try offline demo") { pairingKey = ""; store.loadDemo(); dismiss() }
                } footer: {
                    Text("Explore a clearly labeled sample conversation. The demo does not generate replies or contact a server or model.")
                }
                Section { StoreFeedback() }.listRowBackground(Color.clear)
            }
            .scrollContentBackground(.hidden)
            .background(HernessTheme.background)
            .navigationTitle("Server connection")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { pairingKey = ""; dismiss() } } }
            .onAppear { serverURL = store.serverURL }
            .onDisappear { pairingKey = "" }
            .confirmationDialog("Forget this server’s saved pairing?", isPresented: $confirmForget, titleVisibility: .visible) {
                Button("Forget pairing", role: .destructive) { pairingKey = ""; store.disconnect(forgetPairing: true); serverURL = "" }
                Button("Cancel", role: .cancel) {}
            } message: { Text("This removes the key from this iPhone. Your server and conversations are unchanged.") }
        }
    }
}
