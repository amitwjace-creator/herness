import SwiftUI
import UniformTypeIdentifiers

private struct KnowledgeDocumentContext: Equatable {
    let server: String
    let workspace: String
}

private enum KnowledgeSection: String, CaseIterable, Identifiable {
    case search = "Search", sources = "Sources", embeddings = "Embeddings"
    var id: String { rawValue }
}

@MainActor
struct KnowledgeView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var section: KnowledgeSection = .search
    @State private var query = ""
    @State private var kind = "all"
    @State private var hits: [KnowledgeHit] = []
    @State private var searched = false
    @State private var searching = false
    @State private var selected = Set<String>()
    @State private var confirmClear = false
    @State private var protocolName = "ollama"
    @State private var embeddingURL = "http://127.0.0.1:11434"
    @State private var embeddingModel = "nomic-embed-text"
    @State private var embeddingKey = ""
    @State private var settingsLoaded = false
    @State private var settingsDirty = false
    @State private var saving = false
    @State private var importingDocument = false
    @State private var uploadingDocument = false
    @State private var documentContext: KnowledgeDocumentContext?
    @State private var documentError: String?
    let useExcerpt: (String) -> Void

    private var currentDocumentContext: KnowledgeDocumentContext {
        KnowledgeDocumentContext(server: store.serverURL, workspace: store.workspace)
    }
    private var documentTypes: [UTType] {
        [.plainText, .pdf, .json, .sourceCode, .commaSeparatedText, .html, .xml]
            + ["docx", "xlsx", "pptx"].compactMap { UTType(filenameExtension: $0) }
    }
    private var indexing: Bool { store.knowledge?.indexing.status == "running" }
    private var ready: Bool {
        guard let knowledge = store.knowledge else { return false }
        return knowledge.configured && !knowledge.requiresRebuild && knowledge.error == nil
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if store.connection != .connected {
                        ContentUnavailableView("Connect your server", systemImage: "network", description: Text("Semantic search uses the documents and memories indexed by your Herness server."))
                    } else if let knowledge = store.knowledge {
                        statistics(knowledge)
                        if let error = knowledge.error {
                            Text(error).font(.footnote).foregroundStyle(.orange)
                        }
                        if knowledge.requiresRebuild {
                            Text("The embedding model changed. Clear the vector index in Sources, then index the sources again.")
                                .font(.footnote).foregroundStyle(.orange)
                        } else if !knowledge.configured {
                            Button("Choose an embedding model") { section = .embeddings }
                                .buttonStyle(.bordered)
                        }
                        if knowledge.indexing.status != "idle" { indexingStatus(knowledge.indexing) }
                        Picker("Knowledge section", selection: $section) {
                            ForEach(KnowledgeSection.allCases) { item in Text(item.rawValue).tag(item) }
                        }
                        .pickerStyle(.segmented)
                        switch section {
                        case .search: searchPanel
                        case .sources: sourcesPanel(knowledge)
                        case .embeddings: embeddingPanel
                        }
                    } else {
                        ProgressView("Loading knowledge…").frame(maxWidth: .infinity).padding(40)
                    }
                    StoreFeedback()
                }
                .frame(maxWidth: 760)
                .padding(18)
                .frame(maxWidth: .infinity)
            }
            .background(HernessTheme.background)
            .navigationTitle("Knowledge")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { Task { await store.refreshKnowledge() } } label: { Image(systemName: "arrow.clockwise") }
                        .accessibilityLabel("Refresh knowledge")
                        .disabled(store.connection != .connected)
                }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { embeddingKey = ""; dismiss() } }
            }
            .task {
                while !Task.isCancelled {
                    if store.connection == .connected && store.isForeground {
                        await store.refreshKnowledge()
                        if !settingsLoaded, let settings = store.knowledge?.settings { loadSettings(settings) }
                    }
                    do { try await Task.sleep(for: .seconds(2)) }
                    catch { break }
                }
            }
            .refreshable { await store.refreshKnowledge() }
            .fileImporter(isPresented: $importingDocument, allowedContentTypes: documentTypes, allowsMultipleSelection: false, onCompletion: importDocument)
            .alert("Document upload", isPresented: Binding(get: { documentError != nil }, set: { if !$0 { documentError = nil } })) {
                Button("OK", role: .cancel) { documentError = nil }
            } message: { Text(documentError ?? "") }
            .onDisappear { embeddingKey = "" }
            .onChange(of: store.workspace) { _, _ in
                selected = []; hits = []; searched = false; settingsLoaded = false; embeddingKey = ""
            }
            .onChange(of: store.serverURL) { _, _ in
                selected = []; hits = []; searched = false; settingsLoaded = false; embeddingKey = ""
            }
            .onChange(of: store.knowledge?.availableSources) { _, sources in
                let current = sources ?? []
                let available = Set(current.filter(\.indexable).map(\.id))
                selected.formIntersection(available)
                let indexed = Set(current.filter { $0.indexed && !$0.stale }.map(\.id))
                hits.removeAll { !indexed.contains($0.sourceID) }
            }
            .confirmationDialog("Clear this project’s vector index?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Clear vector index", role: .destructive) {
                    Task { await store.clearKnowledge(); hits = []; searched = false; selected = [] }
                }
                Button("Cancel", role: .cancel) {}
            } message: { Text("This removes indexed vectors. Your original documents and memories stay on the server.") }
        }
    }

    private func statistics(_ knowledge: KnowledgeOverview) -> some View {
        HStack(spacing: 10) {
            statistic("Sources", value: String(knowledge.stats.sources))
            statistic("Passages", value: String(knowledge.stats.chunks))
            statistic("Dimensions", value: knowledge.stats.dimensions.map(String.init) ?? "—")
        }
    }

    private func statistic(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(value).font(.title2.weight(.medium)).foregroundStyle(HernessTheme.primaryText)
            Text(title).font(.caption).foregroundStyle(HernessTheme.secondaryText)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 13))
        .accessibilityElement(children: .combine)
    }

    private func indexingStatus(_ job: IndexingJob) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(job.status == "running" ? "Indexing \(job.completed) of \(job.total) sources" : "Indexing: \(job.status)")
                    .font(.subheadline)
                Spacer()
                if job.status == "running" {
                    Button("Stop") { Task { await store.stopIndexing() } }
                        .disabled(store.isWorking)
                }
            }
            if job.status == "running" {
                ProgressView(value: Double(job.completed), total: Double(max(job.total, 1)))
                if let current = job.current, !current.isEmpty { Text(current).font(.caption).foregroundStyle(HernessTheme.secondaryText) }
            }
            if let error = job.error { Text(error).font(.caption).foregroundStyle(.orange) }
        }
        .padding(13)
        .background(HernessTheme.accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
    }

    private var searchPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top, spacing: 10) {
                TextField("What would you like to find?", text: $query, axis: .vertical)
                    .lineLimit(1...3).textFieldStyle(.roundedBorder).submitLabel(.search)
                    .onSubmit { search() }
                Button { search() } label: {
                    if searching { ProgressView() } else { Image(systemName: "magnifyingglass") }
                }
                .frame(minWidth: 44, minHeight: 40)
                .buttonStyle(.borderedProminent)
                .disabled(searching || !ready || (store.knowledge?.stats.chunks ?? 0) == 0 || query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel("Search knowledge")
            }
            Picker("Look in", selection: $kind) {
                Text("All sources").tag("all")
                Text("Documents").tag("document")
                Text("Memory").tag("memory")
                Text("Notes").tag("note")
            }
            .pickerStyle(.menu)
            if hits.isEmpty {
                ContentUnavailableView(searched ? "No matching passages" : "Search your knowledge", systemImage: "text.magnifyingglass", description: Text(searched ? "Try another question or add more indexed sources." : "Choose sources to index, then search by meaning. Results include their original citations."))
            } else {
                Text("\(hits.count) closest passages").font(.caption).foregroundStyle(HernessTheme.secondaryText)
                ForEach(hits) { hit in
                    VStack(alignment: .leading, spacing: 10) {
                        Text(hit.title).font(.headline)
                        Text(hit.location).font(.caption).foregroundStyle(HernessTheme.accent)
                        Text(hit.text).font(.subheadline).textSelection(.enabled)
                        if let score = hit.score { Text("Similarity \(score, specifier: "%.3f")").font(.caption.monospacedDigit()).foregroundStyle(HernessTheme.secondaryText) }
                        Button("Use in chat", systemImage: "text.quote") {
                            useExcerpt("Source: \(hit.title) [\(hit.sourceID) · \(hit.location)]\n\(hit.text)")
                        }
                        .buttonStyle(.bordered)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(15)
                    .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 14))
                }
            }
        }
    }

    private func sourcesPanel(_ knowledge: KnowledgeOverview) -> some View {
        VStack(alignment: .leading, spacing: 15) {
            Button {
                documentContext = currentDocumentContext
                importingDocument = true
            } label: {
                if uploadingDocument { Label("Uploading document…", systemImage: "arrow.up.doc") }
                else { Label("Add document", systemImage: "doc.badge.plus") }
            }
            .buttonStyle(.bordered)
            .disabled(uploadingDocument || store.isWorking || store.connection != .connected)
            Text("Choose a PDF, text, or Office document up to 4 MB. It uploads to your Herness server; select it below to index it separately.")
                .font(.caption).foregroundStyle(HernessTheme.secondaryText)
            HStack {
                Button("Select all") { selected = Set(knowledge.availableSources.filter(\.indexable).prefix(100).map(\.id)) }
                    .disabled(indexing)
                Spacer()
                Text("\(selected.count) selected").font(.caption).foregroundStyle(HernessTheme.secondaryText)
                Button("Index") {
                    Task { await store.indexKnowledge(sourceIDs: Array(selected)) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(selected.isEmpty || !ready || indexing || store.isWorking)
            }
            Text("Selected source text is sent from your server to \(knowledge.settings.baseURL) using \(knowledge.settings.model).")
                .font(.caption).foregroundStyle(HernessTheme.secondaryText).textSelection(.enabled)
            if knowledge.availableSources.isEmpty {
                ContentUnavailableView("No available sources", systemImage: "doc.text", description: Text("Documents and active memories added to your Herness workspace appear here. Chat attachments are separate from the document library."))
            }
            ForEach(knowledge.availableSources) { source in
                Button {
                    if selected.contains(source.id) { selected.remove(source.id) }
                    else if selected.count < 100 { selected.insert(source.id) }
                } label: {
                    HStack(alignment: .top, spacing: 11) {
                        Image(systemName: selected.contains(source.id) ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(selected.contains(source.id) ? HernessTheme.accent : HernessTheme.secondaryText)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(source.title).font(.subheadline.weight(.medium)).foregroundStyle(HernessTheme.primaryText)
                            Text("\(source.kind.capitalized) · \(source.characters.formatted()) characters")
                                .font(.caption).foregroundStyle(HernessTheme.secondaryText)
                            Text(source.stale ? "Needs update" : source.indexed ? "Indexed" : source.indexable ? "Not indexed" : "No extracted text")
                                .font(.caption).foregroundStyle(source.stale ? Color.orange : HernessTheme.secondaryText)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(13)
                    .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)
                .disabled(!source.indexable || indexing)
                .accessibilityLabel("\(selected.contains(source.id) ? "Deselect" : "Select") \(source.title)")
            }
            Button("Clear vector index", role: .destructive) { confirmClear = true }
                .disabled(indexing || store.isWorking || (knowledge.stats.chunks == 0 && knowledge.stats.dimensions == nil))
                .padding(.top, 10)
            Text("Clearing the index keeps your original documents and memories.").font(.caption).foregroundStyle(HernessTheme.secondaryText)
        }
    }

    private var embeddingPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Embedding model").font(.headline)
            Text("These settings are separate from your chat model. The embedding service runs on, or is reached by, your Herness server.")
                .font(.footnote).foregroundStyle(HernessTheme.secondaryText)
            Picker("Provider", selection: Binding(get: { protocolName }, set: { provider in
                protocolName = provider
                embeddingURL = provider == "ollama" ? "http://127.0.0.1:11434" : "https://api.openai.com/v1"
                embeddingModel = provider == "ollama" ? "nomic-embed-text" : "text-embedding-3-small"
                embeddingKey = ""
                settingsDirty = true
            })) {
                Text("Local Ollama").tag("ollama")
                Text("OpenAI-compatible").tag("openai")
            }
            .pickerStyle(.menu)
            labeledField("Endpoint URL") {
                TextField("Embedding endpoint", text: Binding(get: { embeddingURL }, set: { embeddingURL = $0; settingsDirty = true }))
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
            }
            labeledField("Embedding model") {
                TextField("Model", text: Binding(get: { embeddingModel }, set: { embeddingModel = $0; settingsDirty = true }))
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
            }
            labeledField("API key") {
                SecureField("Leave blank to keep the saved key", text: Binding(get: { embeddingKey }, set: { embeddingKey = $0; settingsDirty = true }))
                    .textInputAutocapitalization(.never).autocorrectionDisabled().privacySensitive()
            }
            if store.knowledge?.settings.keyPresent == true { Text("A key is already configured on the server.").font(.caption).foregroundStyle(HernessTheme.secondaryText) }
            Text("Indexing sends selected source text to \(embeddingURL). Searching sends your query. Saving settings sends no embedding requests; a connection test sends a short sample.")
                .font(.caption).foregroundStyle(HernessTheme.secondaryText)
            HStack {
                Button(saving ? "Saving…" : "Save & enable") {
                    saving = true
                    store.dismissError()
                    Task {
                        await store.configureEmbeddings(protocolName: protocolName, baseURL: embeddingURL, model: embeddingModel, apiKey: embeddingKey)
                        embeddingKey = ""
                        saving = false
                        if store.errorMessage == nil, let settings = store.knowledge?.settings { loadSettings(settings) }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(saving || indexing || embeddingURL.isEmpty || embeddingModel.isEmpty)
                Button("Test saved connection") { Task { await store.testEmbeddings() } }
                    .buttonStyle(.bordered)
                    .disabled(saving || indexing || settingsDirty || store.knowledge?.configured != true || store.isWorking)
            }
        }
    }

    private func labeledField<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.caption).foregroundStyle(HernessTheme.secondaryText)
            content().textFieldStyle(.roundedBorder)
        }
    }

    private func loadSettings(_ settings: EmbeddingSettings) {
        protocolName = settings.protocolName
        embeddingURL = settings.baseURL
        embeddingModel = settings.model
        embeddingKey = ""
        settingsDirty = false
        settingsLoaded = true
    }

    private func search() {
        guard !searching, ready, !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        searching = true
        hits = []
        searched = false
        store.dismissError()
        Task {
            await store.searchKnowledge(query: query, kinds: kind == "all" ? nil : [kind])
            searching = false
            if store.errorMessage == nil { hits = store.searchResults; searched = true }
        }
    }

    private func importDocument(_ result: Result<[URL], Error>) {
        switch result {
        case .failure(let error):
            documentError = error.localizedDescription
        case .success(let urls):
            guard let url = urls.first, let context = documentContext else { return }
            guard context == currentDocumentContext, store.connection == .connected else {
                documentError = "The server or project changed. Choose the document again in the current project."
                return
            }
            uploadingDocument = true
            Task {
                defer { uploadingDocument = false }
                do {
                    let document = try await Task.detached(priority: .userInitiated) {
                        let scoped = url.startAccessingSecurityScopedResource()
                        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                        let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
                        guard values.isRegularFile != false, (values.fileSize ?? 0) <= 4_000_000 else {
                            throw APIError.invalidInput("Choose a document no larger than 4 MB.")
                        }
                        let data = try Data(contentsOf: url)
                        guard !data.isEmpty, data.count <= 4_000_000 else {
                            throw APIError.invalidInput("Choose a nonempty document no larger than 4 MB.")
                        }
                        return (url.lastPathComponent, data)
                    }.value
                    guard context == currentDocumentContext, store.connection == .connected else {
                        throw APIError.invalidInput("The server or project changed before upload. Choose the document again in the current project.")
                    }
                    let accepted = await store.uploadKnowledgeDocument(name: document.0, data: document.1)
                    if accepted, context == currentDocumentContext {
                        store.noticeMessage = "Document added. Select it and choose Index when you’re ready."
                    }
                } catch {
                    documentError = error.localizedDescription
                }
            }
        }
    }
}
