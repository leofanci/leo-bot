// Render dashboard/logo.svg as a macOS app icon PNG on Apple's icon grid:
// the tile sits at 824/1024 of the canvas, centred, with a soft drop shadow,
// so the Dock and Finder show it the same size as the system apps.
// Usage: swift render-icon.swift <logo.svg> <out.png> <pixels>
import AppKit

let args = CommandLine.arguments
guard args.count == 4, let logo = NSImage(contentsOfFile: args[1]), let px = Int(args[3]) else {
    FileHandle.standardError.write("usage: render-icon.swift <logo.svg> <out.png> <pixels>\n".data(using: .utf8)!)
    exit(2)
}
let s = CGFloat(px) / 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSColor.clear.set()
NSRect(x: 0, y: 0, width: px, height: px).fill()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
shadow.shadowOffset = NSSize(width: 0, height: -10 * s)
shadow.shadowBlurRadius = 20 * s
shadow.set()
logo.draw(in: NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s))
NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: args[2]))
