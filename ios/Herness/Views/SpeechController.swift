import AVFoundation
import Combine
import Foundation

/// Reads only replies explicitly passed by the chat completion observer.
/// Loading history and enabling this setting never starts speech by itself.
@MainActor
final class SpeechController: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var enabled = false {
        didSet {
            if !enabled {
                stop()
            }
        }
    }

    private let synthesizer = AVSpeechSynthesizer()
    private var lastSpokenIdentity: String?

    override init() {
        super.init()
        synthesizer.delegate = self
        // Let the synthesizer manage its audio session without changing the
        // app's shared audio-session configuration or recording permission.
        synthesizer.usesApplicationAudioSession = false
    }

    func toggle() {
        enabled.toggle()
    }

    /// Call after a newly completed reply, with a session-qualified identity.
    func readCompletedReply(text: String, identity: String) {
        let content = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard enabled, !content.isEmpty, !identity.isEmpty,
              identity != lastSpokenIdentity else {
            return
        }

        lastSpokenIdentity = identity
        synthesizer.stopSpeaking(at: .immediate)

        let utterance = AVSpeechUtterance(string: content)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
    }

    /// Also call when the app leaves the foreground. Keeping the last identity
    /// prevents a refreshed copy of the same reply from being read again.
    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
    }
}
