import AppKit

// A simulated device, upright, in millimetres from its top left corner: the
// body, the display, the buttons. Its panel (the VM's screen) is fixed when
// the VM starts; the frame is drawn from these numbers.
struct DeviceProfile {
    let name: String            // the VM script's name for it
    let title: String
    let width: CGFloat
    let height: CGFloat
    let cornerRadius: CGFloat
    let display: NSRect
    let displayRadius: CGFloat
    let home: NSRect
    // Drawn button, and the larger area a click finds it in.
    let buttons: [(button: HardwareButton, drawn: NSRect, hit: NSRect)]
    let muteSwitch: NSRect?
    let camera: NSPoint
    let earpiece: NSRect?
    let darkFront: Bool
    // Window pixels per millimetre: both devices open about as tall.
    var scale: CGFloat { 720 / height }
    static let margin: CGFloat = 24

    // iPad (6th generation), Wi-Fi: 169.5 × 240 mm, 9.7-inch 4:3 display.
    static let ipad6 = DeviceProfile(
        name: "ipad6", title: "iPad (6th generation)", width: 169.5, height: 240, cornerRadius: 10,
        display: NSRect(x: 10.835, y: 21.45, width: 147.83, height: 197.10), displayRadius: 0.12,
        home: NSRect(x: 79.2, y: 223.15, width: 11.1, height: 11.1),
        buttons: [(.power, NSRect(x: 128.5, y: -1.1, width: 19, height: 2.3), NSRect(x: 125, y: -5, width: 28, height: 10)),
                  (.volumeUp, NSRect(x: 168.4, y: 34.5, width: 2.3, height: 12), NSRect(x: 164, y: 31, width: 11, height: 19)),
                  (.volumeDown, NSRect(x: 168.4, y: 53, width: 2.3, height: 12), NSRect(x: 164, y: 50, width: 11, height: 19))],
        muteSwitch: nil, camera: NSPoint(x: 84.75, y: 10.65), earpiece: nil, darkFront: false)

    // iPhone 7: 67.1 × 138.3 mm, 4.7-inch 16:9 display (750 × 1334 pixels),
    // black front; the side button on the right, volume and the ring/silent
    // switch on the left, the camera beside the earpiece.
    static let iphone7 = DeviceProfile(
        name: "iphone7", title: "iPhone 7", width: 67.1, height: 138.3, cornerRadius: 9.4,
        display: NSRect(x: 4.3, y: 17.15, width: 58.5, height: 104.0), displayRadius: 0.2,
        home: NSRect(x: 28.1, y: 124.3, width: 10.9, height: 10.9),
        buttons: [(.power, NSRect(x: 66.75, y: 33, width: 1.6, height: 10.5), NSRect(x: 63, y: 30, width: 9, height: 16)),
                  (.volumeUp, NSRect(x: -1.25, y: 33.5, width: 1.6, height: 8.5), NSRect(x: -5, y: 31, width: 9, height: 12)),
                  (.volumeDown, NSRect(x: -1.25, y: 45, width: 1.6, height: 8.5), NSRect(x: -5, y: 43, width: 9, height: 12))],
        muteSwitch: NSRect(x: -1.1, y: 22, width: 1.4, height: 5), camera: NSPoint(x: 23.2, y: 8.7),
        earpiece: NSRect(x: 28.6, y: 8.05, width: 9.9, height: 1.3), darkFront: true)

    static let all = [ipad6, iphone7]
    static func named(_ name: String) -> DeviceProfile { all.first { $0.name == name } ?? ipad6 }
}

// How the window holds the device: the edge that is up, named as the
// accelerometer (iio-sensor-proxy) names it, and the clockwise turn that
// draws the device so.
enum Orientation: String, CaseIterable {
    case normal, leftUp = "left-up", bottomUp = "bottom-up", rightUp = "right-up"
    var degrees: CGFloat { CGFloat(Orientation.allCases.firstIndex(of: self)!) * 90 }
    var quarterTurned: Bool { self == .leftUp || self == .rightUp }
    // A clockwise quarter turn brings the left edge up.
    func turned(clockwise: Bool) -> Orientation {
        let all = Orientation.allCases
        return all[(all.firstIndex(of: self)! + (clockwise ? 1 : 3)) % 4]
    }
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
    private var commandKeyUpMonitor: Any?
    private(set) var profile = DeviceProfile.ipad6
    private(set) var orientation = Orientation.normal
    private var displayRect: NSRect { profile.display }
    private var margin: CGFloat { DeviceProfile.margin }

