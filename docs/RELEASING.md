# Public beta release runbook

No public app release has been made yet. The first intended release is
`v0.2.0-beta.1`, not a stable/all-plugins-supported release.

## One-time owner setup

1. Use an active [Apple Developer Program](https://developer.apple.com/programs/) account.
2. In Xcode → Settings → Accounts → your team → Manage Certificates, create a
   **Developer ID Application** certificate. It must have its private key in this
   Mac's login Keychain. Apple Development, Mac Distribution and Developer ID Installer
   are not substitutes. Certificate creation may require the team's Account Holder.
   See [Apple's Developer ID guide](https://developer.apple.com/developer-id/).
   Full Xcode is optional: alternatively create a CSR with Keychain Access's
   Certificate Assistant, upload it in the developer portal under Certificates →
   **Developer ID Application**, and open the downloaded certificate on the same Mac.
   See [Apple's portal procedure](https://developer.apple.com/help/account/certificates/create-developer-id-certificates)
   and [CSR instructions](https://developer.apple.com/help/account/certificates/create-a-certificate-signing-request).
3. In Terminal, run `security find-identity -v -p codesigning`. There must be a valid
   Developer ID Application entry. Its displayed name or SHA-1 identifier is safe to
   provide to the release operator; the private key is not.
4. In your own Terminal, run:

   ```sh
   xcrun notarytool store-credentials logic-mcp-notary
   ```

   Follow Apple's interactive prompts for Apple ID, Team ID and an app-specific
   password. Create the app-specific password in your Apple Account if needed.
   Credentials are stored by Apple's tool in Keychain. Do not paste them into a chat,
   source file or command supplied to an agent. You do not need to export a `.p12`
   or upload credentials to GitHub for a release built on this Mac.
5. Tell the release operator only that setup is complete, the signing identity and
   the Keychain profile name `logic-mcp-notary`. Approve any legitimate Keychain prompt
   locally. Never disable Gatekeeper or reset other applications' permissions.

Apple's [notarization requirements](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
include Developer ID, Hardened Runtime and secure timestamps. The code-signing private
key and notary credentials are separate prerequisites.

## Build from a clean committed checkout

Use Apple Silicon, Xcode Command Line Tools and a dedicated Python 3.12 environment
with `requirements-lock.txt` installed. The app includes its runtime; this is a
maintainer requirement, not an end-user requirement.

Build the pinned backend from `avispanf/logic-pro-mcp` commit
`7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4`. Run the same native test filter and release
build as `.github/workflows/build.yml`. Keep its `.build/checkouts` for license notices.
Do not use an arbitrary Homebrew binary.

```sh
export LOGIC_MCP_SIGN_IDENTITY='Developer ID Application: YOUR NAME (TEAMID)'
export LOGIC_MCP_NOTARY_PROFILE='logic-mcp-notary'
export LOGIC_MCP_RELEASE=1
export LOGIC_MCP_PYTHON='/absolute/path/to/release-venv/bin/python'
export LOGIC_MCP_BACKEND='/absolute/path/to/pinned-core/.build/release/LogicProMCP'
export LOGIC_MCP_BACKEND_SOURCE='/absolute/path/to/pinned-core'
export LOGIC_MCP_SHARE_DIR='/absolute/path/to/pinned-core/Scripts'
export LOGIC_MCP_OUTPUT='/absolute/path/to/new-empty-release-directory'

"$LOGIC_MCP_PYTHON" scripts/release_preflight.py --release
scripts/build_app.sh
"$LOGIC_MCP_PYTHON" scripts/smoke_app.py "$LOGIC_MCP_OUTPUT/Logic MCP.app"
"$LOGIC_MCP_PYTHON" scripts/verify_distribution.py "$LOGIC_MCP_OUTPUT"
```

The preflight checks identity, clean source and Keychain authentication before building.
The build signs nested code and framework containers inside-out, verifies signatures
and entitlements, submits the app, requires Accepted status, retrieves the notary log,
staples the app, then creates the ZIP and DMG. It signs/notarizes/staples the DMG too.
SHA256SUMS is written only after final package mutation. Both final archives are
extracted/mounted read-only and assessed independently. Nothing installs, changes Logic
or publishes automatically. Output files are never silently replaced.

Keep `notary-*-result.json` and `notary-*-log.json`. A timeout or In Progress status is
not success. Inspect the saved submission ID with `notarytool info/wait/log` instead
of automatically submitting again. Only the two final archives and SHA256SUMS belong
in the download assets; `notarize.zip` is an intermediate, unstapled submission.

## Candidate acceptance record

Record the source commit and SHA256SUMS of the exact final candidate, not a previous
ad hoc build. A signature or CI success does not prove these runtime checks:

- [ ] CI passes on the source commit, Python 3.12 and 3.14.
- [ ] ZIP/DMG signature, Team ID, timestamps, Hardened Runtime, tickets and Gatekeeper pass.
- [ ] Packaged smoke passes without creating MIDI ports or contacting Logic.
- [ ] Download the candidate on a clean Mac/account with normal quarantine retained.
      Install through Finder; launch without Terminal workarounds.
- [ ] First-run helper/Accessibility/Automation approvals work and explain exactly
      what to grant. No unrelated Accessibility grants are changed.
- [ ] Set up one MCU pair; observe stable endpoint identities and no duplicates.
- [ ] On a disposable Logic project, verify track selection, stopped/play/stop state,
      and one supported plugin edit through separate readback and restoration.
- [ ] OFF stops the backend/removes its ports and rejects new requests. ON starts
      only new work; no interrupted writes resume. Test a second OFF/ON cycle.
- [ ] Reboot/login, update in place and service removal preserve the manual barrier.
- [ ] Test the advertised minimum macOS 14 runtime, or explicitly narrow the supported
      runtime range before publishing. A Mach-O deployment-target check is not this test.

The existing build 7/8 Logic 13 records are a baseline, not evidence for a newly
Developer-ID-signed build. Recheck that exact signed build: Hardened Runtime and TCC
identity can change behavior. Do not overwrite the user's working app during candidate QA.

## Prepare the GitHub release

```sh
python scripts/release_draft.py "$LOGIC_MCP_OUTPUT" --tag v0.2.0-beta.1
python scripts/release_draft.py "$LOGIC_MCP_OUTPUT" --tag v0.2.0-beta.1 --create-draft
```

The first command verifies and prints a plan only. The second creates an explicitly
**draft, prerelease** entry targeting the manifest's exact source commit with only the
three final assets. It does not publish. Review release notes and attach the acceptance
record. Publish the draft only after all applicable checks pass. Never mark this beta
as the latest stable release. A stable release additionally requires broader plugin,
group/bus and real-audio measurement qualification; current evidence does not cover it.

## Credential boundaries

Local signing is the initial supported path. The development GitHub workflow has no
signing secrets and does not publish releases. Do not put credentials or certificate
exports in Actions artifacts, support logs, shell history or this repository. Rotating
certificates/Team ID or changing bundle identifiers requires a fresh permission/update test.
