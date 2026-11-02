"""Compare a dump against a profile: every field, every context it applies to.

An expectation says which profile value must appear at which dump key in
which contexts. ``WINDOW`` contexts have a document; ``ALL`` includes the
workers. Header expectations compare the recorded request headers.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable

ALL = ("main", "iframe_same", "iframe_cross", "worker", "shared_worker", "service_worker")
WINDOW = ("main", "iframe_same", "iframe_cross")
MAIN = ("main",)


def _get(obj: Any, path: "str | tuple[str, ...]") -> Any:
    """Dotted path lookup. A tuple of segments is used where a key itself
    contains dots (MIME type strings)."""
    cur = obj
    parts = path if isinstance(path, tuple) else path.split(".")
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else _MISSING
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


@dataclass
class Expectation:
    profile_path: str
    dump_path: "str | tuple[str, ...]"
    contexts: tuple[str, ...] = ALL
    transform: Callable[[Any, dict], Any] | None = None  # (profile_value, profile) -> expected dump value
    note: str = ""


def reported_quota_headroom(disk_total_bytes: int) -> int:
    """estimate() quota - usage for a disk of that size (CalculateReportedQuota
    in storage/browser/quota/quota_manager_impl.cc): 10 GiB from a 10 GiB
    disk up, else the disk rounded up to a whole GiB."""
    gib = 1 << 30
    return 10 * gib if disk_total_bytes >= 10 * gib else -(-disk_total_bytes // gib) * gib


def accept_language(langs: list[str]) -> str:
    out = []
    for i, lang in enumerate(langs):
        out.append(lang if i == 0 else f"{lang};q={max(0.1, round(1.0 - 0.1 * i, 1))}")
    return ",".join(out)


# Stock Chrome reports an RTT or downlink estimate scaled by a per-host factor
# 0.90 + n * 0.01 (n in 0..20, from a salt the page cannot know), then
# rounded: NetworkStateNotifier::RoundRtt/RoundMbps in the renderer,
# RoundRtt/RoundKbpsToMbps in content/browser/client_hints/client_hints.cc.
NOISE_FACTORS = [0.9 + float(n) * 0.01 for n in range(21)]


def _round_half_away(x: float) -> int:
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def reported_rtt(rtt_ms: int, factor: float) -> int:
    us = min(int(float(rtt_ms * 1000) * factor), 3_000_000)  # TimeDelta * double truncates; 3 s cap
    return (us + 25_000) // 50_000 * 50  # RoundToMultiple(50 ms)


def reported_downlink(kbps: int, factor: float, *, renderer: bool = True) -> float:
    # The renderer holds Mbps (kbps / 1000) and multiplies back; the browser
    # starts from integer kbps.
    x = (kbps / 1000) * 1000 if renderer else float(kbps)
    return _round_half_away(min(x * factor, 10_000.0) / 50) * 50 / 1000


EXPECTATIONS: list[Expectation] = [
    # OS and identity
    Expectation("os.navigator_platform", "navigator.platform"),
    Expectation("os.ch_platform", "navigator.userAgentData.platform"),
    Expectation("os.ch_platform_version", "navigator.userAgentData.highEntropy.platformVersion"),
    Expectation("os.architecture", "navigator.userAgentData.highEntropy.architecture"),
    Expectation("os.bitness", "navigator.userAgentData.highEntropy.bitness"),
    Expectation("os.wow64", "navigator.userAgentData.highEntropy.wow64"),
    Expectation("os.model", "navigator.userAgentData.highEntropy.model"),
    Expectation("os.form_factors", "navigator.userAgentData.highEntropy.formFactors"),
    Expectation("os.ua_platform_token", "navigator.userAgent", transform=lambda v, p: ("ua_platform", v), note="UA platform token"),
    # Hardware
    Expectation("hardware.concurrency", "navigator.hardwareConcurrency"),
    Expectation("hardware.device_memory", "navigator.deviceMemory"),
    Expectation("hardware.max_touch_points", "navigator.maxTouchPoints", WINDOW),
    Expectation("hardware.disk_total_bytes", "storage", transform=lambda v, _p: ("quota_headroom", reported_quota_headroom(v)), note="estimate() quota - usage"),
    Expectation("hardware.js_heap_size_limit", "storage.jsHeapSizeLimit", WINDOW),
    # Display
    Expectation("display.width", "screen.width", WINDOW),
    Expectation("display.height", "screen.height", WINDOW),
    Expectation("display.avail_width", "screen.availWidth", WINDOW),
    Expectation("display.avail_height", "screen.availHeight", WINDOW),
    Expectation("display.avail_left", "screen.availLeft", WINDOW),
    Expectation("display.avail_top", "screen.availTop", WINDOW),
    Expectation("display.color_depth", "screen.colorDepth", WINDOW),
    Expectation("display.color_depth", "screen.pixelDepth", WINDOW),
    Expectation("display.device_pixel_ratio", "screen.devicePixelRatio", WINDOW),
    Expectation("display.device_pixel_ratio", "mediaQueries.resolutionDppx", WINDOW),
    Expectation("display.is_extended", "screen.isExtended", MAIN),
    Expectation("display.orientation.type", "screen.orientation.type", WINDOW),
    Expectation("display.window.outer_width", "screen.outerWidth", MAIN),
    Expectation("display.window.outer_height", "screen.outerHeight", MAIN),
    Expectation("display.pointer.primary", "mediaQueries.pointerFine", WINDOW, transform=lambda v, _p: v == "fine"),
    Expectation("display.pointer.primary", "mediaQueries.pointerCoarse", WINDOW, transform=lambda v, _p: v == "coarse"),
    Expectation("display.pointer.primary", "mediaQueries.pointerNone", WINDOW, transform=lambda v, _p: v == "none"),
    Expectation("display.pointer.hover", "mediaQueries.hoverHover", WINDOW, transform=lambda v, _p: v == "hover"),
    Expectation("display.pointer.any_pointer", "mediaQueries.anyPointerFine", WINDOW, transform=lambda v, _p: "fine" in v),
    Expectation("display.pointer.any_pointer", "mediaQueries.anyPointerCoarse", WINDOW, transform=lambda v, _p: "coarse" in v),
    Expectation("display.pointer.any_hover", "mediaQueries.anyHoverHover", WINDOW, transform=lambda v, _p: "hover" in v),
    Expectation("display.pointer.hover", "mediaQueries.hoverNone", WINDOW, transform=lambda v, _p: v == "none"),
    Expectation("display.pointer.any_pointer", "mediaQueries.anyPointerNone", WINDOW, transform=lambda v, _p: not v),
    Expectation("display.pointer.any_hover", "mediaQueries.anyHoverNone", WINDOW, transform=lambda v, _p: not v),
    Expectation("display.color_gamut", "mediaQueries.colorGamutP3", WINDOW, transform=lambda v, _p: v in ("p3", "rec2020")),
    Expectation("display.color_gamut", "mediaQueries.colorGamutRec2020", WINDOW, transform=lambda v, _p: v == "rec2020"),
    Expectation("display.dynamic_range", "mediaQueries.dynamicRangeHigh", WINDOW, transform=lambda v, _p: v == "high"),
    Expectation("display.prefers_color_scheme", "mediaQueries.prefersColorSchemeDark", WINDOW, transform=lambda v, _p: v == "dark"),
    Expectation("display.prefers_reduced_motion", "mediaQueries.prefersReducedMotion", WINDOW, transform=lambda v, _p: v == "reduce"),
    Expectation("display.prefers_contrast", "mediaQueries.prefersContrastMore", WINDOW, transform=lambda v, _p: v == "more"),
    Expectation("display.prefers_contrast", "mediaQueries.prefersContrastLess", WINDOW, transform=lambda v, _p: v == "less"),
    Expectation("display.forced_colors", "mediaQueries.forcedColorsActive", WINDOW, transform=lambda v, _p: v == "active"),
    # GPU: WebGL1 and WebGL2 in every context, including OffscreenCanvas in workers
    Expectation("gpu.webgl.vendor", "webgl.params.VENDOR"),
    Expectation("gpu.webgl.renderer", "webgl.params.RENDERER"),
    Expectation("gpu.webgl.unmasked_vendor", "webgl.unmaskedVendor"),
    Expectation("gpu.webgl.unmasked_renderer", "webgl.unmaskedRenderer"),
    Expectation("gpu.webgl.version", "webgl.params.VERSION"),
    Expectation("gpu.webgl.shading_language_version", "webgl.params.SHADING_LANGUAGE_VERSION"),
    Expectation("gpu.webgl.extensions", "webgl.extensions"),
    Expectation("gpu.webgl.params", "webgl.params", transform=lambda v, _p: ("subset", v)),
    Expectation("gpu.webgl.precision", "webgl.precision"),
    Expectation("gpu.webgl.max_anisotropy", "webgl.maxAnisotropy"),
    # powerPreference differs by context in stock Chrome; the profile stores main's.
    Expectation("gpu.webgl.context_attributes", "webgl.contextAttributes", MAIN, transform=lambda v, _p: ("subset", v)),
    Expectation("gpu.webgl2.unmasked_vendor", "webgl2.unmaskedVendor"),
    Expectation("gpu.webgl2.unmasked_renderer", "webgl2.unmaskedRenderer"),
    Expectation("gpu.webgl2.version", "webgl2.params.VERSION"),
    Expectation("gpu.webgl2.shading_language_version", "webgl2.params.SHADING_LANGUAGE_VERSION"),
    Expectation("gpu.webgl2.extensions", "webgl2.extensions"),
    Expectation("gpu.webgl2.params", "webgl2.params", transform=lambda v, _p: ("subset", v)),
    Expectation("gpu.webgl2.precision", "webgl2.precision"),
    Expectation("gpu.webgl2.max_anisotropy", "webgl2.maxAnisotropy"),
    Expectation("gpu.webgl2.context_attributes", "webgl2.contextAttributes", MAIN, transform=lambda v, _p: ("subset", v)),
    Expectation("gpu.webgpu.available", "webgpu.adapter", transform=lambda v, _p: ("present", v)),
    Expectation("gpu.webgpu.vendor", "webgpu.adapter.vendor"),
    Expectation("gpu.webgpu.architecture", "webgpu.adapter.architecture"),
    Expectation("gpu.webgpu.subgroup_min_size", "webgpu.adapter.subgroupMinSize"),
    Expectation("gpu.webgpu.subgroup_max_size", "webgpu.adapter.subgroupMaxSize"),
    Expectation("gpu.webgpu.is_fallback_adapter", "webgpu.adapter.isFallbackAdapter"),
    Expectation("gpu.webgpu.features", "webgpu.adapter.features"),
    Expectation("gpu.webgpu.limits", "webgpu.adapter.limits"),
    Expectation("gpu.webgpu.preferred_canvas_format", "webgpu.preferredCanvasFormat"),
    # gpu.webgpu.wgsl_language_features is not compared: the list belongs to the
    # build's WGSL compiler, so it is judged against stock of the same major.
    Expectation("gpu.hardware_decode.h264", "codecs.decodingInfo.h264_1080p.powerEfficient", MAIN),
    Expectation("gpu.hardware_decode.hevc", "codecs.decodingInfo.hevc_1080p.powerEfficient", MAIN),
    Expectation("gpu.hardware_decode.vp9", "codecs.decodingInfo.vp9_4k.powerEfficient", MAIN),
    Expectation("gpu.hardware_decode.av1", "codecs.decodingInfo.av1_1080p.powerEfficient", MAIN),
    # Audio
    Expectation("audio.sample_rate", "audio.sampleRate", WINDOW),
    Expectation("audio.base_latency", "audio.baseLatency", WINDOW),
    Expectation("audio.output_latency", "audio.outputLatency", WINDOW),
    Expectation("audio.max_channel_count", "audio.maxChannelCount", WINDOW),
    # Fonts: presence list is an allowlist; every present font must be allowed and vice versa for the probed set.
    Expectation("fonts.families", "fonts.present", WINDOW, transform=lambda v, _p: ("fontset", v)),
    Expectation("fonts.families", "fontsLocal.present", ALL, transform=lambda v, _p: ("fontset_if_probed", v), note="local() hits are installed faces, so each must be a listed family"),
    Expectation("fonts.ui_font.family", "fonts.systemFontKeywords.caption.fontFamily", WINDOW),
    # Locale and time
    Expectation("locale.languages", "navigator.languages"),
    Expectation("locale.languages", "navigator.language", transform=lambda v, _p: v[0]),
    Expectation("locale.timezone", "intl.dateTimeFormat.timeZone"),
    Expectation("locale.languages", "intl.dateTimeFormat.locale", transform=lambda v, p: p.get("locale", {}).get("intl_locale") or v[0]),
    # Platform capabilities
    Expectation("speech.voices", "speech.voices", WINDOW, transform=lambda v, _p: [{"name": x["name"], "lang": x["lang"], "voiceURI": x["voice_uri"], "localService": x["local_service"], "default": x["default"]} for x in v]),
    # The dump has no capture permission: stock lists one blank entry per kind that has a device.
    Expectation("media_devices.audioinput", "mediaDevices.counts.audioinput", WINDOW, transform=lambda v, _p: min(v, 1) if v else _MISSING),
    Expectation("media_devices.audiooutput", "mediaDevices.counts.audiooutput", WINDOW, transform=lambda v, _p: min(v, 1) if v else _MISSING),
    Expectation("media_devices.videoinput", "mediaDevices.counts.videoinput", WINDOW, transform=lambda v, _p: min(v, 1) if v else _MISSING),
    Expectation("media.widevine.supported", "codecs.keySystems.widevine.supported", MAIN),
    Expectation("media.hevc", ("codecs", "canPlayType", 'video/mp4; codecs="hev1.1.6.L93.B0"'), MAIN, transform=lambda v, _p: ("nonempty", v)),
    Expectation("media.dolby_audio", ("codecs", "canPlayType", 'video/mp4; codecs="ac-3"'), MAIN, transform=lambda v, _p: ("nonempty", v)),
    Expectation("media.playready", "codecs.keySystems.playready.supported", MAIN),
    Expectation("webauthn.user_verifying_platform_authenticator", "webauthn.isUserVerifyingPlatformAuthenticatorAvailable", MAIN),
    Expectation("webauthn.conditional_mediation", "webauthn.isConditionalMediationAvailable", MAIN),
    # hybridTransport is whether a Bluetooth LE adapter is present
    # (AuthenticatorCommonImpl::IsHybridTransportSupported), on every OS.
    Expectation("capabilities.bluetooth_available", "webauthn.getClientCapabilities.hybridTransport", WINDOW),
    Expectation("capabilities.bluetooth", "navigator.has.bluetooth", WINDOW),
    Expectation("capabilities.web_share", "navigator.has.share", WINDOW),
    Expectation("capabilities.battery.charging", "navigator.battery.charging", WINDOW),
    Expectation("capabilities.battery.level", "navigator.battery.level", WINDOW),
    # No UPower estimate: a full battery (or none) charges in 0 s, anything else in Infinity.
    Expectation("capabilities.battery", "navigator.battery.chargingTime", WINDOW, transform=lambda v, _p: 0 if v["charging"] and v["level"] == 1 else math.inf),
    Expectation("capabilities.battery", "navigator.battery.dischargingTime", WINDOW, transform=lambda v, _p: math.inf),
    # Network quality: the type exactly; rtt and downlink within stock's noise
    # (the guard checks the seeded value exactly).
    Expectation("network.connection.effective_type", "navigator.connection.effectiveType"),
    Expectation("network.connection.rtt", "navigator.connection.rtt", transform=lambda v, _p: ("one_of", {reported_rtt(v, f) for f in NOISE_FACTORS})),
    Expectation("network.connection.downlink", "navigator.connection.downlink", transform=lambda v, _p: ("one_of", {reported_downlink(round(v * 1000), f) for f in NOISE_FACTORS})),
    Expectation("system_colors", "systemColors", WINDOW, transform=lambda v, _p: ("subset", v)),
    # Automation
    Expectation("automation.report_headful", "navigator.userAgent", transform=lambda v, _p: ("no_headless_token", v)),
]

# Invariants that hold for every profile: they come from the build, not the
# device. Headless matching headful is covered by the expectations above (no
# HeadlessChrome token; display, pointer and GPU hold in headless as in headful).
def _brand_names(brands):
    return sorted(b.get("brand") for b in (brands or []) if isinstance(b, dict))


def _check_brands(dump_ctx: dict) -> tuple[bool, str]:
    """Stock Chrome's brand list is GREASE + Chromium + Google Chrome."""
    ua = (dump_ctx.get("navigator") or {}).get("userAgentData") or {}
    names = _brand_names(ua.get("brands"))
    if not names:
        return False, "no userAgentData.brands"
    missing = [b for b in ("Google Chrome", "Chromium") if b not in names]
    grease = [b for b in names if b not in ("Google Chrome", "Chromium")]
    if missing or len(grease) != 1 or len(names) != 3:
        return False, f"brands {names}: missing {missing}, GREASE {grease}"
    return True, ""


