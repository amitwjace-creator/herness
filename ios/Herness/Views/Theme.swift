import SwiftUI

/// Shared colors for the native Herness workspace.
enum HernessTheme {
    static let background = Color(red: 23 / 255, green: 23 / 255, blue: 23 / 255)
    static let surface = Color(red: 37 / 255, green: 37 / 255, blue: 37 / 255)
    static let raised = Color(red: 46 / 255, green: 46 / 255, blue: 46 / 255)
    static let accent = Color(red: 77 / 255, green: 107 / 255, blue: 254 / 255)
    static let primaryText = Color(red: 239 / 255, green: 239 / 255, blue: 241 / 255)
    static let secondaryText = Color(red: 165 / 255, green: 165 / 255, blue: 173 / 255)
}

/// A decorative, static character. It does not animate or imply a running task.
struct HernessOrb: View {
    var size: CGFloat = 56

    var body: some View {
        ZStack {
            Circle()
                .fill(
                    RadialGradient(
                        stops: [
                            .init(color: Color(red: 190 / 255, green: 214 / 255, blue: 1), location: 0),
                            .init(color: Color(red: 121 / 255, green: 157 / 255, blue: 1), location: 0.18),
                            .init(color: HernessTheme.accent, location: 0.45),
                            .init(color: Color(red: 39 / 255, green: 64 / 255, blue: 181 / 255), location: 0.78),
                            .init(color: Color(red: 22 / 255, green: 33 / 255, blue: 101 / 255), location: 1)
                        ],
                        center: UnitPoint(x: 0.3, y: 0.16),
                        startRadius: 0,
                        endRadius: size * 0.92
                    )
                )
                .overlay {
                    Circle().strokeBorder(Color.white.opacity(0.12), lineWidth: 0.7)
                }
                .shadow(color: Color.black.opacity(0.25), radius: size * 0.14, x: 0, y: size * 0.12)

            Ellipse()
                .fill(
                    LinearGradient(
                        colors: [Color.white.opacity(0.22), Color.white.opacity(0)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .frame(width: size * 0.59, height: size * 0.28)
                .rotationEffect(.degrees(-18))
                .offset(x: -size * 0.04, y: -size * 0.27)

            HStack(spacing: size * 0.23) {
                eye
                eye
            }
            .offset(y: -size * 0.01)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private var eye: some View {
        Capsule()
            .fill(Color.white)
            .frame(width: size * 0.065, height: size * 0.17)
            .shadow(color: Color.white.opacity(0.12), radius: 2)
    }
}

/// A consistent 44-point target for navigation and toolbar actions.
struct RoundIconButton: View {
    var symbol: String
    var accessibilityLabel: String
    var isActive: Bool = false
    var action: () -> Void

    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 20, weight: .regular))
                .foregroundStyle(isActive ? HernessTheme.accent : HernessTheme.primaryText)
                .frame(width: 44, height: 44)
                .background(isActive ? HernessTheme.accent.opacity(0.16) : HernessTheme.surface, in: Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .opacity(isEnabled ? 1 : 0.45)
        .accessibilityLabel(Text(accessibilityLabel))
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }
}

private struct HernessThemePreview: PreviewProvider {
    static var previews: some View {
        VStack(spacing: 28) {
            HernessOrb()
            Text("Hi, what would you like to do\nwith Herness today?")
                .font(.body)
                .foregroundStyle(HernessTheme.primaryText)
                .multilineTextAlignment(.center)
            HStack(spacing: 16) {
                RoundIconButton(symbol: "line.3.horizontal", accessibilityLabel: "Open conversations") {}
                RoundIconButton(symbol: "speaker.wave.2", accessibilityLabel: "Read replies aloud", isActive: true) {}
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(HernessTheme.background)
        .preferredColorScheme(.dark)
    }
}
