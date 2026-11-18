// Detect faces at given timestamps with macOS Vision (local, nothing uploaded).
// usage: face_anchor <video> <t1,t2,...>
// prints JSON: [{"t": 1.0, "faces": [{"x":..,"y":..,"w":..,"h":..}]}]
// Boxes are normalized to the displayed frame, origin top-left.
import AVFoundation
import Foundation
import Vision

guard CommandLine.arguments.count == 3 else {
    fputs("usage: face_anchor <video> <t1,t2,...>\n", stderr)
    exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = CMTime(value: 1, timescale: 10)
generator.requestedTimeToleranceAfter = CMTime(value: 1, timescale: 10)
generator.maximumSize = CGSize(width: 960, height: 960)

var results: [[String: Any]] = []
for token in CommandLine.arguments[2].split(separator: ",") {
    guard let t = Double(token) else { continue }
    var faces: [[String: Double]] = []
    if let image = try? generator.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil) {
        let request = VNDetectFaceRectanglesRequest()
        try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        for face in request.results ?? [] {
            let b = face.boundingBox
            faces.append(["x": Double(b.minX), "y": Double(1 - b.maxY), "w": Double(b.width), "h": Double(b.height)])
        }
    }
    results.append(["t": t, "faces": faces])
}
let data = try JSONSerialization.data(withJSONObject: results)
FileHandle.standardOutput.write(data)
