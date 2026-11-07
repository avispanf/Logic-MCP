import AppKit
import CoreGraphics
import Foundation

private func logError(_ message: String) {
    let line = "[LogicMCPMenuBar] \(message)\n"
    FileHandle.standardError.write(Data(line.utf8))
}

private func bundledRuntimeURL() -> URL {
    Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/runtime/logic-mcp")
}

private final class LogicSurfaceController: @unchecked Sendable {
    func run(_ command: String) -> SurfaceState {
        let process = Process()
        let pipe = Pipe()
        let runtime = bundledRuntimeURL()
        let action = command == "diagnose" ? "status" : command
        guard FileManager.default.isExecutableFile(atPath: runtime.path) else {
            return .unavailable("Bundled runtime is missing. Reinstall Logic MCP.")
        }
        process.executableURL = runtime
        process.arguments = ["--control", action]
        process.standardOutput = pipe
        process.standardError = FileHandle.standardError
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        do {
            try process.run()
            guard finished.wait(timeout: .now() + 15) == .success else {
                process.terminate()
                return .unavailable("Control helper timed out; mode needs verification")
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return SurfaceState.decode(data, exitStatus: process.terminationStatus, command: command)
        } catch {
            return .unavailable(error.localizedDescription)
        }
    }
}

@MainActor
private final class AppDelegate: NSObject, NSApplicationDelegate {
    private let controller = LogicSurfaceController()
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var overlayPanel: NSPanel?
    private var overlayButton: NSButton?
    private var timer: Timer?
    private var busy = false
    private var pendingCommand: String?
    private var currentState: SurfaceState = .unavailable("Waiting for the first status check.")

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let button = statusItem.button else { return }
        button.title = "MCP/MCU …"
        button.toolTip = "Pause or resume Logic MCP and MCU"
        button.target = self
        button.action = #selector(toggle)
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        configureOverlay()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    private func configureOverlay() {
        let size = NSSize(width: 180, height: 34)
        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.backgroundColor = NSColor.windowBackgroundColor.withAlphaComponent(0.94)
        panel.isOpaque = false
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true

        let button = NSButton(frame: NSRect(x: 3, y: 3, width: 174, height: 28))
        button.bezelStyle = .rounded
        button.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        button.target = self
        button.action = #selector(toggle)
        button.toolTip = "Disable MCP/MCU while editing by hand"
        panel.contentView?.addSubview(button)

        overlayPanel = panel
        overlayButton = button
        updateOverlayVisibility()
    }

