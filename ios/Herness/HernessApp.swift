import SwiftUI

@main
@MainActor
struct HernessApp: App {
    @StateObject private var store = AppStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            HernessRootView()
                .environmentObject(store)
                .preferredColorScheme(.dark)
                .tint(HernessTheme.accent)
                .task {
                    if !store.serverURL.isEmpty {
                        await store.reconnect()
                    }
                }
                .onChange(of: scenePhase) { _, phase in
                    store.setSceneActive(phase == .active)
                }
        }
    }
}
