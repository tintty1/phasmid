"""Turn a harness dump from a real machine into a profile.

The dump is the capture format: run ``consistency dump --headful`` with stock
Chrome on a real Windows or Linux machine, then ``consistency capture``
to produce a profile whose GPU, font, voice, codec and color data all come
from that device. Values that cannot be read from a page (vendor/device IDs,
driver version, physical memory, disk size) are passed on the command line or
left for the operator to fill from chrome://gpu, and the profile is validated
before it is written.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date
from pathlib import Path
from typing import Any

import jsonschema

from .gpu_identity import ANGLE_VENDOR_IDS, IDENTITY_FIELDS, QUALCOMM_ID, angle_device_id, angle_parts

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "profiles" / "schema.json"

NAV_PLATFORM_BY_FAMILY = {"windows": "Win32", "linux": "Linux x86_64"}
CH_PLATFORM_BY_FAMILY = {"windows": "Windows", "linux": "Linux"}

_UA_PLATFORM_RE = re.compile(r"^Mozilla/5\.0 \(([^)]*)\)")



def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def validate(profile: dict) -> list[str]:
    """Return human-readable schema violations, empty when valid."""
    validator = jsonschema.Draft202012Validator(load_schema(), format_checker=jsonschema.FormatChecker())
    errors = []
    for err in sorted(validator.iter_errors(profile), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")
    return errors


def _family_from_dump(nav: dict) -> str:
    platform = (nav.get("userAgentData") or {}).get("platform") or nav.get("platform", "")
    if platform.startswith("Win"):
        return "windows"
    if platform.lower().startswith("mac"):
        raise ValueError("os.family: a macOS dump cannot become a profile; Phasmid claims Windows and Linux only")
    return "linux"


def _gpu_ids_from_system_info(info: dict) -> dict[str, str]:
    """CDP SystemInfo.getInfo: the active GPU device and the GL strings
    chrome://gpu shows. Returns the capture's gpu_ids keys."""
    gpu = info.get("gpu") or {}
    devices = gpu.get("devices") or []
    if not devices:
        return {}
    dev = devices[0]
    aux = gpu.get("auxAttributes") or {}
    out = {
        "vendor_id": f"0x{int(dev.get('vendorId', 0)):04x}",
        "device_id": f"0x{int(dev.get('deviceId', 0)):04x}",
        "vendor_name": dev.get("vendorString", ""),
        "device_name": dev.get("deviceString", ""),
        "driver_vendor": dev.get("driverVendor", ""),
        "driver_version": dev.get("driverVersion", ""),
    }
    for key, aux_key in (("gl_vendor", "glVendor"), ("gl_renderer", "glRenderer"), ("gl_version", "glVersion")):
        if aux.get(aux_key):
            out[key] = aux[aux_key]
    # The device strings are what chrome://gpu prints in brackets after the
    # ids, and an empty one is measured too: Windows GPUInfo collects the
    # adapter from DXGI and leaves vendor_string empty, where Linux ANGLE
    # fills it from GL_VENDOR. Keep them as read; falling back to the WebGL
    # vendor would add a bracket the real report does not have.
    return {k: v for k, v in out.items() if v or k in ("vendor_name", "device_name")}


def _gpu_ids_from_renderer(renderer: str, family: str) -> dict[str, str]:
    """The PCI ids real Chrome reports for this WebGL renderer, where the string
    determines them: ANGLE prints the adapter's device id on D3D11 (the DXGI
    adapter GPUInfo reads) and Vulkan, software GL is 0xffff:0xffff
    (gpu_info_collector.cc). The browser derives the same when the profile
    leaves them out (gpu_identity.derive); a D3D11 renderer with an unknown
    vendor fails here so the capture names it. Elsewhere the string names no
    id."""
    if "SwiftShader" in renderer:
        return {"vendor_id": "0xffff", "device_id": "0xffff"}
    parts = angle_parts(renderer)
    backend = _angle_backend(renderer)
    if not parts or backend not in ("d3d11", "vulkan"):
        return {}
    found = angle_device_id(parts[1])
    if not found:
        return {}
    vendor_id = QUALCOMM_ID if parts[0] == "Qualcomm" and backend == "vulkan" else ANGLE_VENDOR_IDS.get(parts[0])
    if vendor_id is None:
        if backend == "vulkan":
            return {}
        raise ValueError(f"gpu.vendor_id: no 16-bit PCI vendor id known for ANGLE vendor {parts[0]!r}; pass --gpu vendor_id=0x....")
    if found[0] > 0xFFFF:
        raise ValueError(f"gpu.device_id: {renderer!r} has ids wider than the schema's 0x0000-0xffff")
    return {"vendor_id": f"0x{vendor_id:04x}", "device_id": f"0x{found[0]:04x}"}


