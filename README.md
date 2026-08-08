# Logic-MCP

Two MCP servers for Logic Pro on macOS, built to sit alongside
[logic-pro-mcp](https://github.com/MongLong0214/logic-pro-mcp) rather than replace it.

| Server | Scope |
|---|---|
| `logic_plugins_mcp.py` | plugin parameters, mixer survey, transport, metering, menu navigation, control surfaces |
| `logic_audio_mcp.py` | MIDI export parsing, BS.1770 loudness audit, naming conventions, project bundle inspection |

`logic_plugins_mcp.py` groups its tools roughly as follows.

**Plugins.** `plugin_snapshot` reads an open editor as a parameter table with an exact path
for every control. `plugins_sweep` does the same across every open editor.
`plugin_write_path` writes one control and verifies it. `plugin_plan` and `plugin_apply`
batch edits behind an explicit confirmation. `au_list` and `au_parameters` read parameter
dictionaries straight from the Audio Unit, so a parameter index is never guessed.

**Mixer.** `mixer_strips` indexes the channel strips; `mixer_survey` reads name, fader level
in decibels, pan, mute, solo, clipping state, output bus, sends and insert chain for each.
`mixer_locate` finds the Mixer pane in the accessibility tree, because element indices move
between sessions.

**Transport and metering.** `control_bar` reports playback state, tempo, signature, key and
playhead position. `transport_press` presses real Control Bar buttons and reports what
changed. `meter_watch` polls meter values during playback.

**Application.** `menu_list` and `menu_click` navigate Logic's menus, with destructive items
refused unless explicitly allowed. `surfaces_bypass` reads and sets Bypass All Control
Surfaces, which silences an attached control surface without disturbing its port
assignment.

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

**Paths are not stable across sessions.** Element indices shift when Logic's layout changes
or the application restarts, and a stale path resolves to nothing or, worse, to the wrong
element. The mixer tools locate the Mixer pane by searching for it rather than assuming a
path. Plugin window indices carry the same hazard and are not yet handled the same way.

**Transport is two buttons, not one toggle.** Logic replaces the Go to Beginning button with
a Stop button during playback, so pressing Play a second time does not stop transport.

**Attaching a control surface changes window behaviour.** With Open/Close Plug-in Window and
Follows Focused View enabled, Logic opens and closes plugin editors on its own, which
invalidates window indices held from earlier in a session.

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

Instrument editors cannot be read through accessibility: they expose thousands of elements
and overrun any sensible budget. Plugin windows must be opened manually; `ax_press` can
press an insert slot, but nothing here drives that automatically. Insert order cannot be
changed and plugins cannot be removed. `ProjectData` is not parsed, so arrangement data
comes from a MIDI export.

Known rough edges, stated plainly because they were measured rather than assumed: the size
probe in `plugins_sweep` counts only the top level, so heavy editors are still excluded by
timeout rather than by size; `au_list` has not completed a run on a large plugin collection;
`strip_settings_inspect` has never been given a real channel strip file; and `keys_fire` in
the audio server needs `python-rtmidi`, which is optional and usually absent.

## Licence

MIT.
