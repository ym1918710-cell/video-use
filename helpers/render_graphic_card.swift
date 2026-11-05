// Rasterize brand-driven graphic cards (stickers, numbers, headlines,
// lower thirds) to transparent PNGs with native CoreText shaping, so Arabic,
// mixed Arabic/Latin, and any installed or brand-supplied font render
// correctly. Animation is applied afterwards in Python.
import AppKit
import CoreText
import Foundation

struct Line: Decodable {
    let text: String
    let font: String?
    let size: CGFloat
    let color: String
    let weight: String?
}

struct Paint: Decodable {
    let colors: [String]
    let angle: CGFloat?
}

struct Shadow: Decodable {
    let color: String
    let blur: CGFloat
    let dx: CGFloat
    let dy: CGFloat
}

struct Extrude: Decodable {
    let depth: Int
    let color: String
    let dx: CGFloat
    let dy: CGFloat
}

struct Card: Decodable {
    let file: String
    let lines: [Line]
    let fill: Paint?
    let stroke: Paint?
    let strokeWidth: CGFloat?
    let radius: CGFloat?
    let padX: CGFloat
    let padY: CGFloat
    let lineGap: CGFloat?
    let maxWidth: CGFloat
    let shadow: Shadow?
    let glow: Shadow?
    let textStroke: Shadow?
    let extrude: Extrude?
    let align: String?
}

struct Payload: Decodable {
    let cards: [Card]
}

func color(_ hex: String) -> NSColor {
    var s = hex.trimmingCharacters(in: .whitespaces)
    if s.hasPrefix("#") { s.removeFirst() }
    if s.count == 6 { s += "FF" }
    var value: UInt64 = 0
    Scanner(string: s).scanHexInt64(&value)
    return NSColor(
        srgbRed: CGFloat((value >> 24) & 0xFF) / 255,
        green: CGFloat((value >> 16) & 0xFF) / 255,
        blue: CGFloat((value >> 8) & 0xFF) / 255,
        alpha: CGFloat(value & 0xFF) / 255
    )
}

var registered = Set<String>()

func font(_ spec: String?, size: CGFloat, weight: String?) -> NSFont {
    let w: NSFont.Weight = ["black": .black, "heavy": .heavy, "bold": .bold, "semibold": .semibold,
                            "medium": .medium, "regular": .regular][weight ?? "bold"] ?? .bold
    guard let spec = spec, !spec.isEmpty else { return NSFont.systemFont(ofSize: size, weight: w) }
    if spec.contains("/") {
        let url = URL(fileURLWithPath: spec)
        if !registered.contains(spec) {
            CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
            registered.insert(spec)
        }
        if let descriptors = CTFontManagerCreateFontDescriptorsFromURL(url as CFURL) as? [CTFontDescriptor],
           let first = descriptors.first {
            return CTFontCreateWithFontDescriptor(first, size, nil) as NSFont
        }
    }
    // A family name ("Avenir Next") honours the requested weight; an exact
    // face name ("AvenirNext-Bold") is used as given.
    let managerWeight = ["black": 12, "heavy": 11, "bold": 9, "semibold": 8, "medium": 6, "regular": 5][weight ?? "bold"] ?? 9
    if let family = NSFontManager.shared.font(withFamily: spec, traits: [], weight: managerWeight, size: size) {
        return family
    }
    return NSFont(name: spec, size: size) ?? NSFont.systemFont(ofSize: size, weight: w)
}

func isRTL(_ text: String) -> Bool {
    text.unicodeScalars.contains { (0x0590...0x08FF).contains($0.value) || (0xFB1D...0xFEFF).contains($0.value) }
}

func attributed(_ line: Line, align: NSTextAlignment, colorOverride: NSColor? = nil) -> NSAttributedString {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = align
    paragraph.baseWritingDirection = isRTL(line.text) ? .rightToLeft : .leftToRight
    paragraph.lineBreakMode = .byWordWrapping
    return NSAttributedString(string: line.text, attributes: [
        .font: font(line.font, size: line.size, weight: line.weight),
        .foregroundColor: colorOverride ?? color(line.color),
        .paragraphStyle: paragraph,
    ])
}

func gradient(_ paint: Paint) -> NSGradient? {
    let colors = paint.colors.map(color)
    if colors.count == 1 { return NSGradient(colors: [colors[0], colors[0]]) }
    return NSGradient(colors: colors)
}

