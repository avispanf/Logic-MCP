import Foundation

enum LogicMCPInstall {
    static let identifier = "com.avispanf.logic-mcp-menubar"

    static func installedLocation(_ bundle: URL, home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Bool {
        let path = bundle.standardizedFileURL.path
        return ["/Applications/", home.appendingPathComponent("Applications").path + "/"].contains { path.hasPrefix($0) }
    }

    static func serviceConfigured(_ bundle: URL, home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Bool {
        let file = home.appendingPathComponent("Library/LaunchAgents/com.avispanf.logic-pro-mcp-bridge.plist")
        guard let data = try? Data(contentsOf: file),
              let value = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else { return false }
        return value["Label"] as? String == "com.avispanf.logic-pro-mcp-bridge" &&
            value["ProgramArguments"] as? [String] == [bundle.appendingPathComponent("Contents/Resources/runtime/logic-mcp").path, "--service"]
    }

    static func installCopy(_ source: URL, home: URL = FileManager.default.homeDirectoryForCurrentUser,
                            verify: (URL) throws -> Void = verifySignature) throws -> URL {
        let files = FileManager.default
        let target = home.appendingPathComponent("Applications/Logic MCP.app")
        guard !files.fileExists(atPath: target.path) else {
            throw NSError(domain: "LogicMCPInstall", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "Logic MCP already exists at \(target.path). It was not overwritten. Open that app to connect clients, or replace it through Finder after switching MCP OFF."])
        }
        let infoURL = source.appendingPathComponent("Contents/Info.plist")
        guard let data = try? Data(contentsOf: infoURL),
              let info = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              info["CFBundleIdentifier"] as? String == identifier else {
            throw NSError(domain: "LogicMCPInstall", code: 2, userInfo: [NSLocalizedDescriptionKey: "Unexpected application bundle; installation stopped."])
        }
        for relative in ["Contents/MacOS/LogicMCPMenuBar", "Contents/Resources/runtime/logic-mcp", "Contents/Resources/bin/LogicProMCP"] {
            guard files.isExecutableFile(atPath: source.appendingPathComponent(relative).path) else {
                throw NSError(domain: "LogicMCPInstall", code: 3, userInfo: [NSLocalizedDescriptionKey: "The application is incomplete. Download the full package."])
            }
        }
        try verify(source)
        let applications = target.deletingLastPathComponent()
        try files.createDirectory(at: applications, withIntermediateDirectories: true)
        let staging = applications.appendingPathComponent(".LogicMCP-Install-" + UUID().uuidString)
        try files.createDirectory(at: staging, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: staging) }
        let copied = staging.appendingPathComponent("Logic MCP.app")
        try files.copyItem(at: source, to: copied)
        try verify(copied)
        try files.moveItem(at: copied, to: target)
        return target
    }

    static func verifySignature(_ bundle: URL) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        process.arguments = ["--verify", "--deep", "--strict", bundle.path]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let done = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in done.signal() }
        try process.run()
        guard done.wait(timeout: .now() + 15) == .success else {
            process.terminate()
            throw NSError(domain: "LogicMCPInstall", code: 4, userInfo: [NSLocalizedDescriptionKey: "Signature verification timed out. Installation stopped."])
        }
        guard process.terminationStatus == 0 else {
            throw NSError(domain: "LogicMCPInstall", code: 5, userInfo: [NSLocalizedDescriptionKey: "The application signature is invalid. Download a fresh package from the project repository."])
        }
    }
}
