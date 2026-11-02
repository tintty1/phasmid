"""Cross-context agreement, judged against a stock baseline.

For each flattened key present in more than one context, report any
disagreement that the stock baseline does not have. Keys that legitimately
differ per context (origin, scope, DOM-only sizes) are excluded. Header
surfaces are compared against the JavaScript values they must match.
Within-context checks: canvas readback is a pure function of content, Range
and Element rects agree and equal offsets, and the UA major equals the
fullVersionList major with uaFullVersion equal to the Chromium entry.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .diff import flatten

CONTEXTS = ["main", "iframe_same", "iframe_cross", "worker", "shared_worker", "service_worker"]

# Per-context keys that are expected to differ or are not comparable.
EXPECTED_PER_CONTEXT = [
    re.compile(r"^meta\."),
    re.compile(r"^storage\.usage"),
    re.compile(r"^errors\.stackDepth$"),
    re.compile(r"^audio\.state$"),
    re.compile(r"^navigator\.connection\.(rtt|downlink)$"),
    re.compile(r"^mediaDevices\.devices\.\d+\.(deviceIdLength|groupIdLength)$"),
    re.compile(r"^screen\.(innerWidth|innerHeight|visualViewport|documentClient|scrollbarWidth)"),  # frames have their own viewport
    re.compile(r"^mediaQueries\.widthMatchesInner$"),
    re.compile(r"^rects\."),
    re.compile(r"^webrtc\.candidates"),
    re.compile(r"^webrtc\.sdpLines$"),
    re.compile(r"^fetchError$"),
    re.compile(r"^permissions\."),  # cross-origin frames are permission-policy restricted; checked separately
    re.compile(r"^navigator\.has\.(serviceWorker|bluetooth|share|canShare|getBattery|usb|hid|serial|xr|keyboard|virtualKeyboard|wakeLock|ink|windowControlsOverlay|contacts|credentials|mediaSession|presentation|clipboard|geolocation|mediaDevices|login|devicePosture|managed|adAuctionComponents|joinAdInterestGroup)$"),  # scope-dependent API exposure
    re.compile(r"^globals\."),  # scope-dependent; the per-scope set is compared by the parity guard instead
    re.compile(r"^webauthn\."),  # cross-origin frames: permissions policy
    re.compile(r"^canvas\.(toDataURL|toDataURLJpeg|toBlob|viaImageBitmap)$"),  # DOM-only paths; compared within a context
    re.compile(r"^webgl2?\.toDataURL$"),
]


# Detector invariants the probe records (canvasNoise, audioInvariants). Each
# holds in stock Chrome headless and headful; graphics/canvas-readback.patch
# and audio/webaudio.patch must keep them with noise on.
CANVAS_NOISE_INVARIANTS = [
    "clearedIsZero", "putRandomExact", "solidExact", "inOutStable", "readTwiceIdentical",
    "encodeTwiceIdentical", "translatedInteriorMatches", "decodeRoundTrip", "decodeRoundTripImg",
    "transparentTextStable",
]
AUDIO_INVARIANTS = [
    "analyserTailMatchesBuffer", "copyMatchesGetChannelData", "getChannelDataLive", "freshAnalyserSilent",
    "silentIsZero", "scriptBufferUntouched", "pageBufferPlaysExactly", "constantStaysConstant",
]


def _comparable(key: str) -> bool:
    return not any(p.search(key) for p in EXPECTED_PER_CONTEXT)


def _disagreements(dump: dict) -> dict[str, dict[str, Any]]:
    contexts = dump.get("contexts", {})
    flat = {name: flatten(contexts[name]) for name in CONTEXTS if isinstance(contexts.get(name), dict)}
    keys: set[str] = set()
    for f in flat.values():
        keys.update(f)
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(keys):
        if not _comparable(key) or key.endswith(".__len"):
            continue
        values = {name: f[key] for name, f in flat.items() if key in f}
        if len(values) < 2:
            continue
        distinct = {json.dumps(v, sort_keys=True, default=str) for v in values.values()}
        if len(distinct) > 1:
            out[key] = values
    return out


def _partition(values: dict[str, Any]) -> frozenset[frozenset[str]]:
    """Which contexts agree with which: the shape of a disagreement."""
    groups: dict[str, set[str]] = {}
    for ctx, v in values.items():
        groups.setdefault(json.dumps(v, sort_keys=True, default=str), set()).add(ctx)
    return frozenset(frozenset(g) for g in groups.values())


def cross_context_report(dump: dict, baseline: dict | None = None) -> dict:
    """Disagreements between contexts.

    With a baseline dump (stock Chrome of the same major), a disagreement is
    reported only when the baseline does not show the same split between the
    same contexts. Stock Chrome legitimately differs per context in places
    (worker-only processes resolve generic fonts differently, decodingInfo
    flips powerEfficient off the main thread, cross-origin frames are
    permissions-policy restricted); Phasmid must not add new splits.
    """
    disagreements = _disagreements(dump)
    inherited: dict[str, dict[str, Any]] = {}
    if baseline is not None:
        base = _disagreements(baseline)
        for key in list(disagreements):
            if key in base and _partition(base[key]) == _partition(disagreements[key]):
                inherited[key] = disagreements.pop(key)
    header_checks = _header_checks(dump)
    within = _within_context_checks(dump)
    return {"disagreements": disagreements, "inherited_from_baseline": inherited, "header_checks": header_checks, "within_context": within}


def _header_checks(dump: dict) -> list[dict]:
    """Headers vs JavaScript."""
    checks = []
    main = dump.get("contexts", {}).get("main", {})
    nav = main.get("navigator", {}) if isinstance(main, dict) else {}
    ua_data = nav.get("userAgentData") or {}
    high = ua_data.get("highEntropy") or {}
    headers = dump.get("headers", {})

    def check(slot: str, header: str, js_value: Any, transform=None):
        h = headers.get(slot, {})
        if "error" in h and len(h) == 1:
            checks.append({"slot": slot, "header": header, "ok": None, "note": h["error"]})
            return
        hv = h.get(header)
        expected = transform(js_value) if transform and js_value is not None else js_value
        if hv is None:
            # Not sent on this request. Whether it should be is a parity
            # question against stock Chrome (the diff command), not a
            # JS-vs-header question.
            checks.append({"slot": slot, "header": header, "ok": None, "note": "not sent", "js_value": expected})
            return
        checks.append({"slot": slot, "header": header, "header_value": hv, "js_value": expected, "ok": hv == expected})

    def sf_string(v):
        return f'"{v}"'

    def brand_list(brands):
        return ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brands)

    def accept_language(langs):
        # Chrome: first language without q, then q=0.9, 0.8, ... down to 0.1.
        out = []
        for i, lang in enumerate(langs):
            if i == 0:
                out.append(lang)
            else:
                q = max(0.1, round(1.0 - 0.1 * i, 1))
                out.append(f"{lang};q={q}")
        return ",".join(out)

    for slot in ["navigation", "navigation_with_hints", "fetch_main", "fetch_worker", "fetch_shared_worker", "fetch_service_worker", "fetch_iframe_same", "fetch_iframe_cross", "worker_script", "service_worker_script"]:
        check(slot, "user-agent", nav.get("userAgent"))
        check(slot, "accept-language", nav.get("languages"), accept_language)
        if ua_data:
            check(slot, "sec-ch-ua", ua_data.get("brands"), brand_list)
            check(slot, "sec-ch-ua-mobile", ua_data.get("mobile"), lambda m: "?1" if m else "?0")
            check(slot, "sec-ch-ua-platform", ua_data.get("platform"), sf_string)
    for slot in ["navigation_with_hints", "fetch_main", "fetch_worker", "fetch_service_worker"]:
        if high and "error" not in high:
            check(slot, "sec-ch-ua-platform-version", high.get("platformVersion"), sf_string)
            check(slot, "sec-ch-ua-arch", high.get("architecture"), sf_string)
            check(slot, "sec-ch-ua-bitness", high.get("bitness"), sf_string)
            check(slot, "sec-ch-ua-model", high.get("model"), sf_string)
            check(slot, "sec-ch-ua-full-version-list", high.get("fullVersionList"), brand_list)
            check(slot, "sec-ch-ua-wow64", high.get("wow64"), lambda w: "?1" if w else "?0")
        check(slot, "sec-ch-device-memory", nav.get("deviceMemory"), lambda m: f"{m:g}")
        check(slot, "device-memory", nav.get("deviceMemory"), lambda m: f"{m:g}")
    screen = main.get("screen", {}) if isinstance(main, dict) else {}
    if screen and "error" not in screen:
        check("navigation_with_hints", "sec-ch-dpr", screen.get("devicePixelRatio"), lambda d: f"{d:g}")
        check("navigation_with_hints", "dpr", screen.get("devicePixelRatio"), lambda d: f"{d:g}")
    return checks


def _within_context_checks(dump: dict) -> list[dict]:
    """Checks that only make sense inside one context."""
    out = []
    for name, ctx in dump.get("contexts", {}).items():
        if not isinstance(ctx, dict):
            continue
        canvas = ctx.get("canvas")
        if isinstance(canvas, dict) and "error" not in canvas:
            out.append({"context": name, "check": "canvas.deterministic", "ok": canvas.get("deterministic")})
            out.append({"context": name, "check": "canvas.subRectMatchesFull", "ok": canvas.get("subRectMatchesFull")})
            if "offscreenGetImageData" in canvas:
                out.append({"context": name, "check": "canvas.offscreen==dom getImageData", "ok": canvas.get("offscreenGetImageData") == canvas.get("getImageData")})
        audio = ctx.get("audio")
        if isinstance(audio, dict) and "deterministic" in audio:
            out.append({"context": name, "check": "audio.deterministic", "ok": audio.get("deterministic")})
            for k in ["analyserRepeatReadIdentical", "analyserByteRepeatReadIdentical", "copyFromChannelMatchesGetChannelData"]:
                if k in audio:
                    out.append({"context": name, "check": f"audio.{k}", "ok": audio.get(k)})
        noise = ctx.get("canvasNoise")
        if isinstance(noise, dict) and "error" not in noise:
            out.append({"context": name, "check": "canvasNoise.pixelMods == 0", "ok": noise.get("pixelMods") == 0})
            for k in CANVAS_NOISE_INVARIANTS:
                if k in noise:
                    out.append({"context": name, "check": f"canvasNoise.{k}", "ok": noise.get(k)})
        invariants = ctx.get("audioInvariants")
        if isinstance(invariants, dict) and "error" not in invariants:
            for k in AUDIO_INVARIANTS:
                if k in invariants:
                    out.append({"context": name, "check": f"audioInvariants.{k}", "ok": invariants.get(k)})
        detached = ctx.get("detachedDocuments")
        if isinstance(detached, dict) and isinstance(canvas, dict) and isinstance(gl1 := ctx.get("webgl"), dict):
            for dname, d in detached.items():
                if not isinstance(d, dict) or "error" in d:
                    continue
                # Canvas pixels in a detached document differ from the main
                # document's even in stock Chrome, so they
                # are recorded for the parity diff, not asserted here.
                if "unmaskedRenderer" in d:
                    out.append({"context": name, "check": f"detached.{dname}.unmaskedRenderer==webgl", "ok": d["unmaskedRenderer"] == gl1.get("unmaskedRenderer")})
        rects = ctx.get("rects")
        if isinstance(rects, dict) and "error" not in rects:
            out.append({"context": name, "check": "rects.rangeEqualsSpan", "ok": rects.get("rangeEqualsSpan")})
            out.append({"context": name, "check": "rects.plainMatchesOffsets", "ok": rects.get("plainMatchesOffsets")})
            out.append({"context": name, "check": "rects.flowMatchesOffsets", "ok": rects.get("flowMatchesOffsets")})
            out.append({"context": name, "check": "rects.percentMatchesOffsets", "ok": rects.get("percentMatchesOffsets")})
        gl1, gl2 = ctx.get("webgl", {}), ctx.get("webgl2", {})
        if isinstance(gl1, dict) and isinstance(gl2, dict) and gl1.get("supported") and gl2.get("supported"):
            out.append({"context": name, "check": "webgl.unmaskedRenderer==webgl2", "ok": gl1.get("unmaskedRenderer") == gl2.get("unmaskedRenderer")})
            out.append({"context": name, "check": "webgl.unmaskedVendor==webgl2", "ok": gl1.get("unmaskedVendor") == gl2.get("unmaskedVendor")})
        nav = ctx.get("navigator", {})
        if isinstance(nav, dict) and nav.get("userAgentData") and isinstance(nav["userAgentData"].get("highEntropy"), dict):
            he = nav["userAgentData"]["highEntropy"]
            ua = nav.get("userAgent", "")
            m = re.search(r"Chrome/(\d+)\.", ua)
            fvl = he.get("fullVersionList") or []
            majors = {v["version"].split(".")[0] for v in fvl if not v["brand"].startswith("Not")}
            out.append({"context": name, "check": "ua major == fullVersionList major", "ok": bool(m) and majors == {m.group(1)}, "detail": {"ua": m.group(1) if m else None, "fullVersionList": sorted(majors)}})
            if "uaFullVersion" in he and fvl:
                chromium = [v["version"] for v in fvl if v["brand"] == "Chromium"]
                out.append({"context": name, "check": "uaFullVersion == Chromium fullVersionList entry", "ok": bool(chromium) and he["uaFullVersion"] == chromium[0]})
    return out


def format_cross_context(report: dict) -> str:
    lines = []
    dis = report["disagreements"]
    inherited = report.get("inherited_from_baseline", {})
    suffix = f" (+{len(inherited)} also present in baseline, not shown)" if inherited else ""
    lines.append(f"cross-context disagreements: {len(dis)}{suffix}")
    for key, values in dis.items():
        lines.append(f"  {key}")
        for ctx, v in values.items():
            s = json.dumps(v, default=str)
            lines.append(f"    {ctx:15s} {s if len(s) <= 100 else s[:97] + '...'}")
    bad_headers = [c for c in report["header_checks"] if c.get("ok") is False]
    unknown_headers = [c for c in report["header_checks"] if c.get("ok") is None]
    lines.append(f"header/js mismatches: {len(bad_headers)} (of {len(report['header_checks'])} checks, {len(unknown_headers)} not sent or not recorded)")
    for c in bad_headers:
        lines.append(f"  {c['slot']:22s} {c['header']:30s} header={c['header_value']!r} js={c['js_value']!r}")
    bad_within = [c for c in report["within_context"] if c.get("ok") is not True]
    lines.append(f"within-context failures: {len(bad_within)} (of {len(report['within_context'])})")
    for c in bad_within:
        lines.append(f"  {c['context']:15s} {c['check']} -> {c.get('ok')} {c.get('detail', '')}")
    return "\n".join(lines)
