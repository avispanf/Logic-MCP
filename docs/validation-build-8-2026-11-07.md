# Installed build 8: menu and setup polish

Code: `e98fef15c13288449c44bd366b5dcc042b33aa94`, app 0.2.0 beta, build 8.
Environment: Apple Silicon, macOS 26.6, Logic Pro 13.0, English Logic UI.
Installed location: `~/Applications/Logic MCP.app`.

This is an incremental UI/setup validation, not a new mix/mastering qualification.
The [build 7 record](validation-2026-11-07.md) contains the preceding installed-runtime
transport, selection and restored Limiter-edit tests and the outstanding release gates.
Build 8 keeps that native backend and frozen Python runtime unchanged. The installed
runtime SHA-256 is `d553ec590c1f32fc06e304dfadaa645b87127a09222f75f687ec173c7204a740`.

## Changes

- Short status labels have explanatory tooltips; idle stale MCU feedback is not
  silently promoted to ready or described as evidence of port churn.
- An unverified mode makes a toggle request manual mode. The pause intent is
  retained if a pending request runs after another status refresh.
- The right-click menu exposes separate manual/enable actions, actual-path MCP client
  configuration, the installed permission helper and Accessibility settings.
- Diagnostics show a human summary, with detailed JSON copied only on an explicit action.
- The floating button avoids redundant show/hide/reposition operations.
- Setup help explains permissions, TOML configuration, interrupted-audit recovery and
  the limitations of ad hoc development packages.

## Evidence

| Layer | Result |
| --- | --- |
| Local tests | 190 tests passed. The native presentation fixture performed 57 checks. |
| Client configuration | Real TOML parsing recovered all three server entries for paths containing spaces, quotes, Unicode and a backslash. |
| Packaged smoke | 77 tools registered; manual rejection and HTTP manual health passed. Bundle signatures passed strict verification. |
| Installed update | Bundle version 8 verified. The runtime hash matched build 7. All four backend permissions remained granted without re-authorization. |
| Actual menu | A right-click opened all 12 expected items. Diagnostics displayed a human manual-mode summary with Close and Copy diagnostics buttons; Close worked. |
| Enable action | Enable NEW automation started the installed backend. MCU registration and connection were confirmed; stale feedback remained explicitly unverified in the menu tooltip. |
| Port stability | Seven observations over 90.6 seconds kept one unchanged backend PID and exactly one MCU source/destination, with unique IDs `-103083858` / `-1873549471`. |
| Manual button | The actual menu-bar button returned to verified MANUAL / MCP OFF. A separate process/endpoint read found no backend and no MCU ports. |
| Rejection in manual mode | New HTTP track-selection and frozen plugin-write requests both explicitly failed with manual-mode errors. No write reached Logic. |
| Setup help layout | Visually inspected at a 1000-pixel viewport; DOM width checks at an actual 500-pixel narrow viewport found no horizontal overflow. A requested 420-pixel headless window was clamped to 500, so 420-pixel validation is not claimed. |

The UI test reads the status item's current Accessibility position before sending
mouse events: its menu-bar position changes as labels and foreground application menus
change. Stale fixed coordinates were not accepted as evidence of a broken menu.

Clipboard-writing commands were checked through the formatter/parse tests, not by
overwriting the user's clipboard. Finder reveal and the Accessibility deep link were
not exercised in this UI pass. The light color scheme was not independently forced.
No project parameter, transport or selection changes were requested in this polish pass.
The final installed state is verified MANUAL / MCP OFF.

## CI and release status

[Tests and build checks](https://github.com/avispanf/Logic-MCP/actions/runs/42414524736)
passed for the code commit above. The separate
[downloadable-app workflow](https://github.com/avispanf/Logic-MCP/actions/runs/42414571846)
must be checked independently; the local packaged smoke is not a substitute for its result.

This remains a development beta. Developer ID signing, notarization, a clean-Mac
download/quarantine installation test and runtime testing on the minimum macOS target
remain outstanding. No stable public release is claimed.
