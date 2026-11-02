# Surfaces

One section per surface patch in [`patches/phasmid/series`](../patches/phasmid/series). Each patch's header lists every hook and the contexts it covers; this page gives the overview and the traps that shape each patch. Every patch has a guard under `tests/guards/`.

Paths are relative to the Chromium source tree and match the pinned version. They move between releases; confirm with `rg` before editing.

## Core

### Profile loading (`core/profile.patch`)

Adds `components/phasmid/`: loading, validation, the accessors, seeded noise keys and the font allowlist. Exports `PHASMID_PROFILE` and `TZ` before any child process starts. See [architecture.md](architecture.md).

- **Time zone** has no patch of its own. `TZ` is honoured by ICU's host zone detection in every process, and `TimeZoneMonitorLinux` stops watching `/etc/localtime` when `TZ` is set. DevTools' time zone override keeps working.

## Hardware

### Hardware concurrency (`hardware/hardware-concurrency.patch`)

`navigator.hardwareConcurrency` from `hardware.concurrency`. Hook: `core/frame/navigator_concurrent_hardware.cc`, the mixin shared by `Navigator` and `WorkerNavigator`.

### Device memory (`hardware/device-memory.patch`)

`navigator.deviceMemory` and the `Device-Memory` and `Sec-CH-Device-Memory` headers. Hook: `third_party/blink/common/device_memory/approximated_device_memory.cc`.

- The value is computed independently in each process (renderer for JavaScript, browser for the header), so it is patched once in `blink/common` and fed from the profile everywhere.
- Valid values are powers of two inside the clamp in `CalculateAndSetApproximatedDeviceMemory` (2 to 32 GB on desktop at the pinned version). Read the clamp; it changes between releases.

### Storage quota (`hardware/storage-quota.patch`)

`navigator.storage.estimate()`, `webkitTemporaryStorage.queryUsageAndQuota` and `StorageBucket.estimate()` report the quota stock Chrome computes for a disk of `hardware.disk_total_bytes`. The enforced quota and eviction follow the same disk. Hooks: `storage/browser/quota/quota_device_info_helper.cc`, `quota_manager_impl.cc` (`CallGetVolumeInfo`).

- Two host-disk reads feed quota: the volume info behind the reported quota, and the total disk size behind the pool settings. Both must be patched.
- Not covered: blob storage limits in `storage/browser/blob/blob_memory_controller.cc` still use the host's disk.

### JS heap limit (`hardware/js-heap-limit.patch`)

`performance.memory.jsHeapSizeLimit`, and the real V8 heap limit behind it, follow `hardware.physical_memory_mb`. Hook: `gin/isolate_holder.cc` (`ResourceConstraints::ConfigureDefaults`).

- Below 8 GB of RAM the limit follows RAM. The input to V8 is patched, not the reported number, so the limit a page can fill is the one it is told.

## Identity

### User-Agent and Client Hints (`identity/user-agent.patch`)

UA string, `navigator.userAgent`, `appVersion`, `platform`, `navigator.userAgentData` (low and high entropy) and every `Sec-CH-UA*` header, from `os.*`. Hooks: the builders in `components/embedder_support/user_agent_utils.cc`, and `GetReducedNavigatorPlatform` in `core/execution_context/navigator_base.cc`.

- `GetUnifiedPlatform` and `GetReducedNavigatorPlatform` hardcode `X11; Linux x86_64` under `#if BUILDFLAG(IS_LINUX)` and need runtime branching on the profile.
- A Chromium-branded build emits GREASE plus `Chromium` only, which stock Chrome never does. The patch emits GREASE, `Chromium` and `Google Chrome` in Chrome's order for the build's major version.
- Every version string is the build's own Chromium version.

## Display

### Screen and window (`display/screen-window.patch`)

`screen.*`, `devicePixelRatio`, `isExtended`, orientation, the `resolution` and `device-width` media queries, and `window.outer*` and `screenX/Y`, from `display.*`. Hooks: `RenderWidgetHostViewBase::GetNewScreenInfosForUpdate` (the one browser-side `ScreenInfo` source), `RenderWidgetHostImpl` (`SendScreenRects`, `GetVisualProperties`), `window_sizer.cc`, and the device scale factor and headless screen set from `BasicStartupComplete`.

- Headless mode defaults to an 800x600 screen and is initialized before the profile loads; the profile's screen is applied later and wins.
- `GetVisualProperties` clamps bit depth to 24 whenever hardware acceleration is off, which includes every headless and Xvfb run.
- A window origin that is not a whole physical pixel at a fractional DPR makes the viewport 2 px wider than its outer size, so the first window is placed on a whole pixel.

