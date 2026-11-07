# Third-party components

The packaged backend is built from https://github.com/avispanf/logic-pro-mcp at
`7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4` (upstream: MongLong0214/logic-pro-mcp).
Its MIT notice follows. Python runtime and package notices are retained by the frozen
distribution; release builds must include their collected license files.
The package additionally retains the exact CPython version's `Doc/license.rst`,
including its incorporated-software notices (such as OpenSSL), under
`licenses/Python/THIRD-PARTY-LICENSES.rst`. Its source URL and SHA-256 are in the manifest.
The build also retains the pinned native checkout's dependency and vendored notices
under `licenses/Native/`, including notices for its build/test dependencies. The app's
build manifest lists the collected files. Packaging fails if the matching source
checkout or dependency notices are missing.

MIT License

Copyright (c) 2025 Kolton Jacobs

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
