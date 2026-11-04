import SwiftUI
import UniformTypeIdentifiers
import UIKit

private enum AppSheet: String, Identifiable {
    case history, models, connection, knowledge, approval
    var id: String { rawValue }
}

private struct DraftContext: Hashable {
    let origin: String
    let workspace: String
    let session: String
}

private struct ComposerDraft {
    var text: String
    var attachments: [AttachmentUpload]
}

@MainActor
struct HernessRootView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var speech = SpeechController()
    @State private var sheet: AppSheet?
    @State private var draft = ""
    @State private var savedDrafts: [DraftContext: ComposerDraft] = [:]
    @State private var attachments: [AttachmentUpload] = []
    @State private var importing = false
    @State private var importError: String?
    @State private var followLatest = true
    @State private var runningSession: String?
    @FocusState private var composerFocused: Bool

    private var draftContext: DraftContext { DraftContext(origin: store.serverURL, workspace: store.workspace, session: store.sessionName) }
    private var canChat: Bool { store.connection == .connected }
    private var hasDraft: Bool { !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty }
    private var isEmpty: Bool { store.messages.isEmpty && store.streaming.isEmpty }

    var body: some View {
        VStack(spacing: 0) {
            header
            if isEmpty { welcome } else { conversation }
        }
        .background(HernessTheme.background.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) { composerArea }
        .sheet(item: $sheet) { destination in
            sheetContent(destination)
                .environmentObject(store)
                .presentationBackground(HernessTheme.background)
                .preferredColorScheme(.dark)
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.plainText, .pdf, .image, .json, .sourceCode, .commaSeparatedText], allowsMultipleSelection: true, onCompletion: importFiles)
        .alert("Attachment unavailable", isPresented: Binding(get: { importError != nil }, set: { if !$0 { importError = nil } })) {
            Button("OK", role: .cancel) { importError = nil }
        } message: { Text(importError ?? "") }
        .onChange(of: store.approval?.id) { _, identity in
            if identity != nil { composerFocused = false; sheet = .approval }
            else if sheet == .approval { sheet = nil }
        }
        .onChange(of: store.busy) { wasBusy, busy in
            if busy { runningSession = store.sessionName }
            else if wasBusy, runningSession == store.sessionName, let reply = store.messages.last, reply.role == "assistant" {
                speech.readCompletedReply(text: reply.text, identity: store.sessionName + ":" + reply.id + ":" + reply.text)
                runningSession = nil
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { speech.stop(); runningSession = nil }
        }
        .onChange(of: draftContext) { previous, current in
            savedDrafts[previous] = ComposerDraft(text: draft, attachments: attachments)
            let restored = savedDrafts[current]
            draft = restored?.text ?? ""
            attachments = restored?.attachments ?? []
            followLatest = true
            runningSession = nil
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            RoundIconButton(symbol: "line.3.horizontal", accessibilityLabel: "Open conversations") {
                composerFocused = false
                sheet = .history
            }
            Button {
                composerFocused = false
                if canChat { sheet = .models } else { sheet = .connection }
            } label: {
                HStack(spacing: 8) {
                    Text(modelLabel)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                    Image(systemName: "chevron.down").font(.caption2.weight(.semibold))
                }
                .foregroundStyle(HernessTheme.primaryText)
                .padding(.horizontal, 15)
                .frame(minHeight: 44)
                .background(HernessTheme.surface, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Choose model or connection")
            Spacer(minLength: 8)
            RoundIconButton(symbol: speech.enabled ? "speaker.wave.2" : "speaker.slash", accessibilityLabel: speech.enabled ? "Turn off reading replies aloud" : "Read new replies aloud", isActive: speech.enabled) {
                speech.toggle()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var welcome: some View {
        GeometryReader { proxy in
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 24) {
                HernessOrb(size: proxy.size.height < 250 ? 42 : 54)
                Text("Hi, what would you like to do\nwith Herness today?")
                    .font(.body)
                    .foregroundStyle(HernessTheme.primaryText)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                if !canChat {
                    Button("Connect your server") { composerFocused = false; sheet = .connection }
                        .font(.subheadline.weight(.medium))
                        .buttonStyle(.borderedProminent)
                        .buttonBorderShape(.capsule)
                }
                if store.connection == .demo {
                    Text("Offline sample conversation · connect a server to chat")
                        .font(.caption)
                        .foregroundStyle(HernessTheme.secondaryText)
                }
            }
            .frame(maxWidth: .infinity)
                .padding(.top, max(12, proxy.size.height * 0.32))
                .padding(.horizontal, 18)
                .padding(.bottom, 24)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var modelLabel: String {
        switch store.connection {
        case .disconnected: return store.serverURL.isEmpty ? "Connect" : "Reconnect"
        case .connecting: return "Connecting…"
        case .demo: return "Demo"
        case .connected: return store.modelName.isEmpty ? "Choose model" : store.modelName
        }
    }

    private var conversation: some View {
        ScrollViewReader { reader in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    ForEach(store.messages) { message in
                        ChatMessageView(message: message) { text in quote(text) }
                            .id(message.id)
                    }
                    if !store.streaming.isEmpty {
                        VStack(alignment: .leading, spacing: 9) {
                            Text("Herness").font(.caption.weight(.semibold)).foregroundStyle(HernessTheme.accent)
                            MessageMarkdown(text: store.streaming)
                        }
                        .accessibilityLabel("Herness is replying")
                    } else if store.busy && store.approval == nil {
                        HStack(spacing: 9) {
                            ProgressView().controlSize(.small)
                            Text(store.stopping ? "Stopping…" : "Working on it…").font(.subheadline)
                        }
                        .foregroundStyle(HernessTheme.secondaryText)
                    }
                    if store.approval != nil {
                        Button { composerFocused = false; sheet = .approval } label: {
                            Label("Review requested action", systemImage: "hand.raised")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                    }
                    Color.clear.frame(height: 1).id("conversation-bottom")
                }
                .frame(maxWidth: 760)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 18)
                .padding(.vertical, 20)
            }
            .scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(DragGesture(minimumDistance: 8).onChanged { _ in followLatest = false })
            .overlay(alignment: .bottomTrailing) {
                if !followLatest {
                    RoundIconButton(symbol: "arrow.down", accessibilityLabel: "Jump to latest message") {
                        followLatest = true
                        withAnimation { reader.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .padding(14)
                }
            }
            .onAppear { reader.scrollTo("conversation-bottom", anchor: .bottom) }
            .onChange(of: store.messages.count) { _, _ in
                if followLatest { reader.scrollTo("conversation-bottom", anchor: .bottom) }
            }
            .onChange(of: store.streaming) { _, _ in
                if followLatest { reader.scrollTo("conversation-bottom", anchor: .bottom) }
            }
        }
    }

    private var composerArea: some View {
        VStack(spacing: 10) {
            StoreFeedback()
            if isEmpty && !composerFocused { shortcuts }
            if !attachments.isEmpty { attachmentChips }
            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button("Attach files", systemImage: "paperclip") { importing = true }
                        .disabled(store.busy || attachments.count >= 4 || !canChat)
                    Picker("Agent mode", selection: Binding(get: { store.runMode }, set: { mode in Task { await store.setRunMode(mode) } })) {
                        Text("Act").tag("act")
                        Text("Plan — read only").tag("plan")
                    }
                    .disabled(store.busy || !canChat)
                    Button("Knowledge", systemImage: "books.vertical") { composerFocused = false; sheet = .knowledge }
                    Button("New conversation", systemImage: "square.and.pencil") { Task { await store.newConversation() } }
                        .disabled(store.busy || !canChat)
                    Button("Server connection", systemImage: "network") { composerFocused = false; sheet = .connection }
                } label: {
                    Image(systemName: "plus").font(.system(size: 21))
                        .foregroundStyle(HernessTheme.primaryText)
                        .frame(width: 44, height: 44)
                        .background(HernessTheme.raised, in: Circle())
                }
                .accessibilityLabel("Add files or tools")
                TextField(canChat ? "Ask me. Task me…" : "Connect your server to chat", text: $draft, axis: .vertical)
                    .font(.body)
                    .foregroundStyle(HernessTheme.primaryText)
                    .lineLimit(1...5)
                    .focused($composerFocused)
                    .padding(.vertical, 11)
                    .submitLabel(.return)
                    .accessibilityLabel("Message Herness")
                Button {
                    if store.busy {
                        let context = draftContext
                        Task {
                            guard draftContext == context else { return }
                            await store.stop(expectedSession: context.session)
                        }
                    } else { send() }
                } label: {
                    Image(systemName: store.busy ? "stop.fill" : "arrow.up")
                        .font(.system(size: store.busy ? 15 : 19, weight: .semibold))
                        .foregroundStyle(hasDraft || store.busy ? Color.white : HernessTheme.secondaryText)
                        .frame(width: 44, height: 44)
                        .background(hasDraft || store.busy ? HernessTheme.accent : HernessTheme.raised, in: Circle())
                }
                .buttonStyle(.plain)
                .disabled(store.stopping || (!store.busy && (!hasDraft || !canChat || store.isWorking)))
                .accessibilityLabel(store.busy ? "Stop response" : "Send message")
            }
            .padding(8)
            .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 30))
            .overlay(RoundedRectangle(cornerRadius: 30).stroke(Color.white.opacity(0.04), lineWidth: 1))
        }
        .frame(maxWidth: 760)
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity)
        .background(HernessTheme.background)
    }

    private var shortcuts: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                shortcut("New chat", symbol: "plus") { if canChat { Task { await store.newConversation() } } else { sheet = .connection } }
                shortcut("Make a plan", symbol: "checklist") { draft = "Help me make a practical plan for this project."; composerFocused = true }
                shortcut("Write something", symbol: "square.and.pencil") { draft = "Help me draft a clear project brief."; composerFocused = true }
                shortcut("Knowledge", symbol: "books.vertical") { sheet = .knowledge }
            }
        }
    }

    private func shortcut(_ title: String, symbol: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .font(.subheadline)
                .foregroundStyle(HernessTheme.primaryText)
                .padding(.horizontal, 15)
                .frame(minHeight: 42)
                .background(HernessTheme.surface, in: Capsule())
        }
        .buttonStyle(.plain)
    }

    private var attachmentChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { item in
                    HStack(spacing: 6) {
                        Image(systemName: "doc")
                        Text(item.name).lineLimit(1)
                        Button { attachments.removeAll { $0.id == item.id } } label: {
                            Image(systemName: "xmark").frame(width: 32, height: 36)
                        }
                        .accessibilityLabel("Remove \(item.name)")
                    }
                    .font(.caption)
                    .foregroundStyle(HernessTheme.secondaryText)
                    .padding(.leading, 10)
                    .background(HernessTheme.surface, in: Capsule())
                }
            }
        }
    }

    @ViewBuilder private func sheetContent(_ destination: AppSheet) -> some View {
        switch destination {
        case .history:
            ConversationHistoryView(onKnowledge: { sheet = .knowledge }, onConnection: { sheet = .connection })
        case .models:
            ModelPickerView(onConnection: { sheet = .connection })
        case .connection:
            ConnectionSettingsView()
        case .knowledge:
            KnowledgeView { excerpt in
                sheet = nil
                quote(excerpt)
            }
        case .approval:
            ApprovalView()
        }
    }

    private func send() {
        guard hasDraft, !store.busy, canChat else { return }
        let context = draftContext
        let submittedText = draft
        let files = attachments
        let text = submittedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !files.isEmpty ? "Please review the attached files." : submittedText
        followLatest = true
        composerFocused = false
        Task {
            guard draftContext == context, store.connection == .connected else {
                store.noticeMessage = "The connection or conversation changed. Your draft is kept; review its destination before sending."
                return
            }
            if await store.send(prompt: text, attachments: files, expectedSession: context.session) {
                if draftContext == context {
                    if draft == submittedText { draft = "" }
                    if attachments == files { attachments = [] }
                    savedDrafts[context] = ComposerDraft(text: draft, attachments: attachments)
                } else if var saved = savedDrafts[context] {
                    if saved.text == submittedText { saved.text = "" }
                    if saved.attachments == files { saved.attachments = [] }
                    savedDrafts[context] = saved
                }
            }
        }
    }

    private func quote(_ text: String) {
        if !draft.isEmpty { draft += "\n\n" }
        draft += text.components(separatedBy: "\n").map { "> " + $0 }.joined(separator: "\n") + "\n\n"
        composerFocused = true
    }

    private func importFiles(_ result: Result<[URL], Error>) {
        switch result {
        case .failure(let error): importError = error.localizedDescription
        case .success(let urls):
            let context = draftContext
            guard attachments.count + urls.count <= 4 else { importError = "Attach up to 4 files."; return }
            Task {
                do {
                    let loaded = try await Task.detached(priority: .userInitiated) {
                        try urls.map { url -> (String, Data) in
                            let access = url.startAccessingSecurityScopedResource()
                            defer { if access { url.stopAccessingSecurityScopedResource() } }
                            let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                            guard size <= 4_000_000 else { throw APIError.invalidInput("Each attachment must be no larger than 4 MB.") }
                            return (url.lastPathComponent, try Data(contentsOf: url))
                        }
                    }.value
                    guard draftContext == context, store.connection == .connected else {
                        importError = "The connection or conversation changed. Select the files again in the intended conversation."
                        return
                    }
                    let incoming = try loaded.map { try AttachmentUpload(name: $0.0, data: $0.1) }
                    guard attachments.count + incoming.count <= 4 else { throw APIError.invalidInput("Attach up to 4 files.") }
                    let total = incoming.reduce(0) { $0 + $1.byteCount } + attachments.reduce(0) { $0 + $1.byteCount }
                    guard total <= 8_000_000 else { throw APIError.invalidInput("Attachments must total no more than 8 MB.") }
                    attachments.append(contentsOf: incoming)
                } catch { importError = error.localizedDescription }
            }
        }
    }
}

