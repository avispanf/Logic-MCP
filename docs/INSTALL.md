# Install and use Logic MCP

Logic MCP is an independent open-source project, not an Apple product.
It needs Logic Pro and an MCP-capable AI client. Start on a copy of a project.

## Download

Use [GitHub Releases](https://github.com/avispanf/Logic-MCP/releases) when a verified
public beta is available. A development Actions artifact is not a signed public release.
Choose the Apple Silicon ZIP or DMG. Intel Macs are not supported by these packages.
The build targets macOS 14+, but current live validation is macOS 26.6 / Logic Pro 13.0;
see the validation records for exact coverage and outstanding compatibility checks.

1. Open the DMG and drag **Logic MCP.app** to Applications, or unzip and move the app
   to Applications. Your user Applications folder also works. Do not run it inside the DMG.
2. Open the app. Right-click its top menu-bar button → **Install / repair service**.
3. Choose **Reveal permission helper**. Finder selects the actual installed executable.
   Choose **Accessibility settings**, unlock the pane yourself and add that executable
   using **+**. The helper is `Logic MCP.app/Contents/Resources/runtime/logic-mcp`;
   granting only the menu app or `bin/LogicProMCP` is not enough.
4. Choose **Enable NEW automation**, open Logic and a disposable project, and approve
   macOS Automation prompts for Logic Pro and System Events. Open **Diagnostics**.
5. In Logic's Control Surfaces Setup, create/use one Logic Control/MCU-compatible
   surface. Set both input and output to `LogicProMCP-MCU-Internal [bridge]`.
   Preserve any unrelated hardware controllers. Do not create a new surface on every launch.
6. Choose **Copy MCP client configuration**. Paste it into your client's MCP configuration
   and reload the client. It uses the actual installed path. The example is TOML; clients
   using JSON need the equivalent URL/command/args fields. Core requires Streamable HTTP.

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
