// Person mattes for still images with macOS Vision (local, nothing uploaded).
// usage: person_mask_images <fast|balanced|accurate> <in1.png> <out1.png> [<in2.png> <out2.png> ...]
// Each output is an 8-bit grey PNG at the input's size: 255 = person.
import AppKit
import CoreImage
import Foundation
import Vision

let a = Array(CommandLine.arguments.dropFirst())
guard a.count >= 3, (a.count - 1) % 2 == 0 else {
    fputs("usage: person_mask_images <quality> <in> <out> [...]\n", stderr)
    exit(2)
}
let level: VNGeneratePersonSegmentationRequest.QualityLevel =
    a[0] == "fast" ? .fast : (a[0] == "balanced" ? .balanced : .accurate)
let ctx = CIContext()
var i = 1
while i < a.count {
    let inURL = URL(fileURLWithPath: a[i]), outURL = URL(fileURLWithPath: a[i + 1])
    i += 2
    guard let img = NSImage(contentsOf: inURL), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fputs("unreadable \(inURL.path)\n", stderr)
        continue
    }
    let req = VNGeneratePersonSegmentationRequest()
    req.qualityLevel = level
    req.outputPixelFormat = kCVPixelFormatType_OneComponent8
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    guard let buf = req.results?.first?.pixelBuffer else {
        fputs("no person in \(inURL.path)\n", stderr)
        continue
    }
    var mask = CIImage(cvPixelBuffer: buf)
    let sx = CGFloat(cg.width) / mask.extent.width, sy = CGFloat(cg.height) / mask.extent.height
    mask = mask.transformed(by: CGAffineTransform(scaleX: sx, y: sy))
    guard let out = ctx.createCGImage(mask, from: CGRect(x: 0, y: 0, width: cg.width, height: cg.height),
                                      format: .L8, colorSpace: CGColorSpaceCreateDeviceGray()) else { continue }
    let rep = NSBitmapImageRep(cgImage: out)
    try? rep.representation(using: .png, properties: [:])?.write(to: outURL)
}
