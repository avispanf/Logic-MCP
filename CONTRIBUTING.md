# Contributing

Use Python 3.12 or 3.14 on macOS and `requirements-lock.txt` in an isolated environment.
Run `python -B -m unittest discover -s tests -v`. Native presentation checks require
Apple Silicon and Xcode Command Line Tools. Build and release instructions are in
`docs/RELEASING.md`; development packaging is `.github/workflows/build.yml`.

Keep changes scoped. Do not weaken identity/readback, ownership, manual-mode or
cancellation checks to make a test pass. Include regression tests for failure paths.
Never report compilation, mocked unit tests or CI as proof of live Logic behavior.
Live tests require a disposable project, state capture and independent readback/restoration.
Do not automate record, destructive edits, plugin dragging or permission resets in CI.

For bug reports, supply versions, source/build, expected/actual results and a minimal
reproduction. Redact project names and paths from Diagnostics. No credentials or songs.
Use private reporting for vulnerabilities, as described in SECURITY.md.