### Media queries (`display/media-queries.patch`)

`pointer`, `hover`, `any-*`, `navigator.maxTouchPoints`, `prefers-color-scheme`, `prefers-reduced-motion`, `prefers-contrast`, `forced-colors`, `color-gamut` and `dynamic-range`, and the `Sec-CH-Prefers-*` hints. Hooks: `slow_web_preference_cache.cc`, `web_contents_impl.cc` (`ComputeWebPreferences`), `chrome_content_browser_client.cc` (contrast, forced colors, color scheme), `render_widget_host_impl.cc` (`display_color_spaces`).

- Blink evaluates media queries from `WebPreferences` and `ScreenInfo`, so only browser-side sources are patched.
- Stock headless has no input devices and reports pointer and hover `none`. Without hardware acceleration `GetVisualProperties` strips HDR.
- Blink reports every HDR screen as `p3`. On Windows, `prefers-contrast: more` exists only with forced colors; the profile validation refuses other combinations.

### System colors (`display/system-colors.patch`)

The CSS system color keywords for the profile's color scheme come from `system_colors`; the other scheme keeps Chromium's set. Hooks: `LayoutTheme::SystemColorFromColorProvider`, `renderer_preferences_util.cc` (`UpdateFromSystemSettings`), `theme_helper.cc`.

- The host desktop leaks through `RendererPreferences`, not the color mixers: with the GTK or Qt theme, which is the default on most desktops, the selection and focus ring colors are copied from the desktop. The patch stops that.
- OffscreenCanvas `fillStyle` resolves keywords from a fixed table with no color provider; stock differs there too, and that difference stays.

## Graphics

### GPU info (`graphics/gpu-info.patch`)

The GPU identity outside the WebGL and WebGPU APIs: `webglcontextcreationerror.statusMessage`, `chrome://gpu` and CDP `SystemInfo.getInfo`. Hooks: `RendererBlinkPlatformImpl::CollectWebGLContextInfo`, `gpu_internals_ui.cc`, `system_info_handler.cc`.

- Report-only: GPU collection, blocklists, workarounds, ANGLE and VA-API keep using the host's GPU.
- Identity fields no page reads (PCI ids, driver, GL strings) are optional in the profile. Missing ones are derived at load from `gpu.webgl` in the real ANGLE formats for the claimed backend, or reported empty; never the host's.

### WebGL (`graphics/webgl.patch`)

Every WebGL1 and WebGL2 context, in windows and in worker OffscreenCanvas: unmasked vendor and renderer, every cached limit, shader precision formats, and the extension list, from `gpu.webgl` and `gpu.webgl2`. Hooks: the passthrough decoder in the GPU process (`gles2_cmd_decoder_passthrough.cc`, `..._doers.cc`), and `ExtensionSupportedAndAllowed` in `webgl_rendering_context_base.cc`.

- `UNMASKED_RENDERER_WEBGL` does not read `GPUInfo`. Blink asks the command buffer for `GL_RENDERER`, which the GPU process passes to the driver. Patching the GPU process covers every renderer and OffscreenCanvas at once, and Blink's own checks then enforce the claimed limits.
- Renderer strings use the real ANGLE format for the claimed OS and backend. Extensions and limits come from captured data for that GPU, driver and backend. Never hand-edit them.
- A limit or extension the host cannot back is logged as unbacked.

### WebGL fallback (`graphics/webgl-fallback.patch`)

On a host without a usable GPU (containers, VMs, Xvfb, `--disable-gpu`) WebGL runs on SwiftShader instead of offering no context. Hook: the default of `prefs::kEnableUnsafeSwiftShader` in `ChromeContentBrowserClient::RegisterLocalStatePrefs`.

- The switch only allows the fallback; it never replaces a working GPU. Forcing `--use-angle=swiftshader` would.

### WebGL performance caveat (`graphics/webgl-caveat.patch`)

With a profile claiming a hardware backend, `failIfMajorPerformanceCaveat: true` returns a context on the SwiftShader fallback instead of null. Hook: `GLES2CommandBufferStub::Initialize`.

### Headless GPU (`graphics/headless-gpu.patch`)

With a profile, headless mode does not append `--use-angle=swiftshader-webgl`, so it renders on the host GPU as headful does, and falls back to SwiftShader as stock does when there is none. Hook: `HeadlessModeHandleImpl::SetUpCommandLine`.

