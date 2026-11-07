# Security and privacy

Report security issues privately through
[GitHub private vulnerability reporting](https://github.com/avispanf/Logic-MCP/security/advisories/new).
Do not publish credentials, private projects or exploitable details in a normal issue.
Only the latest beta is maintained; no response-time or comprehensive-security guarantee is offered.

Logic MCP runs with your macOS user permissions and can inspect/control Logic once you
approve Accessibility/Automation. Treat your MCP client and agents as trusted software.
The shared backend binds to `127.0.0.1`, not a public network interface. Do not proxy
or expose port 8765 to a LAN or the internet. Loopback is not authentication against
other untrusted programs running on your Mac. The operation lease prevents conflicting
operations; it is not a replacement for OS user isolation.

The project has no built-in analytics or project-upload service. Tool responses can
contain project names, track names, plugin settings and local file paths. Your MCP
client/AI provider may transmit or retain these responses under its own policy.
Local logs, support bundles and audit journals may contain that metadata. Review them
before sharing and keep real projects out of bug reports. The native backend's opt-in
doctor update lookup contacts GitHub for upstream release information; it does not
upload projects. Notarization tools contact
Apple only during maintainer builds; dependencies are obtained during development builds.

Manual mode rejects new work; it does not undo previous writes, save a project or
guarantee restoration after interruption. Use project backups. Never disable Gatekeeper,
export signing keys into an issue, reset unrelated Accessibility grants or execute
commands copied from untrusted issue attachments.
