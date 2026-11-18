// Faces and on-screen text in still images, with macOS Vision (local, nothing uploaded).
// usage: frame_boxes [--langs] <image1> <image2> ...
// prints JSON: [{"file": .., "faces": [{x,y,w,h}], "text": [{x,y,w,h,"s": "...", "c": 0.9}]}]
// Boxes are normalized to the image, origin top-left.
import AppKit
import Foundation
import Vision

let args = Array(CommandLine.arguments.dropFirst())
if args.first == "--langs" {
    let r = VNRecognizeTextRequest()
    r.recognitionLevel = .accurate
    let langs = (try? r.supportedRecognitionLanguages()) ?? []
    print(langs.joined(separator: ","))
    exit(0)
}
guard !args.isEmpty else {
    fputs("usage: frame_boxes [--langs] <image>...\n", stderr)
    exit(2)
}

func box(_ b: CGRect) -> [String: Double] {
    ["x": Double(b.minX), "y": Double(1 - b.maxY), "w": Double(b.width), "h": Double(b.height)]
}

var results: [[String: Any]] = []
for path in args {
    var row: [String: Any] = ["file": path, "faces": [], "text": []]
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        row["error"] = "unreadable"
        results.append(row)
        continue
    }
    let faces = VNDetectFaceRectanglesRequest()
    let text = VNRecognizeTextRequest()
    text.recognitionLevel = .accurate
    text.usesLanguageCorrection = false
    let supported = (try? text.supportedRecognitionLanguages()) ?? []
    text.recognitionLanguages = ["ar", "en-US"].filter { lang in supported.contains { $0.hasPrefix(lang.prefix(2)) } }
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([faces, text])
    row["faces"] = (faces.results ?? []).map { box($0.boundingBox) }
    row["text"] = (text.results ?? []).compactMap { obs -> [String: Any]? in
        guard let top = obs.topCandidates(1).first else { return nil }
        var b: [String: Any] = box(obs.boundingBox)
        b["s"] = top.string
        b["c"] = Double(top.confidence)
        return b
    }
    results.append(row)
}
let data = try JSONSerialization.data(withJSONObject: results, options: [])
print(String(data: data, encoding: .utf8)!)
