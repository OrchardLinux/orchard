import AppKit

// iPad (6th generation), Wi-Fi: 169.5 × 240 mm, 9.7-inch 4:3 display.
// Coordinates are expressed in device-relative units, with a 240-unit body height.
enum Device {
    static let width: CGFloat = 169.5
    static let height: CGFloat = 240
    static let scale: CGFloat = 3.0
    static let margin: CGFloat = 24
    static let canvas = NSSize(width: width * scale + margin * 2, height: height * scale + margin * 2)
}

enum HardwareButton: String, CaseIterable {
    case power, volumeUp, volumeDown, home
    var label: String {
        switch self {
        case .power: return "Power"
        case .volumeUp: return "Volume up"
        case .volumeDown: return "Volume down"
        case .home: return "Home"
        }
    }
}

final class SimulatorView: NSView {
    private var tracking: NSTrackingArea?
    private var hovered: HardwareButton?
    private var pressed: HardwareButton?
    private var mouseDownScreen: NSPoint?
    private var windowOrigin: NSPoint?
    private var resizeCorner: (right: Bool, top: Bool)?
    private var resizeFrame: NSRect?
    private var hoverCorner: Int?
    private var display: GuestDisplay?
    private let screen = GuestScreenView()
    private var hardware: VMHardware?
    private var brightness: CGFloat = 1 { didSet { screen.brightness = brightness } }
    private var guestConnected = false
    private var guestPointerDown = false
    private var touchPosition = NSPoint.zero
    private(set) var touchMode = true
    private var guestFocused = false
    private var guestKeysDown = Set<String>()
    private var resignObserver: NSObjectProtocol?
    private var appResignObserver: NSObjectProtocol?
    private let displayRect = NSRect(x: 10.835, y: 21.45, width: 147.83, height: 197.10)
    // The inner half of the bezel is the screen's edge: a drag that starts
    // there is a finger sliding onto the screen (the keyboard from the
    // bottom, panels from the sides and the top). The outer half moves the
    // frame; the hardware buttons keep their places.
    private var edgeRect: NSRect {
        let side = displayRect.minX / 2, end = displayRect.minY / 2
        return displayRect.insetBy(dx: -side, dy: -end)
    }
    private var edgeTouch = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        appResignObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.releaseGuestInput() }
        addSubview(screen)
        if let directory = ProcessInfo.processInfo.environment["ORCHARD_VM_DIR"] {
            let display = GuestDisplay(qmpPath: directory + "/viewer.sock")
            display.onFrame = { [weak self, weak display] in self?.screen.show(display?.image()) }
            display.onConnection = { [weak self] connected in
                guard let self else { return }
                self.guestConnected = connected
                if !connected {
                    self.releaseGuestInput()
                    self.hardware?.resetTouchConnection()
                    self.screen.show(nil)
                    self.guestFocused = false
                }
            }
            self.display = display
            display.start()
            do {
                let bridge = VMHardware(directory: directory)
                bridge.onBrightness = { [weak self] value in
                    guard let self, self.brightness != value else { return }
                    self.brightness = value
                    self.needsDisplay = true
                }
                hardware = bridge
                bridge.start()
            }
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let observer = resignObserver { NotificationCenter.default.removeObserver(observer) }
        resignObserver = nil
        if let window {
            resignObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didResignKeyNotification, object: window, queue: .main
            ) { [weak self] _ in self?.releaseGuestInput() }
        }
    }

    deinit {
        if let observer = resignObserver { NotificationCenter.default.removeObserver(observer) }
        if let observer = appResignObserver { NotificationCenter.default.removeObserver(observer) }
    }

    private func sendGuestKey(_ key: String, down: Bool) {
        if down { guestKeysDown.insert(key) } else { guestKeysDown.remove(key) }
        hardware?.key(key, down: down)
    }

    private func releaseGuestKeys() {
        pendingCommandTap?.cancel()
        pendingCommandTap = nil
        heldCommand = nil
        commandSent = false
        for key in guestKeysDown { hardware?.key(key, down: false) }
        guestKeysDown.removeAll()
    }

    private func releaseGuestInput() {
        releaseGuestKeys()
        if let pressed {
            hardware?.key(hardwareKey(pressed), down: false)
            self.pressed = nil
            needsDisplay = true
        }
        if guestPointerDown {
            if touchMode || edgeTouch { hardware?.touch("end", x: touchPosition.x, y: touchPosition.y) }
            else { hardware?.pointer(x: touchPosition.x, y: touchPosition.y, buttons: 0) }
            guestPointerDown = false
            edgeTouch = false
        }
    }

    func setTouchMode(_ enabled: Bool) {
        releaseGuestInput()
        touchMode = enabled
    }

    private func sendTouch(_ event: NSEvent, phase: String) {
        let point = devicePoint(event)
        // Clamped: a touch from the bezel lands on the screen's edge pixel.
        touchPosition = NSPoint(x: min(1, max(0, (point.x - displayRect.minX) / displayRect.width)),
                                y: min(1, max(0, (point.y - displayRect.minY) / displayRect.height)))
        hardware?.touch(phase, x: touchPosition.x, y: touchPosition.y)
    }

    private func sendPointer(_ event: NSEvent, buttons: UInt8) {
        let point = devicePoint(event)
        touchPosition = NSPoint(x: (point.x - displayRect.minX) / displayRect.width,
                                y: (point.y - displayRect.minY) / displayRect.height)
        hardware?.pointer(x: touchPosition.x, y: touchPosition.y, buttons: buttons)
    }

    private var viewScale: CGFloat { bounds.width / Device.canvas.width }
    private func corner(at point: NSPoint) -> (right: Bool, top: Bool)? {
        let s = viewScale
        let left = Device.margin * s
        let right = (Device.margin + Device.width * Device.scale) * s
        let top = Device.margin * s
        let bottom = (Device.margin + Device.height * Device.scale) * s
        let reach: CGFloat = 17 * s
        if abs(point.x - left) < reach && abs(point.y - top) < reach { return (false, true) }
        if abs(point.x - right) < reach && abs(point.y - top) < reach { return (true, true) }
        if abs(point.x - left) < reach && abs(point.y - bottom) < reach { return (false, false) }
        if abs(point.x - right) < reach && abs(point.y - bottom) < reach { return (true, false) }
        return nil
    }

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override var isOpaque: Bool { false }

    override func updateTrackingAreas() {
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.activeAlways, .mouseMoved, .mouseEnteredAndExited, .inVisibleRect], owner: self)
        addTrackingArea(area)
        tracking = area
        super.updateTrackingAreas()
    }

    private func devicePoint(_ event: NSEvent) -> NSPoint {
        let p = convert(event.locationInWindow, from: nil)
        let s = viewScale
        return NSPoint(x: (p.x / s - Device.margin) / Device.scale, y: (p.y / s - Device.margin) / Device.scale)
    }

    private func button(at p: NSPoint) -> HardwareButton? {
        if NSRect(x: 125, y: -5, width: 28, height: 10).contains(p) { return .power }
        if NSRect(x: 164, y: 31, width: 11, height: 19).contains(p) { return .volumeUp }
        if NSRect(x: 164, y: 50, width: 11, height: 19).contains(p) { return .volumeDown }
        if NSPoint(x: p.x - 84.75, y: p.y - 228.7).length < 7.3 { return .home }
        return nil
    }

    override func mouseMoved(with event: NSEvent) {
        if !touchMode && guestConnected && displayRect.contains(devicePoint(event)) { sendPointer(event, buttons: 0) }
        let next = button(at: devicePoint(event))
        let cornerPosition = corner(at: convert(event.locationInWindow, from: nil))
        let cornerIndex = cornerPosition.map { ($0.top ? 0 : 2) + ($0.right ? 1 : 0) }
        if next != hovered || cornerIndex != hoverCorner {
            hovered = next
            hoverCorner = cornerIndex
            window?.invalidateCursorRects(for: self)
            needsDisplay = true
        }
    }
    override func mouseExited(with event: NSEvent) { hovered = nil; hoverCorner = nil; window?.invalidateCursorRects(for: self); needsDisplay = true }
    override func resetCursorRects() {
        if let corner = hoverCorner {
            if #available(macOS 15.0, *) {
                let positions: [NSCursor.FrameResizePosition] = [.topLeft, .topRight, .bottomLeft, .bottomRight]
                addCursorRect(bounds, cursor: .frameResize(position: positions[corner], directions: .all))
            } else {
                addCursorRect(bounds, cursor: .crosshair)
            }
        } else if hovered != nil { addCursorRect(bounds, cursor: .pointingHand) }
        else { addCursorRect(bounds, cursor: .arrow) }
    }
    override func mouseDown(with event: NSEvent) {
        if guestConnected && displayRect.contains(devicePoint(event)) {
            guestFocused = true
            guestPointerDown = true
            window?.makeFirstResponder(self)
            if touchMode { sendTouch(event, phase: "begin") } else { sendPointer(event, buttons: 1) }
            return
        }
        if guestConnected && edgeRect.contains(devicePoint(event)) && button(at: devicePoint(event)) == nil {
            // Always a touch, also in mouse mode: edge swipes are touch gestures.
            guestFocused = true
            guestPointerDown = true
            edgeTouch = true
            window?.makeFirstResponder(self)
            sendTouch(event, phase: "begin")
            return
        }
        releaseGuestKeys()
        guestFocused = false
        resizeCorner = corner(at: convert(event.locationInWindow, from: nil))
        pressed = resizeCorner == nil ? button(at: devicePoint(event)) : nil
        if let pressed { hardware?.key(hardwareKey(pressed), down: true) }
        if pressed == nil {
            mouseDownScreen = NSEvent.mouseLocation
            windowOrigin = window?.frame.origin
            resizeFrame = window?.frame
        }
        needsDisplay = true
    }
    override func mouseDragged(with event: NSEvent) {
        if guestPointerDown {
            if touchMode || edgeTouch { sendTouch(event, phase: "update") } else { sendPointer(event, buttons: 1) }
            return
        }
        if let start = mouseDownScreen, let origin = windowOrigin {
            let now = NSEvent.mouseLocation
            if let corner = resizeCorner, let frame = resizeFrame {
                let aspect = Device.canvas.width / Device.canvas.height
                let dx = (now.x - start.x) * (corner.right ? 1 : -1)
                let dy = (now.y - start.y) * (corner.top ? 1 : -1) * aspect
                let delta = abs(dx) >= abs(dy) ? dx : dy
                let width = min(1400, max(260, frame.width + delta))
                let height = width / aspect
                let x = corner.right ? frame.minX : frame.maxX - width
                let y = corner.top ? frame.minY : frame.maxY - height
                window?.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
            } else {
                window?.setFrameOrigin(NSPoint(x: origin.x + now.x - start.x, y: origin.y + now.y - start.y))
            }
        }
    }
    override func mouseUp(with event: NSEvent) {
        if guestPointerDown {
            if touchMode || edgeTouch { sendTouch(event, phase: "end") } else { sendPointer(event, buttons: 0) }
            guestPointerDown = false
            edgeTouch = false
            return
        }
        if let pressed { hardware?.key(hardwareKey(pressed), down: false) }
        pressed = nil
        mouseDownScreen = nil
        windowOrigin = nil
        resizeCorner = nil
        resizeFrame = nil
        needsDisplay = true
    }

    // Optional integration probe: hold at a guest pixel while an observer checks the VM UI.
    func testTouchHold(x: CGFloat, y: CGFloat, completion: @escaping () -> Void) {
        guard let window else { completion(); return }
        let device = NSPoint(x: displayRect.minX + displayRect.width * x / 768,
                             y: displayRect.minY + displayRect.height * y / 1024)
        let location = convert(NSPoint(x: (Device.margin + device.x * Device.scale) * viewScale,
                                       y: (Device.margin + device.y * Device.scale) * viewScale), to: nil)
        func event(_ type: NSEvent.EventType) -> NSEvent? {
            NSEvent.mouseEvent(with: type, location: location, modifierFlags: [], timestamp: 0,
                               windowNumber: window.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)
        }
        if let down = event(.leftMouseDown) { mouseDown(with: down) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
            if let up = event(.leftMouseUp) { self?.mouseUp(with: up) }
            completion()
        }
    }

    func testTouchSwipe(completion: @escaping () -> Void) {
        guard let window else { completion(); return }
        func event(_ type: NSEvent.EventType, _ y: CGFloat) -> NSEvent? {
            let device = NSPoint(x: displayRect.minX + displayRect.width * 620 / 768,
                                 y: displayRect.minY + displayRect.height * y / 1024)
            let location = convert(NSPoint(x: (Device.margin + device.x * Device.scale) * viewScale,
                                           y: (Device.margin + device.y * Device.scale) * viewScale), to: nil)
            return NSEvent.mouseEvent(with: type, location: location, modifierFlags: [], timestamp: 0,
                                      windowNumber: window.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)
        }
        if let down = event(.leftMouseDown, 700) { mouseDown(with: down) }
        for step in 1...20 {
            DispatchQueue.main.asyncAfter(deadline: .now() + Double(step) * 0.025) { [weak self] in
                if let drag = event(.leftMouseDragged, 700 - CGFloat(step) * 18) { self?.mouseDragged(with: drag) }
                if step == 20 {
                    if let up = event(.leftMouseUp, 340) { self?.mouseUp(with: up) }
                    completion()
                }
            }
        }
    }

    func testPowerButton(completion: @escaping (String) -> Void) {
        guard let window else { completion("FAIL: no window"); return }
        let initial = brightness
        let p = convert(NSPoint(x: (Device.margin + 138 * Device.scale) * viewScale,
                                y: (Device.margin - 0.2 * Device.scale) * viewScale), to: nil)
        func click() {
            for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                guard let event = NSEvent.mouseEvent(with: type, location: p, modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1) else { continue }
                if type == .leftMouseDown { mouseDown(with: event) } else { mouseUp(with: event) }
            }
        }
        click()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self else { completion("FAIL: view closed"); return }
            let off = self.brightness
            click()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                let restored = self.brightness
                completion(off == 0 && restored > 0 ? "PASS: power click blanked and restored (\(initial), \(off), \(restored))" : "FAIL: brightness (\(initial), \(off), \(restored))")
            }
        }
    }

    private func hardwareKey(_ button: HardwareButton) -> String {
        switch button {
        case .power: return "power"
        case .volumeUp: return "volumeup"
        case .volumeDown: return "volumedown"
        case .home: return "ac_home"
        }
    }

    override func rightMouseDown(with event: NSEvent) { if !touchMode && guestConnected && displayRect.contains(devicePoint(event)) { guestFocused = true; window?.makeFirstResponder(self); sendPointer(event, buttons: 4) } }
    override func rightMouseUp(with event: NSEvent) { if !touchMode && guestConnected { sendPointer(event, buttons: 0) } }
    override func rightMouseDragged(with event: NSEvent) { if !touchMode && guestConnected { sendPointer(event, buttons: 4) } }
    override func scrollWheel(with event: NSEvent) {
        guard !touchMode && guestConnected && displayRect.contains(devicePoint(event)) && event.scrollingDeltaY != 0 else { return }
        sendPointer(event, buttons: event.scrollingDeltaY > 0 ? 8 : 16)
        sendPointer(event, buttons: 0)
    }
    // macOS virtual key codes (US layout positions) to QEMU qcodes.
    private static let qcodes: [UInt16: String] = [
        0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v", 11: "b", 12: "q",
        13: "w", 14: "e", 15: "r", 16: "y", 17: "t", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6", 23: "5",
        24: "equal", 25: "9", 26: "7", 27: "minus", 28: "8", 29: "0", 30: "bracket_right", 31: "o", 32: "u",
        33: "bracket_left", 34: "i", 35: "p", 36: "ret", 37: "l", 38: "j", 39: "apostrophe", 40: "k",
        41: "semicolon", 42: "backslash", 43: "comma", 44: "slash", 45: "n", 46: "m", 47: "dot", 48: "tab",
        49: "spc", 50: "grave_accent", 51: "backspace", 53: "esc", 117: "delete", 115: "home", 119: "end",
        116: "pgup", 121: "pgdn", 123: "left", 124: "right", 125: "down", 126: "up",
        122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5", 97: "f6", 98: "f7", 100: "f8", 101: "f9",
        109: "f10", 103: "f11", 111: "f12",
        56: "shift", 60: "shift_r", 59: "ctrl", 62: "ctrl_r", 58: "alt", 61: "alt_r", 55: "meta_l", 54: "meta_r",
    ]
    // Command is the guest's Super, but macOS keeps Cmd+Tab and the like:
    // the window loses focus before Cmd comes up, which would reach the
    // guest as a lone Super tap. So Cmd goes to the guest only with the key
    // it modifies, or as a tap of its own while this window stays active.
    private var heldCommand: String?
    private var commandSent = false
    // macOS switches apps only after Cmd comes up from Cmd+Tab: a lone Cmd
    // tap waits a moment and is dropped when the window loses focus meanwhile.
    private var pendingCommandTap: DispatchWorkItem?

    override func keyDown(with event: NSEvent) {
        if guestFocused, let key = Self.qcodes[event.keyCode] {
            if let command = heldCommand, !commandSent { sendGuestKey(command, down: true); commandSent = true }
            if !event.isARepeat { sendGuestKey(key, down: true) }
        }
        else { super.keyDown(with: event) }
    }
    override func keyUp(with event: NSEvent) { if guestFocused, let key = Self.qcodes[event.keyCode] { sendGuestKey(key, down: false) } else { super.keyUp(with: event) } }
    override func flagsChanged(with event: NSEvent) {
        guard guestFocused, let key = Self.qcodes[event.keyCode] else { return }
        let flags: [UInt16: NSEvent.ModifierFlags] = [56: .shift, 60: .shift, 59: .control, 62: .control, 58: .option, 61: .option, 55: .command, 54: .command]
        guard let flag = flags[event.keyCode] else { return }
        let down = event.modifierFlags.contains(flag)
        guard flag == .command else { sendGuestKey(key, down: down); return }
        if down {
            heldCommand = key
            commandSent = false
        } else if heldCommand == key {
            if commandSent { sendGuestKey(key, down: false) }
            else {
                let tap = DispatchWorkItem { [weak self] in
                    guard let self, NSApp.isActive, self.window?.isKeyWindow == true else { return }
                    self.sendGuestKey(key, down: true)
                    self.sendGuestKey(key, down: false)
                }
                pendingCommandTap = tap
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: tap)
            }
            heldCommand = nil
            commandSent = false
        }
    }

    private func rounded(_ rect: NSRect, _ radius: CGFloat, fill: NSColor, stroke: NSColor? = nil, lineWidth: CGFloat = 0.3) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        fill.setFill(); path.fill()
        if let stroke { stroke.setStroke(); path.lineWidth = lineWidth; path.stroke() }
    }
    private func gradient(_ rect: NSRect, radius: CGFloat, colors: [NSColor], angle: CGFloat = 0) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        NSGradient(colors: colors)?.draw(in: path, angle: angle)
    }
    private func c(_ white: CGFloat, _ alpha: CGFloat = 1) -> NSColor { NSColor(white: white, alpha: alpha) }
    private func glow(_ rect: NSRect, radius: CGFloat) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow(); shadow.shadowColor = NSColor(calibratedRed: 0.30, green: 0.58, blue: 0.92, alpha: 0.75)
        shadow.shadowBlurRadius = 3.5; shadow.set()
        NSColor(calibratedRed: 0.34, green: 0.60, blue: 0.95, alpha: 0.65).setStroke()
        path.lineWidth = 0.7; path.stroke()
        NSGraphicsContext.restoreGraphicsState()
    }

    // The guest screen is its own layer above the drawn frame, so a guest
    // frame never redraws the device around it.
    override func layout() {
        super.layout()
        let s = viewScale * Device.scale
        // On whole device pixels, so drawing the frame adds no half-pixel blur.
        screen.frame = backingAlignedRect(NSRect(x: (Device.margin + displayRect.minX * Device.scale) * viewScale,
                                                 y: (Device.margin + displayRect.minY * Device.scale) * viewScale,
                                                 width: displayRect.width * s, height: displayRect.height * s),
                                          options: .alignAllEdgesNearest)
    }
    override func setFrameSize(_ newSize: NSSize) { super.setFrameSize(newSize); needsLayout = true }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSGraphicsContext.saveGraphicsState()
        let transform = NSAffineTransform()
        transform.scale(by: viewScale)
        transform.translateX(by: Device.margin, yBy: Device.margin)
        transform.scale(by: Device.scale)
        transform.concat()

        // Hardware controls sit behind the aluminium edge.
        let power = NSRect(x: 128.5, y: -1.1, width: 19, height: 2.3)
        let up = NSRect(x: 168.4, y: 34.5, width: 2.3, height: 12)
        let down = NSRect(x: 168.4, y: 53, width: 2.3, height: 12)
        for (button, rect) in [(HardwareButton.power, power), (.volumeUp, up), (.volumeDown, down)] {
            gradient(rect, radius: 0.8, colors: [c(0.55), c(0.9), c(0.65)], angle: 0)
            if hovered == button || pressed == button { glow(rect.insetBy(dx: -0.5, dy: -0.5), radius: 1.2) }
        }

        // A shadow follows the silhouette, rather than adding a rectangular window border.
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow(); shadow.shadowColor = c(0, 0.40); shadow.shadowBlurRadius = 8
        shadow.shadowOffset = NSSize(width: 0, height: -2); shadow.set()
        rounded(NSRect(x: 0, y: 0, width: 169.5, height: 240), 10, fill: c(0.75))
        NSGraphicsContext.restoreGraphicsState()

        // Silver chamfer, softly rounded white front glass, and the thin dark display gasket.
        gradient(NSRect(x: 0, y: 0, width: 169.5, height: 240), radius: 10,
                 colors: [c(0.65), c(0.96), c(0.72)], angle: 0)
        rounded(NSRect(x: 0.85, y: 0.85, width: 167.8, height: 238.3), 9.1,
                fill: c(0.96), stroke: c(1, 0.9), lineWidth: 0.35)
        gradient(NSRect(x: 1.25, y: 1.25, width: 167, height: 237.5), radius: 8.8,
                 colors: [c(0.94), c(0.992), c(0.96)], angle: 0)

        // 9.7-inch display is 147.83 × 197.10 mm (4:3), centered horizontally.
        let display = displayRect
        rounded(display.insetBy(dx: -0.35, dy: -0.35), 0.45, fill: c(0.14))
        rounded(display, 0.12, fill: NSColor(calibratedRed: 0.025, green: 0.029, blue: 0.035, alpha: 1))
        // A restrained glass reflection; no fake desktop or fabricated device state.
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: display).addClip()
        let reflection = NSBezierPath()
        reflection.move(to: NSPoint(x: display.minX, y: display.minY))
        reflection.line(to: NSPoint(x: display.maxX, y: display.minY))
        reflection.line(to: NSPoint(x: display.minX, y: display.minY + 88))
        reflection.close()
        c(1, 0.012).setFill(); reflection.fill()
        NSGraphicsContext.restoreGraphicsState()

        // Front camera and home button ring.
        rounded(NSRect(x: 83.1, y: 9.0, width: 3.3, height: 3.3), 1.65, fill: c(0.71))
        rounded(NSRect(x: 83.55, y: 9.45, width: 2.4, height: 2.4), 1.2,
                fill: NSColor(calibratedRed: 0.10, green: 0.14, blue: 0.18, alpha: 1))
        rounded(NSRect(x: 84.25, y: 9.95, width: 0.65, height: 0.65), 0.32, fill: c(0.49))
        let home = NSRect(x: 79.2, y: 223.15, width: 11.1, height: 11.1)
        gradient(home, radius: 5.55, colors: [c(0.62), c(0.94), c(0.69)], angle: 45)
        rounded(home.insetBy(dx: 0.65, dy: 0.65), 4.9, fill: c(0.97), stroke: c(1, 0.8), lineWidth: 0.15)
        if hovered == .home || pressed == .home { glow(home.insetBy(dx: -0.6, dy: -0.6), radius: 6.2) }
        if pressed == .home { rounded(home.insetBy(dx: 0.8, dy: 0.8), 4.7, fill: c(0.86, 0.35)) }

        NSGraphicsContext.restoreGraphicsState()
    }
}