- This runs in `ChromeMain` before the profile is loaded, so it tests the switch rather than `phasmid::Profile`.
- Headless has no hardware WebGPU adapter even on a host GPU; the claimed adapter is backed by the SwiftShader fallback there.

### WebGPU (`graphics/webgpu.patch`)

`requestAdapter()` resolves to the profile's adapter in every context: `GPUAdapterInfo`, features and all limits from `gpu.webgpu`. Hooks: `webgpu_decoder_impl.cc` in the GPU process, and `gpu.cc` and `gpu_adapter.cc` in Blink.

- Values come from Dawn in the GPU process, not from `GPUInfo`.
- Without a hardware adapter the SwiftShader Vulkan fallback backs the claim, allowed past the CPU-adapter blocklist only when a profile claims an adapter. `--enable-unsafe-webgpu` would also expose unsafe features. The renderer-side adapter type stays CPU, which the swap buffer provider needs.
- Features and limits the backing adapter lacks are listed but refused at `requestDevice()` with stock errors.

### Hardware video decode (`graphics/hardware-decode.patch`)

The GPU process offers the decoder configs of the claimed codecs, so HEVC `canPlayType`, `isTypeSupported` and `decodingInfo`, and main-thread `powerEfficient`, follow `gpu.hardware_decode`. Hooks: `GpuMojoMediaClient::GetSupportedVideoDecoderConfigs`, `RenderMediaClient::SetGpuFeatureInfoInternal`, `MediaCapabilities::GetGpuFactoriesSupport`.

- Stock purges the HEVC cache when accelerated decode is off (headless, software GL); the patch keeps the claimed configs.
- A claimed codec decodes with the host's VA-API if present, else in software. HEVC has no software decoder and fails to play; this is logged as unbacked.

### Canvas readback (`graphics/canvas-readback.patch`)

With `canvas.noise`, `getImageData`, `toDataURL`, `toBlob` and `convertToBlob` apply one local, idempotent, seed-keyed change: one low bit of a pixel that blends its neighbours, only where anti-aliased or blending rasterization wrote. Nothing on screen changes. Hooks: the four readback exits in `html_canvas_element.cc`, `offscreen_canvas.cc` and `base_rendering_context_2d.cc`, and write tracking in `canvas_2d_recorder_context.cc`.

- Hook the exits, not the snapshot: `GetImage()` and `Snapshot()` also feed `drawImage`, `createImageBitmap`, WebGL uploads and on-screen painting.
- Exact writes (`putImageData`, pixel-aligned solid fills, 1:1 image draws) read back exactly. The same drawing read twice, or as sub-rectangles, gives the same answer.

## Audio

### Web Audio (`audio/webaudio.patch`)

`AudioContext` `sampleRate`, `baseLatency`, `outputLatency` and `destination.maxChannelCount` from `audio.*`; the sink opens at those parameters and the audio service resamples. With `audio.noise`, the `DynamicsCompressor` applies two seed-chosen gain offsets of 1 to 4 ULP, so offline and realtime output, analysers and `getChannelData` agree and silence stays zero. Hooks: `renderer_webaudiodevice_impl.cc`, `platform/audio/dynamics_compressor.cc`.

- A Windows profile follows WASAPI's buffer size rules.

## Fonts

### Font availability (`fonts/availability.patch`)

Only font files that publish a family in `fonts.families` are reachable: CSS, per-character and emoji fallback, `local()`, `queryLocalFonts()` and canvas. Hooks: `ExportFontAllowlist` in `components/phasmid/font_allowlist_linux.cc` (called from `BasicStartupComplete`), the browser's family matchers (`FontDataServiceImpl`, `FontServiceApp`), and the generic family defaults in `prefs_tab_helper.cc`.

- On Linux every font lookup ends in fontconfig in the browser process; the renderer only forwards. The patch writes a fontconfig file that rejects every other file by path, checks that it loads and reaches exactly the kept files, and exports it as `FONTCONFIG_FILE`.
- fontconfig answers a configuration it cannot load with a fallback that reaches every installed font, so the browser loads the file itself before trusting it. `<glob>` rejection runs before pattern rules.
- A Windows profile gets a standalone configuration over a local Windows font store (`--phasmid-windows-fonts`), keeping only files whose SHA-256 matches the store's manifest, with Windows render settings. Only listed names resolve, so `sans`, `serif`, `emoji` and Linux families fail as on DirectWrite. The font files are imported by the user; none are distributed.
- Never remap a claimed font to a different host font; the metrics would contradict the name.

### Font metrics (`fonts/metrics.patch`)