    @objc private func toggle() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            for (title, action) in [("Setup instructions", #selector(showHelp)),
                                    ("Manual mode — MCP OFF", #selector(pauseAutomation)),
                                    ("Enable NEW automation", #selector(resumeAutomation)),
                                    ("Copy MCP client configuration", #selector(copyClientConfiguration)),
                                    ("Reveal permission helper", #selector(revealPermissionHelper)),
                                    ("Accessibility settings", #selector(openAccessibility)),
                                    ("Install / repair service", #selector(setupService)),
                                    ("Diagnostics", #selector(diagnose)),
                                    ("Repair manual state", #selector(repair)),
                                    ("Remove service", #selector(removeService)),
                                    ("Show / hide floating button", #selector(toggleOverlay)),
                                    ("Quit", #selector(quit))] {
                let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
                item.target = self
                if action == #selector(pauseAutomation), currentState == .bypassed { item.state = .on }
                menu.addItem(item)
            }
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        request("toggle")
    }

    @objc private func showHelp() {
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/Help.html")
        NSWorkspace.shared.open(url)
    }
    @objc private func pauseAutomation() { request("pause") }
    @objc private func resumeAutomation() { request("resume") }
    @objc private func setupService() { request("setup") }
    @objc private func diagnose() { request("diagnose") }
    @objc private func repair() { request("repair") }
    @objc private func revealPermissionHelper() {
        if let runtime = permissionHelper() { NSWorkspace.shared.activateFileViewerSelecting([runtime]) }
    }
    private func permissionHelper() -> URL? {
        let runtime = bundledRuntimeURL()
        guard FileManager.default.isExecutableFile(atPath: runtime.path) else {
            let alert = NSAlert()
            alert.messageText = "Permission helper is missing"
            alert.informativeText = "Reinstall the complete Logic MCP.app, then try again."
            alert.runModal()
            return nil
        }
        return runtime
    }
    @objc private func copyClientConfiguration() {
        guard let runtime = permissionHelper() else { return }
        do {
            let text = try MCPClientConfiguration.text(runtimePath: runtime.path)
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        } catch {
            logError(error.localizedDescription)
        }
    }
    @objc private func openAccessibility() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }
    @objc private func removeService() {
        let alert = NSAlert()
        alert.messageText = "Remove Logic MCP service?"
        alert.informativeText = "Paused automation will be cancelled. The manual barrier is retained. Remove MCP client entries afterwards."
        alert.addButton(withTitle: "Remove service")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn { request("uninstall") }
    }
    @objc private func toggleOverlay() {
        UserDefaults.standard.set(!UserDefaults.standard.bool(forKey: "hideOverlay"), forKey: "hideOverlay")
        updateOverlayVisibility()
    }
    @objc private func quit() { NSApp.terminate(nil) }

    private func refresh() {
        perform("status")
    }

    private func request(_ command: String) {
        let requested = currentState.safeCommand(command)
        if busy { pendingCommand = requested }
        else { perform(requested) }
    }

    private func perform(_ requestedCommand: String) {
        guard !busy else { return }
        let command = currentState.safeCommand(requestedCommand)
        busy = true
        if command != "status" && command != "diagnose" {
            statusItem.button?.isEnabled = false
            overlayButton?.isEnabled = false
            statusItem.button?.title = "MCP …"
            overlayButton?.title = "MCP …"
        }
        let controller = self.controller
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let state = controller.run(command)
            DispatchQueue.main.async {
                guard let self else { return }
                self.apply(state)
                if case .message(let notice) = state {
                    if let observed = notice.state { self.apply(observed) }
                    let alert = NSAlert()
                    alert.messageText = "Logic MCP"
                    alert.informativeText = notice.message
                    alert.addButton(withTitle: "Close")
                    alert.addButton(withTitle: "Copy diagnostics")
                    if alert.runModal() == .alertSecondButtonReturn {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(notice.details, forType: .string)
                    }
                } else if case .unavailable(let detail) = state, command != "status" {
                    let alert = NSAlert()
                    alert.messageText = "Logic MCP needs attention"
                    alert.informativeText = detail
                    alert.runModal()
                }
                self.statusItem.button?.isEnabled = true
                self.overlayButton?.isEnabled = true
                self.busy = false
                self.updateOverlayVisibility()
                if let pending = self.pendingCommand {
                    self.pendingCommand = nil
                    self.perform(pending)
                } else if case .message = state {
                    self.perform("status")
                }
            }
        }
    }

    private func updateOverlayVisibility() {
        guard let panel = overlayPanel else { return }
        let logicIsFrontmost = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
            == "com.apple.logic10" || NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.apple.mobilelogic"
        guard !UserDefaults.standard.bool(forKey: "hideOverlay"), logicIsFrontmost, let screen = logicScreen() else {
            if panel.isVisible { panel.orderOut(nil) }
            return
        }
        let visible = screen.visibleFrame
        let origin = NSPoint(
                x: visible.maxX - panel.frame.width - 14,
                y: visible.maxY - panel.frame.height - 10
            )
        if panel.frame.origin != origin { panel.setFrameOrigin(origin) }
        if !panel.isVisible { panel.orderFrontRegardless() }
    }

    private func logicScreen() -> NSScreen? {
        guard let logic = NSWorkspace.shared.runningApplications.first(where: {
            $0.bundleIdentifier == "com.apple.logic10" || $0.bundleIdentifier == "com.apple.mobilelogic"
        }) else { return nil }
        let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let rawWindows = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
                as? [[String: Any]] else {
            return NSScreen.main ?? NSScreen.screens.first
        }
        let candidates = rawWindows.compactMap { row -> CGRect? in
            guard (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value
                    == logic.processIdentifier,
                  (row[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let bounds = row[kCGWindowBounds as String] as? NSDictionary else {
                return nil
            }
            return CGRect(dictionaryRepresentation: bounds as CFDictionary)
        }
        guard let logicBounds = candidates.max(by: {
            $0.width * $0.height < $1.width * $1.height
        }) else {
            return NSScreen.main ?? NSScreen.screens.first
        }
        return NSScreen.screens.max(by: { left, right in
            intersectionArea(logicBounds, displayBounds(left))
                < intersectionArea(logicBounds, displayBounds(right))
        })
    }

    private func displayBounds(_ screen: NSScreen) -> CGRect {
        guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")]
                as? NSNumber else { return .null }
        return CGDisplayBounds(CGDirectDisplayID(number.uint32Value))
    }

    private func intersectionArea(_ left: CGRect, _ right: CGRect) -> CGFloat {
        let intersection = left.intersection(right)
        return intersection.isNull ? 0 : intersection.width * intersection.height
    }

    private func apply(_ state: SurfaceState) {
        guard let button = statusItem.button else { return }
        if case .message = state { return }
        let changed = state != currentState
        currentState = state
        button.title = state.title
        button.toolTip = state.guidance
        switch state {
        case .active:
            overlayButton?.bezelColor = .systemOrange
        case .bypassed:
            overlayButton?.bezelColor = .systemGreen
        case .unavailable(let message):
            overlayButton?.bezelColor = .systemRed
            if changed { logError(message) }
        case .notReady:
            overlayButton?.bezelColor = .systemOrange
        case .message:
            break
        }
        overlayButton?.title = button.title
        overlayButton?.toolTip = button.toolTip
    }
}

@main
private enum LogicMCPMenuBarMain {
    @MainActor
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if let command = arguments.first {
            let commands = ["--status": "status", "--toggle": "toggle",
                            "--manual": "pause", "--automatic": "resume"]
            guard let action = commands[command] else {
                fputs("usage: LogicMCPMenuBar [--status|--toggle|--manual|--automatic]\n", stderr)
                exit(64)
            }
            let state = LogicSurfaceController().run(action)
            print(state.commandValue)
            if case .unavailable = state { exit(1) }
            exit(0)
        }

        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) {
            application.run()
        }
    }
}
