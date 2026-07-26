// macOS Vision 前景抠图工具：把输入图的主体抠出为透明背景 PNG。
// 用法：pet_foreground_mask <input.png> <output.png>
// 由 scripts/cut_pet_assets.py 按需编译调用（需要 macOS 14+ 与 Xcode CLT）。

import CoreImage
import Foundation
import Vision

guard CommandLine.arguments.count == 3 else {
    FileHandle.standardError.write(Data("usage: pet_foreground_mask <input.png> <output.png>\n".utf8))
    exit(2)
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])

guard let image = CIImage(contentsOf: inputURL) else {
    FileHandle.standardError.write(Data("failed to load \(inputURL.path)\n".utf8))
    exit(1)
}

let handler = VNImageRequestHandler(ciImage: image, options: [:])
let request = VNGenerateForegroundInstanceMaskRequest()

do {
    try handler.perform([request])
    guard let observation = request.results?.first else {
        FileHandle.standardError.write(Data("no foreground instance found in \(inputURL.path)\n".utf8))
        exit(1)
    }
    let buffer = try observation.generateMaskedImage(
        ofInstances: observation.allInstances,
        from: handler,
        croppedToInstancesExtent: false
    )
    let masked = CIImage(cvPixelBuffer: buffer)
    let context = CIContext()
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
        FileHandle.standardError.write(Data("failed to create sRGB color space\n".utf8))
        exit(1)
    }
    try context.writePNGRepresentation(of: masked, to: outputURL, format: .RGBA8, colorSpace: colorSpace)
} catch {
    FileHandle.standardError.write(Data("error: \(error)\n".utf8))
    exit(1)
}