func render(_ card: Card) throws {
    let align: NSTextAlignment = card.align == "left" ? .left : card.align == "right" ? .right : .center
    let gap = card.lineGap ?? 0
    let textMax = card.maxWidth - 2 * card.padX
    var sizes: [CGSize] = []
    for line in card.lines {
        let r = attributed(line, align: align).boundingRect(
            with: NSSize(width: textMax, height: 10_000), options: [.usesLineFragmentOrigin, .usesFontLeading])
        sizes.append(CGSize(width: ceil(r.width), height: ceil(r.height)))
    }
    let textW = sizes.map(\.width).max() ?? 0
    let textH = sizes.map(\.height).reduce(0, +) + gap * CGFloat(max(0, sizes.count - 1))
    let boxW = textW + 2 * card.padX
    let boxH = textH + 2 * card.padY
    let effects = [card.shadow, card.glow].compactMap { $0 }
    var margin: CGFloat = 4 + (card.strokeWidth ?? 0)
    for e in effects { margin = max(margin, e.blur * 2 + max(abs(e.dx), abs(e.dy))) }
    if let ex = card.extrude { margin = max(margin, CGFloat(ex.depth) * max(abs(ex.dx), abs(ex.dy)) + 4) }
    let W = Int(ceil(boxW + 2 * margin)), H = Int(ceil(boxH + 2 * margin))

    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: W, pixelsHigh: H, bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0),
          let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
        throw NSError(domain: "video-use", code: 1, userInfo: [NSLocalizedDescriptionKey: "canvas"])
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    ctx.imageInterpolation = .high
    let cg = ctx.cgContext
    let box = NSRect(x: margin, y: margin, width: boxW, height: boxH)
    let radius = min(card.radius ?? 0, boxH / 2)
    let shape = NSBezierPath(roundedRect: box, xRadius: radius, yRadius: radius)

    func withShadow(_ s: Shadow?, _ draw: () -> Void) {
        cg.saveGState()
        if let s = s { cg.setShadow(offset: CGSize(width: s.dx, height: -s.dy), blur: s.blur, color: color(s.color).cgColor) }
        draw()
        cg.restoreGState()
    }

    if let fill = card.fill, let g = gradient(fill) {
        withShadow(card.shadow) {
            // Draw a solid shape for the shadow, then the gradient on top.
            color(fill.colors[0]).setFill()
            shape.fill()
        }
        g.draw(in: shape, angle: fill.angle ?? 90)
    }
    if let stroke = card.stroke, let width = card.strokeWidth, width > 0, let g = gradient(stroke) {
        cg.saveGState()
        let inset = NSBezierPath(roundedRect: box.insetBy(dx: width / 2, dy: width / 2),
                                 xRadius: max(0, radius - width / 2), yRadius: max(0, radius - width / 2))
        inset.lineWidth = width
        cg.addPath(inset.cgPath)
        cg.setLineWidth(width)
        cg.replacePathWithStrokedPath()
        cg.clip()
        g.draw(in: box.insetBy(dx: -width, dy: -width), angle: stroke.angle ?? 0)
        cg.restoreGState()
    }

    // Lines are stacked top → bottom (AppKit's origin is bottom-left).
    var cursor = box.maxY - card.padY
    for (index, line) in card.lines.enumerated() {
        let h = sizes[index].height
        cursor -= h
        let rect = NSRect(x: box.minX + card.padX, y: cursor, width: textW, height: h)
        let opts: NSString.DrawingOptions = [.usesLineFragmentOrigin, .usesFontLeading]
        if let ex = card.extrude, ex.depth > 0 {
            let side = attributed(line, align: align, colorOverride: color(ex.color))
            for step in stride(from: ex.depth, through: 1, by: -1) {
                side.draw(with: rect.offsetBy(dx: ex.dx * CGFloat(step), dy: -ex.dy * CGFloat(step)), options: opts)
            }
        }
        if let ts = card.textStroke {
            let outline = NSMutableAttributedString(attributedString: attributed(line, align: align))
            outline.addAttributes([.strokeColor: color(ts.color), .strokeWidth: ts.blur], range: NSRange(location: 0, length: outline.length))
            outline.draw(with: rect, options: opts)
        }
        withShadow(card.fill == nil ? (card.glow ?? card.shadow) : card.glow) {
            attributed(line, align: align).draw(with: rect, options: opts)
        }
        cursor -= gap
    }
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "video-use", code: 2, userInfo: [NSLocalizedDescriptionKey: "png"])
    }
    try png.write(to: URL(fileURLWithPath: card.file))
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: render_graphic_card <payload.json>\n", stderr)
    exit(2)
}
let payload = try JSONDecoder().decode(Payload.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
for card in payload.cards { try render(card) }