def v8_heap_size_limit(physical_memory_mb: int) -> int:
    """performance.memory.jsHeapSizeLimit for that much RAM; mirrors
    V8HeapSizeLimit in patches/phasmid/core/profile.patch, which refuses a
    profile whose hardware.js_heap_size_limit differs from it."""
    mb = 1024 * 1024
    physical = physical_memory_mb * mb
    old_generation = min(max(physical // 2, 256 * mb), 4096 * mb)
    semi_space = 2 * mb
    while semi_space < physical // 4 // 32 and semi_space < 32 * mb:
        semi_space *= 2
    return old_generation + 3 * semi_space


def _rounds_to_device_memory(physical_memory_mb: int, device_memory: int) -> bool:
    """The check core/profile.patch applies: RAM rounded to the nearest power of
    two in GB and clamped to [2, 32], as ApproximatedDeviceMemory does on
    desktop, equals hardware.device_memory."""
    lower = 1 << (physical_memory_mb.bit_length() - 1)
    nearest = lower if physical_memory_mb - lower <= 2 * lower - physical_memory_mb else 2 * lower
    return min(max(nearest // 1024, 2), 32) == device_memory


def physical_memory_from_dump(device_memory: int, js_heap_size_limit: Any) -> int:
    """RAM that reproduces the captured deviceMemory and jsHeapSizeLimit. Below
    8 GB the heap limit follows RAM one-to-one, so it is inverted exactly; from
    8 GB up it is constant and deviceMemory is all a page can see."""
    if not isinstance(js_heap_size_limit, int) or js_heap_size_limit >= v8_heap_size_limit(8192):
        physical = device_memory * 1024
    else:
        found = [mb for mb in range(1024, 8192) if v8_heap_size_limit(mb) == js_heap_size_limit]
        if not found:
            raise ValueError(f"hardware.physical_memory_mb: no RAM gives jsHeapSizeLimit {js_heap_size_limit}; pass --physical-memory-mb")
        physical = found[0]
    if not _rounds_to_device_memory(physical, device_memory):
        raise ValueError(f"hardware.physical_memory_mb: {physical} MB does not round to deviceMemory {device_memory}, which the browser refuses")
    return physical


GIB = 1024**3
# Drive capacities sold as N GB: IDEMA LBA1-03 gives 97696368 + 1953504 * (N - 50) sectors of 512 bytes.
DISK_SIZES_GB = (256, 512, 1000, 2000)


def disk_total_bytes_from_dump(quota_headroom: Any, seed: str) -> int:
    """A disk that reproduces the captured estimate() headroom: below 10 GiB the headroom is the disk
    rounded up to a GiB; from 10 GiB up it is always 10 GiB, so any real
    capacity reproduces it and one is picked by the profile's seed."""
    if isinstance(quota_headroom, int) and 0 < quota_headroom < 10 * GIB:
        return quota_headroom
    gb = DISK_SIZES_GB[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(DISK_SIZES_GB)]
    return (97696368 + 1953504 * (gb - 50)) * 512


def _webgl(ctx: dict) -> dict:
    p = ctx.get("params", {})
    return {
        "vendor": p.get("VENDOR"),
        "renderer": p.get("RENDERER"),
        "unmasked_vendor": ctx.get("unmaskedVendor"),
        "unmasked_renderer": ctx.get("unmaskedRenderer"),
        "version": p.get("VERSION"),
        "shading_language_version": p.get("SHADING_LANGUAGE_VERSION"),
        "extensions": ctx.get("extensions", []),
        "params": {k: v for k, v in p.items() if k not in ("VENDOR", "RENDERER", "VERSION", "SHADING_LANGUAGE_VERSION")},
        "precision": ctx.get("precision", {}),
        "context_attributes": ctx.get("contextAttributes", {}),
        "max_anisotropy": ctx.get("maxAnisotropy"),
        "max_draw_buffers": ctx.get("maxDrawBuffersWebgl"),
    }


def _angle_backend(renderer: str) -> str:
    r = renderer or ""
    if "SwiftShader" in r:
        return "swiftshader"
    if "Direct3D11" in r or "D3D11" in r:
        return "d3d11"
    if "Vulkan" in r:
        return "vulkan"
    return "opengl"


def _pointer(mq: dict) -> dict:
    primary = "fine" if mq.get("pointerFine") else "coarse" if mq.get("pointerCoarse") else "none"
    any_pointer = [k for k, q in (("fine", "anyPointerFine"), ("coarse", "anyPointerCoarse")) if mq.get(q)]
    return {
        "primary": primary,
        "hover": "hover" if mq.get("hoverHover") else "none",
        "any_pointer": any_pointer,
        "any_hover": ["hover"] if mq.get("anyHoverHover") else [],
    }


def capture(dump: dict, *, profile_id: str, os_version: str, gpu_ids: dict[str, str], seed: str | None = None,
            disk_total_bytes: int | None = None, physical_memory_mb: int | None = None, note: str = "") -> dict:
    main = dump["contexts"]["main"]
    nav = main["navigator"]
    ua_data = nav.get("userAgentData") or {}
    high = ua_data.get("highEntropy") or {}
    screen = main["screen"]
    mq = main["mediaQueries"]
    gl, gl2 = main["webgl"], main["webgl2"]
    gpu = main.get("webgpu", {})
    adapter = gpu.get("adapter") or {}
    audio = main["audio"]
    fonts = main["fonts"]
    codecs = main["codecs"]
    dec = codecs.get("decodingInfo", {})
    widevine = codecs.get("keySystems", {}).get("widevine", {})
    family = _family_from_dump(nav)
    # Flags win, then chrome://gpu or CDP SystemInfo, then what the WebGL
    # renderer string determines.
    gpu_ids = {**_gpu_ids_from_system_info(dump.get("system_info") or {}), **gpu_ids}
    from_renderer = {} if {"vendor_id", "device_id"} <= gpu_ids.keys() else _gpu_ids_from_renderer(gl.get("unmaskedRenderer", ""), family)
    gpu_ids = {**from_renderer, **gpu_ids}

    seed = seed or secrets.token_hex(16)
    # What a page cannot read comes from the arguments when given, otherwise
    # from the values it determines; each derivation is named in the note.
    derived = []
    if not physical_memory_mb:
        physical_memory_mb = physical_memory_from_dump(nav["deviceMemory"], main.get("storage", {}).get("jsHeapSizeLimit"))
        derived.append("physical_memory_mb from deviceMemory and jsHeapSizeLimit")
    if not disk_total_bytes:
        storage = main.get("storage", {})
        headroom = storage["quota"] - storage["usage"] if isinstance(storage.get("quota"), int) and isinstance(storage.get("usage"), int) else None
        disk_total_bytes = disk_total_bytes_from_dump(headroom, seed)
        derived.append("disk_total_bytes from the quota headroom")
    if from_renderer:
        derived.append("gpu vendor_id and device_id from the WebGL renderer")

    m = _UA_PLATFORM_RE.match(nav["userAgent"])
    ua_platform_token = m.group(1) if m else ""

    caption = fonts.get("systemFontKeywords", {}).get("caption", {})
    ui_family = (caption.get("fontFamily") or fonts.get("systemUiComputed") or "").strip('"')
    ui_size = float(str(caption.get("fontSize", "16px")).replace("px", "") or 16)

    profile: dict[str, Any] = {
        "schema_version": 1,
        "id": profile_id,
        "seed": seed,
        "captured": {
            "browser_version": dump["harness"].get("binary_version", ""),
            "date": date.today().isoformat(),
            "note": "; ".join(filter(None, [note, f"derived: {', '.join(derived)}" if derived else ""])),
            "dump_sha256": hashlib.sha256(json.dumps(dump, sort_keys=True).encode()).hexdigest(),
        },
        "os": {
            "family": family,
            "version": os_version,
            "ua_platform_token": ua_platform_token,
            "navigator_platform": nav.get("platform") or NAV_PLATFORM_BY_FAMILY[family],
            "ch_platform": ua_data.get("platform") or CH_PLATFORM_BY_FAMILY[family],
            "ch_platform_version": high.get("platformVersion", ""),
            "architecture": high.get("architecture", "x86"),
            "bitness": high.get("bitness", "64"),
            "wow64": bool(high.get("wow64", False)),
            "model": high.get("model", ""),
            "form_factors": high.get("formFactors") or ["Desktop"],
        },
        "hardware": {
            "concurrency": nav["hardwareConcurrency"],
            "device_memory": nav["deviceMemory"],
            "max_touch_points": nav.get("maxTouchPoints", 0),
        },
        "display": {
            "width": screen["width"],
            "height": screen["height"],
            "avail_width": screen["availWidth"],
            "avail_height": screen["availHeight"],
            "avail_left": screen.get("availLeft", 0),
            "avail_top": screen.get("availTop", 0),
            "color_depth": screen["colorDepth"],
            "device_pixel_ratio": screen["devicePixelRatio"],
            "is_extended": bool(screen.get("isExtended", False)),
            "orientation": screen.get("orientation") or {"type": "landscape-primary", "angle": 0},
            "window": {
                "outer_width": screen["outerWidth"],
                "outer_height": screen["outerHeight"],
                "screen_x": screen["screenX"],
                "screen_y": screen["screenY"],
                "inner_width": screen["innerWidth"],
                "inner_height": screen["innerHeight"],
            },
            "color_gamut": "rec2020" if mq.get("colorGamutRec2020") else "p3" if mq.get("colorGamutP3") else "srgb",
            "dynamic_range": "high" if mq.get("dynamicRangeHigh") else "standard",
            "prefers_color_scheme": "dark" if mq.get("prefersColorSchemeDark") else "light",
            "prefers_reduced_motion": "reduce" if mq.get("prefersReducedMotion") else "no-preference",
            "prefers_contrast": "more" if mq.get("prefersContrastMore") else "less" if mq.get("prefersContrastLess") else "no-preference",
            "forced_colors": "active" if mq.get("forcedColorsActive") else "none",
            "pointer": _pointer(mq),
        },
        "gpu": {
            # Unmeasured identity fields are left out; the browser derives them
            # from the WebGL claim where the format determines them.
            **{k: v for k, v in gpu_ids.items() if k in IDENTITY_FIELDS or k == "gl_version"},
            "angle_backend": _angle_backend(gl.get("unmaskedRenderer", "")),
            "webgl": _webgl(gl),
            "webgl2": _webgl(gl2),
            "webgpu": (
                {
                    "available": True,
                    "vendor": adapter.get("vendor", ""),
                    "architecture": adapter.get("architecture", ""),
                    "device": adapter.get("device", ""),
                    "description": adapter.get("description", ""),
                    "subgroup_min_size": adapter.get("subgroupMinSize", 0),
                    "subgroup_max_size": adapter.get("subgroupMaxSize", 0),
                    "is_fallback_adapter": bool(adapter.get("isFallbackAdapter", False)),
                    "features": adapter.get("features", []),
                    "limits": adapter.get("limits", {}),
                    "preferred_canvas_format": gpu.get("preferredCanvasFormat", "bgra8unorm"),
                    "wgsl_language_features": gpu.get("wgslLanguageFeatures") or [],
                }
                if adapter
                else {"available": False}
            ),
            "hardware_decode": {
                "h264": bool(dec.get("h264_1080p", {}).get("powerEfficient")),
                "hevc": bool(dec.get("hevc_1080p", {}).get("powerEfficient")),
                "vp9": bool(dec.get("vp9_4k", {}).get("powerEfficient")),
                "av1": bool(dec.get("av1_1080p", {}).get("powerEfficient")),
            },
        },
        "canvas": {"noise": False},
        "audio": {
            "sample_rate": audio["sampleRate"],
            "base_latency": audio["baseLatency"],
            "output_latency": audio["outputLatency"],
            "max_channel_count": audio["maxChannelCount"],
            "noise": False,
        },
        "fonts": {
            # Width hits plus local() hits: icon and symbol faces draw no
            # Latin text, so only local() finds them.
            "families": sorted(set(fonts["present"]) | set(main.get("fontsLocal", {}).get("present", []))),
            "ui_font": {"family": ui_family, "size_px": ui_size},
        },
        "locale": {
            "languages": nav["languages"],
            "timezone": main["intl"]["dateTimeFormat"]["timeZone"],
        },
        "speech": {
            "voices": [
                {"name": v["name"], "lang": v["lang"], "voice_uri": v["voiceURI"], "local_service": v["localService"], "default": v["default"]}
                for v in main.get("speech", {}).get("voices", [])
            ]
        },
        "media_devices": {
            kind: main["mediaDevices"].get("counts", {}).get(kind, 0) for kind in ("audioinput", "audiooutput", "videoinput")
        },
        "media": {
            "widevine": {"supported": bool(widevine.get("supported")), "robustness": sorted(set(widevine.get("videoRobustness", [])))},
            "hevc": codecs.get("canPlayType", {}).get('video/mp4; codecs="hev1.1.6.L93.B0"', "") != "",
            "dolby_audio": codecs.get("canPlayType", {}).get('video/mp4; codecs="ac-3"', "") != "",
        },
        "webauthn": {
            "user_verifying_platform_authenticator": bool(main["webauthn"].get("isUserVerifyingPlatformAuthenticatorAvailable")),
            "conditional_mediation": bool(main["webauthn"].get("isConditionalMediationAvailable")),
        },
        "capabilities": {
            "bluetooth": bool(nav.get("has", {}).get("bluetooth")),
            "web_share": bool(nav.get("has", {}).get("share")),
        },
        "system_colors": {k: v for k, v in main.get("systemColors", {}).items() if isinstance(v, str) and v.startswith("rgb")},
        "network": {"webrtc": {"ip_handling_policy": "default"}},
        "automation": {"report_headful": True},
    }
    profile["hardware"]["physical_memory_mb"] = physical_memory_mb
    profile["hardware"]["disk_total_bytes"] = disk_total_bytes
    # The driver shows only in chrome://gpu, CDP and a failed context's status
    # message, gl_version only in chrome://gpu and CDP; a page visit cannot
    # read them. An unknown value is left out rather than written as an empty
    # string: the browser derives it from gl_renderer where that names it.
    for key in ("driver_vendor", "driver_version", "gl_version"):
        if not profile["gpu"].get(key, True):
            del profile["gpu"][key]
    if isinstance(main.get("storage", {}).get("jsHeapSizeLimit"), int):
        profile["hardware"]["js_heap_size_limit"] = main["storage"]["jsHeapSizeLimit"]
    # Whether the device has a Bluetooth adapter: getAvailability() where it
    # answers (Windows Chrome can leave it pending in the top frame), else
    # getClientCapabilities() hybridTransport, which is the adapter's
    # presence on every OS (AuthenticatorCommonImpl::IsHybridTransportSupported).
    hybrid = ((main.get("webauthn") or {}).get("getClientCapabilities") or {}).get("hybridTransport")
    if "bluetoothAvailability" in nav and isinstance(nav["bluetoothAvailability"], bool):
        profile["capabilities"]["bluetooth_available"] = nav["bluetoothAvailability"]
    elif isinstance(hybrid, bool):
        profile["capabilities"]["bluetooth_available"] = hybrid
    battery = nav.get("battery")
    if isinstance(battery, dict) and isinstance(battery.get("charging"), bool) and isinstance(battery.get("level"), (int, float)):
        profile["capabilities"]["battery"] = {"charging": battery["charging"], "level": battery["level"]}
    # The page's values, after stock's per-host noise: the closest the dump
    # comes to the estimate (network/connection.patch applies the noise again).
    connection = nav.get("connection")
    if isinstance(connection, dict) and isinstance(connection.get("rtt"), int) and isinstance(connection.get("downlink"), (int, float)):
        profile["network"]["connection"] = {"effective_type": connection["effectiveType"], "rtt": connection["rtt"], "downlink": connection["downlink"]}
    return profile
