// Draws the Notetaker app icon (1024x1024 PNG) with CoreGraphics.
// Usage: swift scripts/make_icon.swift <out.png>
// scripts/build_icon.sh turns it into build/Notetaker.icns.
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(red: CGFloat((hex >> 16) & 0xff) / 255,
            green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

let space = CGColorSpaceCreateDeviceRGB()
let ctx = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8,
                    bytesPerRow: 0, space: space,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!

// Base tile on the macOS icon grid: 824pt rounded square centred on a 1024 canvas.
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)

ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 28, color: rgb(0x000000, 0.45))
ctx.addPath(tilePath)
ctx.setFillColor(rgb(0x1c1c1e))
ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(tilePath)
ctx.clip()
let bg = CGGradient(colorsSpace: space, colors: [rgb(0x3a3a3c), rgb(0x161618)] as CFArray,
                    locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])

// Note card: the "notes" half of the app.
let card = CGRect(x: 262, y: 250, width: 420, height: 540)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 30, color: rgb(0x000000, 0.5))
ctx.addPath(CGPath(roundedRect: card, cornerWidth: 44, cornerHeight: 44, transform: nil))
ctx.setFillColor(rgb(0xf2f2f7))
ctx.fillPath()
ctx.restoreGState()

// Text lines on the card; the first is the bold title.
let lines: [(CGFloat, CGFloat, CGFloat)] = [  // (y, width, thickness)
    (700, 250, 34), (622, 320, 22), (570, 300, 22), (518, 330, 22), (466, 210, 22),
]
for (y, w, h) in lines {
    let r = CGRect(x: card.minX + 46, y: y - h / 2, width: w, height: h)
    ctx.addPath(CGPath(roundedRect: r, cornerWidth: h / 2, cornerHeight: h / 2, transform: nil))
    ctx.setFillColor(h > 30 ? rgb(0x2c2c2e) : rgb(0x8e8e93, 0.7))
    ctx.fillPath()
}

// Record badge: the "recording" half, in the UI's accent red, with a waveform inside.
let center = CGPoint(x: 690, y: 330)
let radius: CGFloat = 150
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 34, color: rgb(0x000000, 0.55))
ctx.addEllipse(in: CGRect(x: center.x - radius - 16, y: center.y - radius - 16,
                          width: (radius + 16) * 2, height: (radius + 16) * 2))
ctx.setFillColor(rgb(0x1c1c1e))
ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addEllipse(in: CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2))
ctx.clip()
let red = CGGradient(colorsSpace: space, colors: [rgb(0xff6b61), rgb(0xe0352b)] as CFArray,
                     locations: [0, 1])!
ctx.drawLinearGradient(red, start: CGPoint(x: center.x, y: center.y + radius),
                       end: CGPoint(x: center.x, y: center.y - radius), options: [])
ctx.restoreGState()

let bars: [CGFloat] = [70, 130, 190, 110, 160, 80]
let barW: CGFloat = 24, gap: CGFloat = 16
let totalW = CGFloat(bars.count) * barW + CGFloat(bars.count - 1) * gap
for (i, h) in bars.enumerated() {
    let x = center.x - totalW / 2 + CGFloat(i) * (barW + gap)
    let r = CGRect(x: x, y: center.y - h / 2, width: barW, height: h)
    ctx.addPath(CGPath(roundedRect: r, cornerWidth: barW / 2, cornerHeight: barW / 2, transform: nil))
    ctx.setFillColor(rgb(0xffffff))
    ctx.fillPath()
}
ctx.restoreGState()

let image = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: image)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("Wrote \(out)")
