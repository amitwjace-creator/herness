import SwiftUI

@MainActor
struct ModelPickerView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    let onConnection: () -> Void

    var body: some View {
        NavigationStack {
            List {
                Section("Current model") {
                    LabeledContent("Model", value: store.modelName.isEmpty ? "Not connected" : store.modelName)
                    if store.connection == .demo { Text("Offline demo shows a sample conversation, without a model API.").font(.footnote).foregroundStyle(HernessTheme.secondaryText) }
                }
                Section("Saved model profiles") {
                    ForEach(store.modelProfiles) { profile in
                        Button {
                            Task { await store.selectProfile(id: profile.id); if store.errorMessage == nil { dismiss() } }
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(profile.name).foregroundStyle(HernessTheme.primaryText)
                                    Text(profile.config.model).font(.caption).foregroundStyle(HernessTheme.secondaryText)
                                }
                                Spacer()
                                if profile.config.model == store.modelName { Image(systemName: "checkmark").foregroundStyle(HernessTheme.accent) }
                            }
                        }
                        .disabled(store.busy || store.isWorking)
                    }
                    if store.modelProfiles.isEmpty {
                        Text("Save model profiles in your Herness workspace to switch between them here. Provider credentials stay on your server.")
                            .font(.footnote).foregroundStyle(HernessTheme.secondaryText)
                    }
                }
                Section { Button("Server connection", systemImage: "network", action: onConnection) }
                Section { StoreFeedback() }.listRowBackground(Color.clear)
            }
            .scrollContentBackground(.hidden)
            .background(HernessTheme.background)
            .navigationTitle("Models")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task { await store.refreshProfiles() }
            .refreshable { await store.refreshProfiles() }
        }
    }
}