For a Windows profile, text is measured as DirectWrite measures it: OS/2 win (or typo) ascent and descent with no VDMX, the GDI line gap, average width as the advance of `x`, linear advances with subpixel positioning, GDI-classic integer advances for faces with an embedded bitmap strike at the size, and synthetic bold from weight 600. Hooks: `font_metrics.cc`, `simple_font_data.cc`, `font_platform_data.cc`, `harfbuzz_face.cc`, `font_cache_skia.cc`, and a new `fonts/windows_text_metrics.cc`.

- No noise on text metrics: a width no real installation produces is itself a fingerprint.

### Generic families (`fonts/generic-families.patch`)

A Windows profile registers Windows Chrome's font defaults (Times New Roman, Arial, Consolas, per-script entries and minimum font size) by the UI locale, the first of `locale.languages`. Hook: `PrefsTabHelper::RegisterProfilePrefs`.

### System font (`fonts/system-font.patch`)

For a Windows profile, `system-ui` is the Windows menu font (Segoe UI for English), and `menu`, `small-caption` and `status-bar` use it at 12 px. Hooks: `RenderViewHostImpl::GetPlatformSpecificPrefs`, `layout_theme_font_provider_default.cc`.

### Windows fallback (`fonts/windows-fallback.patch`)

Per-character fallback for a Windows profile runs a Linux-compiled copy of Windows Chrome's fallback tables (emoji, Unicode blocks, Han by locale) before fontconfig, and maps Courier, MS Sans Serif, MS Serif and Times as Windows does. Hooks: `font_cache_linux.cc` `PlatformFallbackFontForCharacter`, new `fonts/linux/windows_fallback.cc`, `font_cache.cc`.

- Scripts outside Windows' tables fall back through fontconfig over the store instead of DirectWrite's system fallback.

### Family names (`fonts/family-names.patch`)

With a Windows font store, a CSS family name resolves to the face DirectWrite would pick: weight-stretch-style family names, DirectWrite's face matching, and Windows' one-time retry of `<family> <weight or stretch word>`. Hooks: `FontServiceApp::MatchFamilyName` (a global `SkFontConfigInterface` built from the store), `font_cache_skia.cc` `CreateFontPlatformData`.

- `FontDescription::SkiaFontStyle` maps every condensed stretch to semi-condensed, on Windows too; this is kept.

## Locale

### Language (`locale/accept-language.patch`)

`Accept-Language`, `navigator.language(s)` and the default `Intl` locale. The header is built from `locale.languages` as stock builds it from the `intl.accept_languages` pref; `navigator.languages` is the same list. Hooks: `ComputeAcceptLanguage` in `profile_network_context_service.cc`, `UpdateFromSystemSettings`, `GetAcceptLangs`, the prefetch service delegate.

- On Linux `--lang` does not set the ICU default in renderers: they fork from a zygote that chose its locale from the host environment. The `Intl` locale is set explicitly.

### Geolocation (`locale/geolocation.patch`)

A granted page gets `locale.geolocation` from a location provider that behaves like the one stock Chrome runs on the claimed OS: the first fix 200 to 300 ms after start (fixed per profile), timestamps and re-reports as the Linux network provider or the Windows WinRT provider produce them. Hook: `LocationProviderManager::InitializeProvider`, new `profile_location_provider.cc`.

- Permissions, `Emulation.setGeolocationOverride` and Blink's `maximumAge`, timeouts and watches stay stock.
- A CDP grant (`Browser.grantPermissions`) does not opt the user into location services, so neither stock nor Phasmid reports a fix after it. Grant through the content setting.
- Without the field, the build's network provider runs and fails with `POSITION_UNAVAILABLE`.

## Network

### WebRTC (`network/webrtc.patch`)

The IP handling policy comes from `network.webrtc.ip_handling_policy`. With `network.webrtc.public_ip`, the mapped address in STUN binding responses is replaced, so server-reflexive candidates carry the profile's address. Hooks: `UpdateFromSystemSettings`, `P2PSocketUdp::HandleReadResult`.

- Host candidates stay stock (mDNS names). ICE checks and TURN are untouched; WebRTC keeps working.

### Network Information (`network/connection.patch`)

`navigator.connection` `effectiveType`, `rtt` and `downlink` in windows and workers, and the `ECT`, `RTT` and `Downlink` hints, from `network.connection`. Hooks: `NetworkQualityTracker`, and `GetRandomMultiplier` in `client_hints.cc` and `network_state_notifier.cc`.

- Stock applies per-host noise twice with different salts, so its header and JavaScript values can disagree. The patch keys both on the profile seed, so they agree for every host.

## Platform capabilities

