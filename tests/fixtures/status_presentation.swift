import Foundation

@main
enum StatusPresentationTests {
    static func main() throws {
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--configuration" {
            print(try MCPClientConfiguration.text(runtimePath: CommandLine.arguments[2]))
            return
        }
        var count = 0
        func check(_ condition: Bool, _ label: String) {
            precondition(condition, label)
            count += 1
        }
        func decode(_ value: [String: Any], exit: Int32 = 0, command: String = "status") throws -> SurfaceState {
            SurfaceState.decode(try JSONSerialization.data(withJSONObject: value), exitStatus: exit, command: command)
        }
        let manual: [String: Any] = ["mode": "manual", "verified": true, "active": [], "unknown": [], "errors": []]
        let automatic: [String: Any] = ["mode": "automatic", "verified": true, "ready": true, "service_state": "ready", "errors": []]
        check(try decode(manual) == .bypassed, "Verified manual state")
        check(try decode(automatic) == .active, "Verified ready state")
        check(SurfaceState.bypassed.title == "MANUAL · MCP OFF", "Manual label remains explicit")
        check(SurfaceState.active.commandValue == "automatic", "CLI automatic value")
        check(SurfaceState.unavailable("timeout").safeCommand("toggle") == "pause", "Unverified toggle requests manual mode")
        check(SurfaceState.bypassed.safeCommand("toggle") == "toggle", "Verified manual toggle remains explicit")
        check(SurfaceState.unavailable("timeout").safeCommand("resume") == "resume", "Explicit new automation intent is retained")
        check(SurfaceState.notReady("checking").safeCommand("toggle") == "toggle", "Known automatic mode still pauses normally")
        for reason in ["offline", "logic-closed", "permissions-required", "mcu-setup-required", "mcu-feedback-required", "mcu-feedback-stale", "checking", "future-state"] {
            var value = automatic
            value["ready"] = false
            value["service_state"] = reason
            let state = try decode(value)
            check(state == .notReady(reason), "Readiness state retained: " + reason)
            check(state.title.count <= 20, "Short status label: " + reason)
            check(!state.guidance.isEmpty, "Actionable guidance: " + reason)
        }
        let stale = SurfaceState.notReady("mcu-feedback-stale")
        check(stale.title == "MCP · MCU check", "Stale feedback is not a technical code")
        check(stale.guidance.contains("Readiness is unverified"), "Stale feedback never promotes readiness")
        check(stale.guidance.contains("can happen while stopped"), "Idle telemetry explanation is qualified")
        for field in ["verified", "ready"] {
            var value = automatic
            value[field] = 1
            check(try decode(value) != .active, "Numeric flag is not a Boolean: " + field)
        }
        for (field, value) in [("verified", false as Any), ("mode", "unknown" as Any), ("service_state", "offline" as Any)] {
            var response = automatic
            response[field] = value
            check(try decode(response) != .active, "Contradictory status fails closed: " + field)
        }
        for field in ["active", "unknown"] {
            var value = manual
            value[field] = [123]
            check(try decode(value) != .bypassed, "Active or unknown worker prevents a green manual state")
            value.removeValue(forKey: field)
            check(try decode(value) != .bypassed, "Missing worker inventory is unverified")
        }
        check(try decode(manual, exit: 1) != .bypassed, "Nonzero helper exit fails closed")
        check(SurfaceState.decode(Data("not JSON".utf8), exitStatus: 0, command: "status").title == "MCP CHECK", "Invalid JSON fails closed")
        if case .message(let notice) = try decode(manual, command: "diagnose") {
            check(notice.message.contains("Manual — MCP OFF"), "Human diagnostics mode")
            check(notice.state == .bypassed, "Diagnostics retain observed state")
            let raw = try JSONSerialization.jsonObject(with: Data(notice.details.utf8)) as? [String: Any]
            check(raw?["mode"] as? String == "manual", "Copy details retains original JSON")
        } else { preconditionFailure("Diagnostics did not produce a notice") }
        var broken = automatic
        broken["verified"] = false
        broken["errors"] = ["Cannot identify workers"]
        if case .message(let notice) = try decode(broken, exit: 1, command: "diagnose") {
            check(notice.message.contains("Mode: Unverified"), "Failed diagnostics do not claim automation allowed")
            check(notice.message.contains("Cannot identify workers"), "Diagnostics retain error cause")
        } else { preconditionFailure("Failed diagnostics did not produce a notice") }
        let path = "/Users/test/Audio \"Work\"/Logic MCP.app/Contents/Resources/runtime/logic-mcp"
        let config = try MCPClientConfiguration.text(runtimePath: path)
        check(config.contains("[mcp_servers.logic-pro]"), "Core config included")
        check(config.contains("args = [\"--plugins\"]"), "Plugin connection included")
        check(config.contains("args = [\"--audio\"]"), "Audio connection included")
        let command = try requireCommand(config)
        check(command == path, "Installation paths with spaces and quotes are escaped losslessly")
        print("Status presentation: \(count) checks passed")
    }

    static func requireCommand(_ config: String) throws -> String {
        let line = config.split(separator: "\n").first { $0.hasPrefix("command = ") }!
        let literal = String(line.dropFirst("command = ".count))
        return try JSONSerialization.jsonObject(with: Data(literal.utf8), options: .fragmentsAllowed) as! String
    }
}
