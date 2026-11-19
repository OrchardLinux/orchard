import AppKit

// The guest's screen from QEMU's D-Bus display (display/qemu-display.c): the
// frame lives in memory shared with QEMU and is drawn as soon as QEMU says a
// rectangle changed. Nothing polls.
final class GuestDisplay {
    var onFrame: (() -> Void)?
    var onConnection: ((Bool) -> Void)?
    private(set) var pixels: UnsafeRawPointer?
    private(set) var width = 0, height = 0, stride = 0
    private var pending = false
    private let lock = NSLock()
    private let qmpPath: String

    init(qmpPath: String) { self.qmpPath = qmpPath }

    // Set up once per connection; when the VM goes away (a restart for
    // another device, say) the window connects to the next one by itself.
    private var connecting = false

    func start() {
        guard !connecting else { return }
        connecting = true
        Thread.detachNewThread { [weak self] in
            while let self {
                var error = [CChar](repeating: 0, count: 512)
                let callbacks = qd_callbacks(
                    context: Unmanaged.passUnretained(self).toOpaque(),
                    scanout: { context, pixels, width, height, stride in
                        let display = Unmanaged<GuestDisplay>.fromOpaque(context!).takeUnretainedValue()
                        display.lock.lock()
                        display.pixels = pixels
                        display.width = Int(width); display.height = Int(height); display.stride = Int(stride)
                        display.lock.unlock()
                        DispatchQueue.main.async { display.onConnection?(true) }
                        display.changed()
                    },
                    update: { context, _, _, _, _ in
                        Unmanaged<GuestDisplay>.fromOpaque(context!).takeUnretainedValue().changed()
                    },
                    disable: { context in
                        Unmanaged<GuestDisplay>.fromOpaque(context!).takeUnretainedValue().lost(reconnect: false)
                    },
                    closed: { context in
                        Unmanaged<GuestDisplay>.fromOpaque(context!).takeUnretainedValue().lost(reconnect: true)
                    })
                if qd_start(self.qmpPath, callbacks, &error, Int32(error.count)) == 0 {
                    DispatchQueue.main.async { self.connecting = false }
                    return
                }
                Thread.sleep(forTimeInterval: 1)
            }
        }
    }

    // No surface any more. A closed connection (the VM went away, a restart
    // for another device, say) is followed by connecting to the next VM; a
    // guest that only stopped showing one keeps its connection.
    private func lost(reconnect: Bool) {
        lock.lock()
        pixels = nil
        lock.unlock()
        DispatchQueue.main.async { self.onConnection?(false) }
        if reconnect { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.start() } }
    }

    // One redraw per main-loop turn, however many rectangles arrived.
    private func changed() {
        lock.lock()
        let schedule = !pending
        pending = true
        lock.unlock()
        guard schedule else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.lock.lock(); self.pending = false; self.lock.unlock()
            self.onFrame?()
        }
    }

    // The current frame as an image; QEMU keeps writing into the shared
    // memory, so it is copied once per drawn frame.
    func image() -> CGImage? {
        lock.lock(); defer { lock.unlock() }
        guard let pixels, width > 0, height > 0 else { return nil }
        guard let data = CFDataCreate(nil, pixels.assumingMemoryBound(to: UInt8.self), stride * height),
              let provider = CGDataProvider(data: data) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: stride,
                       space: CGColorSpaceCreateDeviceRGB(),
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
    }
}
