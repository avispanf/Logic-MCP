# Logic-MCP

Logic Pro automation with a verified manual-work switch and one shared MIDI backend.

[Download the Apple Silicon developer preview](https://github.com/avispanf/Logic-MCP/releases/tag/v0.2.0-beta.1)
— open the app and choose **Install & connect**. See [installation](docs/INSTALL.md)
for the macOS approvals that cannot be automated.

**Status: 0.2.0 beta.** Apple Silicon; build target macOS 14+. Logic integration was measured
on Logic Pro 12.3 / macOS 26. Earlier supported OS builds require device validation.
Logic 13.0 validation on macOS 26.6 covers transport discovery, stable MCU ports,
Inspector-verified selection and a restored Limiter gain edit; it is not a complete
plugin or mix/mastering qualification. Native plugin insertion on Logic 13 remains
unverified when Logic exposes only a generic slot label, and is rolled back.
GitHub developer-preview packages use ad hoc signing. An Apple account is not required
to publish them; macOS may block first launch. Developer ID/notarization is an optional
distribution route for smoother Gatekeeper handling, not a requirement to share code on GitHub.
See the [Logic 13 runtime validation](docs/validation-2026-11-07.md) and
[installed build 8 polish checks](docs/validation-build-8-2026-11-07.md) for tested
behaviour and the remaining release gates.
The [build 10 installer record](docs/validation-build-10-2026-11-07.md) covers the
guided installer, two-client configuration and unsigned-preview package checks.

## Use the application

See the [end-user installation guide](docs/INSTALL.md). Maintainers preparing the first
public beta should follow the [release runbook](docs/RELEASING.md).
The [release readiness record](docs/release-readiness-2026-11-07.md) separates completed
development checks from signing and signed-candidate runtime checks still outstanding.
Security/privacy and safe reporting are described in [SECURITY.md](SECURITY.md).

1. Download the app ZIP or DMG from [Releases](https://github.com/avispanf/Logic-MCP/releases)
   when a verified package is available. Development builds are also produced by the
   **Build downloadable app** GitHub Actions workflow.
2. Open **Logic MCP.app**, then click **Install & connect**. On a fresh installation it
   copies itself to your user Applications folder, installs the shared service and
   configures both Codex and Claude Desktop, with backups. Existing apps are not overwritten.
3. Approve Accessibility/Automation yourself using **Reveal permission helper** and
   **Accessibility settings**. Unsigned downloads may also require Apple's **Open Anyway** approval.
4. Enable new automation and check Diagnostics. If Logic does not already detect the MCU,
   assign both Logic Control ports to `LogicProMCP-MCU-Internal [bridge]`.
5. Restart Codex/Claude Desktop when convenient. On a fresh install the core tool cache
   is empty until the backend has been enabled once; reload the client after that.
6. Left-click the button to switch between manual work and automation.

The right-click menu also has explicit **Manual mode — MCP OFF** and **Enable NEW
automation** actions. **Copy MCP client configuration** uses the actual installed path;
the copied example is TOML, so adapt it if your client uses another format.
**Reveal permission helper** selects the correct executable in Finder, and
**Accessibility settings** opens the matching macOS pane. Permissions still require
your approval. Diagnostics show an explanation; **Copy diagnostics** copies the full
helper response only when you choose it.

**Install & connect Codex + Claude** reruns the client setup. Only the three Logic MCP
entries are changed; unrelated settings, servers and tool restrictions are retained.
Claude's STDIO core connection forwards to the same HTTP backend, never a second MCU engine.
Setup leaves MCP OFF and does not edit Logic projects or silently restart either client.

The package includes Python and the patched backend. End users do not need a compiler,
a venv, a source checkout, or Homebrew for the mode switch and plugin control. Audio-file
analysis additionally needs ffmpeg/ffprobe; the audio diagnostics report missing helpers.
The bundled setup page explains Accessibility/Automation permissions required for Logic
control (the mode button itself needs none).
For the installed shared service, macOS attributes Accessibility requests to
`Logic MCP.app/Contents/Resources/runtime/logic-mcp`, which starts the backend.
Grant that installed executable access; adding only `bin/LogicProMCP` is not sufficient.
An ad hoc development update can invalidate the old grant even while its switch still
looks enabled. Remove the old `logic-mcp` entry, add the current installed executable
again, then restart the service and verify Diagnostics. Do not reset unrelated apps.
If the bundled runtime is missing, the app reports a reinstall error; it never falls
back to running scripts from a developer checkout.


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
Wrapped Controls values retain their display units during verified writes. Decimal
commas and Unicode minus signs are accepted; incompatible units or a unit change during
stepping stop the operation. No display-to-raw unit conversion is guessed.

**Mixer.** `mixer_strips` indexes the channel strips; `mixer_survey` reads name, fader level
in decibels, pan, mute, solo, clipping state, output bus, sends and insert chain for each.
Insert and send lists are reversed from AX order into actual signal-flow order.
The survey reads only each strip's direct grouped properties instead of recursively walking
every insert/send subtree, and returns `retry_offsets` plus the first incomplete offset for
bounded resumption. Undocumented non-finite or out-of-range send slider encodings are kept
as raw evidence with `level=null`; they are never presented as guessed decibels.
`main_window` and `mixer_locate` find the Tracks window and Mixer pane in the accessibility
tree, because opening an editor changes every window index.

**Transport and metering.** `control_bar` reports playback state, tempo, signature, key and
playhead position. `transport_press` presses real Control Bar buttons and reports what
changed. `meter_watch` polls meter values during playback.

**Application.** `menu_list` and `menu_click` navigate Logic's menus, with destructive items
refused unless explicitly allowed. `surfaces_bypass` reads and sets Bypass All Control
Surfaces, which silences an attached control surface without disturbing its port
assignment. `surfaces_doctor` is the read-only preflight for phantom fader or automation
jumps: it checks the expected MCU source/destination pair directly through CoreMIDI,
separates harmless duplicate general-MIDI ports from competing MCU namespaces, reports
duplicate server processes, and flags stale `[audit]`/`[test]` port references retained in
Logic's control-surface preferences. `ax_show_menu` exposes context-menu capabilities without clicking them, and
`close_plugin_windows` clears editor dialogs without closing the project window.

### Shared transport and manual mode

A persistent loopback-only HTTP supervisor owns one restartable backend at
`http://127.0.0.1:8765/mcp`. All plugin/audio MCP tools use a cross-process operation
lock. The audit runner holds exclusive ownership for its entire run and connects to that
same HTTP backend; it never creates another MCU namespace.
The shared backend selects tracks through its registered MCU first. A sent selection
is not a verified selection: audit operations independently check the exact selected-track
Inspector name before changing solo, mute or plugin parameters. The standalone core keeps
its AX-first route, since its private MCU may not be registered in Logic.

For an HTTP-capable MCP client:

```toml
[mcp_servers.logic-pro]
url = "http://127.0.0.1:8765/mcp"
tool_timeout_sec = 180

[mcp_servers.logic-plugins]
command = "/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp"
args = ["--plugins"]

[mcp_servers.logic-audio]
command = "/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp"
args = ["--audio"]
```

Use `~/Applications` instead if installed there. Other clients must support Streamable
HTTP for the core connection; use their supported configuration surface rather than
assuming every client accepts the same JSON format.

**MANUAL · MCP OFF** confirms that identifiable workers are stopped and no uncertain
candidate is present. Discovery uses actual argv, including paths with spaces and
alternate checkouts. Unidentified candidates prevent a green confirmation. Hosts and
Logic are never intentionally suspended.

Enabling cancels old paused workers before opening the gate. **Old jobs are not resumed.**
Start a new request. The supervisor stays available during manual mode and restarts the
backend after enabling; MIDI endpoints may be recreated once per transition, using stable
MCU identities. The switch checks OS processes, never Logic's menus.

Emergency pausing can interrupt an audit before its restoration steps. Inspect solo,
transport and plugin state and consult the audit journal before starting again.
Automatic recovery must not apply an old project's state to a new project.

Right-click offers diagnostics, repair, service removal and an overlay preference.
`MCP · offline` / `MCP · logic-closed` means automation is allowed but not ready.
`MCP CHECK` is unverified. The service's `/health` remains reachable in manual mode and
reports `state=manual`, `ready=false`.
The compact label **MCP · MCU check** means old feedback, not a confirmed port failure.
Its tooltip explains idle telemetry and keeps readiness unverified. Technical state
codes remain available in copied diagnostics. If the mode itself cannot be verified,
left-clicking **MCP CHECK** requests a pause rather than enabling automation.
Background ping or diagnostic timeouts do not restart a live backend or recreate its
MIDI ports. Readiness becomes unverified and the health response explains the timeout;
the next probe can recover in place. A closed transport still triggers backend recovery.
MCU setup and feedback are checked from the reported `registered_as_device`, `connected`
and `feedback_stale` fields; missing data is not reported as ready.

```bash
"/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp" --control status
"/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp" --control repair
```

Repair backs up invalid state and keeps manual mode enabled. Remove service from the menu,
remove MCP client entries, quit the app, then move it to Trash. The manual barrier is
retained deliberately; installing again preserves it. No project files are deleted.

**Mix audit orchestration.** `mix_inventory` merges the mature server's `logic://tracks`
and `logic://mixer` resources with `mixer_survey`, preserving stable track references and
AX-only Aux/Bus paths. Project-track indices and visible-mixer positions are separate
namespaces: records bind only through a unique exact name/master identity, a named AX strip,
or demonstrably complete positional sets. Unbound rows remain explicit warnings, and an
`all` plan fails closed until every surface identity is corroborated. `mix_audit_plan`
accepts `track`, `group`, `aux`, `bus`, `master` or
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

Development uses the pinned dependency set in `requirements-lock.txt`. `python-rtmidi`
is optional and affects one MIDI-trigger tool.

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
~/dev/venv/bin/pip install -r requirements-lock.txt
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
`7979bc7`. It coalesces overlapping background/explicit AX refreshes, gives the measured
full-cache operation a bounded 90-second deadline, and returns schema-valid structured
content even for legacy prose responses. On Logic 13 it prefers the transport toolbar over
the other Control Bar group, and rejects non-chooser window titles before scanning chooser
contents. Track-header discovery stops at the matching container, and mixer discovery
skips hidden panes, Library rows and channel-strip control subtrees. Run it through the shared HTTP bridge described
above so all desktop clients use one stable MIDI namespace:

```toml
[mcp_servers.logic-pro]
url = "http://127.0.0.1:8765/mcp"
```

In Logic Pro > Control Surfaces > Setup, assign both input and output to
`LogicProMCP-MCU-Internal [bridge]` once. The endpoint keeps a deterministic CoreMIDI
identity, so Logic reconnects after the MCP process restarts without rebuilding the
control surface. The patched `set_master_volume` also verifies through the Control Bar
AX slider when Logic does not echo the master-fader move over MCU; it does not report a
successful write without that independent read-back.

### Unattended mix-audit runner

`run_mix_audit.py` is a local client for long audits. It starts custom plugin/audio
sessions and connects to the already-running HTTP backend, journals every full response,
holds exclusive ownership, and schedules state restoration. This lets edited server code be
tested without restarting the desktop MCP host. Live isolation and bounce still require
the explicit `--confirmed` flag:

```bash
~/dev/venv/bin/python run_mix_audit.py \
  --scope all --selector '' --confirmed \
  --output-root ~/Desktop/Logic-MCP-Audits/nightly
```

To continue after a safely stopped long run without repeating earlier project
tracks, add `--start-index N` (zero-based). The runner filters both the project
resource and AX survey before planning, so omitted tracks cannot re-enter as
unbound mixer rows. If a newly connected core temporarily exposes only its
generic eight-strip MCU bank, pass the prior evidence file with
`--tracks-snapshot /path/to/runner.jsonl`; only a readable `ax_live` capture is
accepted, and the open project path plus every selected Inspector name are still
verified live.

Several journals (including continuation runs) can be consolidated without opening
Logic. The report preserves every WAV path, LUFS-I/True Peak result, readable insert
chain, plugin parameter label/display value and count, runner status, and failed step:

```bash
~/dev/venv/bin/python generate_mix_audit_report.py \
  /path/to/first/runner.jsonl /path/to/continuation/runner.jsonl \
  --json /path/to/report.json --markdown /path/to/report.md
```

The report deliberately treats loudness values as measurements rather than automatic
gain targets. It never writes to Logic.

Large Controls tables are read with grouped label/value/role requests on the AX table,
then serialized locally. A 320-row Pro-Q 3 table measured about 36 seconds in one bulk
page instead of roughly 12 minutes over eight sequential pages. If a plugin exposes a
heterogeneous table that rejects the grouped request, the tool automatically falls back
to verified 40-row pages. The runner requests a 500-row page by default so the fast path
does not repeat the same whole-table query.

The runner uses the shared `LogicProMCP-MCU-Internal [bridge]` control surface. The runner rejects the generic `Track 1…8`
bank that appears during MCU startup and asks the patched core for a full AX refresh
before planning. The runner uses MCU only to select an indexed track, then requires the
Inspector name to match the exact Arrange-row name before it presses anything. Solo/Mute
are actuated and read back on the selected-track Inspector buttons because Logic 12.3
advertises AXPress on the Arrange-header checkboxes but silently ignores it. MCU LED
feedback is never accepted as verification. Each target is isolated, bounced to a new
non-overwriting WAV, measured with BS.1770, and restored before the next target. Full
evidence is stored in `runner.jsonl`; `summary.json` and bounded stdout contain compact
progress and results. Off-screen tracks whose insert names Logic does not expose are
still measured, but their plugin chains are recorded as unreadable rather than guessed.
The shared operation lease rejects other clients' tool operations during a runner audit.

macOS permissions: Accessibility must be granted to the process that launches the
server, not to the script. Automation for Logic Pro and System Events is also required.

## Measured behaviour

Everything below was established against Logic Pro 12.3 on macOS 26.5, not assumed.

**`entire contents` is unusable.** AppleScript returns a list of references that cannot
be dereferenced inside a `tell process` block; `contents of` fails with -1700. Index
recursion with bulk per-container property queries works and is what the walker uses.

**Controls view exposes host-rendered Audio Unit parameters.** It worked with the tested
third-party custom interfaces. This does not guarantee every parameter is writable:
read-only controls or buttons without readable state are refused or left unverified.
Instrument editors expose thousands of elements and are skipped by a size probe.

**Native editor restoration is orientation-aware.** Apple's Loudness Meter offers
`Vertical` and `Horizontal` rather than a generic `Editor` menu item. Snapshots preserve
the exact restorable orientation, and restore verifies the resulting window geometry
before closing the editor.

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
Automatic file-project discovery requires one exact Tracks-window/bundle-name match.
Templates retained in Logic's open file descriptors are never selected by shortest path.
An unsaved project or ambiguous identity resolves to no project; save it before a file audit.

On Logic 13, some Controls rows can expose a state-less AXButton instead of a readable
checkbox. Those states are not guessed. Native insertion may expose only `audio plug-in`;
the verified insertion path rolls it back rather than accepting an unknown plugin.
MCU feedback can age while stopped even though the ports and registration are intact.
The health display conservatively reports stale feedback; this alone is not evidence of
endpoint churn, and each actual write still needs independent readback.

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

## Build and release

Install the locked build requirements, then provide the supported backend:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
LOGIC_MCP_PYTHON="$PWD/.venv/bin/python" \\
LOGIC_MCP_BACKEND="/path/to/patched/LogicProMCP" \\
LOGIC_MCP_SHARE_DIR="/path/to/core/Scripts" scripts/build_app.sh
```

Build provenance: backend source commit `7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4`
from the linked fork. The build workflow compiles this pinned source rather than using an
unversioned Homebrew binary. The package rejects backends lacking namespace support.
Native dependency and vendored notices are collected from that exact source checkout.
If the binary has been moved away from its build tree, set `LOGIC_MCP_BACKEND_SOURCE`
to the pinned checkout. Missing notices or a different source commit fail packaging.
`scripts/install.sh` builds and installs a development app with a versioned backup.
Legacy installer script names delegate to the new installer.

Outputs: standalone app, ZIP and DMG under `dist/`. Build scripts refuse to overwrite an
existing app output. `LOGIC_MCP_OUTPUT` selects a new destination.
Every bundled Mach-O binary is checked for arm64 support and a deployment target no
newer than the app's declared minimum OS. A Python built with newer Homebrew libraries
can fail this check even when the outer app targets macOS 14; use the CI build or a
Python distribution whose bundled libraries support the declared minimum.

For Developer ID signing, set `LOGIC_MCP_SIGN_IDENTITY`. For notarization also set
`LOGIC_MCP_NOTARY_PROFILE` to a configured notarytool keychain profile. Nested executables
are signed before the bundle, the notary result is awaited, and the ticket is stapled.
The app declares why it uses Apple Events. Its menu executable, bundled runtime and
backend are signed with the Apple Events automation entitlement; build verification
checks all three signatures. This allows a permission request, not silent permission
granting. Users still approve Accessibility and Automation in macOS System Settings.
These credentials are intentionally not in the repository. Unsigned workflow artifacts
are development builds, not notarized stable releases.

Before publishing: run the tests, smoke-test frozen MCP connections, test installation
from a path with spaces, verify app signatures and deployment targets, validate on the
claimed minimum OS, and test a downloaded quarantined signed build on a clean Mac.
Use a versioned release with checksums and third-party notices. The current CI checks
Python 3.12/3.14 and the native build; the manual build workflow provides downloadable
development artifacts. Intel and universal builds are not claimed.
