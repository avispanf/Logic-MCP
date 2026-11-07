# Build 10: guided install and two-client setup

Packaged code: `41cfb1af6540a80f086cbfec100ff6d7b85b4726`, version 0.2.0 beta,
build 10. Local package: Apple Silicon, Python 3.14.6, macOS 26.6.
This is an explicitly unsigned/not-notarized developer preview, not a stable release.

Published as [v0.2.0-beta.1](https://github.com/avispanf/Logic-MCP/releases/tag/v0.2.0-beta.1).
The release is a public prerelease, not latest stable; its tag points to the packaged
code commit above. ZIP, DMG and SHA256SUMS were downloaded back from GitHub and their
hashes matched the locally verified candidate before publication.

## Verified

- 213 tests passed locally. The Foundation status/installer fixture performed 64 checks.
- [CI](https://github.com/avispanf/Logic-MCP/actions/runs/42421163902) passed on Python
  3.12 and 3.14, including native compilation for the declared macOS 14 target.
- Both Codex TOML and Claude Desktop JSON configurations were checked, including paths
  containing spaces, quotes and Unicode, unrelated settings/servers, disabled tools,
  idempotence, backups, private permissions, malformed files, symlinks and rollback.
- A protocol-only integration test sent a real STDIO request through the HTTP relay
  to one fixture backend. Shared-owner metadata and explicit manual-mode rejection passed.
  It used no MIDI or live Logic connection.
- Packaged smoke passed: 77 custom tools, both frozen client-configuration exporters,
  the frozen STDIO core relay, HTTP manual health and rejection of a manual-mode write.
- The actual complete ad hoc signed app was copied by the native installer into an
  isolated temporary user Applications directory. Source, staged and installed signatures
  verified. An existing installation is never silently overwritten.
- Final ZIP/DMG were independently extracted/mounted read-only. Provenance, binary hashes,
  notices, deployment targets, app signatures and matching contents passed verification.
- The first-run Install & connect / Cancel window was launched and visually inspected.
  It explains the two clients, backups, OFF mode and permission/restart requirements.

Archive SHA-256:

```text
b568d1884b5ac5703c3c5e32f8673919a7e51cb620cc04ab9bd67c149e09b949  Logic-MCP-macos-arm64.zip
abcb3fab71c71232ec4b265d5aa77c5be689ca5ea35458644fbf1f92b77b7567  Logic-MCP-macos-arm64.dmg
```

## What is automated

On a fresh install, Install & connect copies the app to the user's Applications folder,
puts MCP in verified manual mode, configures only the three Logic MCP entries in Codex
and Claude Desktop and installs the shared service. Other client settings and tool
restrictions are retained. The core relay forwards to the same HTTP service; it does
not launch another native backend. No AI client is forcibly closed or restarted.

## Not claimed

- No Developer ID, real notarization, stapled ticket or Gatekeeper acceptance. Users of
  this preview may need Apple's explicit per-app Open Anyway approval. No global
  Gatekeeper disabling or quarantine stripping is performed.
- Accessibility/Automation approvals remain manual. MCU assignment in Logic is not
  silently changed; use the setup instructions if existing/automatic detection does not work.
- Full fresh-account installation, permission approval, login/update and client restart
  were not exercised end-to-end. The existing working build 8 and real client config
  files were not replaced during this pass.
- Automated Cancel clicking was not confirmed: the system screenshot interface was
  above the alert. The guarded test refused to send a click to that other process.
  The user-owned screenshot interface was not closed by the test.
- Minimum-macOS runtime and clean-Mac browser-download/quarantine tests remain unrun.
  Previous live Logic checks are documented in the build 7/8 records; this packaging
  pass is not a new all-plugins, all-groups or real-song mixing/mastering qualification.