    // The device as the window holds it, in millimetres, and the window's
    // canvas around it.
    private var heldSize: NSSize {
        orientation.quarterTurned ? NSSize(width: profile.height, height: profile.width)
                                  : NSSize(width: profile.width, height: profile.height)
    }
    var canvas: NSSize {
        NSSize(width: heldSize.width * profile.scale + margin * 2, height: heldSize.height * profile.scale + margin * 2)
    }

    // Device millimetres (upright) to the held device's millimetres, and back.
    private func held(_ p: NSPoint) -> NSPoint {
        let w = profile.width, h = profile.height
        switch orientation {
        case .normal: return p
        case .leftUp: return NSPoint(x: h - p.y, y: p.x)
        case .bottomUp: return NSPoint(x: w - p.x, y: h - p.y)
        case .rightUp: return NSPoint(x: p.y, y: w - p.x)
        }
    }
    private func upright(_ p: NSPoint) -> NSPoint {
        let w = profile.width, h = profile.height
        switch orientation {
        case .normal: return p
        case .leftUp: return NSPoint(x: p.y, y: h - p.x)
        case .bottomUp: return NSPoint(x: w - p.x, y: h - p.y)
        case .rightUp: return NSPoint(x: w - p.y, y: p.x)
        }
    }
    // A device point to view coordinates.
    private func viewPoint(_ p: NSPoint) -> NSPoint {
        let q = held(p)
        return NSPoint(x: (margin + q.x * profile.scale) * viewScale, y: (margin + q.y * profile.scale) * viewScale)
    }

    func setDevice(_ next: DeviceProfile) {
        releaseGuestInput()
        profile = next
        orientation = .normal
        needsLayout = true
        needsDisplay = true
    }