INVARIANTS = [
    ("brands: GREASE + Chromium + Google Chrome", ALL, _check_brands),
]

HEADER_EXPECTATIONS = [
    # (profile path, header, slots, transform)
    ("os.ua_platform_token", "user-agent", ("navigation", "fetch_main", "fetch_worker", "fetch_service_worker"), lambda v, p: ("ua_platform", v)),
    ("locale.languages", "accept-language", ("navigation", "fetch_main", "fetch_worker", "fetch_service_worker"), lambda v, p: accept_language(v)),
    ("os.ch_platform", "sec-ch-ua-platform", ("navigation", "fetch_main"), lambda v, p: f'"{v}"'),
    ("os.ch_platform_version", "sec-ch-ua-platform-version", ("navigation_with_hints", "fetch_main"), lambda v, p: f'"{v}"'),
    ("os.architecture", "sec-ch-ua-arch", ("navigation_with_hints", "fetch_main"), lambda v, p: f'"{v}"'),
    ("os.bitness", "sec-ch-ua-bitness", ("navigation_with_hints", "fetch_main"), lambda v, p: f'"{v}"'),
    ("os.model", "sec-ch-ua-model", ("navigation_with_hints", "fetch_main"), lambda v, p: f'"{v}"'),
    ("os.wow64", "sec-ch-ua-wow64", ("navigation_with_hints", "fetch_main"), lambda v, p: "?1" if v else "?0"),
    ("hardware.device_memory", "sec-ch-device-memory", ("navigation_with_hints", "fetch_main"), lambda v, p: f"{v:g}"),
    ("hardware.device_memory", "device-memory", ("navigation_with_hints", "fetch_main"), lambda v, p: f"{v:g}"),
    ("display.device_pixel_ratio", "sec-ch-dpr", ("navigation_with_hints",), lambda v, p: f"{v:g}"),
    ("display.prefers_color_scheme", "sec-ch-prefers-color-scheme", ("navigation_with_hints",), lambda v, p: v),
    ("display.prefers_reduced_motion", "sec-ch-prefers-reduced-motion", ("navigation_with_hints",), lambda v, p: v),
    ("os.form_factors", "sec-ch-ua-form-factors", ("navigation_with_hints",), lambda v, p: ", ".join(f'"{f}"' for f in v)),
    ("network.connection.effective_type", "ect", ("navigation_with_hints", "fetch_main"), lambda v, p: v),
    ("automation.report_headful", "user-agent", ("navigation", "fetch_main"), lambda v, p: ("no_headless_token", v)),
]