final class GuestScreenView: NSView {
    private let dim = CALayer()
    var brightness: CGFloat = 1 { didSet { dim.opacity = Float(1 - max(0, min(1, brightness))) } }
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.contentsGravity = .resize
        layer?.magnificationFilter = .linear
        // The guest draws at the iPad's own 1536 x 2048, more pixels than the
        // window usually has room for. Plain linear: trilinear blends in the
        // half-size mipmap and softens a frame shrunk only to about 3/4.
        layer?.minificationFilter = .linear
        dim.backgroundColor = NSColor.black.cgColor
        dim.opacity = 0
        layer?.addSublayer(dim)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func layout() { super.layout(); dim.frame = bounds }
    func show(_ image: CGImage?) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        layer?.contents = image
        CATransaction.commit()
    }
}

private extension NSPoint {
    var length: CGFloat { sqrt(x * x + y * y) }
}

final class SimulatorWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        if ProcessInfo.processInfo.environment["ORCHARD_VM_SCRIPT"] != nil {
            let stop = NSMenuItem(title: "Shut Down Development VM", action: #selector(stopVM), keyEquivalent: "")
            stop.target = self
            appMenu.addItem(stop)
            appMenu.addItem(.separator())
        }
        if let directory = ProcessInfo.processInfo.environment["ORCHARD_VM_DIR"] {
            // The tablets' GPU desktop (on software OpenGL here) or the
            // desktop of a board without a GPU. Switching restarts the session.
            let current = (try? String(contentsOfFile: directory + "/desktop", encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? "gpu"
            for (title, name) in [("GPU Desktop", "gpu"), ("Software Desktop", "software")] {
                let item = NSMenuItem(title: title, action: #selector(selectDesktop(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = name
                item.state = current == name ? .on : .off
                appMenu.addItem(item)
            }
            appMenu.addItem(.separator())
        }
        let touch = NSMenuItem(title: "Touch Input", action: #selector(selectTouchInput(_:)), keyEquivalent: "")
        touch.target = self
        touch.tag = 1
        touch.state = .on
        appMenu.addItem(touch)
        let mouse = NSMenuItem(title: "Mouse Input", action: #selector(selectTouchInput(_:)), keyEquivalent: "")
        mouse.target = self
        mouse.tag = 0
        appMenu.addItem(mouse)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Orchard Simulator", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        if ProcessInfo.processInfo.environment["ORCHARD_VM_SCRIPT"] != nil {
            let detach = NSMenuItem(title: "Quit and Keep VM Running", action: #selector(quitKeepingVM), keyEquivalent: "q")
            detach.keyEquivalentModifierMask = [.command, .option]
            detach.target = self
            detach.isAlternate = true
            appMenu.addItem(detach)
        }
        appItem.submenu = appMenu
        app.mainMenu = menu
        window = SimulatorWindow(contentRect: NSRect(origin: .zero, size: Device.canvas), styleMask: [.borderless], backing: .buffered, defer: false)
        window.title = "Orchard Simulator"
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.acceptsMouseMovedEvents = true
        window.isReleasedWhenClosed = false
        window.contentView = SimulatorView(frame: NSRect(origin: .zero, size: Device.canvas))
        window.center()
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        if let path = ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_TOUCH_CHECK"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let view = self?.window.contentView as? SimulatorView else { app.terminate(nil); return }
                view.testTouchHold(x: 480, y: 480) {
                    try? "sent".write(toFile: path, atomically: true, encoding: .utf8)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { app.terminate(nil) }
                }
            }
        }
        if let path = ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_SWIPE_CHECK"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let view = self?.window.contentView as? SimulatorView else { app.terminate(nil); return }
                view.testTouchSwipe {
                    try? "sent".write(toFile: path, atomically: true, encoding: .utf8)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { app.terminate(nil) }
                }
            }
        }
        if let path = ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_SELF_TEST"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                guard let view = self?.window.contentView as? SimulatorView else { app.terminate(nil); return }
                view.testPowerButton { result in
                    try? result.write(toFile: path, atomically: true, encoding: .utf8)
                    app.terminate(nil)
                }
            }
        }
        if let path = ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_RENDER_CHECK"] {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                if let view = self?.window.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    if let data = bitmap.representation(using: .png, properties: [:]) { try? data.write(to: URL(fileURLWithPath: path)) }
                }
                app.terminate(nil)
            }
        }
    }
    @objc private func selectDesktop(_ item: NSMenuItem) {
        guard let name = item.representedObject as? String else { return }
        runVM(["desktop", name]) { ok, message in
            if ok {
                for entry in item.menu?.items ?? [] where entry.action == #selector(self.selectDesktop(_:)) {
                    entry.state = entry === item ? .on : .off
                }
                return
            }
            let alert = NSAlert()
            alert.messageText = "Could not switch the desktop"
            alert.informativeText = message
            alert.runModal()
        }
    }
    @objc private func selectTouchInput(_ item: NSMenuItem) {
        guard let view = window.contentView as? SimulatorView else { return }
        view.setTouchMode(item.tag == 1)
        for entry in item.menu?.items ?? [] where entry.action == #selector(selectTouchInput(_:)) {
            entry.state = entry === item ? .on : .off
        }
    }
    // Quitting shuts the VM down with it, so no VM keeps running unseen. The
    // test modes and Option-Quit leave it running.
    private var keepVM = ["TOUCH_CHECK", "SWIPE_CHECK", "SELF_TEST", "RENDER_CHECK"].contains {
        ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_" + $0] != nil
    }
    private var stopping = false
    @objc private func quitKeepingVM() {
        keepVM = true
        NSApplication.shared.terminate(nil)
    }
    // Runs the VM script with COMMAND; calls DONE on the main queue with success and output.
    private func runVM(_ command: String, done: @escaping (Bool, String) -> Void) {
        runVM([command], done: done)
    }
    private func runVM(_ arguments: [String], done: @escaping (Bool, String) -> Void) {
        guard let script = ProcessInfo.processInfo.environment["ORCHARD_VM_SCRIPT"] else { done(true, ""); return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: script)
        process.arguments = arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.terminationHandler = { task in
            let message = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async { done(task.terminationStatus == 0, message) }
        }
        do { try process.run() }
        catch { done(false, error.localizedDescription) }
    }
    @objc private func stopVM() {
        runVM("stop") { ok, message in
            guard !ok else { return }
            let alert = NSAlert()
            alert.messageText = "VM shutdown failed"
            alert.informativeText = message
            alert.runModal()
        }
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if keepVM || ProcessInfo.processInfo.environment["ORCHARD_VM_SCRIPT"] == nil { return .terminateNow }
        if !stopping {
            stopping = true
            window.alphaValue = 0.5
            runVM("stop") { ok, message in
                if ok { sender.reply(toApplicationShouldTerminate: true); return }
                let alert = NSAlert()
                alert.messageText = "The VM did not shut down"
                alert.informativeText = message
                alert.addButton(withTitle: "Force Off")
                alert.addButton(withTitle: "Leave Running")
                alert.addButton(withTitle: "Cancel")
                switch alert.runModal() {
                case .alertFirstButtonReturn:
                    self.runVM("kill") { _, _ in sender.reply(toApplicationShouldTerminate: true) }
                case .alertSecondButtonReturn:
                    sender.reply(toApplicationShouldTerminate: true)
                default:
                    self.stopping = false
                    self.window.alphaValue = 1
                    sender.reply(toApplicationShouldTerminate: false)
                }
            }
        }
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
