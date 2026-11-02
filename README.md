# Phasmid

Phasmid is a Chromium fork, built on [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium), that reports a device described by a profile instead of the machine it runs on. GPU, screen, fonts, CPU and memory, locale and time zone, media devices, speech voices, codecs and the User-Agent all come from one JSON file, and they agree with each other in every page, frame, worker and HTTP header.

> Phasmids (stick and leaf insects) survive by looking exactly like their surroundings.

## Why in the source

Fingerprinting scripts read dozens of browser properties and compare them. Values changed by injected JavaScript or command-line switches tend to miss workers and iframes, leave traces in function source, and contradict the HTTP headers.

Phasmid changes each value where Chromium computes it: in the browser process, the GPU process, the network service or Blink's platform layer. Every context then receives the same value through Chromium's own plumbing, and nothing is added that a page could detect. The reported Chrome version is always the version the binary was built from.

## Status

Pre-release. Linux x64 builds are published on the [releases page](https://github.com/tintty1/phasmid/releases), each with its Chromium version and checksum.

- Builds for **Linux x64** only.
- Profiles may claim **Windows** or **Linux**. macOS is not supported.
- Chromium version: [`chromium_version.txt`](chromium_version.txt).
- Patched surfaces are listed in [`docs/surfaces.md`](docs/surfaces.md): hardware, User-Agent and Client Hints, screen and window, media queries, system colors, GPU info, WebGL, WebGPU, canvas and audio readback, fonts (with Windows metrics and fallback), locale, time zone, geolocation, WebRTC, network information, speech voices, media devices, Widevine, hardware decode, WebAuthn, Bluetooth and Web Share, battery, and automation signals.

A Windows profile on a Linux host reports Windows wherever the host allows it. Where the host cannot back a claim (a font file it lacks, a Widevine CDM that names Linux, GPU arithmetic that is not Direct3D's), the browser logs a warning at startup and reports the claim as well as it can. See [unbacked claims](docs/architecture.md#unbacked-claims).

## Quick start

```bash
chrome --phasmid-profile=profiles/examples/linux-arch-rtx3060-1080p.json
```

The browser validates the profile at startup and exits with a message naming the field if it is invalid. Without `--phasmid-profile` the binary is plain Chromium.

Phasmid is an ordinary Chromium binary and works with Playwright, Puppeteer, Selenium and any other CDP client: pass the switch in the launch arguments.

| Switch | Meaning |
| --- | --- |
| `--phasmid-profile=<path>` | The profile to load. |
| `--phasmid-windows-fonts=<dir>` | Windows font store for Windows profiles. Default `$XDG_DATA_HOME/phasmid/fonts/windows/<os.build>`. |
| `--phasmid-widevine-cdm=<dir>` | Installed Widevine CDM, used when the profile claims Widevine. Default `/opt/google/chrome/WidevineCdm`. |

## Docker

[`docker/`](docker/) builds a runtime image from a release: the browser, its system libraries, common Linux fonts, Xvfb, and CDP on port 9222.

```bash
docker build -t phasmid docker/
docker run --rm -p 127.0.0.1:9222:9222 --security-opt seccomp=unconfined \
    -v "$PWD/profiles/examples/linux-arch-rtx3060-1080p.json:/profile.json:ro" \
    phasmid --phasmid-profile=/profile.json
```

Then connect a CDP client to `http://127.0.0.1:9222`, e.g. Playwright's `chromium.connect_over_cdp`.

- `--security-opt seccomp=unconfined` lets Chrome's sandbox create its namespaces. Do not use `--no-sandbox` instead: a headful window shows it as a warning infobar.
- `-e PHASMID_HEADFUL=1` runs headful under Xvfb. `PHASMID_XVFB_SCREEN` (default `3840x2160x24`) must be at least the profile's screen size.
- Without a GPU device the browser renders WebGL and WebGPU on SwiftShader. Pass one with `--device /dev/dri` (or the NVIDIA container toolkit) to back a hardware GPU claim.
- The image has no Widevine CDM. Mount Google Chrome's `WidevineCdm` directory and pass `--phasmid-widevine-cdm=<dir>` for a profile that claims Widevine.
- A Windows profile needs the Windows font store: mount it and pass `--phasmid-windows-fonts=<dir>`.
- From a local build: `docker build -t phasmid --build-context dist=<dir> --build-arg PHASMID_VERSION=<version> docker/`, where `<dir>` holds `phasmid-<version>-linux-x64.tar.zst`.

## Profiles

A profile is one JSON file matching [`profiles/schema.json`](profiles/schema.json). It embeds everything the browser reports, so the browser needs nothing else.

| Path | Contents |
| --- | --- |
| `profiles/examples/` | Complete profiles, each captured from a real machine. |
| `profiles/samples/` | Generated profiles for development. Each regenerates byte for byte from its seed. |
| `profiles/data/` | Captured device data: the only source for GPU, font, voice and codec values. |
| `tools/genprofile/` | Deterministic profile generator. With `--exit-ip` it derives time zone, geolocation, languages and WebRTC address from a proxy's exit IP. |

Values belong to consistency groups (OS, GPU, display, hardware, location) whose members must agree. The generator and the browser's startup validation both enforce them.

```bash
cd tools/genprofile
uv run python -m genprofile --os linux --seed <32 hex> --timezone Europe/Berlin --languages de-DE,de,en-US,en -o out.json
uv run python -m genprofile --os windows --seed <32 hex> --exit-ip <proxy exit IP> -o out.json
```

See [`tools/genprofile/README.md`](tools/genprofile/README.md). [`tools/proxyroute/`](tools/proxyroute/README.md) launches the browser with requests routed to proxies by host pattern.

## Building

Requirements: a Linux x64 host, about 100 GB of free disk, 32 GB of RAM or more, Python 3.10 or newer and the usual Chromium build dependencies. The tests and tools need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
./scripts/fetch_source.sh            # Chromium source for chromium_version.txt, unpacked into build/src
./scripts/apply_patches.sh           # prune, upstream series minus disabled, Phasmid series, domain substitution
./scripts/configure.sh ci            # or: release, local; uses sccache when available
autoninja -C build/src/out/Release chrome
```

A cold build takes hours. Incremental builds after a patch change take minutes.

## Testing

The test pipeline is in [`tests/`](tests/README.md):

- `consistency/` dumps every surface from every context (main frame, same- and cross-origin iframes, dedicated, shared and service workers, OffscreenCanvas, HTTP headers) and compares it with a stock Chrome baseline and with the profile.
- `guards/` has one script per patch that asserts the patch's observable effect.
- `static/` checks patch headers, the schema and the example profiles without a build.
- `masking/` records the verdicts of public detection sites. It is informational.
- `inspector/` traces which Web APIs a page calls. See [`docs/inspector.md`](docs/inspector.md).

## Repository layout

```
chromium_version.txt     Pinned Chromium version (from ungoogled-chromium)
revision.txt             ungoogled-chromium revision of that version
patches/series           ungoogled-chromium patch order (upstream, never edited)
patches/core/, extra/,   ungoogled-chromium patches (upstream, never edited)
  upstream-fixes/
patches/phasmid/         Phasmid patches, applied after upstream
  series                 Phasmid patch order
  disabled-upstream.txt  Upstream patches Phasmid skips, with reasons
profiles/                Schema, example and sample profiles, captured data
tools/                   Profile generator, proxy router
tests/                   Consistency harness, guards, static tests, masking report, inspector
scripts/                 Fetch, patch and configure scripts
docker/                  Runtime image built from a release
docs/                    Phasmid docs, plus ungoogled-chromium's own
utils/, devutils/        ungoogled-chromium tooling
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md): profile loading, transport to child processes, ground rules, consistency groups.
- [`docs/surfaces.md`](docs/surfaces.md): every patched surface, its hooks and traps.
- [`docs/inspector.md`](docs/inspector.md): the fingerprint inspector.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): patch conventions, the quilt workflow, required tests.

