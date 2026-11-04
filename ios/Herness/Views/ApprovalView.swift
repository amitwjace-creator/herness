import SwiftUI

@MainActor
struct ApprovalView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var responding = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let request = store.approval {
                        Label("Your approval is needed", systemImage: "hand.raised.fill")
                            .font(.title3.weight(.semibold)).foregroundStyle(HernessTheme.primaryText)
                        Text("Review the complete action below. Approving allows this action once on your server.")
                            .foregroundStyle(HernessTheme.secondaryText)
                        detail("Action", request.kind)
                        detail("Request", request.description)
                        if let path = request.path { detail("Path", path) }
                        if let arguments = request.arguments { detail("Arguments", arguments.prettyPrinted) }
                        if let diff = request.diff, !diff.isEmpty { diffView(diff) }
                        if let content = request.content, !content.isEmpty { detail("Proposed content", content) }
                        Text("Approval requests expire on the server. A command or file write starts only after approval.")
                            .font(.footnote).foregroundStyle(HernessTheme.secondaryText)
                    } else {
                        ContentUnavailableView("Request ended", systemImage: "checkmark.circle", description: Text("This request was resolved or expired."))
                    }
                    StoreFeedback()
                }
                .padding(20)
            }
            .background(HernessTheme.background)
            .safeAreaInset(edge: .bottom) {
                if let reviewedRequest = store.approval {
                    HStack(spacing: 12) {
                        Button("Deny", role: .destructive) { respond(false, reviewedID: reviewedRequest.id) }
                            .buttonStyle(.bordered).frame(maxWidth: .infinity)
                        Button(responding ? "Sending…" : "Approve once") { respond(true, reviewedID: reviewedRequest.id) }
                            .buttonStyle(.borderedProminent).frame(maxWidth: .infinity)
                    }
                    .controlSize(.large)
                    .disabled(responding)
                    .padding(18)
                    .background(HernessTheme.background)
                }
            }
            .navigationTitle("Review action")
            .navigationBarTitleDisplayMode(.inline)
            .interactiveDismissDisabled(store.approval != nil)
            .onChange(of: store.approval?.id) { _, identity in
                responding = false
                if identity == nil { dismiss() }
            }
        }
    }

    private func detail(_ title: String, _ content: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(HernessTheme.secondaryText)
            Text(content).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(13).background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private func diffView(_ diff: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Proposed changes").font(.caption.weight(.semibold)).foregroundStyle(HernessTheme.secondaryText)
            ScrollView(.horizontal) {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(diff.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                        Text(line.isEmpty ? " " : line)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(line.hasPrefix("+") ? Color.green : line.hasPrefix("-") ? Color.orange : HernessTheme.primaryText)
                    }
                }
                .padding(13)
            }
            .background(HernessTheme.surface, in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private func respond(_ allow: Bool, reviewedID: String) {
        guard !responding else { return }
        guard store.approval?.id == reviewedID else {
            store.errorMessage = "This approval request changed. Review the current action before deciding."
            return
        }
        responding = true
        Task {
            await store.approve(allow: allow, reviewedID: reviewedID)
            responding = false
            if store.approval == nil { dismiss() }
        }
    }
}
