# Build fixes

Patches needed to build Chromium outside Google's infrastructure on Linux:
system esbuild and node, no hermetic Python, reverts of generated-code steps
that need internal tooling, arm64 compile fixes.

Vendored unchanged from
[ungoogled-chromium-portablelinux](https://github.com/ungoogled-software/ungoogled-chromium-portablelinux)
(`patches/ungoogled-chromium/portablelinux/`, BSD-3-Clause), commit
`9e6902e95b2a01cc35548c101ff9c38d6c2ec90a`, which pins ungoogled-chromium
`c7b4b99` (155.0.8059.84-1). They change nothing a page can observe and are
refreshed from that repository on every Chromium update, never edited here.
