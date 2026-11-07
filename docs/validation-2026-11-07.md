# Installed build 7: Logic 13 validation

Code: `4a804cfe5e6975fadd41f7b46c1266a86a10102d`, app 0.2.0 beta, build 7.
Native backend: `7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4`.
Environment: Apple Silicon, macOS 26.6, Logic Pro 13.0, English Logic UI.
The installed application and its bundled runtime/backend were exercised, not only
Python functions from the checkout. Accessibility was re-granted after the ad hoc update.

Testing used the isolated three-track `Untitled 2` QuickStart project. No user's song
was mixed, mastered or rendered. No audible or loudness-target qualification is claimed.

## Live results

| Check | Observed result |
| --- | --- |
| Permissions | Backend reported Accessibility, Logic Automation, System Events Automation and PostEvent granted. |
| MCU selection | All three tracks selected through MCU; the frozen plugin server independently confirmed each exact Inspector name. MCU send results alone remained State B. |
| Port stability | 90.4 seconds, one unchanged backend PID, exactly one MCU source and destination, no duplicate or unique-ID changes. |
| Limiter parameter | Frozen MCP dry-run, Gain 0 to -1 dB, separate parameter-table read, restoration to 0 dB and another separate read all passed. Editor view and window state were restored. |
| Transport | Playback and stop confirmed by independent Control Bar reads; position restored to 1.1.1.1, Cycle and metronome state preserved. Output mute was independently verified during playback and restored afterward. |
| Manual button | The menu-bar button disabled automation. Backend exited, MCU endpoints disappeared, and new HTTP track-selection and frozen plugin-write requests were explicitly rejected as manual mode. |
| Re-enable | The same button started a new backend; MCU registration and connection recovered without reassigning Logic ports. MCU unique IDs matched the pre-pause IDs. |
| Handoff | Final state is verified MANUAL / MCP OFF, playback stopped, plugin gain and output mute restored. |

The stable MCU source/destination unique IDs were `-103083858` and `-1873549471`.
Endpoint enumeration indices are not identities and can change after restarting.

## Automated and packaged checks

- 189 local Python tests passed, including shared-owner, manual-transition and timeout safety regressions.
- 31 targeted native discovery, legacy-layout, main-window and project-chooser tests passed.
- Packaged smoke test passed: 77 registered tools, manual rejection and HTTP manual health.
- Bundle signatures, Apple Events entitlements and Mach-O deployment targets passed validation.
- The manifest lists 14 native license/notice files, in addition to Python notices.
- [Python CI](https://github.com/avispanf/Logic-MCP/actions/runs/42404659944) and the
  [downloadable-app build](https://github.com/avispanf/Logic-MCP/actions/runs/42404690557)
  completed successfully for the code commit above.

## Limits and release gates

- This is not proof that every plugin parameter, instrument, bus/group membership or
  live analyzer meter is readable or writable. Read-only and state-less controls are
  not guessed. No complete mix/mastering pass was performed.
- An earlier development-binary test of native Gain insertion on this Logic version
  returned a generic `audio plug-in`
  label. The operation refused verification and rolled back; a fresh inventory
  independently confirmed the slot was empty. Successful insertion is not claimed.
- Idle MCU feedback becomes stale even with stable ports and a registered device.
  The conservative health warning remains; stale telemetry alone is not port churn.
- An unsaved project's open template descriptors are not accepted as file-project
  identity. File auditing requires a unique saved-project identity.
- These packages are ad hoc signed. Developer ID signing, notarization and a clean-Mac
  downloaded/quarantined installation test remain required before a stable public release.
  Runtime validation on the declared minimum macOS version is also outstanding.
