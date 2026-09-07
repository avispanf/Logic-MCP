import AppKit
import CoreGraphics
import Foundation

private func logError(_ message: String) {
    let line = "[LogicMCPMenuBar] \(message)\n"
    FileHandle.standardError.write(Data(line.utf8))
}

private enum SurfaceState: Equatable {
    case active
    case bypassed
    case unavailable(String)

    var commandValue: String {
        switch self {
        case .active: return "automatic"
        case .bypassed: return "manual"
        case .unavailable(let message): return "unavailable:\(message)"
        }
    }
}

private final class LogicSurfaceController: @unchecked Sendable {
    func run(_ command: String) -> SurfaceState {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let python = Bundle.main.object(forInfoDictionaryKey: "MCPPythonPath") as? String
            ?? home + "/dev/venv/bin/python"
        let helper = Bundle.main.object(forInfoDictionaryKey: "MCPHelperPath") as? String
            ?? home + "/dev/Logic-MCP/logic_manual_mode.py"
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [helper, command]
        process.standardOutput = pipe
        process.standardError = FileHandle.standardError
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        do {
            try process.run()
            guard finished.wait(timeout: .now() + 15) == .success else {
                process.terminate()
                return .unavailable("Pause helper timed out; state needs verification")
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  result["verified"] as? Bool == true, process.terminationStatus == 0 else {
                return .unavailable(String(data: data, encoding: .utf8) ?? "Helper failed")
            }
            switch result["mode"] as? String {
            case "manual": return .bypassed
            case "automatic": return .active
            default: return .unavailable("Unknown helper state")
            }
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
    private var pendingToggle = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let button = statusItem.button else { return }
        button.title = "MCP/MCU …"
        button.toolTip = "Pause or resume Logic MCP and MCU"
        button.target = self
        button.action = #selector(toggle)
        button.sendAction(on: [.leftMouseUp])
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
        if busy {
            pendingToggle = true
            return
        }
        perform("toggle")
    }

    private func refresh() {
        updateOverlayVisibility()
        perform("status")
    }

    private func perform(_ command: String) {
        guard !busy else { return }
        busy = true
        if command != "status" {
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
                self.statusItem.button?.isEnabled = true
                self.overlayButton?.isEnabled = true
                self.busy = false
                self.updateOverlayVisibility()
                if self.pendingToggle {
                    self.pendingToggle = false
                    self.perform("toggle")
                }
            }
        }
    }

    private func updateOverlayVisibility() {
        guard let panel = overlayPanel else { return }
        let logicIsFrontmost = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
            == "com.apple.logic10" || NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.apple.mobilelogic"
        guard logicIsFrontmost, let screen = logicScreen() else {
            panel.orderOut(nil)
            return
        }
        let visible = screen.visibleFrame
        panel.setFrameOrigin(
            NSPoint(
                x: visible.maxX - panel.frame.width - 14,
                y: visible.maxY - panel.frame.height - 10
            )
        )
        panel.orderFrontRegardless()
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
        switch state {
        case .active:
            button.title = "MCP ON"
            button.toolTip = "Automation enabled. Click to pause all Logic MCP workers for manual editing."
            overlayButton?.bezelColor = .systemOrange
        case .bypassed:
            button.title = "MANUAL · MCP OFF"
            button.toolTip = "Logic MCP/MCU workers are paused. Click to resume automation and any paused jobs."
            overlayButton?.bezelColor = .systemGreen
        case .unavailable(let message):
            button.title = "MCP CHECK"
            button.toolTip = "Manual mode could not be verified: \(message)"
            overlayButton?.bezelColor = .systemRed
            logError(message)
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
