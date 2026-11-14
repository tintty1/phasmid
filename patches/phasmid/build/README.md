# Build fixes

Patches needed to build Chromium outside Google's infrastructure on Linux:
system esbuild and node, no hermetic Python, reverts of generated-code steps
that need internal tooling, arm64 compile fixes.

Vendored unchanged from
[ungoogled-chromium-portablelinux](https://github.com/ungoogled-software/ungoogled-chromium-portablelinux)
(`patches/ungoogled-chromium/portablelinux/`, BSD-3-Clause), commit
`6f0aa178c2394fc0a177edc0fd64e41f402a0418`, which pins ungoogled-chromium
`28a211a` (156.0.8078.79-1). They change nothing a page can observe and are
refreshed from that repository on every Chromium update, never edited here.