@MainActor
struct StoreFeedback: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        if let message = store.errorMessage ?? store.noticeMessage {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: store.errorMessage == nil ? "info.circle" : "exclamationmark.circle")
                Text(message).font(.caption).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button {
                    store.dismissError()
                    store.noticeMessage = nil
                } label: { Image(systemName: "xmark").frame(width: 32, height: 32) }
                .accessibilityLabel("Dismiss message")
            }
            .foregroundStyle(store.errorMessage == nil ? HernessTheme.secondaryText : Color.orange)
            .padding(10)
            .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 12))
        }
    }
}

private struct ChatMessageView: View {
    let message: ChatMessage
    let quote: (String) -> Void

    var body: some View {
        if message.role == "tool" {
            DisclosureGroup("Tool result") {
                Text(message.text).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
            }
            .font(.caption)
            .foregroundStyle(HernessTheme.secondaryText)
        } else {
            VStack(alignment: message.role == "user" ? .trailing : .leading, spacing: 6) {
                if message.role != "user" {
                    Text("Herness").font(.caption.weight(.semibold)).foregroundStyle(HernessTheme.accent)
                }
                MessageMarkdown(text: message.text)
                    .padding(message.role == "user" ? 13 : 0)
                    .background(message.role == "user" ? HernessTheme.surface : Color.clear, in: RoundedRectangle(cornerRadius: 18))
                ForEach(message.attachments) { attachment in
                    Label(attachment.name, systemImage: "paperclip").font(.caption).foregroundStyle(HernessTheme.secondaryText)
                }
                HStack(spacing: 4) {
                    Button { UIPasteboard.general.string = message.text } label: {
                        Image(systemName: "doc.on.doc").frame(width: 38, height: 36)
                    }
                    .accessibilityLabel("Copy message")
                    Menu {
                        Button("Quote in chat", systemImage: "text.quote") { quote(message.text) }
                    } label: { Image(systemName: "ellipsis").frame(width: 38, height: 36) }
                    .accessibilityLabel("More message actions")
                }
                .font(.caption)
                .foregroundStyle(HernessTheme.secondaryText)
            }
            .frame(maxWidth: .infinity, alignment: message.role == "user" ? .trailing : .leading)
        }
    }
}

private struct MessageMarkdown: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(text.components(separatedBy: "```").enumerated()), id: \.offset) { index, part in
                if index.isMultiple(of: 2) {
                    Text((try? AttributedString(markdown: part, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(part))
                        .font(.body)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    ScrollView(.horizontal) {
                        Text(part).font(.system(.callout, design: .monospaced)).textSelection(.enabled).padding(12)
                    }
                    .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 10))
                }
            }
        }
        .foregroundStyle(HernessTheme.primaryText)
        .environment(\.openURL, OpenURLAction { url in
            ["http", "https"].contains(url.scheme?.lowercased() ?? "") ? .systemAction : .discarded
        })
    }
}
