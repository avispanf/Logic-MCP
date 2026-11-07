# Logic MCP 0.2.0 beta 1

Public beta for Apple Silicon. Not a stable release or a guarantee of complete
automatic mixing/mastering.

## Included

- Menu-bar and optional floating manual-work switch, explicit OFF/ON actions and diagnostics.
- One shared loopback MIDI/MCU backend across MCP clients; OFF rejects new automation.
- Bundled runtime, actual-path client configuration and permission-helper discovery.
- First-run Install &amp; connect: user-folder installation, shared service and automatic
  Codex/Claude Desktop configuration with backups; existing apps are not overwritten.
- Claude STDIO core relay uses the same shared backend without creating another MCU engine.
- Controls-view plugin inspection and identity-bound, independently verified supported edits.
- Track/mixer audit tools and separate audio-file loudness analysis.
- Retained Python/native dependency notices and source/build provenance.

## Install

Download the Apple Silicon DMG or ZIP, open Logic MCP.app and choose Install &amp; connect.
See [installation](https://github.com/avispanf/Logic-MCP/blob/main/docs/INSTALL.md)
for required Accessibility/Automation approvals, MCU setup and client configuration.
End users do not need Python or Homebrew for plugin control. File analysis needs
ffmpeg/ffprobe. Checksums are in SHA256SUMS.

## Important limits

Current runtime evidence is from macOS 26.6 / Logic Pro 13.0 on Apple Silicon;
earlier work used Logic 12.3. The app targets macOS 14+, not Intel.
Consult the candidate acceptance record for minimum-OS and clean-Mac testing.
Some plugin parameters, instrument UIs, group/bus relationships and live analyzer meters
remain unsupported or unverified. Native insertion on Logic 13 can expose generic labels
and is refused/rolled back when identity cannot be verified. Idle MCU feedback can go stale
without port churn. No full real-song mix, mastering or streaming loudness qualification
is claimed. Use a copy of your project and verify readbacks.

After interrupted automation, inspect solo/mute/transport and plugin state before
continuing. The OFF switch does not undo writes or repair a mix automatically.
