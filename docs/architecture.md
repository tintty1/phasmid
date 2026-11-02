# Architecture

Every Phasmid surface patch reads from one profile, loaded and validated once by `patches/phasmid/core/profile.patch`, which adds `components/phasmid/`.

## Loading and validation

The browser process reads `--phasmid-profile=<path>` in `ChromeMainDelegate::BasicStartupComplete`, the earliest browser callback. It validates the schema version, the required sections and every field an accessor exposes, and it checks group rules: values no real machine reports together, and capabilities no stock Chrome on the claimed OS reports, are refused. Fingerprinting-noise switches inherited from upstream are refused beside a profile.

A profile that fails validation stops the browser with a message naming the field. There is no fallback to host values.

Without the switch the binary is plain Chromium. This is what the stock-parity test runs use.

## Transport to child processes

The validated profile travels as canonical JSON in the `PHASMID_PROFILE` environment variable. The browser sets it, together with `TZ=<locale.timezone>` and, for fonts, `FONTCONFIG_FILE`, before the zygote, GPU and utility processes are launched, so every child inherits them: renderers forked from the zygote, the GPU process and the network service.

The environment is used instead of IPC because some values must be in place at process start. ICU and libc read `TZ` when they initialize, so the profile's time zone applies before any script runs and the host's zone is never observable, in any process. A process environment is not visible to pages.

## Accessors

Surface patches call `phasmid::Profile::Get()` and its typed accessors (`hardware_concurrency()`, `device_memory_gb()`, `timezone()`, ...).

- `Get()` returns null when no profile is active, and the patch then leaves Chromium's own code path untouched.
- Patches never read `base::CommandLine`, parse the profile or hardcode a value. A new value gets a new accessor, validated once in `Profile::Init`.
- In the browser process `Get()` is null until `BasicStartupComplete` has run. Code that runs earlier sees null, so a value it needs is set from `BasicStartupComplete`.
- `//components/phasmid` depends only on `//base`, so Blink, content, gpu and chrome can all depend on it. Each target whose sources include it lists it in its own GN `deps`.
- Noise keys for canvas, audio and network information are derived from the profile's seed in `components/phasmid/noise.*`.

## Ground rules

1. **Patch at the source of truth.** Change a value where Chromium computes it (platform layer, Blink core, GPU process, browser-side state), not in JavaScript bindings. Source-level values reach every context; binding-level changes leak.
2. **Every context reports what stock Chrome reports there.** Main frame, same- and cross-origin iframes, dedicated, shared and service workers, and OffscreenCanvas each match stock Chrome's behaviour for the claimed device. Stock Chrome itself differs between some contexts (worker-only processes resolve generic fonts differently, worker requests carry no Client Hints); those differences stay, with profile values on both sides. A patch never adds a difference stock Chrome does not have. When a value crosses IPC, patch the browser side so every renderer receives it already correct, and prefer hooks shared by window and worker (`NavigatorBase`, `NavigatorConcurrentHardware`) over `Navigator` alone.
3. **Headers and JavaScript agree.** User-Agent, Client Hints, `Accept-Language` and `Device-Memory` match their JavaScript counterparts exactly, including on worker requests.
4. **Noise is deterministic.** Where noise is used, it is seeded from the profile, stable across calls and restarts, and identical across contexts and readback paths. It never depends on readback-relative coordinates.
5. **Plausible, not unique.** Only values that occur together on real devices. An impossible combination is worse than the true value.
6. **The version is real.** The reported Chrome version equals the build's Chromium version in the UA string, `fullVersionList`, `uaFullVersion` and every `Sec-CH-UA-Full-Version*` header.
7. **Don't break the web.** A claimed capability works, or fails exactly as it does on the claimed platform.
8. **No page-visible artifacts.** No new IDL attributes, globals, `chrome://` resources or timing tricks. A property that exists only in Phasmid is a bug.
9. **Fail loudly.** A broken profile is an error at startup, not a fallback at runtime.

## Unbacked claims

A profile can be valid and still claim something this host or build cannot provide: a font the host lacks, a Widevine CDM that is not installed, a GPU limit above the host's, a platform authenticator. Such a claim runs. The browser process logs one line per field at startup:

```
Phasmid: host cannot back <field>: <detail>
```

The surface then reports the claim as well as it can, and fails the way the claimed platform would where it must fail (for example, a share sheet that is dismissed, or a speech engine whose server is unreachable). Child processes never re-check or re-log.

## Consistency groups

Before changing a value, check its group. Every member must agree. The profile generator and the browser's startup validation both enforce group rules.

| Group | Members |
| --- | --- |
| OS | UA and Client Hints platform, fonts, font metrics, system colors, GPU string format, speech voices, codecs and DRM, WebAuthn, Bluetooth and Web Share, audio defaults, screen `avail*` conventions |
| GPU | GPU info, WebGL renderer, extensions and limits, WebGPU adapter, hardware decode, canvas rendering |
| Display | Screen size, DPR, window sizes, media queries, touch points, pointer and hover |
| Hardware | Concurrency, device memory (JS and header), storage quota, JS heap limit, device class |
| Location | Proxy exit IP, time zone, locale, `Accept-Language`, geolocation, WebRTC candidates |

## Data sources

Every reported value maps to a field in [`profiles/schema.json`](../profiles/schema.json). GPU, font, voice and codec data come only from captures of real machines in `profiles/data/`. Screen, window and hardware shapes may also come from the profile generator's pinned statistical model.

Schema fields are never deleted. A field the browser stops reading is listed in `removed_fields` with the version that removed it, so older profiles still validate.