### Speech voices (`platform/speech-voices.patch`)

`speechSynthesis.getVoices()` lists `speech.voices` in order. Hooks: `TtsControllerImpl::GetVoices`, the `TtsPlatformImplLinux` constructor.

- Google Chrome's voices come from a component extension that synthesizes on Google's servers with Chrome's API key. This build has neither. The voices are listed under that engine without loading it, so `speak()` behaves as Google Chrome does with the server unreachable: no events until `cancel()`.
- The host's speech-dispatcher is never used under a profile.

### Media devices (`platform/media-devices.patch`)

`enumerateDevices()` lists the profile's devices, with the virtual entries the claimed OS's audio stack adds first: "Default" on Linux, "Default - ..." and "Communications - ..." on Windows. Device IDs are stable per profile and origin. Hooks: `MediaDevicesManager::DevicesEnumerated`, `GetMediaDeviceSaltAndOrigin` in `media_devices_util.cc`.

- Pre-permission blanking and the per-origin HMAC stay stock.
- A claimed device opens no host device: capture rejects with `NotReadableError`, as stock does for a camera in use. The default audio input still opens the host's microphone.

### Widevine (`platform/widevine.patch`)

Widevine is the installed Google Chrome CDM (`--phasmid-widevine-cdm`) or absent, as the profile says. Nothing is bundled or downloaded. Hook: `component_widevine_cdm_hint_file_linux.cc`.

- The CDM names Linux in every license request, so a Windows profile's Widevine claim is logged as unbacked and Widevine is absent.

### WebAuthn (`platform/webauthn.patch`, `platform/webauthn-hybrid.patch`)

`isUserVerifyingPlatformAuthenticatorAvailable()` and `getClientCapabilities()` follow `webauthn.*`, through the delegate override Chrome uses for Google Password Manager passkeys, so conditional mediation and ceremony transports follow too. `hybridTransport` follows `capabilities.bluetooth_available` instead of the host's BlueZ. A Windows profile enables Secure Payment Confirmation, as Windows Chrome does. Hooks: `ChromeWebAuthenticationDelegate::IsUserVerifyingPlatformAuthenticatorAvailableOverride`, `RegisterFeatureOverrides`, `AuthenticatorCommonImpl::IsHybridTransportSupported`.

- A claimed authenticator is reported but cannot complete a ceremony; this is logged as unbacked.

### Bluetooth and Web Share (`platform/bluetooth-share.patch`)

A Windows profile gets `navigator.bluetooth`, `navigator.share` and `canShare`, as Windows Chrome always has them; a Linux profile has neither, as Linux Chrome. `getAvailability()` follows `capabilities.bluetooth_available`. Hooks: `ChromeContentRendererClient::SetRuntimeFeaturesDefaultsBeforeBlinkInitialization`, `ShareServiceImpl` (built on Linux), `WebBluetoothServiceImpl::GetAvailability`.

- A frame asking for an interface with no binder is killed, so `ShareServiceImpl` must be bound on Linux. With no share sheet, `share()` ends as a dismissed sheet does: `AbortError`.

### Battery (`platform/battery.patch`)

`navigator.getBattery()` resolves with `capabilities.battery`, with the charging and discharging times both OSes report when there is no OS estimate. No battery event fires. Hook: `BatteryStatusService::AddCallback`.

- ungoogled-chromium removes the platform battery manager, so every host otherwise reports "no battery".

## Automation

### Automation signals (`automation/signals.patch`)

`navigator.webdriver` is false and the "controlled by automated test software" infobar is not shown under `--enable-automation`, `--headless` and remote debugging. Hooks: `SetRuntimeFeaturesFromCommandLine` in `content/child/runtime_features.cc`, `AddInfoBarsIfNecessary`.

### CDP attachment (`automation/cdp-stealth.patch`)

Describing values for a CDP client (console arguments, exceptions, previews) runs no page code and changes nothing a page can read later. Hooks: `v8/src/inspector/value-mirror.cc`.

- `Runtime.enable`, sent by Playwright on attach, otherwise lets a page detect the client through getters and `stack` accessors that the inspector reads. The protocol itself is unchanged, so automation clients need no changes.

## DevTools

### Fingerprint inspector (`devtools/fingerprint-trace.patch`, `devtools/fingerprint-trace-intl.patch`)

Trace events for allowlisted Web API calls, CSS media features, font lookups and `Intl` `resolvedOptions()`, emitted only when a tracing session enables `disabled-by-default-phasmid.fingerprint`. Nothing is page-visible. See [inspector.md](inspector.md).
