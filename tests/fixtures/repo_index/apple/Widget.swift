import Foundation

struct SwiftWidget {
    func renderWidget(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespaces)
    }
}
