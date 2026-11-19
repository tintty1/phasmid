# Build fixes

Patches needed to build Chromium outside Google's infrastructure on Linux:
system esbuild and node, no hermetic Python, reverts of generated-code steps
that need internal tooling, arm64 compile fixes.

Vendored from
[ungoogled-chromium-portablelinux](https://github.com/ungoogled-software/ungoogled-chromium-portablelinux)
(`patches/ungoogled-chromium/portablelinux/`, BSD-3-Clause), commit
`6f0aa178c2394fc0a177edc0fd64e41f402a0418`, which pins ungoogled-chromium
`28a211a` (156.0.8078.79-1). They change nothing a page can observe and are
refreshed from that repository on every Chromium update.

At 157.0.8103.78 that repository has no release yet, so the 156 patches are
refreshed here: `fix-compiling-on-arm64.patch` without its `build_bindgen.py`
import, which 157 no longer needs, and `chromium-154-use-system-tsc.patch`
against the new `webui_features.gni`. The 156 patches removing Private
Verification Tokens and reverting the Crubit changes are dropped: Chromium's
Rust toolchain package ships `cc_bindings_from_rs` and `rs_bindings_from_cc`,
so this build compiles both as stock Chrome does.
