import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import Vision

enum MaskError: Error { case invalidArguments, noFrame, writeFailed }

let arguments = CommandLine.arguments
guard arguments.count >= 5,
      let fps = Double(arguments[3]), fps > 0 else {
    fputs("usage: person_mask <video> <frames-dir> <fps> <fast|balanced|accurate>\n", stderr)
    throw MaskError.invalidArguments
}

let videoURL = URL(fileURLWithPath: arguments[1])
let outputDirectory = URL(fileURLWithPath: arguments[2], isDirectory: true)
let qualityName = arguments[4]
try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

let asset = AVURLAsset(url: videoURL)
let duration = try await asset.load(.duration)
let durationSeconds = CMTimeGetSeconds(duration)
guard durationSeconds.isFinite && durationSeconds > 0 else { throw MaskError.invalidArguments }

let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero

let request = VNGeneratePersonSegmentationRequest()
switch qualityName {
case "fast": request.qualityLevel = .fast
case "accurate": request.qualityLevel = .accurate
default: request.qualityLevel = .balanced
}
request.outputPixelFormat = kCVPixelFormatType_OneComponent8
let context = CIContext(options: [.cacheIntermediates: false])

let frameCount = max(1, Int((durationSeconds * fps).rounded(.up)))
for index in 0..<frameCount {
    let time = CMTime(seconds: min(Double(index) / fps, max(0, durationSeconds - 0.001)), preferredTimescale: 600)
    let image = try generator.copyCGImage(at: time, actualTime: nil)
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    guard let observation = request.results?.first else { throw MaskError.noFrame }
    let maskImage = CIImage(cvPixelBuffer: observation.pixelBuffer)
    guard let cgMask = context.createCGImage(maskImage, from: maskImage.extent) else { throw MaskError.noFrame }
    let frameURL = outputDirectory.appendingPathComponent(String(format: "mask_%06d.png", index + 1))
    guard let destination = CGImageDestinationCreateWithURL(frameURL as CFURL, UTType.png.identifier as CFString, 1, nil) else { throw MaskError.writeFailed }
    CGImageDestinationAddImage(destination, cgMask, nil)
    guard CGImageDestinationFinalize(destination) else { throw MaskError.writeFailed }
}
