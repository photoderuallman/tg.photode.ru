import SwiftUI

extension Color {
    static let photodeBackground = Color("Background")
    static let photodeActive = Color("ActiveText")
    static let photodeDisabled = Color("DisabledText")
}

struct AccentBowl: View {
    let seed: Int64
    var size: CGFloat = 10

    private var colors: [Color] {
        let positive = UInt64(bitPattern: seed)
        let first = Double(positive % 360) / 360
        let second = Double((positive / 17 + 62) % 360) / 360
        return [
            Color(hue: first, saturation: 0.58, brightness: 0.86),
            Color(hue: second, saturation: 0.72, brightness: 0.48),
        ]
    }

    var body: some View {
        Circle()
            .fill(
                LinearGradient(
                    colors: colors,
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            )
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

enum PhotodeMetrics {
    static let screenInset: CGFloat = 24
    static let messageInset: CGFloat = 36
    static let glassControlHeight: CGFloat = 38
    static let minimumHitArea: CGFloat = 44
    static let glassSpacing: CGFloat = 12
    static let glassContentInset: CGFloat = 12
    static let messageGroupSpacing: CGFloat = 16
    static let messageSpacing: CGFloat = 8
    static let channelPostSpacing: CGFloat = 20
}

extension View {
    @ViewBuilder
    nonisolated func photodeGlassCapsule(
        interactive: Bool = false
    ) -> some View {
        if #available(iOS 26.0, *) {
            glassEffect(
                interactive ? .regular.interactive() : .regular,
                in: Capsule()
            )
        } else {
            background(.ultraThinMaterial, in: Capsule())
                .overlay {
                    Capsule()
                        .stroke(Color.white.opacity(0.18), lineWidth: 0.75)
                }
        }
    }

    @ViewBuilder
    nonisolated func photodeGlassCircle(
        interactive: Bool = false
    ) -> some View {
        if #available(iOS 26.0, *) {
            glassEffect(
                interactive ? .regular.interactive() : .regular,
                in: Circle()
            )
        } else {
            background(.ultraThinMaterial, in: Circle())
                .overlay {
                    Circle()
                        .stroke(Color.white.opacity(0.18), lineWidth: 0.75)
                }
        }
    }

    @ViewBuilder
    nonisolated func photodeHeaderTypography() -> some View {
        if #available(iOS 26.0, *) {
            font(.system(size: 18, weight: .medium))
                .lineHeight(.exact(points: 20))
        } else {
            font(.system(size: 18, weight: .medium))
                .lineSpacing(2)
        }
    }

    @ViewBuilder
    nonisolated func photodeMessageTypography() -> some View {
        if #available(iOS 26.0, *) {
            font(.system(size: 16, weight: .regular))
                .lineHeight(.exact(points: 18))
        } else {
            font(.system(size: 16, weight: .regular))
                .lineSpacing(2)
        }
    }

    @ViewBuilder
    nonisolated func photodeChatScrollEdges() -> some View {
        if #available(iOS 26.0, *) {
            scrollEdgeEffectStyle(.soft, for: [.top, .bottom])
        } else {
            self
        }
    }
}

struct PhotodePressButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(
                reduceMotion || !configuration.isPressed ? 1 : 0.96
            )
            .animation(
                reduceMotion ? nil : .easeOut(duration: 0.15),
                value: configuration.isPressed
            )
    }
}

struct PhotodeGlassGroup<Content: View>: View {
    private let spacing: CGFloat
    private let content: Content

    init(
        spacing: CGFloat = PhotodeMetrics.glassSpacing,
        @ViewBuilder content: () -> Content
    ) {
        self.spacing = spacing
        self.content = content()
    }

    @ViewBuilder
    var body: some View {
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: spacing) {
                content
            }
        } else {
            content
        }
    }
}