_UA_PLATFORM_RE = re.compile(r"^Mozilla/5\.0 \(([^)]*)\)")


def _judge(expected: Any, actual: Any) -> tuple[bool, str]:
    """Special expectation encodings return (ok, detail)."""
    if isinstance(expected, tuple) and len(expected) == 2 and isinstance(expected[0], str):
        kind, val = expected
        if kind == "ua_platform":
            m = _UA_PLATFORM_RE.match(actual or "")
            got = m.group(1) if m else None
            return got == val, f"platform token {got!r} != {val!r}" if got != val else ""
        if kind == "no_headless_token":
            if not val:
                return True, ""
            bad = "HeadlessChrome" in (actual or "")
            return not bad, "UA contains HeadlessChrome" if bad else ""
        if kind == "subset":
            if not isinstance(actual, dict):
                return False, f"expected object, got {type(actual).__name__}"
            diffs = {k: (v, actual.get(k, _MISSING)) for k, v in val.items() if actual.get(k, _MISSING) != v}
            return not diffs, "; ".join(f"{k}: {a!r} != {b!r}" for k, (a, b) in list(diffs.items())[:5]) + (f" (+{len(diffs) - 5} more)" if len(diffs) > 5 else "")
        if kind == "fontset":
            if not isinstance(actual, list):
                return False, "no font list"
            allowed = set(val)
            present = set(actual)
            extra = sorted(present - allowed)
            # Only families the probe knows about can be judged missing.
            return not extra, f"present but not in profile: {extra[:8]}" if extra else ""
        if kind == "fontset_if_probed":
            # Dumps older than the probe section carry no list to judge.
            return (True, "") if actual is _MISSING else _judge(("fontset", val), actual)
        if kind == "present":
            present = actual is not _MISSING and actual is not None
            return present == bool(val), f"adapter present={present}, profile says {val}"
        if kind == "quota_headroom":
            quota, usage = (actual.get("quota"), actual.get("usage")) if isinstance(actual, dict) else (None, None)
            if not isinstance(quota, int) or not isinstance(usage, int):
                return False, f"no estimate: {actual!r}"
            return quota - usage == val, f"quota - usage {quota - usage} != {val}" if quota - usage != val else ""
        if kind == "nonempty":
            nonempty = bool(actual)
            return nonempty == bool(val), f"canPlayType={actual!r}, profile says {val}"
        if kind == "one_of":
            return actual in val, "" if actual in val else f"{actual!r} not in {sorted(val)}"
    if expected is _MISSING:
        return True, ""
    return expected == actual, "" if expected == actual else f"{actual!r} != {expected!r}"


