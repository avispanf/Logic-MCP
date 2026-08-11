# Logic-MCP

Two MCP servers for Logic Pro on macOS, built to sit alongside
[logic-pro-mcp](https://github.com/MongLong0214/logic-pro-mcp) rather than replace it.

| Server | Scope |
|---|---|
| `logic_plugins_mcp.py` | plugin parameters, mixer survey, transport, metering, menu navigation, control surfaces |
| `logic_audio_mcp.py` | MIDI export parsing, BS.1770 loudness audit, naming conventions, project bundle inspection |

`logic_plugins_mcp.py` groups its tools roughly as follows.

**Plugins.** `plugin_snapshot` identifies an open editor without descending into a heavy
custom interface. `plugin_set_view` switches it to Logic's generic Controls view, and
`plugin_parameters` reads even large parameter tables row by row with filtering and
pagination. `plugins_sweep` handles all open editors. `plugin_write_path` writes one
control and verifies it; checkboxes are pressed rather than assigned. `plugin_plan` and
`plugin_apply` batch edits behind an explicit confirmation. `au_list` and `au_parameters`
read parameter dictionaries straight from the Audio Unit, so a parameter index is never
guessed.

**Mixer.** `mixer_strips` indexes the channel strips; `mixer_survey` reads name, fader level
in decibels, pan, mute, solo, clipping state, output bus, sends and insert chain for each.
Insert and send lists are reversed from AX order into actual signal-flow order.
`main_window` and `mixer_locate` find the Tracks window and Mixer pane in the accessibility
tree, because opening an editor changes every window index.

**Transport and metering.** `control_bar` reports playback state, tempo, signature, key and
playhead position. `transport_press` presses real Control Bar buttons and reports what
changed. `meter_watch` polls meter values during playback.

**Application.** `menu_list` and `menu_click` navigate Logic's menus, with destructive items
refused unless explicitly allowed. `surfaces_bypass` reads and sets Bypass All Control
Surfaces, which silences an attached control surface without disturbing its port
assignment. `ax_show_menu` exposes context-menu capabilities without clicking them, and
`close_plugin_windows` clears editor dialogs without closing the project window.

**Mix audit orchestration.** `mix_inventory` merges the mature server's `logic://tracks`
and `logic://mixer` resources with `mixer_survey`, preserving stable track references and
AX-only Aux/Bus paths. `mix_audit_plan` accepts `track`, `group`, `aux`, `bus`, `master` or
`all`, then emits an ordered capture → inspect every insert → isolate → bounce/meter →
BS.1770 → restore plan. `mix_audit_start`, `mix_audit_advance`, `mix_audit_status` and
`mix_audit_cancel` enforce ordering and keep restoration steps mandatory. They return
cross-server dispatches to the MCP client instead of launching a second LogicProMCP
process, so one request can use the three registered servers without duplicate MCU ports.

Every mature-server dispatch uses the actual v3.13 wire shape: the MCP tool is
`logic_tracks`, `logic_transport`, `logic_project` or `logic_plugins`, while the selected
action and its arguments are nested under `command` and `params`. Audit and fix runs first
verify the exact open `.logicx` bundle locally, then run the mature server's read-only
project audit. This prevents a syntactically valid plan from being applied to another
frontmost project.
`health` and `--check` compare the installed `LogicProMCP --version` with the pinned
dispatch contract version and warn instead of silently assuming a future release is wire
compatible.

`mix_audit_review` turns per-target LUFS/True Peak results into non-writing recommendations.
Completed and failed step evidence is available through paged `mix_audit_results`, with
optional target/phase filters and compact or full payloads; a large all-mixer parameter
run therefore does not depend on the MCP client retaining every intermediate response.
Analyzer candidates are expanded only after the target strip has been revealed and freshly
read, so an off-screen Ozone or Loudness Meter is not missed because the initial inventory
was partial. Cancellation before mutation is a no-op; cancellation after an editor opens
keeps only that editor's verified cleanup, and state restore is retained only after
isolation, transport or bounce has begun.
The mature server's `logic_plugins get_inventory` is supplemental: its measured 25-second
timeout falls back to the fresh AX strip read and is recorded as failed evidence instead of
aborting that target.
`mix_fix_plan` takes explicit target/plugin/parameter/value fixes. At apply time it reopens
the exact signal-flow insert, re-resolves one exact Controls-table label, optionally checks
the expected old value, writes with plugin/channel identity binding and read-back, restores
the original editor view and closes only that verified window. A second audit can be
compared with `mix_before_after`.

For large mixers, the cheap initial inventory may contain generic off-screen strip labels.
Each target is therefore sent through `mixer_reveal_strip` (`AXScrollToVisible`) and
`mixer_read_strip` immediately before its insert chain is expanded into per-plugin steps.
The revealed strip name must match the mature track inventory; otherwise that target stops.
Isolation is exclusive: captured pre-existing solos are cleared, only the selected track or
group members are enabled, and a master run clears every solo so it measures the full mix.

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
~/dev/venv/bin/python -m unittest discover -s tests -v
```

Register in the MCP client of your choice, pointing `command` at the venv interpreter
and `args` at the scripts inside this checkout (for example,
`/Users/you/dev/Logic-MCP/logic_plugins_mcp.py`), not at copied sibling files. Both
servers accept `--check` for a readiness report. Restart/reload the MCP client after a
code update so its long-running stdio process registers the current tool set.

The companion MCU server used for the verified three-server setup is the patched
[avispanf/logic-pro-mcp](https://github.com/avispanf/logic-pro-mcp) fork at commit
`a80ef0d`. Give each concurrently running MCP client its own stable MIDI namespace:

```toml
[mcp_servers.logic-pro]
command = "/absolute/path/to/LogicProMCP"

[mcp_servers.logic-pro.env]
LOGIC_PRO_MCP_MIDI_INSTANCE_ID = "codex"
LOGIC_PRO_MCP_SHARE_DIR = "/opt/homebrew/opt/logic-pro-mcp/share/logic-pro-mcp"
```

In Logic Pro > Control Surfaces > Setup, assign both input and output to
`LogicProMCP-MCU-Internal [codex]` once. The endpoint keeps a deterministic CoreMIDI
identity, so Logic reconnects after the MCP process restarts without rebuilding the
control surface. The patched `set_master_volume` also verifies through the Control Bar
AX slider when Logic does not echo the master-fader move over MCU; it does not report a
successful write without that independent read-back.

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
path. Tools that target the main project window find the `AXStandardWindow` whose title
ends in ` - Tracks`; plugin editor paths still carry the same hazard and should be obtained
immediately before a write.

**Insert order is backwards in Accessibility.** The visually lowest slot has the smallest
AX index. Mixer reports reverse both insert and send lists so their first item is the first
one reached by the signal.

**The fader scale is only locally linear.** From raw position 113 through unity at 173 the
measured relationship is ten raw units per decibel. Below that span the error grows quickly,
so the server returns `null` with a reason instead of inventing a decibel value.

**Listing does not require plugin validation.** `auval -a` did not finish on a collection
of hundreds of components. `au_list` now reads the public macOS AudioComponent registry
directly, which returns the same identifiers without launching or validating each plugin.

**Live meter readout is capability-tested.** A meter window is counted as measurable only
when AX exposes both a numeric value and a meter label/unit. Logic Loudness Meter's custom
view exposed only its title in the measured session, so the audit correctly fell back to a
target bounce and the independent BS.1770 analyzer instead of reporting a fabricated LUFS
value.

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

Live plugin writes additionally require the expected plugin or channel identity. Audit
fixes never reuse an earlier parameter path: the exact label and optional old value are
resolved again immediately before the write. Track isolation uses session-stable `trk_…`
references when available. A mixer-only Aux/Bus is never treated as a track index; its
mute/solo control is addressed by verified strip path and read back. Initial track,
selection, mute/solo, playhead, cycle and transport intents are converted into explicit
restore dispatches, with missing fields left unknown rather than guessed. Bounce targets
are project-bound, confirmation-gated and created without overwrite.

`--check` includes a name-resolution pass over the server's own source. This exists
because a refactor once silently deleted six module-level definitions: the file still
compiled, imported and registered all tools, and only failed when a write was attempted.

## Limitations

Instrument editors cannot be read through accessibility: they expose thousands of elements
and overrun any sensible budget. Audio-effect editors are opened automatically from their
verified signal-flow insert; stale strip paths, changed plugin names and ambiguous duplicate
inserts are refused. Insert order cannot be changed and plugins cannot be removed.
`ProjectData` is not parsed, so arrangement data comes from a MIDI export.

The audit coordinator is deliberately a cross-server state machine: the MCP client must
execute each returned dispatch and feed its result to `mix_audit_advance`. It cannot call
another already-running stdio MCP server from inside this server. If the client disappears
mid-run, use `mix_audit_status`/`mix_audit_cancel` in the same server session and execute the
returned restore dispatch. In-memory plans do not survive a server restart.

Isolation and restoration tools return child dispatch lists. The coordinator refuses to
advance these steps unless the MCP client reports both `executed: true` and
`verified: true` after running and reading back every child operation.

Group membership is exact when an input inventory exposes stack parent/member data. The
v3.13 `logic://tracks` base schema does not, so `scope=group` also supports one explicit
exact-name fallback: it binds that named track as the stack root and reports
`membership_complete=false`; it never guesses an unnamed set of groups. Mixer-only Aux/Bus
operations require a current full-detail AX strip; off-screen generic strips are reported
as incomplete and are not guessed. Existing analyzer plugins are read when they expose
numeric AX values; otherwise bounce plus BS.1770 is the authoritative measurement path.

Known rough edges, stated plainly because they were measured rather than assumed:
`strip_settings_inspect` has never been given a real channel strip file; and `keys_fire` in
the audio server needs `python-rtmidi`, which is optional and usually absent. Channel strips
outside the visible Mixer viewport expose generic insert labels instead of plugin names;
`mixer_survey` marks those rows partial rather than presenting the placeholders as facts.

## Licence

MIT.
