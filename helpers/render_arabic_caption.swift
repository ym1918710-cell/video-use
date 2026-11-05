import AppKit
import Foundation

struct Caption: Decodable {
    let file: String
    let text: String
}

struct Payload: Decodable {
    let width: Int
    let height: Int
    let font: String
    let fontSize: CGFloat
    let marginV: CGFloat
    let captions: [Caption]
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: render_arabic_caption <payload.json>\n", stderr)
    exit(2)
}

let input = URL(fileURLWithPath: CommandLine.arguments[1])
let payload = try JSONDecoder().decode(Payload.self, from: Data(contentsOf: input))
let font = NSFont(name: payload.font, size: payload.fontSize) ?? NSFont.systemFont(ofSize: payload.fontSize, weight: .bold)

for caption in payload.captions {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: payload.width,
        pixelsHigh: payload.height,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .sRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ), let context = NSGraphicsContext(bitmapImageRep: rep) else {
        throw NSError(domain: "video-use", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not create caption canvas"])
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.imageInterpolation = .high
    NSColor.clear.setFill()
    NSRect(x: 0, y: 0, width: payload.width, height: payload.height).fill()
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    paragraph.baseWritingDirection = .rightToLeft
    paragraph.lineBreakMode = .byWordWrapping
    let attributes: [NSAttributedString.Key: Any] = [
        .font: font,
        .foregroundColor: NSColor.white,
        .strokeColor: NSColor(calibratedWhite: 0.04, alpha: 0.92),
        .strokeWidth: -3.0,
        .paragraphStyle: paragraph,
    ]
    let rect = NSRect(x: 70, y: payload.marginV, width: CGFloat(payload.width - 140), height: 240)
    NSString(string: caption.text).draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attributes)
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "video-use", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not encode caption PNG"])
    }
    try png.write(to: URL(fileURLWithPath: caption.file))
}
