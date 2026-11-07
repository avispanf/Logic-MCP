# Public beta preparation: 2026-11-07

No release or tag was published. The repository is public; private vulnerability
reporting is enabled. First intended release: `v0.2.0-beta.1`.

## Verified without signing credentials

- 203 local tests passed, including actual early rejection of release mode without
  Developer ID and unit tests of Accepted/Invalid/In Progress, missing logs, timeouts,
  identity/Team ID, provenance, checksum, draft-only and mount-cleanup failure paths.
- Signing/notary policy tests use simulated responses. No request was submitted to Apple.
- Code candidate `737f6f49d5c148b26c38efe8b98d6e11622a3ee0` was packaged as build 9
  from clean app and pinned backend source checkouts, with ad hoc signatures.
- Packaged smoke passed: 77 registered tools, manual rejection, HTTP manual health,
  native notices and version-matched CPython third-party notices.
- The final ZIP was extracted and final DMG mounted read-only. Both contained the
  same app manifest, signed resource seal and main executable. All eight native
  binaries passed arm64/deployment-target checks; the bundle and three automation
  executables passed signature/entitlement verification.
- Installed build 8 was deliberately left unchanged in verified MANUAL / MCP OFF.
  There were no new live Logic writes or changes to other applications' permissions.

Development archive hashes, not public-release assets:

```text
a363389ed388744064c96b4a25087704ee203df851816c079dcf5668e63dd7f3  Logic-MCP-macos-arm64.zip
b16cc664a1d5f03cd800954c06b4c28e3c552dc9b9bd00bae33c8b9990416695  Logic-MCP-macos-arm64.dmg
```

The manifest records ad hoc signing, the exact source/native commits, binary hashes,
build version and CPython notice URL/hash. The runtime is byte-identical to installed
build 8 (`d553ec590c1f32fc06e304dfadaa645b87127a09222f75f687ec173c7204a740`).
The [build 7](validation-2026-11-07.md) and [build 8](validation-build-8-2026-11-07.md)
records cover the prior live Logic baseline, not a future signed build.

CI for the packaged source is tracked in
[Tests and build checks](https://github.com/avispanf/Logic-MCP/actions/runs/42417598552).
That workflow does not perform actual Developer ID signing or notarization.

## Blocked on owner setup / remaining runtime QA

`security find-identity -v -p codesigning` found zero valid identities on this Mac.
There is no claim of real Developer ID, Hardened Runtime execution, Accepted notary
status, a valid stapled ticket or Gatekeeper approval. Those branches must be run
with the owner's signing identity and Keychain profile.

The complete [release runbook](RELEASING.md) describes owner setup, final-archive
verification, a dry-run/draft-only GitHub release and the candidate acceptance checklist.
The signed build still needs live Logic restoration tests, clean-Mac/quarantine
installation, reboot/update/removal and minimum-OS runtime testing. None are replaced
by the ad hoc smoke, simulated policy tests or an OS deployment-target inspection.
No stable/all-plugins/all-groups/mix/mastering qualification is claimed.
