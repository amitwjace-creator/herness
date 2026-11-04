import SwiftUI

@MainActor
struct ConversationHistoryView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    let onKnowledge: () -> Void
    let onConnection: () -> Void

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button("New conversation", systemImage: "square.and.pencil") {
                        Task { await store.newConversation(); if store.errorMessage == nil { dismiss() } }
                    }
                    .disabled(store.busy || store.connection != .connected)
                    Button("Knowledge", systemImage: "books.vertical", action: onKnowledge)
                }
                Section("Recent conversations") {
                    if store.conversations.isEmpty {
                        Text(query.isEmpty ? "Your saved conversations will appear here." : "No matching conversations.")
                            .foregroundStyle(HernessTheme.secondaryText)
                    }
                    ForEach(store.conversations.filter { !$0.trashed }) { conversation in
                        Button {
                            Task { await store.loadConversation(name: conversation.name); if store.errorMessage == nil { dismiss() } }
                        } label: {
                            HStack(spacing: 10) {
                                if conversation.pinned { Image(systemName: "pin.fill").font(.caption).foregroundStyle(HernessTheme.accent) }
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(conversation.title).foregroundStyle(HernessTheme.primaryText).lineLimit(2)
                                    Text(Date(timeIntervalSince1970: conversation.updated), style: .relative)
                                        .font(.caption).foregroundStyle(HernessTheme.secondaryText)
                                }
                                Spacer()
                                if conversation.name == store.sessionName { Image(systemName: "checkmark").foregroundStyle(HernessTheme.accent) }
                            }
                        }
                        .disabled(store.busy || store.connection != .connected)
                    }
                }
                Section {
                    Button(action: onConnection) { Label("Server connection", systemImage: "network") }
                    LabeledContent("Status", value: store.connection.label)
                }
                if store.busy {
                    Section { Text("Finish or stop the current response before switching conversations.").font(.footnote).foregroundStyle(HernessTheme.secondaryText) }
                }
                Section { StoreFeedback() }.listRowBackground(Color.clear)
            }
            .scrollContentBackground(.hidden)
            .background(HernessTheme.background)
            .searchable(text: $query, prompt: "Search conversations")
            .navigationTitle("Conversations")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task(id: query) {
                try? await Task.sleep(for: .milliseconds(250))
                guard !Task.isCancelled else { return }
                await store.refreshHistory(query: query)
            }
            .refreshable { await store.refreshHistory(query: query) }
        }
    }
}