def _older_harness_loss(dump: dict, ctx: dict, e: Expectation, expected: Any, actual: Any) -> str:
    """Why a mismatch is a known loss of an older harness rather than the
    browser's value, or "". Two are known: collect.html before
    `nonfinite_literal` wrote Infinity as null (JSON.stringify), and the probe
    before `speech.settleMs` read getVoices() once, before a list arriving in
    parts was complete (Windows: the local voices after the network ones)."""
    harness = dump.get("harness") or {}
    if (actual is None and isinstance(expected, float) and not math.isfinite(expected)
            and harness.get("source") == "collect.html" and not harness.get("nonfinite_literal")):
        return "older collect.html wrote a non-finite number as null"
    speech = ctx.get("speech") if isinstance(ctx.get("speech"), dict) else {}
    # The partial list marks its own first voice default.
    def strip(voices):
        return [{k: v for k, v in voice.items() if k != "default"} for voice in voices if isinstance(voice, dict)]
    if (e.profile_path == "speech.voices" and "settleMs" not in speech and isinstance(actual, list) and isinstance(expected, list)
            and strip(actual) == strip(v for v in expected if not v["localService"]) and len(actual) < len(expected)):
        return "older probe read getVoices() before the local voices arrived"
    return ""


def compare(dump: dict, profile: dict) -> dict:
    failures: list[dict] = []
    passes = 0
    skipped: list[dict] = []
    contexts = dump.get("contexts", {})
    for e in EXPECTATIONS:
        pv = _get(profile, e.profile_path)
        if pv is _MISSING:
            skipped.append({"profile_path": e.profile_path, "reason": "not in profile"})
            continue
        expected = e.transform(pv, profile) if e.transform else pv
        for ctx in e.contexts:
            c = contexts.get(ctx)
            if not isinstance(c, dict):
                failures.append({"context": ctx, "profile_path": e.profile_path, "dump_path": ".".join(e.dump_path) if isinstance(e.dump_path, tuple) else e.dump_path, "detail": "context missing from dump"})
                continue
            actual = _get(c, e.dump_path)
            ok, detail = _judge(expected, actual)
            loss = "" if ok else _older_harness_loss(dump, c, e, expected, actual)
            if ok:
                passes += 1
            elif loss:
                skipped.append({"profile_path": e.profile_path, "reason": f"{ctx}: {loss}"})
            else:
                failures.append({"context": ctx, "profile_path": e.profile_path, "dump_path": ".".join(e.dump_path) if isinstance(e.dump_path, tuple) else e.dump_path, "expected": expected if not isinstance(expected, tuple) else expected[0], "actual": None if actual is _MISSING else actual, "detail": detail})
    for name, ctxs, fn in INVARIANTS:
        for ctx in ctxs:
            c = contexts.get(ctx)
            if not isinstance(c, dict):
                continue
            ok, detail = fn(c)
            if ok:
                passes += 1
            else:
                failures.append({"context": ctx, "profile_path": f"<invariant> {name}", "dump_path": "", "detail": detail})
    headers = dump.get("headers", {})
    for profile_path, header, slots, transform in HEADER_EXPECTATIONS:
        pv = _get(profile, profile_path)
        if pv is _MISSING:
            continue
        expected = transform(pv, profile)
        for slot in slots:
            h = headers.get(slot, {})
            actual = h.get(header, _MISSING)
            if actual is _MISSING:
                skipped.append({"profile_path": profile_path, "reason": f"header {header} not sent on {slot}"})
                continue
            ok, detail = _judge(expected, actual)
            if ok:
                passes += 1
            else:
                failures.append({"context": f"header:{slot}", "profile_path": profile_path, "dump_path": header, "expected": expected if not isinstance(expected, tuple) else expected[0], "actual": actual, "detail": detail})
    return {"passes": passes, "failures": failures, "skipped": skipped}


def format_compare(report: dict, limit: int = 200) -> str:
    lines = [f"profile comparison: {len(report['failures'])} failures, {report['passes']} passes, {len(report['skipped'])} skipped"]
    for f in report["failures"][:limit]:
        exp = f.get("expected")
        act = f.get("actual")
        detail = f.get("detail") or f"{act!r} != {exp!r}"
        if len(detail) > 160:
            detail = detail[:157] + "..."
        lines.append(f"  {f['context']:22s} {f['profile_path']:45s} {detail}")
    if len(report["failures"]) > limit:
        lines.append(f"  ... {len(report['failures']) - limit} more")
    return "\n".join(lines)
