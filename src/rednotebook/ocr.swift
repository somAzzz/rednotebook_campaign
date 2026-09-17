import Foundation
import Vision
import ImageIO
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true
do {
    try VNImageRequestHandler(url: url).perform([request])
    let values = (request.results ?? []).compactMap { item -> [String: Any]? in
        guard let candidate = item.topCandidates(1).first else { return nil }
        return ["text": candidate.string, "confidence": candidate.confidence,
                "box": [item.boundingBox.minX, item.boundingBox.minY,
                         item.boundingBox.width, item.boundingBox.height]]
    }
    let data = try JSONSerialization.data(withJSONObject: values, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
} catch { fputs("ocr_failed", stderr); exit(1) }
