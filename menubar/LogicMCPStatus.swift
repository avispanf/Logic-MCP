import CoreFoundation
import Foundation

struct SurfaceNotice: Equatable {
    let message: String
    let details: String
    let state: SurfaceState?
}

enum SurfaceState: Equatable {
    case active
    case bypassed
    case unavailable(String)
    case notReady(String)
    indirect case message(SurfaceNotice)

    var commandValue: String {
        switch self {
        case .active: return "automatic"
        case .bypassed: return "manual"
        case .unavailable(let message): return "unavailable:\(message)"
        case .notReady(let reason): return "not-ready:\(reason)"
        case .message(let notice): return notice.details
        }
    }

    var title: String {
        switch self {
        case .active: return "MCP ON"
        case .bypassed: return "MANUAL · MCP OFF"
        case .unavailable: return "MCP CHECK"
        case .notReady(let reason): return Self.readiness(reason).title
        case .message: return "Logic MCP"
        }
    }

    var guidance: String {
        switch self {
        case .active:
            return "Automation is enabled. Click to pause MCP/MCU for manual editing."
        case .bypassed:
            return "Manual mode is verified. Click to cancel old jobs and allow NEW automation requests."
        case .unavailable(let message):
            return "The mode could not be verified. \(message) Click to request manual mode. Open Diagnostics before continuing."
        case .notReady(let reason):
            return Self.readiness(reason).detail + " Click to pause. Right-click for Diagnostics."
        case .message(let notice): return notice.message
        }
    }

    func safeCommand(_ requested: String) -> String {
        if requested == "toggle", case .unavailable = self { return "pause" }
        return requested
    }

    static func readiness(_ reason: String) -> (title: String, detail: String) {
        switch reason {
        case "offline":
            return ("MCP · offline", "Automation is allowed, but the shared service is offline. Use Install / repair service.")
        case "logic-closed":
            return ("MCP · open Logic", "Automation is allowed. Open Logic Pro and a project, then check Diagnostics.")
        case "permissions-required":
            return ("MCP · access", "macOS permission is missing. Reveal the permission helper and open Accessibility settings.")
        case "mcu-setup-required":
            return ("MCP · setup MCU", "In Logic Control Surfaces Setup, assign both ports to LogicProMCP-MCU-Internal [bridge].")
        case "mcu-feedback-required":
            return ("MCP · no MCU", "MCU feedback is missing. Check both port assignments and whether control surfaces are bypassed in Logic.")
        case "mcu-feedback-stale":
            return ("MCP · MCU check", "MCU feedback is old. This can happen while stopped; it does not by itself mean the ports changed. Readiness is unverified.")
        case "checking":
            return ("MCP · checking", "The service is checking permissions and MCU feedback. Readiness is not yet verified.")
        default:
            return ("MCP · check", "The service reported an unrecognized state. Readiness is unverified.")
        }
    }

    private static func flag(_ result: [String: Any], _ key: String) -> Bool? {
        guard let number = result[key] as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    private static func verifiedState(_ result: [String: Any], exitStatus: Int32) -> SurfaceState {
        guard flag(result, "verified") == true, exitStatus == 0 else {
            let errors = result["errors"] as? [String] ?? []
            return .unavailable(errors.isEmpty ? "The helper did not confirm the mode." : errors.joined(separator: "\n"))
        }
        if let errors = result["errors"] {
            guard let entries = errors as? [String], entries.isEmpty else {
                return .unavailable("The helper reported errors or malformed error details.")
            }
        }
        switch result["mode"] as? String {
        case "manual":
            for key in ["active", "unknown"] {
                guard let workers = result[key] as? [Any], workers.isEmpty else {
                    return .unavailable("Manual mode has active, unidentified or unreported workers.")
                }
            }
            return .bypassed
        case "automatic":
            let reason = result["service_state"] as? String ?? "checking"
            if flag(result, "ready") == true, reason == "ready" { return .active }
            return .notReady(reason == "ready" ? "checking" : reason)
        default:
            return .unavailable("The helper reported an unknown mode.")
        }
    }

    static func decode(_ data: Data, exitStatus: Int32, command: String) -> SurfaceState {
        guard let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .unavailable("The runtime returned invalid diagnostics. Check the service logs or reinstall the app.")
        }
        let state = verifiedState(result, exitStatus: exitStatus)
        let details = (try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "Diagnostics could not be formatted."
        if command == "diagnose" {
            let mode: String
            switch state {
            case .bypassed: mode = "Manual — MCP OFF"
            case .active, .notReady: mode = "Automation allowed"
            default: mode = "Unverified"
            }
            return .message(SurfaceNotice(message: "Mode: \(mode)\n\n\(state.guidance)\n\nCopy diagnostics includes the detailed helper response.", details: details, state: state))
        }
        if ["setup", "uninstall", "repair"].contains(command), flag(result, "verified") == true, exitStatus == 0 {
            return .message(SurfaceNotice(message: result["message"] as? String ?? state.guidance, details: details, state: nil))
        }
        return state
    }
}

enum MCPClientConfiguration {
    static func text(runtimePath: String) throws -> String {
        let encoded = try JSONSerialization.data(withJSONObject: runtimePath, options: [.fragmentsAllowed, .withoutEscapingSlashes])
        guard let quoted = String(data: encoded, encoding: .utf8) else {
            throw CocoaError(.fileReadInapplicableStringEncoding)
        }
        return """
        [mcp_servers.logic-pro]
        url = "http://127.0.0.1:8765/mcp"
        tool_timeout_sec = 180

        [mcp_servers.logic-plugins]
        command = \(quoted)
        args = ["--plugins"]

        [mcp_servers.logic-audio]
        command = \(quoted)
        args = ["--audio"]
        """
    }
}