    func setOrientation(_ next: Orientation) {
        releaseGuestInput()
        orientation = next
        needsLayout = true
        needsDisplay = true
    }
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
        // While Command is held macOS sends the window no key-up for the other
        // keys; the application still sees them. Without this the guest takes
        // a released key for a held one and repeats it (Super+Shift+Space
        // toggling the bar on and off for as long as Command and Shift stay down).
        if commandKeyUpMonitor == nil {
            commandKeyUpMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyUp) { [weak self] event in
                guard let self, event.window === self.window, event.modifierFlags.contains(.command),
                      self.guestFocused, let key = Self.qcodes[event.keyCode], self.guestKeysDown.contains(key) else { return event }
                self.sendGuestKey(key, down: false)
                return nil
            }
        }
        if let observer = resignObserver { NotificationCenter.default.removeObserver(observer) }
        resignObserver = nil
        if let window {
            resignObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didResignKeyNotification, object: window, queue: .main
            ) { [weak self] _ in self?.releaseGuestInput() }
        }
    }

    deinit {
        if let monitor = commandKeyUpMonitor { NSEvent.removeMonitor(monitor) }
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

    private var viewScale: CGFloat { bounds.width / canvas.width }
    private func corner(at point: NSPoint) -> (right: Bool, top: Bool)? {
        let s = viewScale
        let left = margin * s
        let right = (margin + heldSize.width * profile.scale) * s
        let top = margin * s
        let bottom = (margin + heldSize.height * profile.scale) * s
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
        return upright(NSPoint(x: (p.x / s - margin) / profile.scale, y: (p.y / s - margin) / profile.scale))
    }

    private func button(at p: NSPoint) -> HardwareButton? {
        for entry in profile.buttons where entry.hit.contains(p) { return entry.button }
        if NSPoint(x: p.x - profile.home.midX, y: p.y - profile.home.midY).length < profile.home.width * 0.66 { return .home }
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
        resizeCorner = corner(at: convert(event.locationInWindow, from: nil))
        pressed = resizeCorner == nil ? button(at: devicePoint(event)) : nil
        // A hardware button is part of the tablet: the Mac keyboard keeps
        // typing into the guest after it (Home, then type to search). Only
        // moving or resizing the frame hands the keyboard back to macOS.
        if pressed == nil {
            releaseGuestKeys()
            guestFocused = false
        } else if guestConnected {
            guestFocused = true
            window?.makeFirstResponder(self)
        }
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
                let aspect = canvas.width / canvas.height
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
        let location = convert(viewPoint(device), to: nil)
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
            let location = convert(viewPoint(device), to: nil)
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
        let power = profile.buttons.first { $0.button == .power }!.drawn
        let p = convert(viewPoint(NSPoint(x: power.midX, y: power.midY)), to: nil)
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
            if !event.isARepeat {
                // While Command is held macOS sends no key-up to the window, so a
                // key still marked down was let go unseen: release it first, or
                // the guest takes this press for a held key's repeat.
                if guestKeysDown.contains(key) { sendGuestKey(key, down: false) }
                sendGuestKey(key, down: true)
            }
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
            // The keys pressed with Command never got their key-up (see keyDown).
            let modifiers: Set<String> = ["shift", "shift_r", "ctrl", "ctrl_r", "alt", "alt_r", "meta_l", "meta_r"]
            for held in guestKeysDown.subtracting(modifiers) { sendGuestKey(held, down: false) }
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
    // frame never redraws the device around it. It keeps the panel's own
    // upright shape and turns with the device, as the real panel does; the
    // guest turns its picture inside it.
    override func layout() {
        super.layout()
        let a = viewPoint(NSPoint(x: displayRect.minX, y: displayRect.minY))
        let b = viewPoint(NSPoint(x: displayRect.maxX, y: displayRect.maxY))
        // On whole device pixels, so drawing the frame adds no half-pixel blur.
        let heldRect = backingAlignedRect(NSRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(b.x - a.x), height: abs(b.y - a.y)),
                                          options: .alignAllEdgesNearest)
        let size = orientation.quarterTurned ? NSSize(width: heldRect.height, height: heldRect.width) : heldRect.size
        screen.frameCenterRotation = 0
        screen.frame = NSRect(x: heldRect.midX - size.width / 2, y: heldRect.midY - size.height / 2, width: size.width, height: size.height)
        screen.frameCenterRotation = orientation.degrees
    }
    override func setFrameSize(_ newSize: NSSize) { super.setFrameSize(newSize); needsLayout = true }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let device = profile
        NSGraphicsContext.saveGraphicsState()
        let transform = NSAffineTransform()
        transform.scale(by: viewScale)
        transform.translateX(by: margin, yBy: margin)
        transform.scale(by: device.scale)
        // Held as the window holds it: turned clockwise about its corner.
        switch orientation {
        case .normal: break
        case .leftUp: transform.translateX(by: device.height, yBy: 0)
        case .bottomUp: transform.translateX(by: device.width, yBy: device.height)
        case .rightUp: transform.translateX(by: 0, yBy: device.width)
        }
        transform.rotate(byDegrees: orientation.degrees)
        transform.concat()

        let body = NSRect(x: 0, y: 0, width: device.width, height: device.height)
        let r = device.cornerRadius
        let dark = device.darkFront

        // Hardware controls sit behind the aluminium edge.
        for entry in device.buttons {
            let shades = dark ? [c(0.18), c(0.42), c(0.22)] : [c(0.55), c(0.9), c(0.65)]
            gradient(entry.drawn, radius: 0.8, colors: shades, angle: entry.drawn.width > entry.drawn.height ? 90 : 0)
            if hovered == entry.button || pressed == entry.button { glow(entry.drawn.insetBy(dx: -0.5, dy: -0.5), radius: 1.2) }
        }
        if let mute = device.muteSwitch {
            gradient(mute, radius: 0.5, colors: dark ? [c(0.18), c(0.4), c(0.22)] : [c(0.55), c(0.9), c(0.65)], angle: 0)
        }

        // A shadow follows the silhouette, rather than adding a rectangular window border.
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow(); shadow.shadowColor = c(0, 0.40); shadow.shadowBlurRadius = 8
        shadow.shadowOffset = NSSize(width: 0, height: -2); shadow.set()
        rounded(body, r, fill: c(dark ? 0.2 : 0.75))
        NSGraphicsContext.restoreGraphicsState()

        // The metal edge, the softly rounded front glass, and the thin dark display gasket.
        gradient(body, radius: r, colors: dark ? [c(0.12), c(0.34), c(0.16)] : [c(0.65), c(0.96), c(0.72)], angle: 0)
        let glass = body.insetBy(dx: 0.85, dy: 0.85)
        rounded(glass, r - 0.9, fill: c(dark ? 0.05 : 0.96), stroke: c(dark ? 0.32 : 1, 0.9), lineWidth: 0.35)
        gradient(body.insetBy(dx: 1.25, dy: 1.25), radius: r - 1.2,
                 colors: dark ? [c(0.035), c(0.075), c(0.045)] : [c(0.94), c(0.992), c(0.96)], angle: 0)

        let display = displayRect
        rounded(display.insetBy(dx: -0.35, dy: -0.35), device.displayRadius + 0.33, fill: c(dark ? 0.02 : 0.14))
        rounded(display, device.displayRadius, fill: NSColor(calibratedRed: 0.025, green: 0.029, blue: 0.035, alpha: 1))
        // A restrained glass reflection; no fake desktop or fabricated device state.
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: display).addClip()
        let reflection = NSBezierPath()
        reflection.move(to: NSPoint(x: display.minX, y: display.minY))
        reflection.line(to: NSPoint(x: display.maxX, y: display.minY))
        reflection.line(to: NSPoint(x: display.minX, y: display.minY + display.height * 0.45))
        reflection.close()
        c(1, 0.012).setFill(); reflection.fill()
        NSGraphicsContext.restoreGraphicsState()

        // Front camera, earpiece and the Home button ring.
        let cam = device.camera
        rounded(NSRect(x: cam.x - 1.65, y: cam.y - 1.65, width: 3.3, height: 3.3), 1.65, fill: c(dark ? 0.16 : 0.71))
        rounded(NSRect(x: cam.x - 1.2, y: cam.y - 1.2, width: 2.4, height: 2.4), 1.2,
                fill: NSColor(calibratedRed: 0.10, green: 0.14, blue: 0.18, alpha: 1))
        rounded(NSRect(x: cam.x - 0.5, y: cam.y - 0.7, width: 0.65, height: 0.65), 0.32, fill: c(0.49))
        if let earpiece = device.earpiece {
            rounded(earpiece, earpiece.height / 2, fill: c(dark ? 0.13 : 0.62), stroke: c(dark ? 0.24 : 0.5, 0.8), lineWidth: 0.15)
        }
        let home = device.home
        let ring = dark ? [c(0.28), c(0.55), c(0.32)] : [c(0.62), c(0.94), c(0.69)]
        gradient(home, radius: home.width / 2, colors: ring, angle: 45)
        rounded(home.insetBy(dx: 0.65, dy: 0.65), home.width / 2 - 0.65, fill: c(dark ? 0.06 : 0.97),
                stroke: c(dark ? 0.3 : 1, 0.8), lineWidth: 0.15)
        if hovered == .home || pressed == .home { glow(home.insetBy(dx: -0.6, dy: -0.6), radius: home.width / 2 + 0.6) }
        if pressed == .home { rounded(home.insetBy(dx: 0.8, dy: 0.8), home.width / 2 - 0.8, fill: c(dark ? 0.35 : 0.86, 0.35)) }

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
        let view = SimulatorView(frame: .zero)
        if let directory = ProcessInfo.processInfo.environment["ORCHARD_VM_DIR"] {
            // The device the VM's panel was started for.
            let name = (try? String(contentsOfFile: directory + "/device", encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? "ipad6"
            view.setDevice(DeviceProfile.named(name))
            if ProcessInfo.processInfo.environment["ORCHARD_VM_SCRIPT"] != nil {
                for device in DeviceProfile.all {
                    let item = NSMenuItem(title: device.title, action: #selector(selectDevice(_:)), keyEquivalent: "")
                    item.target = self
                    item.representedObject = device.name
                    item.state = device.name == view.profile.name ? .on : .off
                    appMenu.addItem(item)
                }
                appMenu.addItem(.separator())
            }
            // Upright to begin with, whatever the VM last felt.
            sendControl("orientation normal")
        }
        // For the render check: draw another device, or the device turned.
        if let name = ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_DEVICE"] { view.setDevice(DeviceProfile.named(name)) }
        if let held = ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_ORIENTATION"].flatMap(Orientation.init(rawValue:)) {
            view.setOrientation(held)
        }
        for (title, clockwise, key) in [("Rotate Left", false, NSLeftArrowFunctionKey), ("Rotate Right", true, NSRightArrowFunctionKey)] {
            let item = NSMenuItem(title: title, action: #selector(rotate(_:)), keyEquivalent: String(Character(UnicodeScalar(key)!)))
            item.keyEquivalentModifierMask = [.command, .control]
            item.target = self
            item.tag = clockwise ? 1 : 0
            appMenu.addItem(item)
        }
        appMenu.addItem(.separator())
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
        window = SimulatorWindow(contentRect: NSRect(origin: .zero, size: view.canvas), styleMask: [.borderless], backing: .buffered, defer: false)
        window.title = "Orchard Simulator"
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.acceptsMouseMovedEvents = true
        window.isReleasedWhenClosed = false
        view.frame = NSRect(origin: .zero, size: view.canvas)
        window.contentView = view
        window.center()
        // Started in the background (a rebuild while the person works
        // elsewhere): shown, but not brought to the front.
        if ProcessInfo.processInfo.environment["ORCHARD_SIMULATOR_BACKGROUND"] == "1" {
            window.orderFront(nil)
        } else {
            window.makeKeyAndOrderFront(nil)
            app.activate(ignoringOtherApps: true)
        }
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
    // Turns the device in the window, frame and screen together, and tells the
    // VM's accelerometer; the guest turns its picture as a tablet does.
    @objc private func rotate(_ item: NSMenuItem) {
        guard let view = window.contentView as? SimulatorView else { return }
        let next = view.orientation.turned(clockwise: item.tag == 1)
        refit(view) { view.setOrientation(next) }
        sendControl("orientation \(next.rawValue)")
    }

    // Another device means another panel: the VM restarts with it, on the
    // same disk; the session starts again at the PIN screen.
    @objc private func selectDevice(_ item: NSMenuItem) {
        guard let view = window.contentView as? SimulatorView, let name = item.representedObject as? String,
              name != view.profile.name else { return }
        let device = DeviceProfile.named(name)
        let confirm = NSAlert()
        confirm.messageText = "Switch to \(device.title)?"
        confirm.informativeText = "The VM restarts with the \(device.title)'s screen. Open apps close; the disk stays as it is."
        confirm.addButton(withTitle: "Restart as \(device.title)")
        confirm.addButton(withTitle: "Cancel")
        guard confirm.runModal() == .alertFirstButtonReturn else { return }
        window.alphaValue = 0.5
        runVM(["device", name]) { ok, message in
            self.window.alphaValue = 1
            guard ok else {
                let alert = NSAlert()
                alert.messageText = "Could not switch the device"
                alert.informativeText = message
                alert.runModal()
                return
            }
            self.refit(view) { view.setDevice(device) }
            for entry in item.menu?.items ?? [] where entry.action == #selector(self.selectDevice(_:)) {
                entry.state = entry === item ? .on : .off
            }
        }
    }

    // Resizes the window to the view's new canvas at the same zoom, about the
    // same centre.
    private func refit(_ view: SimulatorView, _ change: () -> Void) {
        let zoom = window.frame.width / view.canvas.width
        let center = NSPoint(x: window.frame.midX, y: window.frame.midY)
        change()
        let size = NSSize(width: view.canvas.width * zoom, height: view.canvas.height * zoom)
        window.setFrame(NSRect(x: center.x - size.width / 2, y: center.y - size.height / 2, width: size.width, height: size.height), display: true)
    }

    // One line to the VM bridge (org.orchard.control), as the VM script sends it.
    private func sendControl(_ line: String) {
        guard let directory = ProcessInfo.processInfo.environment["ORCHARD_VM_DIR"] else { return }
        DispatchQueue.global().async {
            let fd = socket(AF_UNIX, SOCK_STREAM, 0)
            guard fd >= 0 else { return }
            defer { close(fd) }
            var address = sockaddr_un()
            address.sun_family = sa_family_t(AF_UNIX)
            let path = directory + "/control.sock"
            guard path.utf8.count < MemoryLayout.size(ofValue: address.sun_path) else { return }
            withUnsafeMutableBytes(of: &address.sun_path) { buffer in
                path.utf8CString.withUnsafeBytes { buffer.copyMemory(from: $0) }
            }
            let connected = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
            }
            guard connected == 0 else { return }
            let data = Array((line + "\n").utf8)
            _ = data.withUnsafeBytes { Darwin.send(fd, $0.baseAddress, $0.count, 0) }
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