## Relationship to ungoogled-chromium

This repository is a fork of ungoogled-chromium with its history preserved. Upstream patches are applied first and Phasmid's on top. Upstream is merged for each Chromium release.

ungoogled-chromium removes Google integration; Phasmid aims to be indistinguishable from stock Chrome. Where an upstream patch changes something a page can observe, Phasmid disables it rather than editing it, and lists it with a reason in [`patches/phasmid/disabled-upstream.txt`](patches/phasmid/disabled-upstream.txt). Privacy patches with no observable effect stay enabled. The other files under `docs/` are ungoogled-chromium's.

Phasmid is an independent project, not affiliated with or endorsed by ungoogled-chromium, the Chromium project or Google.

## Responsible use

Phasmid is meant for privacy protection, fingerprinting research, and testing web applications and automation you are authorized to run. You are responsible for complying with applicable law and with the terms of the sites you access. Do not use it for fraud, account abuse or unauthorized access.

## License

BSD 3-Clause, as ungoogled-chromium. See [`LICENSE`](LICENSE). Chromium and its third-party components keep their own licenses.

The profile generator's location data keeps its own licenses, detailed in [`tools/genprofile/README.md`](tools/genprofile/README.md): IP geolocation by [DB-IP](https://db-ip.com) (CC BY 4.0, downloaded, not committed); time zone boundaries from timezone-boundary-builder via timezonefinder-data (ODbL 1.0, © OpenStreetMap contributors); the committed Unicode CLDR extract in `profiles/data/cldr/` (Unicode License v3, [`profiles/data/cldr/LICENSE`](profiles/data/cldr/LICENSE)).
