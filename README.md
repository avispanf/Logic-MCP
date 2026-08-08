# Logic-MCP

Two MCP servers for Logic Pro on macOS, built to sit alongside
[logic-pro-mcp](https://github.com/MongLong0214/logic-pro-mcp) rather than replace it.

| Server | Scope |
|---|---|
| `logic_plugins_mcp.py` | plugin parameter read and verified write, accessibility traversal, transport, metering |
| `logic_audio_mcp.py` | MIDI export parsing, BS.1770 loudness audit, naming conventions, project bundle inspection |

Both are single files with one dependency: `mcp`. `python-rtmidi` is optional and
affects one tool only.

## Why these exist

Logic Pro has no public API. `logic-pro-mcp` covers transport, tracks, mixer and
project lifecycle across seven native channels and does it well. Two gaps remain:

**Plugin parameters on third-party plugins.** Its verified apply-back accepts only
canonical `logic.stock.*` identifiers, so UAD, FabFilter and other Audio Units are out
of reach. `plugin_write_path` here addresses controls by an accessibility path and needs
no plugin identity, so it reaches any plugin whose editor is open in Controls view.

**Measurement of the result.** Loudness auditing against a target, MIDI arrangement
parsing and naming validation have no equivalent there.

## Install

```bash
python3 -m venv ~/dev/venv
~/dev/venv/bin/pip install mcp
~/dev/venv/bin/python logic_plugins_mcp.py --check
```

Register in the MCP client of your choice, pointing `command` at the venv interpreter
and `args` at the script. Both servers accept `--check` for a readiness report.

macOS permissions: Accessibility must be granted to the process that launches the
server, not to the script. Automation for Logic Pro and System Events is also required.

## Measured behaviour

Everything below was established against Logic Pro 12.3 on macOS 26.5, not assumed.

**`entire contents` is unusable.** AppleScript returns a list of references that cannot
be dereferenced inside a `tell process` block; `contents of` fails with -1700. Index
recursion with bulk per-container property queries works and is what the walker uses.

**Controls view exposes every Audio Unit.** Logic draws the parameter list itself, so
third-party plugins that render custom interfaces are fully readable and writable while
that view is active. Instrument editors are the exception: they expose thousands of
elements and are skipped by a size probe.

**Sliders do not accept absolute writes.** `set value` moves the control one step toward
the requested value per call regardless of the value passed. The writer attempts an
absolute set first, then runs a directed stepping loop that stops on reaching the target,
on stalling, or on moving away from it.

**Values follow the system number format.** A Spanish locale reports `21,0` where a
US locale reports `21.0`. Verification normalises both before comparing.

**Element names are frequently absent.** Plugin sliders often carry no name; the label
lives in a sibling static text. Controls are therefore addressed by dotted path, and
name-based lookup refuses ambiguous matches rather than guessing.

## Safety model

No write is reported as successful without an independent read-back. Writes default to
dry runs. Batch edits are split into a plan and an explicit confirmation. Key commands
come from an allowlist because keystrokes land wherever focus is. Every accessibility
call runs in its own process group and is killed with its group on timeout, so a hung
AppleScript cannot wedge the server.

`--check` includes a name-resolution pass over the server's own source. This exists
because a refactor once silently deleted six module-level definitions: the file still
compiled, imported and registered all tools, and only failed when a write was attempted.

## Limitations

Instrument editors cannot be read through accessibility. Plugin windows must be opened
manually; nothing here opens them. Insert order cannot be changed and plugins cannot be
removed. `ProjectData` is not parsed, so arrangement data comes from a MIDI export.

## Licence

MIT.
