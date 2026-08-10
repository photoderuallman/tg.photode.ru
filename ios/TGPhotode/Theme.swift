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
