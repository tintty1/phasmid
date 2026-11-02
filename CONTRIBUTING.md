# Contributing to Phasmid

Read [`docs/architecture.md`](docs/architecture.md) for the ground rules and [`docs/surfaces.md`](docs/surfaces.md) for the surface you are changing. This file covers how a change is made and what it must pass.

## Patch conventions

- **One concern per patch.** A fix spanning two surfaces is two patches, at `patches/phasmid/<area>/<surface>.patch`, listed in [`patches/phasmid/series`](patches/phasmid/series).
- **Patch at the source of truth.** Change values where Chromium computes them, not in JavaScript bindings.
- **Minimal diffs.** No reformatting, renaming or moving upstream code. Every extra line is a future merge conflict.
- **Logic in `.cc` files.** Avoid widely included headers (`gpu/config/gpu_info.h`, `ui/display/screen_info.h`, `ui/gfx/geometry/*.h`, `core/dom/element.h`, `user_agent_metadata.h`). A header change recompiles thousands of files and invalidates the shared compile cache. Put new declarations in new headers included only where needed.
- **Read values through the accessors.** Call `phasmid::Profile::Get()`; leave Chromium's path untouched when it returns null. Never read the command line, parse the profile or hardcode a reported value. Add `//components/phasmid` to the GN `deps` of the target that lists the source.
- **Never edit upstream ungoogled-chromium patches** (`patches/core/`, `patches/extra/`, `patches/upstream-fixes/`). To skip one whose effect a page can observe, add it to [`patches/phasmid/disabled-upstream.txt`](patches/phasmid/disabled-upstream.txt) with a reason.
- **Never hand-edit a `.patch` file.** Use quilt (below).
- **Every patch has a guard.** `tests/guards/<area>/<surface>.py` launches the built browser with a profile and asserts the patch's observable effect. A patch that still applies after a rebase but no longer does anything is the most dangerous failure, and only a guard catches it. A guard first proves its precondition took effect, so it cannot pass by testing nothing.
- **Never delete a patch because it no longer applies.** Rebase it, or explain in the pull request why it cannot be.
- No scratch files, debug logging, commented-out code or to-do notes.

## Patch header

Every Phasmid patch starts with this header:

```
Phasmid: <surface name>

What:     <observable effect>
Why:      <the inconsistency it removes>
Profile:  <profile fields read>
Hooks:    <upstream files and functions touched>
Contexts: <contexts verified: main, iframe, worker, shared worker,
          service worker, offscreen, headers>
Guard:    tests/guards/<area>/<surface>.py
```

Wrap at about 80 columns, with continuation lines aligned under the value. `tests/static/` checks that every patch in the series has the header and an existing guard.

## Changing a patch

Patches are managed with quilt, as in ungoogled-chromium (see [`docs/developing.md`](docs/developing.md)). From the repository root, with the source unpacked in `build/src` and domain substitution **not** applied:

```bash
source devutils/set_quilt_vars.sh            # QUILT_PATCHES and friends point at patches/
cd build/src
quilt push -a                                # upstream series, then the Phasmid series
quilt push phasmid/<area>/<surface>.patch    # or: quilt new phasmid/<area>/<surface>.patch
quilt edit <file>                            # edit, then
quilt refresh                                # rewrites the patch from the real diff
quilt pop -a
cd ../.. && ./devutils/validate_patches.py -l build/src
```

Keep hunk context at 3 lines and paths with `a/` and `b/` prefixes. If domain substitution was applied, revert it first with `utils/domain_substitution.py revert`, or the patch will depend on substituted strings.

File paths move between Chromium releases. Confirm every hook with `rg` in the source tree at the pinned tag before editing.

## Building

```bash
./scripts/fetch_source.sh
./scripts/apply_patches.sh
./scripts/configure.sh ci            # or: release, local
autoninja -C build/src/out/Release chrome
```

Use incremental builds. Clean-build only when an incremental build fails for a reason that requires it.

## Testing before a pull request

From `tests/`, after `uv sync` (see [`tests/README.md`](tests/README.md)):

```bash
# Stock baseline of the same major, then Phasmid with a profile.
uv run python -m consistency dump --binary /usr/bin/google-chrome-stable -o out/stock.json
uv run python -m consistency dump --binary <phasmid chrome> --profile <profile.json> --baseline out/stock.json -o out/phasmid.json
uv run python -m consistency diff out/stock.json out/phasmid.json
uv run python -m consistency contexts out/phasmid.json --baseline out/stock.json

# The guard of every touched patch.
uv run python -m guards --binary <phasmid chrome> --profile <profile.json> --patch <area>/<surface>

# Harness, static and unit tests.
uv run pytest -q
```

A pull request that touches a patch must show:

- a successful build;
- the consistency dump with at least one Windows and one Linux profile, headless and headful, with every touched surface matching the profile in every context: main frame, same- and cross-origin iframes, dedicated, shared and service workers, OffscreenCanvas, and HTTP headers;
- zero cross-context disagreements beyond the stock baseline, zero header/JavaScript mismatches and zero within-context failures;
- a passing guard for every touched patch;
- targeted unit tests for touched files (`blink_unittests`, `content_unittests`, `gpu_unittests` with `--gtest_filter`).

Stock Chrome itself differs between some contexts; those differences are inherited from the baseline. Anything Phasmid adds to them is a bug.

When a patch changes what detection sites read, also run the masking report for Phasmid and for stock Chrome in the same modes. It is informational: only a check stock passes and Phasmid fails is a regression.

If a test fails, fix the cause. Do not loosen a threshold, retry until green or skip a test. Flakiness means non-determinism, which in this project is a bug.

## Profiles and the schema

- Every reported value maps to a field in [`profiles/schema.json`](profiles/schema.json). If a surface needs a value the schema lacks, open an issue proposing the field before writing the patch. Schema changes need discussion.
- Schema fields are never deleted. A field the browser stops reading is listed in `removed_fields` with the version that removed it, so older profiles still validate. A test enforces this.
- GPU, font, voice and codec values come only from captured data in `profiles/data/`, with its provenance. Never invent or hand-edit device data.
- Changing `chromium_version.txt` or build arguments needs the same discussion as a schema change.
