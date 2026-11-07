# Install and use Logic MCP

Logic MCP is an independent open-source project, not an Apple product.
It needs Logic Pro and an MCP-capable AI client. Start on a copy of a project.

## Download

Use [GitHub Releases](https://github.com/avispanf/Logic-MCP/releases) when a verified
public beta is available. Developer previews can be published without Apple signing;
they are explicitly marked unsigned/not notarized. This is not a Gatekeeper-approved release.
Choose the Apple Silicon ZIP or DMG. Intel Macs are not supported by these packages.
The build targets macOS 14+, but current live validation is macOS 26.6 / Logic Pro 13.0;
see the validation records for exact coverage and outstanding compatibility checks.

1. Open the DMG or unzip the download, then open **Logic MCP.app**. If macOS blocks an
   unsigned preview, use [Apple's documented approval](https://support.apple.com/en-us/102445)
   only if you trust the source and checked the download. Do not disable Gatekeeper globally.
2. Click **Install & connect**. On a fresh install the app copies itself to
   `~/Applications/Logic MCP.app`, installs the service and configures Codex and Claude
   Desktop with backups of existing files. An existing app is not overwritten: switch
   OFF and replace it through Finder for an update. The same action is available in
   the right-click menu. It never closes your clients or modifies Logic project settings.
3. Choose **Reveal permission helper**. Finder selects the actual installed executable.
   Choose **Accessibility settings**, unlock the pane yourself and add that executable
   using **+**. The helper is `Logic MCP.app/Contents/Resources/runtime/logic-mcp`;
   granting only the menu app or `bin/LogicProMCP` is not enough.
4. Choose **Enable NEW automation**, open Logic and a disposable project, and approve
   macOS Automation prompts for Logic Pro and System Events. Open **Diagnostics**.
5. In Logic's Control Surfaces Setup, create/use one Logic Control/MCU-compatible
   surface. Set both input and output to `LogicProMCP-MCU-Internal [bridge]`.
   Preserve any unrelated hardware controllers. Do not create a new surface on every launch.
6. Restart Codex and Claude Desktop when convenient. Their three MCP entries are already
   configured. Codex uses HTTP for core; Claude's local STDIO relay connects to that same
   service without starting another backend. On first use, enable the backend before
   reloading the client so its tool cache is populated. Manual configuration remains
   available through **Copy MCP client configuration** (TOML) or the runtime's
   `--client-config codex` / `--client-config claude` options.

Default client files are `~/.codex/config.toml` and
`~/Library/Application Support/Claude/claude_desktop_config.json`. A supplied `CODEX_HOME`
is respected. Setup validates both files before writing, backs up existing contents
with private file permissions, preserves other MCP entries and client settings, and
refuses malformed configurations or symlink targets. It does not bypass macOS permissions
or force-enable an existing client entry that you explicitly disabled.

No Python, Homebrew or compiler is needed for the app, switch or plugin control.
Audio-file analysis additionally needs ffmpeg/ffprobe; it is not live meter analysis.
An MCP client may send selected tool results to its AI provider; review its privacy settings.

## Manual work

Click the top/floating button or choose **Manual mode — MCP OFF** before editing by hand.
Verify **MANUAL · MCP OFF**. Automation is rejected and the MCU ports disappear.
They return with stable identities when you enable new automation. Hiding the floating
button does not disable automation. Quitting the menu app is not the OFF switch.

Pausing may interrupt restoration of solo/mute/transport/plugin state. Inspect an
interrupted audit's journal and project settings before starting another task.
Do not assume the mode button undoes a mix or saves the project.

`MCP · MCU check` means feedback is old, possibly because Logic is stopped. It does
not itself prove ports changed or that a write succeeded. Diagnostics explain missing
permissions, unavailable services and MCU setup. Unsupported or unreadable plugin
controls must not be guessed. Every supported write still needs independent readback.

## Update or remove

Switch OFF first and verify it. Replace the app in the same location, open it and use
**Install / repair service**. Keep a backup until verification succeeds. Ad hoc development
builds can invalidate Accessibility grants; re-add only the installed helper if needed.
The first Developer ID update needs its own permission test.

To uninstall, choose **Remove service**, remove your MCP client entries, quit the app
and move it to Trash. The manual barrier and audit journals are intentionally retained
to prevent remaining clients from resuming old work. Do not reset global privacy settings.

## Get help

Use the [bug report form](https://github.com/avispanf/Logic-MCP/issues/new/choose).
Include versions, build/source commit, mode, expected behavior and a minimal reproduction
on a disposable project. Review and redact diagnostics first: project/track names and
local paths may be sensitive. Do not attach passwords, signing keys or a real song project.
